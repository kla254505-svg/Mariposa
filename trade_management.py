"""
trade_management.py — Post-Entry Trade Management (Advisory เท่านั้น — ระบบยังไม่เชื่อมต่อ Broker
จริง ดู claude/auto_execute_risk_guard_spec.md จึงยังไม่มีทางแก้ไข SL/ปิดไม้แทนผู้ใช้ได้จริง)

ก่อนหน้านี้ orders.py's update_orders_status() เช็คแค่ราคาปัจจุบันเทียบ SL/TP1 แบบ Binary (ถึง TP1
ก่อน = win, ถึง SL ก่อน = loss) ไม่มีการ "ดูแลไม้ระหว่างทาง" เลยแม้แต่น้อย ทั้งที่เทรดเดอร์มืออาชีพจริงๆ
จะ (1) ขยับ SL ไป Breakeven ทันทีที่กำไรวิ่งไปพอสมควร กันไม้ที่เคยเป็นกำไรกลับมาโดน SL เต็มจำนวน
(2) พิจารณาล็อกกำไรบางส่วนก่อนถึงเป้าเต็ม (3) เริ่มระวัง/พิจารณาตัดขาดทุนเร็วกว่ารอ SL เต็ม ถ้าเจอ
สัญญาณโครงสร้างเปลี่ยน (structure กลับทิศ) ระหว่างทางที่ยังไม่ทันถึง SL

ฟังก์ชันนี้เรียกทุกรอบ cron (หลัง update_pending_orders/update_orders_status ใน main.py) เช็คไม้ที่
'running' ทุกตัวของ symbol นี้ คำนวณ R-multiple (กำไร/ขาดทุนปัจจุบัน หารด้วยระยะเสี่ยงตอนเข้า 1R)
แล้วคืนข้อความคำแนะนำให้ผู้เรียกส่งเข้า Telegram เอง — dedup ด้วย flag ที่เก็บไว้ใน "ตัวออเดอร์เอง"
(mgmt_breakeven_alerted/mgmt_partial_alerted/mgmt_warning_alerted) ผ่าน save_orders() ตัวเดียวกับที่
orders.py ใช้ กันแจ้งซ้ำทุก 5 นาทีขณะเงื่อนไขเดิมยังจริงอยู่ (ไม่กระทบ schema เดิม — key พวกนี้เป็น
field เสริมที่โค้ดส่วนอื่น (calc_stats/build_orders_dashboard ฯลฯ) ไม่ได้อ่าน ไม่กระทบอะไรที่มีอยู่แล้ว)

ระดับที่เช็ค (ปรับได้ผ่าน config — ดู config.py):
  - trade_mgmt_breakeven_at_r (default 1.0): กำไรถึง 1R -> แนะนำขยับ SL ไป Breakeven (ราคาเข้า) เอง
    (ผู้ใช้ต้องไปทำในแอปโบรกเอง ระบบไม่ได้แก้ไม้ให้จริง)
  - trade_mgmt_partial_at_r (default 1.5): กำไรถึง 1.5R -> แนะนำปิดกำไรบางส่วน (เช่น 50%) ล็อกไว้
    ก่อนลุ้นเป้าที่เหลือ (ถ้าถึงระดับนี้แล้วจะไม่แจ้งระดับ Breakeven ซ้ำอีก เพราะสูงกว่าอยู่แล้วในตัว)
  - trade_mgmt_warning_r (default -0.5): ไม้ขาดทุนไปแล้ว 0.5R (ยังไม่ถึง SL เต็ม 1R) "และ" โครงสร้าง
    ราคา 15M กลับทิศสวนทางไม้นี้ชัดเจนแล้ว -> แจ้งเตือนว่าโมเมนตัมเริ่มเสีย ให้พิจารณาตัดขาดทุนเร็วกว่า
    รอ SL เต็ม (SL เต็มคือ worst-case ที่ยอมรับได้ ไม่ใช่จุดที่ "ควร" รอเสมอไป)
"""

from orders import load_orders, save_orders


def _calc_r_multiple(order, current_price):
    """R-multiple ปัจจุบันของไม้นี้: บวก = กำไร, ลบ = ขาดทุน (หน่วยเป็นเท่าของความเสี่ยงตอนเข้า 1R)"""
    entry = order["entry_price"]
    sl = order["stop_loss"]
    risk_distance = abs(entry - sl)
    if risk_distance == 0:
        return 0.0
    if order["direction"] == "bullish":
        return (current_price - entry) / risk_distance
    return (entry - current_price) / risk_distance


