"""
best_plan.py — สนับสนุนคำสั่ง /best: สรุปเป็นข้อความเดียวว่า "แผนไหนน่าเข้าที่สุดตอนนี้"

*** แก้ไขสถาปัตยกรรมล่าสุด (8 ก.ย. 2026 — ตามข้อเสนอผู้ใช้ข้อ 7) ***
เดิมไฟล์นี้ใช้ 2 เกณฑ์ผ่านพร้อมกันเป็น Hard Gate: (1) Score สูงสุด และ (2) AI ต้อง VALID เท่านั้น
ถ้า AI ไม่ VALID/ยังไม่เคยประเมิน จะไม่ฟันธงเป็นคำแนะนำเลย — ขัดกับหลักการที่วางไว้เองว่า
"PLAN = ตัดสินใจ, AI = รีวิว" (ดู ai_layer.py หัวไฟล์) เพราะ AI เป็น probabilistic second-opinion
layer ไม่ควรมีอำนาจ "ยับยั้ง" คำแนะนำที่ Strategy + Final Score ตัดสินใจมาแล้ว

ตอนนี้: Strategy Score (ผ่านเกรด A+/A/B/C/D/F — ดู claude/trade_quality.py) เป็นตัวตัดสินใจหลักเสมอ
เลือกแผนคะแนนสูงสุดมาแสดงเป็นคำแนะนำได้ทันที ไม่ว่า AI จะเห็นด้วยหรือไม่ก็ตาม — AI แสดงเป็น
"ความเห็นเสริม/คำเตือน" ต่อท้ายเท่านั้น ไม่ Gate การแสดงผลอีกต่อไป

ตั้ง config['ai_hard_gate_on_best_plan']=True (ดู config.py) เพื่อย้อนกลับไปใช้พฤติกรรมแบบเดิม (AI
เป็น Hard Gate) ได้ทุกเมื่อโดยไม่ต้องแก้โค้ด — ค่า default คือ False (ไม่ Gate แล้ว)

*** ข้อจำกัดสำคัญที่ต้องเข้าใจก่อนใช้ (สถาปัตยกรรมเดิมของ ai_layer.py เป็นแบบนี้อยู่แล้ว ไม่ใช่บั๊กของ
ไฟล์นี้) ***
ai_layer.py วิเคราะห์ "ภาพรวมของทุกแผน active ทั้งหมดพร้อมกัน" ในการเรียกแต่ละครั้ง (เรียก Claude API
ได้สูงสุด 1 ครั้งต่อรอบต่อ symbol) ไม่ได้ให้ความเห็นแยกเป็นรายแผน ดังนั้น "AI VALID" ในไฟล์นี้จึงหมายถึง
"ความเห็นล่าสุดของ AI ต่อภาพรวม ณ ตอนนั้น" ไม่ใช่การประเมินที่เจาะจงแผนที่คะแนนสูงสุดที่ถูกเลือกขึ้นมา
โดยตรง — ถ้ามีหลายแผน active พร้อมกัน ข้อความที่ส่งออกจะบอกจำนวนแผน active ทั้งหมดให้เห็นตรงๆ เสมอ
เพื่อให้ตีความเองได้ ว่าความเห็น AI นี้อาจไม่ได้ครอบคลุมเฉพาะแผนที่เลือกมาโชว์เพียงแผนเดียว

นอกจากนี้ ai_layer มีช่วงเวลาทำงานจำกัด (ปกติ 24/7 ตาม config ปัจจุบัน — ดู ai_time_filter_days/hours)
และเป็นแบบ event-driven + cooldown — ถ้ายังไม่เคยมี Event ที่น่าสนใจเกิดขึ้นเลย จะยังไม่มีความเห็นให้ใช้

เกณฑ์ "มาช้าไม่ควรเข้าแล้ว" (entry_missed): ราคาปัจจุบันวิ่งเลยจุด Entry ไปแล้วในทิศทางเทรด เกิน 20%
ของระยะ Entry-to-SL (ตั้ง buffer ไว้กันสัญญาณหลอกจากราคาแกว่งผ่านจุดเข้าเบาๆ ซึ่งเป็นเรื่องปกติ — ถ้าไม่
มี buffer เลยจะเจอ "มาช้า" บ่อยเกินไป) แต่ต้องยังไม่ถึง SL ด้วย (ถ้าราคาชน SL ไปแล้วก็ไม่ใช่แค่ "มาช้า"
แต่คือแผนนี้ผิดจังหวะไปเต็มๆ แล้ว — ปกติสถานะจะเปลี่ยนเป็น loss ไปเองอยู่แล้วในรอบถัดไปผ่าน
update_orders_status()/update_pending_orders() แต่เผื่อไว้กันเคสรอบนั้นยังมาไม่ถึง)
"""

from orders import load_orders, PLAN_LABEL
from trade_quality import compute_grade, risk_pct_for_grade, GRADE_EMOJI
import ai_layer

LATE_ENTRY_BUFFER_RATIO = 0.20  # 20% ของระยะ Entry-to-SL
PLAN1_KEYS = {"plan1_pullback", "plan1_pullback_early"}


