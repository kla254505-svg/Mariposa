"""
plan_coordination.py — Plan Coordination / Conflict Detection

*** ใหม่ (7 ก.ย. 2026) ***
ระบบมี 8 แผนที่ทำงานอิสระจากกันโดยสิ้นเชิง (คนละไฟล์ คนละ engine หา entry) แต่ละแผนไม่รู้จักกันเอง
เลยว่าแผนอื่นกำลังมีออเดอร์ pending/running อยู่ทิศทางไหนบ้าง — เป็นไปได้ที่ 2 แผนจะยิงสัญญาณสวนทางกัน
พร้อมๆ กัน เช่น Plan 5 (SMC Zone Entry) มี Zone LONG กำลัง pending รออยู่ ขณะที่ Plan 2 (Breakout)
เพิ่งยิง SHORT ออกมาสดๆ ผู้ใช้เทรดบัญชีเดียว (ไม่มี broker execution จริง เข้าไม้เองมือ) ถ้าเข้าตาม
ทั้งคู่จะกลายเป็น "hedge ตัวเอง" โดยไม่ตั้งใจ (เสียสเปรด/คอมมิชชั่นสองต่อ) หรือสร้างความสับสนว่า
สรุปแล้ว Net Exposure ของพอร์ตควรเป็นทิศทางไหนกันแน่

ขอบเขตที่ตั้งใจไว้ (ตามคำแนะนำที่เสนอผู้ใช้ไปก่อนแล้ว — ผู้ใช้อนุมัติให้ทำต่อจากข้อ 1 คือ
Score-based Position Sizing): โมดูลนี้ "ไม่ block" การส่ง Alert หรือการบันทึกออเดอร์ใดๆ ทั้งสิ้น
ต่างจาก risk_guard.py ที่ block การส่ง Alert จริงเมื่อเงื่อนไข Portfolio-level (Daily Loss/
Consecutive Loss/Max Concurrent) เกิน — เพราะการเข้า/ไม่เข้าไม้เป็นการตัดสินใจสุดท้ายของผู้ใช้เอง
(ระบบไม่มี broker execution ที่จะไปยกเลิกไม้ให้ได้จริง) แค่ "แจ้งเตือนเพิ่ม" ต่อท้ายข้อความ Telegram
Alert เดิมของทุกแผน (Plan 1-8) ให้ผู้ใช้เห็นภาพรวม Portfolio ก่อนตัดสินใจเข้าไม้ใหม่ทับ

ปิดได้ทั้งหมดผ่าน config['plan_conflict_warning_enabled'] = False (fallback: ไม่เช็ค ไม่แจ้งอะไร
เพิ่ม พฤติกรรมเหมือนไม่มีโมดูลนี้เลย)
"""
from orders import load_orders, PLAN_LABEL, PLAN_SHORT


def find_direction_conflicts(bucket, symbol, new_direction, config, existing_orders=None):
    """
    คืนลิสต์ของออเดอร์ (จากแผนไหนก็ได้) ที่ยังเปิดอยู่จริง (status 'pending' หรือ 'running' เท่านั้น —
    ออเดอร์ที่ปิดจบแล้ว win/loss/expired ไม่นับ เพราะไม่มีผลต่อ Net Exposure ปัจจุบันของพอร์ตแล้ว)
    และมีทิศทาง ('bullish'/'bearish') ตรงข้ามกับ new_direction ที่กำลังจะแจ้งเตือน/บันทึกใหม่

    existing_orders: ถ้าผู้เรียกโหลด orders list มาแล้วก่อนหน้า (เช่น Plan 5-8 ที่โหลดไปเช็ค dedup
    อยู่แล้ว) ส่งเข้ามาตรงนี้เพื่อไม่ต้องยิง kv_get ซ้ำ — ไม่ส่ง (None) จะโหลดใหม่ให้เอง

    ปิดฟีเจอร์นี้ทั้งหมดได้ผ่าน config['plan_conflict_warning_enabled']=False (คืนลิสต์ว่างทันที)
    """
    if not config.get("plan_conflict_warning_enabled", True):
        return []
    if new_direction not in ("bullish", "bearish"):
        return []

    orders = existing_orders if existing_orders is not None else load_orders(bucket, symbol)
    conflicts = []
    for o in orders:
        if o.get("status") not in ("pending", "running"):
            continue
        o_direction = o.get("direction")
        if o_direction not in ("bullish", "bearish"):
            continue
        if o_direction == new_direction:
            continue
        conflicts.append(o)
    return conflicts


def format_conflict_warning(conflicts):
    """
    สร้างข้อความเตือนสั้นๆ ต่อท้าย Alert เดิม — คืน "" ถ้าไม่มี conflict (ไม่ต้องเช็ค None ฝั่งผู้เรียก)
    ขึ้นบรรทัดใหม่ในตัวเองแล้ว (เหมือน risk.format_position_sizing_line) ไม่ต้องเติม \\n นำหน้าเอง
    """
    if not conflicts:
        return ""

    lines = ["", "⚠️ <b>Portfolio Conflict:</b> มีออเดอร์แผนอื่นทิศทางตรงข้ามเปิดอยู่ในระบบ:"]
    # โชว์สูงสุด 5 รายการ กันข้อความยาวเกินไปถ้าบังเอิญมี conflict พร้อมกันหลายแผน
    for o in conflicts[:5]:
        plan_key = o.get("plan", "?")
        plan_label = PLAN_SHORT.get(plan_key, plan_key)
        direction_th = "LONG" if o.get("direction") == "bullish" else "SHORT"
        status_th = "กำลังรัน" if o.get("status") == "running" else "รอราคาถึง Entry"
        entry_price = o.get("entry_price")
        entry_str = f"{entry_price:.4f}" if isinstance(entry_price, (int, float)) else "-"
        lines.append(f"  • Plan {plan_label} — {direction_th} ({status_th}) Entry {entry_str}")
    if len(conflicts) > 5:
        lines.append(f"  ...และอีก {len(conflicts) - 5} ออเดอร์")
    lines.append(
        "พิจารณา Net Exposure รวมของพอร์ตก่อนเข้าไม้นี้เพิ่ม — ระบบไม่ block อัตโนมัติ (ไม่มี broker "
        "execution จริง) การตัดสินใจสุดท้ายอยู่ที่คุณ"
    )
    return "\n".join(lines)
