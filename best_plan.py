"""
best_plan.py — สนับสนุนคำสั่ง /best: สรุปเป็นข้อความเดียวว่า "แผนไหนน่าเข้าที่สุดตอนนี้"

ต้องผ่านเกณฑ์ 2 ข้อพร้อมกัน (ตามที่ตกลงกันไว้):
  1. คะแนน (Score) สูงสุดในบรรดาแผนที่ยัง active อยู่ตอนนี้ (pending/running) — Strategy เป็นคน
     คำนวณคะแนนนี้ไว้อยู่แล้ว (ดู score.py/plan_score.py ผ่าน orders.py) ไฟล์นี้แค่หยิบมาเรียงลำดับ
     ไม่ได้คำนวณคะแนนเองใหม่แต่อย่างใด
  2. ความเห็นล่าสุดของ Central AI Layer (ai_layer.py) ต้องเป็น "VALID" เท่านั้น ถ้าเป็น WEAK/NEUTRAL/
     INVALID หรือยังไม่เคยมีความเห็นเลย จะไม่ฟันธงให้เป็น "แผนที่ดีสุด" แต่จะบอกเหตุผลตรงๆ แทน

*** ข้อจำกัดสำคัญที่ต้องเข้าใจก่อนใช้ (สถาปัตยกรรมเดิมของ ai_layer.py เป็นแบบนี้อยู่แล้ว ไม่ใช่บั๊กของ
ไฟล์นี้) ***
ai_layer.py วิเคราะห์ "ภาพรวมของทุกแผน active ทั้งหมดพร้อมกัน" ในการเรียกแต่ละครั้ง (เรียก Claude API
ได้สูงสุด 1 ครั้งต่อรอบต่อ symbol) ไม่ได้ให้ความเห็นแยกเป็นรายแผน ดังนั้น "AI VALID" ในไฟล์นี้จึงหมายถึง
"ความเห็นล่าสุดของ AI ต่อภาพรวม ณ ตอนนั้น" ไม่ใช่การประเมินที่เจาะจงแผนที่คะแนนสูงสุดที่ถูกเลือกขึ้นมา
โดยตรง — ถ้ามีหลายแผน active พร้อมกัน ข้อความที่ส่งออกจะบอกจำนวนแผน active ทั้งหมดให้เห็นตรงๆ เสมอ
เพื่อให้ตีความเองได้ ว่าความเห็น AI นี้อาจไม่ได้ครอบคลุมเฉพาะแผนที่เลือกมาโชว์เพียงแผนเดียว

นอกจากนี้ ai_layer มีช่วงเวลาทำงานจำกัด (จ-ศ 10:00-22:00 เวลาไทย ตาม config) และเป็นแบบ event-driven +
cooldown — ถ้ายังไม่เคยมี Event ที่น่าสนใจเกิดขึ้นในช่วงเวลานั้นเลย จะยังไม่มีความเห็นให้ใช้ ("ai VALID")
ก็จะไม่มีวันขึ้นเป็น "แผนที่ดีสุด" ได้เลยจนกว่าจะมีการวิเคราะห์จริงเกิดขึ้นก่อนอย่างน้อย 1 ครั้ง

เกณฑ์ "มาช้าไม่ควรเข้าแล้ว" (entry_missed): ราคาปัจจุบันวิ่งเลยจุด Entry ไปแล้วในทิศทางเทรด เกิน 20%
ของระยะ Entry-to-SL (ตั้ง buffer ไว้กันสัญญาณหลอกจากราคาแกว่งผ่านจุดเข้าเบาๆ ซึ่งเป็นเรื่องปกติ — ถ้าไม่
มี buffer เลยจะเจอ "มาช้า" บ่อยเกินไป) แต่ต้องยังไม่ถึง SL ด้วย (ถ้าราคาชน SL ไปแล้วก็ไม่ใช่แค่ "มาช้า"
แต่คือแผนนี้ผิดจังหวะไปเต็มๆ แล้ว — ปกติสถานะจะเปลี่ยนเป็น loss ไปเองอยู่แล้วในรอบถัดไปผ่าน
update_orders_status()/update_pending_orders() แต่เผื่อไว้กันเคสรอบนั้นยังมาไม่ถึง)
"""