def _score_ceiling_for_plan(plan_key):
    """คืน score ceiling ที่ถูกต้องของแผนนั้นๆ — แผนที่ 1 ใช้สูตรละเอียดของตัวเอง (score.py,
    PLAN1_SCORE_CEILING ~120) แผนที่ 2-8 ใช้สูตรทั่วไป (plan_score.py, GENERIC_MAX_SCORE=100) —
    ต้องใช้ ceiling ที่ถูกต้องตามแผน ไม่งั้นเกรดที่คำนวณได้จะเพี้ยน (คะแนนแผนที่ 1 เต็ม ~120 แต่ถ้าเอา
    ไปเทียบ ceiling=100 จะได้ ratio เกิน 1 ตลอด กลายเป็น A+ ง่ายเกินจริง)"""
    if plan_key in PLAN1_KEYS:
        try:
            from score import PLAN1_SCORE_CEILING
            return PLAN1_SCORE_CEILING
        except Exception:
            return 100.0
    try:
        from plan_score import GENERIC_MAX_SCORE
        return GENERIC_MAX_SCORE
    except Exception:
        return 100.0


def _entry_to_sl_distance(order):
    return abs(order["entry_price"] - order["stop_loss"])


def _price_progress_past_entry(order, current_price):
    """ระยะที่ราคาวิ่งเลย Entry ไปแล้วในทิศทางเทรด (ค่าบวก = วิ่งเลยไปแล้ว, ค่าลบ/ศูนย์ = ยังไม่ถึง)"""
    entry = order["entry_price"]
    if order["direction"] == "bullish":
        return current_price - entry
    return entry - current_price


def is_entry_missed(order, current_price):
    """True ถ้าราคาวิ่งเลย Entry ไปแล้วเกิน buffer (20% ของระยะ Entry-to-SL) แต่ยังไม่ชน SL"""
    distance = _entry_to_sl_distance(order)
    if distance <= 0:
        return False
    progress = _price_progress_past_entry(order, current_price)
    if progress <= 0:
        return False  # ยังไม่ถึง Entry เลย หรือเพิ่งถึงพอดี

    if is_stop_hit(order, current_price):
        return False  # ชน SL ไปแล้ว ไม่ใช่แค่ "มาช้า" อีกต่อไป — ดู is_stop_hit() แยกต่างหาก

    return progress > (distance * LATE_ENTRY_BUFFER_RATIO)


def is_stop_hit(order, current_price):
    """True ถ้าราคาปัจจุบันชน SL ไปแล้ว — เช็คแยกจาก is_entry_missed() เพราะเป็นคนละความหมายกัน
    (ชน SL = แผนนี้ผิดจังหวะเต็มๆ แล้ว ไม่ใช่แค่ "มาช้า") ปกติ status จะถูกเปลี่ยนเป็น "loss" เองผ่าน
    update_orders_status() ในรอบ cron ถัดไปอยู่แล้ว แต่ /best เป็นคำสั่ง manual ที่อาจถูกเรียกในช่วงคาบ
    เกี่ยวกันก่อน cron รอบนั้นทัน จึงเช็คตรงนี้ซ้ำอีกชั้นกันโชว์ผลลัพธ์เพี้ยน"""
    sl = order["stop_loss"]
    return (current_price <= sl) if order["direction"] == "bullish" else (current_price >= sl)


def pick_best_active_plan(bucket, symbol):
    """คืน (best_order, active_count) — best_order คือ order dict คะแนนสูงสุดในบรรดาแผนที่ status
    เป็น pending/running อยู่ตอนนี้ (คะแนนเท่ากันจะเลือกอันที่เปิดล่าสุด) คืน (None, 0) ถ้าไม่มีแผน
    ไหน active เลยตอนนี้"""
    orders = load_orders(bucket, symbol)
    active = [o for o in orders if o.get("status") in ("pending", "running") and o.get("score") is not None]
    if not active:
        return None, 0
    active_sorted = sorted(active, key=lambda o: (o["score"], o.get("opened_at", "")), reverse=True)
    return active_sorted[0], len(active)


