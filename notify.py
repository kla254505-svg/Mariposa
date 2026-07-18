import requests
from kvstore import kv_get, kv_set


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
                          stop_loss, take_profits, rr, confidence, bias_4h=None,
                          current_price=None, stale_threshold=None):
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

    # --- กันเข้าไม้ตามข้อความเก่า: ข้อความนี้เป็นภาพนิ่ง ณ เวลาที่ส่ง ไม่ auto-อัปเดต ---
    if current_price is not None:
        lines.append("")
        lines.append(f"ราคา ณ ตอนส่ง Alert: {current_price:.4f}")
        if stale_threshold:
            lines.append(
                f"⚠️ ถ้าตอนที่คุณอ่านข้อความนี้ ราคาห่างจากตัวเลขนี้เกิน {stale_threshold:.2f} "
                f"ให้ถือว่าสัญญาณนี้หมดอายุแล้ว ไม่ควรเข้าตาม — เช็ค Dashboard ล่าสุดก่อนเสมอ"
            )
    return "\n".join(lines)


def send_or_edit_message(token, chat_id, message, kvdb_bucket, key="briefing_message_id"):
    """
    ส่งข้อความใหม่ถ้ายังไม่เคยมีมาก่อน
    ถ้าเคยส่งแล้ว จะ edit ข้อความเดิมทับแทนการส่งใหม่ (กันแชทรก)

    หมายเหตุสำคัญ: Telegram API จะคืน HTTP 400 "message is not modified" ถ้าเนื้อหาที่จะ edit
    เหมือนเป๊ะกับข้อความเดิมทุกตัวอักษร (เช่น รอบนี้วิเคราะห์ได้ผลลัพธ์เหมือนรอบก่อนหน้าเป๊ะๆ)
    เดิมโค้ดตีความ error นี้ว่า "edit ล้มเหลว" แล้ว fallback ไปส่งข้อความใหม่ ทำให้เกิดข้อความซ้ำ
    2 ข้อความในแชท (ข้อความเก่าที่เนื้อหาถูกต้องอยู่แล้ว + ข้อความใหม่ที่เพิ่งส่งซ้ำเข้าไป)
    ตอนนี้เช็คแยกเคสนี้ออกมาก่อน ถ้าเจอให้ถือว่า "สำเร็จ" เลย (เนื้อหาที่ต้องการก็อยู่ในแชทแล้ว
    ไม่จำเป็นต้องส่งซ้ำ) ไม่ตกไปที่ fallback ส่งใหม่
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

            # เช็คเคส "เนื้อหาเดิมไม่มีอะไรเปลี่ยน" แยกออกมาก่อน — Telegram ตอบ 400 กลับมาพร้อม
            # description มีคำว่า "message is not modified" ซึ่งไม่ใช่ error จริง แค่บอกว่า
            # ข้อความในแชทตอนนี้ตรงกับที่เราต้องการอยู่แล้ว ถือว่า "สำเร็จ" ไม่ต้อง fallback ไปส่งใหม่
            try:
                err_description = resp.json().get("description", "")
            except Exception:
                err_description = ""
            if "message is not modified" in err_description.lower():
                return True

            print(f"[Telegram Edit Error] HTTP {resp.status_code}: {err_description}")
        except Exception as e:
            print(f"[Telegram Edit Error] {e}")
        # ถ้า edit ล้มเหลวด้วยเหตุผลอื่นจริงๆ (เช่นข้อความถูกลบไปแล้ว) ให้ตกไปส่งใหม่ด้านล่าง

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
