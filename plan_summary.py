"""
plan_summary.py — สรุปแผนแบบจัดอันดับคะแนน (Ranked Plan Summary)

ส่งเป็นข้อความเสริม แยกต่างหากจาก Order Alert ปกติ (main.py/plan_runner.py ยังส่งเหมือนเดิมทุก
อย่าง ไม่แตะ) เพื่อให้เห็นภาพรวมง่ายๆ ว่าตอนนี้แผนไหนน่าสนใจที่สุด 1-2-3 อันดับ พร้อมอัปเดตสถานะ
เข้าไม้/ผลลัพธ์ทีหลัง (ออกแบบคุยกับผู้ใช้ไว้ 3 ก.ย. 69 — ตกลงกันว่า: ส่งข้อความใหม่ทุกครั้งไม่แก้ทับ,
โชว์ 3 อันดับ)

ทำงานคนละเรื่องกับ Central AI Layer (ai_layer.py) เลย — ไม่พึ่ง AI, ไม่เรียก Claude API ใช้แค่คะแนน
จาก Strategy (อ่านจาก order ที่บันทึกไว้ใน orders.py ผ่าน load_orders() อย่างเดียว ไม่เขียนทับ)

Flow ต่อ 1 symbol ต่อ 1 รอบ cron (เรียกจาก main.py ผ่าน run_plan_summary_cycle):
  1. โหลด order ทั้งหมด กรองเอาเฉพาะที่ "active" (status ใน pending/running เท่านั้น — ไม่เอา
     win/loss/expired เพราะไม่ใช่แผนที่ยัง "แนะนำ" อยู่แล้ว)
  2. เรียงตามคะแนนมาก->น้อย เอา TOP_N (default 3) อันดับแรก
  3. เทียบชุด order id ของ TOP_N รอบนี้ กับ "batch" ล่าสุดที่เคยส่งไว้ (เก็บใน kvdb ต่อ symbol):
     - ชุดเปลี่ยนไป (มีตัวใหม่เข้ามาแทนที่ตัวเดิมอย่างน้อย 1 ตัว) -> ส่งข้อความที่ 1 "แผนที่แนะนำ" ใหม่
       ทับ batch เดิมไปเลย (ไม่ track สถานะของ batch เก่าต่อ ถือว่าจบไปแล้ว)
     - ชุดเดิมเป๊ะ แต่มีบาง order เปลี่ยนสถานะจากที่บันทึกไว้ล่าสุด (pending->running หรือ
       ->win/loss) -> ส่งข้อความที่ 2 (ยังไม่มีตัวไหนจบ) หรือข้อความที่ 3 (มีตัวไหนจบแล้ว
       อย่างน้อย 1 ตัว) โชว์สถานะของทุกตัวใน batch พร้อมกันเสมอ (ไม่ใช่แค่ตัวที่เพิ่งเปลี่ยน)
     - ไม่มีอะไรเปลี่ยนเลย -> เงียบ ไม่ส่งซ้ำ
  ไม่โยน exception ออกจากฟังก์ชันหลักเด็ดขาด (ห่อด้วย try/except ภายใน เหมือนโมดูลอื่นในระบบ)
"""

import json

from kvstore import kv_get, kv_set
from orders import load_orders, PLAN_LABEL

TOP_N = 3
MEDALS = ["🥇", "🥈", "🥉"]
RANK_LABEL = ["แผนหลัก", "แผนที่ 2", "แผนที่ 3", "แผนที่ 4", "แผนที่ 5"]  # เผื่อ TOP_N ขยับในอนาคต

# คะแนนเต็มจริงคือ 120 (รวมน้ำหนักทุกหัวข้อใน score.py) ไม่ใช่ 100 — ข้อความชุดนี้เป็นของใหม่
# เลยโชว์ให้ตรงสเปกจริงไปเลย ต่างจาก Order Alert เดิมที่ยังโชว์ "/100" ไว้ตามเดิม (ผู้ใช้ตัดสินใจ
# ไว้แล้วว่าไม่ต้องไปแก้ป้ายเดิม — ดูการคุยกัน 3 ก.ย. 69 หัวข้อ "คะแนนเต็มจริงไม่ใช่ 100")
SCORE_MAX = 120