def format_best_plan_message(config, symbol, current_price, symbol_label=None):
    """สร้างข้อความเดียวสรุป "แผนที่ดีสุดตอนนี้" — Strategy Score/เกรดเป็นตัวตัดสินใจหลักเสมอ (ดู
    docstring หัวไฟล์) AI Second Opinion แสดงเป็นความเห็นเสริม/คำเตือนต่อท้าย ไม่ Gate การแสดงผล
    (เว้นแต่เปิด config['ai_hard_gate_on_best_plan']=True เพื่อย้อนกลับไปใช้พฤติกรรมเดิม)
    ไม่โยน exception ออกจากฟังก์ชันนี้เอง (ผู้เรียกใน telegram_bot.py ยังมี try/except ห่ออยู่ชั้นนอก
    อีกที เหมือน command handler อื่นๆ ทุกตัว)"""
    bucket = config.get("kvdb_bucket")
    label = symbol_label or symbol
    best, active_count = pick_best_active_plan(bucket, symbol)

    header = f"🔎 <b>แผนที่ดีที่สุดตอนนี้ — {label}</b>"

    if best is None:
        return f"{header}\n\nยังไม่มีแผนไหน active เลยตอนนี้ครับ (ไม่มี Entry ที่กำลังรอ/กำลังรันอยู่)"

    ai_memory = ai_layer.get_ai_memory_snapshot(config, symbol) or {}
    last_analysis = ai_memory.get("last_ai_analysis")
    assessment = last_analysis.get("signal_assessment") if last_analysis else None
    confidence = last_analysis.get("confidence") if last_analysis else None

    plan_key = best.get("plan")
    plan_label = PLAN_LABEL.get(plan_key, plan_key)
    direction_th = "LONG" if best["direction"] == "bullish" else "SHORT"

    score_ceiling = _score_ceiling_for_plan(plan_key)
    grade = compute_grade(best.get("score"), config, score_ceiling=score_ceiling)
    grade_emoji = GRADE_EMOJI.get(grade, "⚪")
    risk_pct = risk_pct_for_grade(grade, config)

    # --- Hard Gate เดิม (ปิดไว้เป็น default — ดู docstring หัวไฟล์) เผื่อย้อนกลับได้ทันที ---
    if config.get("ai_hard_gate_on_best_plan", False) and assessment != "VALID":
        lines = [header, ""]
        if assessment is None:
            reason = "ยังไม่เคยมีความเห็นจาก AI เลย"
        else:
            reason = f'ความเห็นล่าสุดของ AI คือ "{assessment}" ไม่ใช่ VALID'
        lines.append(f"⏸️ ยังไม่ผ่านเกณฑ์ที่ตั้งไว้ครับ (โหมด AI Hard Gate เปิดอยู่) — {reason}")
        lines.append("")
        lines.append(
            f"(แผนที่คะแนนสูงสุดตอนนี้คือ {plan_label} — {direction_th} | Strategy Score {best['score']} "
            f"เกรด {grade} แต่ยังไม่ผ่านการยืนยันจาก AI จึงยังไม่ฟันธงให้เป็นคำแนะนำ)"
        )
        return "\n".join(lines)

    lines = [header, "", f"✅ <b>{plan_label}</b> — {direction_th}"]
    lines.append(f"Entry {best['entry_price']} | SL {best['stop_loss']} | Strategy Score {best['score']}")
    lines.append(f"{grade_emoji} เกรด: <b>{grade}</b> | แนะนำ Risk ต่อไม้: {risk_pct:.2f}%")
    lines.append("")

    # --- AI Second Opinion: ความเห็นเสริม/คำเตือน — ไม่ใช่ Gate อีกต่อไป ---
    if assessment == "VALID":
        conf_txt = f" (มั่นใจ {confidence}%)" if confidence is not None else ""
        lines.append(f"🤖 AI Second Opinion: VALID{conf_txt} — สอดคล้องกับสัญญาณนี้")
    elif assessment is None:
        lines.append("🤖 AI Second Opinion: ยังไม่เคยประเมิน (รอ Event ที่น่าสนใจก่อน) — ยึด Strategy Score เป็นหลัก")
    else:
        conf_txt = f" (มั่นใจ {confidence}%)" if confidence is not None else ""
        lines.append(
            f'⚠️ AI พบ: "{assessment}"{conf_txt} — ไม่ตรงกับสัญญาณนี้ทั้งหมด พิจารณาประกอบการตัดสินใจ '
            f"(ไม่ใช่คำสั่งห้ามเข้า — Entry/SL/TP เดิมไม่เปลี่ยน)"
        )
    if active_count > 1:
        lines.append(
            f"หมายเหตุ: ตอนนี้มี {active_count} แผน active พร้อมกัน — ความเห็นของ AI ประเมินภาพรวม"
            f"ทั้งหมดพร้อมกัน ไม่ได้แยกเจาะจงเฉพาะแผนนี้แผนเดียว"
        )
    lines.append("")

    if current_price is not None and best.get("status") == "running" and is_stop_hit(best, current_price):
        lines.append(
            "⚠️ ราคาล่าสุดชน SL ไปแล้ว — แผนนี้ถือว่าผิดจังหวะแล้ว ไม่ใช่แค่ \"มาช้า\" (ระบบอาจยังไม่ทัน"
            "อัปเดตสถานะในรอบล่าสุด) ไม่ควรเข้าครับ"
        )
    elif current_price is not None and is_entry_missed(best, current_price):
        lines.append("🚫 <b>ราคาวิ่งเลย Entry ไปแล้ว</b> — มาช้าไปแล้วครับ ไม่ควรเข้าตรงนี้แล้ว รอจังหวะ/สัญญาณใหม่ดีกว่า")
    elif best.get("status") == "running":
        lines.append("💸 ราคาถึง Entry แล้ว (สถานะ: กำลังรัน)")
    else:
        lines.append("⏳ ยังไม่ถึง Entry — ยังทันเข้าตามแผนอยู่")

    return "\n".join(lines)