from orders import load_orders, PLAN_LABEL
import ai_layer

LATE_ENTRY_BUFFER_RATIO = 0.20  # 20% ของระยะ Entry-to-SL


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
    """สร้างข้อความเดียวสรุป "แผนที่ดีสุดตอนนี้" — โชว์เป็นคำแนะนำเฉพาะตอนผ่านเกณฑ์ทั้งคู่ (Score สูงสุด
    + AI VALID) เท่านั้น ไม่ผ่านก็บอกเหตุผลตรงๆ ว่าทำไมยังไม่มีคำแนะนำให้ (ไม่มีแผน active / AI ยังไม่
    เคยประเมิน / AI ไม่ VALID) ไม่โยน exception ออกจากฟังก์ชันนี้เอง (ผู้เรียกใน telegram_bot.py ยังมี
    try/except ห่ออยู่ชั้นนอกอีกที เหมือน command handler อื่นๆ ทุกตัว)"""
    bucket = config.get("kvdb_bucket")
    label = symbol_label or symbol
    best, active_count = pick_best_active_plan(bucket, symbol)

    header = f"🔎 <b>แผนที่ดีที่สุดตอนนี้ — {label}</b>"

    if best is None:
        return f"{header}\n\nยังไม่มีแผนไหน active เลยตอนนี้ครับ (ไม่มี Entry ที่กำลังรอ/กำลังรันอยู่)"

    ai_memory = ai_layer.get_ai_memory_snapshot(config, symbol) or {}
    last_analysis = ai_memory.get("last_ai_analysis")
    assessment = last_analysis.get("signal_assessment") if last_analysis else None

    plan_label = PLAN_LABEL.get(best.get("plan"), best.get("plan"))
    direction_th = "LONG" if best["direction"] == "bullish" else "SHORT"

    lines = [header, ""]

    if assessment != "VALID":
        if assessment is None:
            reason = "ยังไม่เคยมีความเห็นจาก AI เลย (รอ Event ที่น่าสนใจเกิดขึ้นในช่วงเวลาที่ AI ทำงาน จ-ศ 10:00-22:00 เวลาไทยก่อน)"
        else:
            reason = f'ความเห็นล่าสุดของ AI คือ "{assessment}" ไม่ใช่ VALID'
        lines.append(f"⏸️ ยังไม่ผ่านเกณฑ์ที่ตั้งไว้ครับ — {reason}")
        lines.append("")
        lines.append(
            f"(แผนที่คะแนนสูงสุดตอนนี้คือ {plan_label} — {direction_th} | คะแนน {best['score']} "
            f"แต่ยังไม่ผ่านการยืนยันจาก AI จึงยังไม่ฟันธงให้เป็นคำแนะนำ)"
        )
        if active_count > 1:
            lines.append("")
            lines.append(
                f"หมายเหตุ: ตอนนี้มี {active_count} แผน active พร้อมกัน — ความเห็นของ AI ประเมินภาพรวม"
                f"ทั้งหมดพร้อมกัน ไม่ได้แยกเจาะจงทีละแผน"
            )
        return "\n".join(lines)

    lines.append(f"✅ <b>{plan_label}</b> — {direction_th}")
    lines.append(f"Entry {best['entry_price']} | SL {best['stop_loss']} | คะแนน {best['score']}")
    confidence = last_analysis.get("confidence", "-")
    lines.append(f"AI ประเมิน: VALID (มั่นใจ {confidence}%)")
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

    if active_count > 1:
        lines.append("")
        lines.append(
            f"หมายเหตุ: ตอนนี้มี {active_count} แผน active พร้อมกัน เลือกอันคะแนนสูงสุดมาให้ — ความเห็น "
            f"AI ข้างต้นเป็นการประเมินภาพรวมทั้งหมด ไม่ได้แยกเจาะจงเฉพาะแผนนี้แผนเดียว"
        )

    return "\n".join(lines)