def _plan_short_name(plan_key):
    """'plan5_zone_single' -> 'Plan 5' (ดึงจากตัวเลขนำหน้า plan_key ตรงๆ กันชื่อเพี้ยนถ้า
    PLAN_LABEL ไม่มี key นี้ — fallback เป็นตัว plan_key ดิบถ้าพาร์สไม่ได้)"""
    digits = "".join(ch for ch in plan_key.split("_")[0] if ch.isdigit())
    return f"Plan {digits}" if digits else plan_key


def _active_orders(bucket, symbol):
    orders = load_orders(bucket, symbol)
    return [o for o in orders if o.get("status") in ("pending", "running")]


def _top_n(orders, n=TOP_N):
    return sorted(orders, key=lambda o: o.get("score") or 0, reverse=True)[:n]


def _batch_key(symbol):
    return f"plan_summary_batch_{symbol}"


def _load_batch(bucket, symbol):
    raw = kv_get(bucket, _batch_key(symbol))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_batch(bucket, symbol, order_ids, status_snapshot):
    payload = json.dumps({"ids": order_ids, "status_snapshot": status_snapshot})
    kv_set(bucket, _batch_key(symbol), payload)


def format_plan_list_message(symbol, ranked_orders):
    """ข้อความที่ 1 — ส่งตอนมี batch ใหม่ (เข้าเงื่อนไขเปลี่ยนไปจากรอบก่อน)"""
    lines = [f"📊 <b>แผนที่แนะนำ ({symbol})</b>", ""]
    for i, o in enumerate(ranked_orders):
        medal = MEDALS[i] if i < len(MEDALS) else "▪️"
        rank_label = RANK_LABEL[i] if i < len(RANK_LABEL) else f"แผนที่ {i + 1}"
        direction_th = "LONG" if o.get("direction") == "bullish" else "SHORT"
        plan_name = _plan_short_name(o.get("plan", ""))
        tp1 = (o.get("take_profits") or {}).get("TP1")
        score = o.get("score")
        score_str = f"{score:.0f}/{SCORE_MAX}" if score is not None else "-"
        lines.append(f"{medal} <b>{plan_name} ({rank_label})</b>")
        lines.append(f"{direction_th} — {score_str}")
        lines.append(
            f"Entry: {o.get('entry_price')} | SL: {o.get('stop_loss')} | TP: {tp1}"
        )
        lines.append("")
    return "\n".join(lines).strip()


def format_status_message(symbol, batch_orders):
    """ข้อความที่ 2 — ส่งตอนมี order ใน batch เปลี่ยนสถานะ แต่ยังไม่มีตัวไหนจบ (win/loss/expired)"""
    lines = [f"🎯 <b>อัปเดตสถานะ ({symbol})</b>", ""]
    for o in batch_orders:
        plan_name = _plan_short_name(o.get("plan", ""))
        entered = o.get("status") in ("running", "win", "loss")
        lines.append(f"{plan_name}: {'เข้าแล้ว ✅' if entered else 'ยังไม่เข้า ⏳'}")
    return "\n".join(lines)


def format_result_message(symbol, batch_orders):
    """ข้อความที่ 3 — ส่งตอนมี order ใน batch จบแล้วอย่างน้อย 1 ตัว (win/loss/expired)"""
    lines = [f"🏁 <b>สรุปผล ({symbol})</b>", ""]
    result_icon = {"win": "TP ✅", "loss": "SL ❌", "expired": "หมดอายุ ⌛"}
    for o in batch_orders:
        plan_name = _plan_short_name(o.get("plan", ""))
        status = o.get("status")
        if status in result_icon:
            lines.append(f"{plan_name}: {result_icon[status]}")
        else:
            lines.append(f"{plan_name}: ยังไม่ถึง TP/SL ⏳")
    return "\n".join(lines)