def check_trade_management(bucket, symbol, current_price, structure, config):
    """เช็คไม้ 'running' ทุกตัวของ symbol นี้ คืน list ของข้อความคำแนะนำ (str) ที่ยังไม่เคยแจ้งมาก่อน
    (list ว่างถ้าไม่มีอะไรใหม่ให้แจ้ง) — ฟังก์ชันนี้ไม่ยิง Telegram เอง (กันผูกมัดกับ notify.py
    โดยตรง) ให้ผู้เรียก (main.py) ตัดสินใจปลายทาง/เงื่อนไข push_notifications_enabled เอง

    structure: ผลจาก analyze_structure() ของรอบนี้ (ใช้ 'trend' เช็คโครงสร้างกลับทิศ) — ส่ง None ได้
    ถ้าไม่มี (จะข้ามการเช็คระดับ 'เตือนโมเมนตัมเสีย' ไปเฉยๆ ไม่ error — ยังเช็ค breakeven/partial ปกติ)
    """
    orders = load_orders(bucket, symbol)
    if not any(o.get("status") == "running" for o in orders):
        return []

    breakeven_r = config.get("trade_mgmt_breakeven_at_r", 1.0)
    partial_r = config.get("trade_mgmt_partial_at_r", 1.5)
    warning_r = config.get("trade_mgmt_warning_r", -0.5)

    messages = []
    changed = False

    for o in orders:
        if o.get("status") != "running":
            continue

        r = _calc_r_multiple(o, current_price)
        plan_label = o.get("plan", "?")
        direction_th = "LONG" if o["direction"] == "bullish" else "SHORT"

        if r >= partial_r and not o.get("mgmt_partial_alerted"):
            messages.append(
                f"🟢 <b>แนะนำ Partial Take Profit — {symbol} [{plan_label}]</b>\n"
                f"{direction_th} Entry {o['entry_price']:.3f} | กำไรตอนนี้ ≈ {r:.2f}R "
                f"(ถึงเกณฑ์ {partial_r:.1f}R แล้ว)\n"
                f"แนะนำ: ปิดกำไรบางส่วน (เช่น 50%) ล็อกไว้ก่อน ปล่อยที่เหลือลุ้นเป้าเดิมต่อ\n"
                f"⚠️ คำแนะนำเท่านั้น ระบบไม่ได้ปิดไม้ให้อัตโนมัติ — ไปดำเนินการในแอปโบรกเอง"
            )
            o["mgmt_partial_alerted"] = True
            # กัน Breakeven เด้งซ้ำทีหลัง — ถึงระดับ Partial (สูงกว่า) แล้วแปลว่าผ่านระดับ Breakeven
            # มาแล้วในตัวเสมอ (partial_r > breakeven_r) ไม่งั้นรอบถัดไปที่ r ยังคง >= partial_r แต่ตัว
            # if บนสุดไม่ทำงานซ้ำ (มี mgmt_partial_alerted แล้ว) จะไหลลงมาเข้า elif ข้างล่างแทนและ
            # แจ้ง Breakeven ซ้ำทั้งที่ควรผ่านจุดนั้นไปนานแล้ว (เคยเป็นบั๊กจริง พบตอนเทส)
            o["mgmt_breakeven_alerted"] = True
            changed = True
        elif r >= breakeven_r and not o.get("mgmt_breakeven_alerted"):
            messages.append(
                f"🟡 <b>แนะนำขยับ SL ไป Breakeven — {symbol} [{plan_label}]</b>\n"
                f"{direction_th} Entry {o['entry_price']:.3f} | กำไรตอนนี้ ≈ {r:.2f}R "
                f"(ถึงเกณฑ์ {breakeven_r:.1f}R แล้ว)\n"
                f"แนะนำ: ขยับ SL มาที่ราคาเข้า ({o['entry_price']:.3f}) กันไม้ที่เคยเป็นกำไรพลิกกลับมาเสีย\n"
                f"⚠️ คำแนะนำเท่านั้น ระบบไม่ได้แก้ SL ให้อัตโนมัติ — ไปแก้ในแอปโบรกเอง"
            )
            o["mgmt_breakeven_alerted"] = True
            changed = True

        if structure and r <= warning_r and not o.get("mgmt_warning_alerted"):
            structure_flipped = structure.get("trend") not in (None, "sideway", o["direction"])
            if structure_flipped:
                messages.append(
                    f"🔴 <b>เตือนโมเมนตัมเสีย — {symbol} [{plan_label}]</b>\n"
                    f"{direction_th} Entry {o['entry_price']:.3f} | ตอนนี้ติดลบ ≈ {abs(r):.2f}R\n"
                    f"โครงสร้างราคา 15M เริ่มกลับทิศเป็น {structure.get('trend')} สวนทางกับไม้นี้แล้ว\n"
                    f"แนะนำ: พิจารณาตัดขาดทุนเร็วกว่ารอ SL เต็ม (SL คือ worst-case ที่ยอมรับได้ "
                    f"ไม่ใช่จุดที่ต้องรอเสมอไป)\n"
                    f"⚠️ คำแนะนำเท่านั้น ไม่ได้ปิดไม้ให้อัตโนมัติ"
                )
                o["mgmt_warning_alerted"] = True
                changed = True

    if changed:
        if not save_orders(bucket, symbol, orders):
            print(f"[Trade Management Error] บันทึก flag การแจ้งเตือนลง kvdb ไม่สำเร็จ (symbol={symbol}) "
                  f"— รอบหน้าอาจแจ้งซ้ำ")

    return messages
