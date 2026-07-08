import requests


def send_telegram_alert(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return False


def send_telegram_photo(token, chat_id, photo_path, caption=""):
    """
    ส่งรูปภาพ (เช่นกราฟที่ chart.py วาดไว้) พร้อมข้อความ caption ผ่าน Telegram sendPhoto
    Telegram caption จำกัดไม่เกิน 1024 ตัวอักษร ถ้ายาวเกินจะตัดให้พอดีอัตโนมัติ
    คืนค่า True/False ว่าส่งสำเร็จไหม (ไม่ throw exception ออกไปกระทบ pipeline หลัก)
    """
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    if len(caption) > 1024:
        caption = caption[:1000] + "\n... (ตัดข้อความ ดูรายละเอียดเต็มที่ Dashboard)"
    try:
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(url, data=payload, files=files, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Telegram Photo Error] {e}")
        return False


def format_alert_message(symbol, timeframe, structure, entry_signal,
                          stop_loss, take_profits, rr, confidence, bias_4h=None):
    direction_th = "LONG (ซื้อ)" if entry_signal["direction"] == "bullish" else "SHORT (ขาย)"
    lines = [
        f"🚨 <b>สัญญาณเทรด: {symbol} ({timeframe})</b>",
        f"ทิศทาง: {direction_th}",
        f"Trend/Event: {structure['trend']} | {structure['event']}",
    ]

    if bias_4h:
        zone_label = {"premium": "Premium", "discount": "Discount", "equilibrium": "Equilibrium"}.get(
            bias_4h.get("zone"), "-"
        )
        lines.append(f"4H Bias: {bias_4h.get('trend')} | โซนราคา: {zone_label}")

    trigger = entry_signal.get("trigger")
    if trigger and trigger.get("confirmed"):
        lines.append(f"5M Trigger: ยืนยันแล้ว ✅ ({trigger['reason']})")

    lines += [
        f"Score: {confidence['score']}/100",
        "",
        f"Entry: {entry_signal['entry_price']:.4f}",
        f"SL: {stop_loss:.4f}",
    ]
    for name, price in take_profits.items():
        lines.append(f"{name}: {price:.4f} (RR {rr[name]})")
    return "\n".join(lines)
from kvstore import kv_get, kv_set


def send_or_edit_message(token, chat_id, message, kvdb_bucket, key="briefing_message_id"):
    """
    ส่งข้อความใหม่ถ้ายังไม่เคยมีมาก่อน
    ถ้าเคยส่งแล้ว จะ edit ข้อความเดิมทับแทนการส่งใหม่ (กันแชทรก)
    """
    message_id = kv_get(kvdb_bucket, key)

    if message_id:
        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(edit_url, data=payload, timeout=10)
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
        except Exception as e:
            print(f"[Telegram Edit Error] {e}")
        # ถ้า edit ไม่สำเร็จ (เช่นข้อความถูกลบไปแล้ว) ให้ตกไปส่งใหม่ด้านล่าง

    send_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(send_url, data=payload, timeout=10)
        resp.raise_for_status()
        new_message_id = resp.json()["result"]["message_id"]
        kv_set(kvdb_bucket, key, new_message_id)
        return True
    except Exception as e:
        print(f"[Telegram Send Error] {e}")
        return False
