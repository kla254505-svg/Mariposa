"""
alert_dispatcher.py — รวม logic "ส่ง Telegram Alert ไปทุกปลายทาง + บันทึกออเดอร์ลง Order Dashboard"
ที่เดิมกระจายซ้ำกัน 4 จุดใน main.py (ท้าย run_pipeline ของ Plan 1, ใน loop ของ Plan 2/3,
และในบล็อกของ Plan 4) มาไว้ที่เดียว

แพทเทิร์นเดิมทุกจุดเหมือนกัน:
  1. หา list ปลายทางที่ควรส่ง (แชทเดี่ยวเสมอ + กลุ่มถ้าตั้ง telegram_group_chat_id ไว้)
  2. ส่งข้อความ (ถ้ามีกราฟแนบไปด้วยได้ ให้ลองส่งเป็นรูปก่อน — ถ้าส่งรูปไม่ผ่านค่อย fallback เป็นข้อความล้วน
     กันไม่ให้ alert หายไปเฉยๆ) — ข้ามการส่งทั้งหมดถ้าปิด push_notifications_enabled ไว้
  3. บันทึกออเดอร์ลง Order Dashboard ผ่าน add_order() — ทำเสมอไม่ว่าจะปิด push ไว้หรือไม่ (ให้ /summary,
     /stats ยังเห็นสถิติได้แม้ปิดแจ้งเตือนอยู่) ถ้าบันทึกไม่สำเร็จแค่ log ไว้ ไม่ทำให้ alert หลักพังตาม
"""


def get_alert_targets(config):
    """คืน list ของ chat_id ปลายทางที่ควรส่ง Alert ไปหา (แชทเดิมเสมอ + กลุ่มถ้าตั้ง telegram_group_chat_id ไว้)"""
    targets = [config["telegram_chat_id"]]
    if config.get("telegram_group_chat_id"):
        targets.append(config["telegram_group_chat_id"])
    return targets


def send_alert_to_targets(config, message, chart_path=None, log_prefix=None):
    """
    ส่งข้อความ Alert ไปทุกปลายทาง (แชทเดิม + กลุ่ม) — ข้ามทั้งหมดถ้าปิด push_notifications_enabled ไว้
    ถ้ามี chart_path จะลองแนบรูปก่อน ถ้าส่งรูปไม่ผ่านจะ fallback ไปส่งข้อความล้วนแทนอัตโนมัติ
    log_prefix (ไม่บังคับ) : ถ้าใส่ไว้จะ print ผลส่งแต่ละปลายทาง เช่น "[Telegram -> {chat_id}] ส่งแจ้งเตือนสำเร็จ"
    (พฤติกรรมเดิมของ Plan 1 เท่านั้น — Plan 2/3/4 เดิมไม่ print log แบบนี้ เลยปล่อย None ไว้ตามเดิม)
    คืน list ของ (chat_id, sent_bool) ต่อปลายทาง
    """
    from notify import send_telegram_alert, send_telegram_photo

    if not config.get("push_notifications_enabled", True):
        return []

    results = []
    for target_chat_id in get_alert_targets(config):
        sent = False
        if chart_path:
            sent = send_telegram_photo(config["telegram_token"], target_chat_id, chart_path, caption=message)
        if not sent:
            sent = send_telegram_alert(config["telegram_token"], target_chat_id, message)
        if log_prefix:
            print(f"{log_prefix} -> {target_chat_id}] " + ("ส่งแจ้งเตือนสำเร็จ" if sent else "ส่งแจ้งเตือนล้มเหลว"))
        results.append((target_chat_id, sent))
    return results


def save_plan_order(config, symbol, direction, entry_price, stop_loss, take_profits, score, plan_key):
    """
    บันทึกออเดอร์ลง Order Dashboard ผ่าน add_order() — คืน order dict ถ้าสำเร็จ หรือ None ถ้าไม่สำเร็จ
    (แค่ log error ให้ ไม่ raise เพราะไม่อยากให้บันทึกสถิติพลาดแล้วลาก alert หลักพังไปด้วย)
    """
    from orders import add_order

    try:
        result = add_order(config["kvdb_bucket"], symbol, direction, entry_price, stop_loss,
                            take_profits, score, plan=plan_key)
        if result is None:
            print(f"[Order Tracking Error] บันทึกออเดอร์ {plan_key} ({symbol}) ลง kvdb ไม่สำเร็จ")
        return result
    except Exception as e:
        print(f"[Order Tracking Error] {plan_key} ({symbol}): {e}")
        return None