def run_plan_summary_cycle(bucket, symbol, config):
    """เรียกจาก main.py ทุกรอบ cron (1 ครั้งต่อ symbol) — คืน list ของข้อความที่ควรส่ง (0-1 ข้อความ
    ต่อรอบเสมอ ไม่มีทางส่งเกิน 1 ข้อความพร้อมกัน) ผู้เรียกเอาไปวนส่งผ่าน send_alert_to_targets เอง
    ไม่โยน exception ออกไปเลย คืน [] เงียบๆ ถ้ามีปัญหาระหว่างทาง

    ลำดับเช็ค (สำคัญ): เช็ค batch เดิมที่ติดตามอยู่ก่อนเสมอ (ด้วย order id ตรงๆ ไม่สนสถานะ) ก่อนจะไป
    มองหา batch ใหม่ — กันบั๊กที่เจอตอนทดสอบ: ถ้า filter เอาแต่ order ที่ยัง pending/running มา
    เทียบตั้งแต่ต้น พอมีตัวไหนใน batch จบ (win/loss/expired) มันจะหลุดออกจากพูล active ทันที ทำให้
    ระบบเข้าใจผิดว่า "ชุดเปลี่ยนไปแล้ว" แล้วส่งข้อความที่ 1 (แผนใหม่) ทับ ทั้งที่ควรส่งข้อความที่ 3
    (สรุปผล) ของ batch เดิมก่อน — ต้อง track batch เดิมจนกว่าทุกตัวจะจบ (win/loss/expired) ครบก่อน
    ถึงจะเริ่มมองหา batch ใหม่ได้
    """
    try:
        all_orders = load_orders(bucket, symbol)
        orders_by_id = {o["id"]: o for o in all_orders if o.get("id")}

        prev_batch = _load_batch(bucket, symbol)
        prev_ids = (prev_batch or {}).get("ids", [])
        prev_snapshot = (prev_batch or {}).get("status_snapshot", {})

        if prev_ids:
            # มี batch เดิมติดตามอยู่ — เช็คด้วย id ตรงๆ (ไม่กรองสถานะ) กันหลุดพูลตอนจบแล้ว
            batch_orders = [orders_by_id[i] for i in prev_ids if i in orders_by_id]
            if batch_orders:
                current_snapshot = {o["id"]: o.get("status") for o in batch_orders}
                all_resolved = all(o.get("status") in ("win", "loss", "expired") for o in batch_orders)

                if current_snapshot != prev_snapshot:
                    # มีตัวไหนใน batch เปลี่ยนสถานะ (fill หรือจบ) — อัปเดต snapshot แล้วส่งข้อความ
                    # ที่เหมาะสม (ที่ 3 ถ้ามีตัวจบแล้วอย่างน้อย 1 ตัว ไม่งั้นที่ 2)
                    _save_batch(bucket, symbol, prev_ids, current_snapshot)
                    any_resolved = any(o.get("status") in ("win", "loss", "expired") for o in batch_orders)
                    if any_resolved:
                        return [format_result_message(symbol, batch_orders)]
                    return [format_status_message(symbol, batch_orders)]

                if not all_resolved:
                    # batch เดิมยังไม่มีอะไรเปลี่ยน และยังไม่จบครบทุกตัว — รอต่อ ยังไม่ไปมองหา
                    # batch ใหม่ (กันสลับความสนใจไปมาระหว่าง batch ที่ยังไม่จบ)
                    return []
                # ถ้า all_resolved แล้วและไม่มีอะไรเปลี่ยน (เคยส่งผลไปแล้วรอบก่อน) -> ตกไปหา batch
                # ใหม่ด้านล่างต่อได้เลย

        # ไม่มี batch เดิม หรือ batch เดิมจบครบแล้ว -> มองหาชุดใหม่จากแผนที่ยัง active อยู่ตอนนี้
        active = [o for o in all_orders if o.get("status") in ("pending", "running")]
        ranked = _top_n(active)
        current_ids = [o["id"] for o in ranked if o.get("id")]

        if not current_ids:
            return []  # ไม่มีแผน active เลยตอนนี้ ไม่มีอะไรต้องส่ง

        status_snapshot = {o["id"]: o.get("status") for o in ranked}
        _save_batch(bucket, symbol, current_ids, status_snapshot)
        return [format_plan_list_message(symbol, ranked)]

    except Exception as e:
        print(f"[Plan Summary Error] {symbol}: {e}")
        return []
