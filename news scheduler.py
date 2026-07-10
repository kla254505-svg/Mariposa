"""
news_scheduler.py
จัดตารางส่งข่าว/ปฏิทินเศรษฐกิจผ่าน Telegram โดยไม่ยุ่งกับ entry logic ของบอทเลย:
  1. สรุปข่าว+ปฏิทินทั้งวัน (00:00-23:59) ส่งครั้งเดียวตอนขึ้นวันใหม่ (เวลาไทย)
  2. เตือนล่วงหน้า ~1 ชม.ก่อนข่าว USD High/Medium Impact แต่ละตัว (ส่งครั้งเดียวต่อข่าว กันสแปม)

ใช้ kvdb เก็บ state ข้ามรอบ เพราะบอทรันแบบ stateless ทุก 5 นาทีบน GitHub Actions
(รูปแบบเดียวกับที่ main.py ใช้ cache 1H/4H และกันส่งซ้ำ Hourly Briefing)
"""

import json
from datetime import datetime, timedelta, timezone

from kvstore import kv_get, kv_set
from news import fetch_usd_calendar_events, fetch_gold_headlines

THAI_TZ = timezone(timedelta(hours=7))


def _thai_today_key():
    return datetime.now(THAI_TZ).strftime("%Y-%m-%d")


def refresh_daily_calendar(kvdb_bucket, symbol):
    """
    เช็คว่าขึ้นวันใหม่ (เวลาไทย) หรือยัง ถ้าใช่ -> ดึงปฏิทิน+ข่าวมาเก็บ cache ใน kvdb ใหม่
    (รีเซ็ตรายการ "เตือนไปแล้ว" ของวันเก่าทิ้งด้วย) แล้วคืนค่า (events, headlines) ที่เพิ่งดึงมา
    คืนค่า (None, None) ถ้ายังเป็นวันเดิม (ไม่ต้องทำอะไร ใช้ cache เดิมต่อ)
    """
    stored_date = kv_get(kvdb_bucket, f"calendar_date_{symbol}")
    today_str = _thai_today_key()
    if stored_date == today_str:
        return None, None

    all_events = fetch_usd_calendar_events()
    today_events = [
        e for e in all_events
        if e["time"].astimezone(THAI_TZ).strftime("%Y-%m-%d") == today_str
    ]
    headlines = fetch_gold_headlines()

    payload = {
        "date": today_str,
        "events": [{**e, "time": e["time"].isoformat()} for e in today_events],
    }
    kv_set(kvdb_bucket, f"calendar_date_{symbol}", today_str)
    kv_set(kvdb_bucket, f"calendar_events_{symbol}", json.dumps(payload))
    kv_set(kvdb_bucket, f"calendar_warned_{symbol}", json.dumps([]))

    return today_events, headlines


def build_daily_summary_message(symbol, events, headlines):
    lines = [f"📰 <b>ข่าว/ปฏิทินวันนี้: {symbol}</b>", ""]

    if events:
        lines.append("📅 <b>ปฏิทินเศรษฐกิจวันนี้ (USD, High/Medium Impact)</b>")
        for e in events:
            t_thai = e["time"].astimezone(THAI_TZ).strftime("%H:%M")
            impact_icon = "🔴" if e["impact"] == "High" else "🟠"
            lines.append(f"{impact_icon} {t_thai} — {e['title']}")
            if e.get("forecast") or e.get("previous"):
                lines.append(f"    Forecast: {e.get('forecast') or '-'} | Previous: {e.get('previous') or '-'}")
    else:
        lines.append("📅 วันนี้ไม่มีข่าว USD ที่มีนัยสำคัญ (High/Medium Impact) เท่าที่ดึงมาได้")

    lines.append("")
    if headlines:
        lines.append("🗞️ <b>ข่าวทองคำล่าสุด</b>")
        for h in headlines:
            lines.append(f"• {h['title']} — {h['source']}")
    else:
        lines.append("🗞️ ยังไม่พบข่าวทองคำล่าสุด (แหล่งข่าวอาจไม่พร้อมใช้งานชั่วคราว)")

    lines.append("")
    lines.append("หมายเหตุ: ข้อมูลนี้ประกอบการตัดสินใจเท่านั้น ไม่มีผลต่อ Score หรือการยิง Alert ของบอท")
    return "\n".join(lines)


def check_and_send_pre_news_warning(kvdb_bucket, symbol):
    """
    เช็คทุกรอบ (ทุก 5 นาที) ว่ามีข่าววันนี้ตัวไหนใกล้ถึงเวลาภายใน 60 นาทีข้างหน้าไหม
    ถ้าเจอและยังไม่เคยเตือน -> คืนค่าข้อความเตือน (ให้ main.py ส่ง Telegram เอง) แล้ว mark ว่าเตือนแล้ว
    ส่งแค่ข่าวเดียวต่อรอบ (กันข้อความรัวถ้ามีหลายข่าวพร้อมกัน ข่าวถัดไปจะเจอในรอบถัดๆ ไปเอง)
    คืนค่า None ถ้าไม่มีอะไรต้องเตือนตอนนี้
    """
    raw = kv_get(kvdb_bucket, f"calendar_events_{symbol}")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if payload.get("date") != _thai_today_key():
            return None  # cache เป็นของเมื่อวาน ยังไม่ถูก refresh (รอรอบ midnight refresh ก่อน)
        events = payload.get("events", [])
    except Exception:
        return None

    warned_raw = kv_get(kvdb_bucket, f"calendar_warned_{symbol}")
    try:
        warned = set(json.loads(warned_raw)) if warned_raw else set()
    except Exception:
        warned = set()

    now = datetime.now(timezone.utc)
    for e in events:
        event_id = f"{e['title']}|{e['time']}"
        if event_id in warned:
            continue
        try:
            event_time = datetime.fromisoformat(e["time"])
        except Exception:
            continue
        minutes_until = (event_time - now).total_seconds() / 60
        if 0 <= minutes_until <= 60:
            warned.add(event_id)
            kv_set(kvdb_bucket, f"calendar_warned_{symbol}", json.dumps(list(warned)))

            t_thai = event_time.astimezone(THAI_TZ).strftime("%H:%M")
            impact_icon = "🔴" if e["impact"] == "High" else "🟠"
            return (
                f"⚠️ <b>เตือนล่วงหน้า: ข่าวสำคัญอีก ~{int(minutes_until)} นาที</b>\n"
                f"{impact_icon} {t_thai} — {e['title']} (USD, {e['impact']} Impact)\n"
                f"Forecast: {e.get('forecast') or '-'} | Previous: {e.get('previous') or '-'}\n\n"
                f"ราคาทองมักผันผวนแรงช่วงนี้ ระวังเป็นพิเศษถ้าจะเข้าไม้ใหม่"
            )

    return None
