"""
news.py
ดึงข้อมูลดิบ 2 อย่าง จากแหล่งฟรีที่ไม่ต้องขอ API key:
  1. ปฏิทินเศรษฐกิจสกุล USD จาก Forex Factory (nfs.faireconomy.media) — ฟีด JSON สาธารณะที่ชุมชน
     นักพัฒนา EA ใช้กันมานาน
  2. พาดหัวข่าวล่าสุดเกี่ยวกับราคาทองคำ จาก GDELT (โปรเจกต์วิเคราะห์ข่าวระดับโลก ฟรี ไม่ต้องคีย์)

เป็นข้อมูล "ประกอบการตัดสินใจ" เท่านั้น ไฟล์นี้ไม่แตะ entry/score logic ของบอทเลย

หมายเหตุสำคัญ: ทั้งสองแหล่งเป็นบริการฟรีของบุคคลที่สาม ไม่มี SLA รับประกัน และยังไม่เคยทดสอบ
กับข้อมูลจริง (พัฒนาในแซนด์บ็อกซ์ที่ไม่มีเน็ต) ทุกฟังก์ชันจึงดักข้อผิดพลาดไว้ให้คืนค่าว่างเงียบๆ
แทนที่จะ throw exception กระทบ pipeline หลัก — ควรเช็ค log ของรันจริงรอบแรกให้ดี

ข้อจำกัดของ Forex Factory: จำกัด 2 request ต่อ 5 นาทีต่อ IP เรียกฟังก์ชัน fetch_usd_calendar_events()
วันละครั้งพอ (news_scheduler.py จัดการ cache ให้แล้ว) อย่าเรียกทุกรอบ 5 นาที จะโดนบล็อก
"""

import requests
from datetime import datetime, timezone

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def fetch_usd_calendar_events(min_impact=("High", "Medium")):
    """
    ดึงปฏิทินเศรษฐกิจทั้งสัปดาห์จาก Forex Factory แล้วกรองเฉพาะสกุล USD (กระทบทองมากสุด)
    ที่ impact อยู่ใน min_impact คืนค่า list ของ dict:
      {'title': str, 'time': datetime (UTC), 'impact': str, 'forecast': str, 'previous': str, 'actual': str}
    หมายเหตุ: 'actual' จะว่างเปล่าจนกว่าข่าวจะประกาศผลจริง ต้องเรียกฟังก์ชันนี้ใหม่หลังเวลาข่าวผ่านไปแล้ว
    ถึงจะได้ค่า actual ที่อัปเดต (ตอน cache ตอนเที่ยงคืนจะยังว่างอยู่เสมอ เพราะข่าวยังไม่เกิด)
    คืนค่า [] เงียบๆ ถ้าดึงไม่สำเร็จ (ไม่ throw exception)
    """
    try:
        resp = requests.get(FF_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        raw_events = resp.json()
    except Exception as e:
        print(f"[News Error] ดึงปฏิทินเศรษฐกิจไม่สำเร็จ: {e}")
        return []

    events = []
    for e in raw_events:
        try:
            if e.get("country") != "USD":
                continue
            if e.get("impact") not in min_impact:
                continue
            event_time = datetime.fromisoformat(e["date"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            event_time_utc = event_time.astimezone(timezone.utc)
            events.append({
                "title": e.get("title", "Unknown Event"),
                "time": event_time_utc,
                "impact": e.get("impact", ""),
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
                "actual": e.get("actual", ""),
            })
        except Exception:
            continue  # ข้าม event ที่ format ผิดปกติ ไม่ให้พังทั้งชุด

    events.sort(key=lambda x: x["time"])
    return events


def fetch_gold_headlines(max_results=3):
    """
    ดึงพาดหัวข่าวล่าสุดที่เกี่ยวกับราคาทองคำจาก GDELT คืนค่า list ของ dict:
      {'title': str, 'source': str, 'url': str}
    คืนค่า [] เงียบๆ ถ้าดึงไม่สำเร็จ (ไม่ throw exception)
    """
    params = {
        "query": "gold price OR XAUUSD",
        "mode": "ArtList",
        "maxrecords": str(max_results),
        "format": "json",
        "sort": "DateDesc",
    }
    try:
        resp = requests.get(GDELT_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])
    except Exception as e:
        print(f"[News Error] ดึงข่าวทองคำไม่สำเร็จ: {e}")
        return []

    headlines = []
    for a in articles[:max_results]:
        try:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            headlines.append({
                "title": title,
                "source": a.get("domain", "-"),
                "url": a.get("url", ""),
            })
        except Exception:
            continue
    return headlines
