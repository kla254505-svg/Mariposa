"""
status_tracker.py
โมดูลเล็กๆ สำหรับบันทึก "เวลาล่าสุดที่แต่ละจุดเชื่อมต่อทำงานสำเร็จ" (heartbeat) ลง Upstash Redis
ผ่าน kvstore.py เดิม (ใช้ bucket แยกต่างหากชื่อ "system_status" ไม่ปนกับข้อมูลเทรดจริง)

หลักการ: ทุกครั้งที่จุดใดจุดหนึ่ง (TwelveData, Claude API, Telegram ฯลฯ) ทำงาน "สำเร็จจริง" ในการรันปกติ
ของบอท (ไม่ใช่การเทส) ให้เรียก heartbeat(component) หนึ่งบรรทัด — Dashboard จะมาอ่านค่านี้ทีหลังว่า
"เห็นครั้งล่าสุดเมื่อไหร่" แล้วเทียบกับเวลาปัจจุบันว่ายังสดอยู่ไหม (ไม่ต้องยิง API จริงซ้ำตอนเปิด Dashboard
ประหยัด quota / ไม่มีค่าใช้จ่ายเพิ่ม)

ห้าม heartbeat() โยน exception ออกไปกระทบ pipeline หลักเด็ดขาด — ถ้าบันทึกสถานะพลาด ก็แค่ไม่เห็น
ข้อมูลบน Dashboard แต่บอทหลักต้องทำงานต่อได้ปกติเสมอ
"""

import time

from kvstore import kv_get, kv_set

STATUS_BUCKET = "system_status"

# รายชื่อ component ทั้งหมดที่ระบบนี้ track ไว้ พร้อม "ช่วงเวลาที่ถือว่ายังปกติ" (วินาที) และคำอธิบาย
# ใช้ค่านี้ทั้งฝั่งเขียน heartbeat และฝั่ง Dashboard อ่านมาสรุปสถานะ ไม่ต้องกำหนดซ้ำสองที่
COMPONENTS = {
    "main_cycle": {
        "label": "รอบวิเคราะห์หลัก (cron-job.org → GitHub Actions → main.py)",
        "max_age_seconds": 8 * 60,  # ควรรันทุก 5 นาที เผื่อ jitter ให้ 8 นาที
    },
    "twelvedata": {
        "label": "TwelveData (ราคาทองคำ XAU/USD)",
        "max_age_seconds": 8 * 60,
    },
    "claude_ai": {
        "label": "Claude API (Central AI Layer)",
        # AI ทำงานเฉพาะช่วง 10:00-22:00 จ-ศ และเฉพาะตอนมี Event เท่านั้น (ไม่ใช่ทุกรอบ 5 นาที)
        # ดังนั้นถ้าห่างไปหลายชั่วโมงระหว่างวันทำการอาจยังปกติอยู่ — ให้ Dashboard โชว์ "เวลาล่าสุด"
        # เฉยๆ ไม่ตัดสินว่า error ง่ายเกินไป (ดู note พิเศษใน get_status_report)
        "max_age_seconds": 24 * 60 * 60,
    },
    "telegram_alert": {
        "label": "Telegram ส่งข้อความแจ้งเตือนออก",
        "max_age_seconds": 24 * 60 * 60,  # ขึ้นกับว่ามีสัญญาณเกิดไหม ไม่ได้ส่งทุกรอบ
    },
    "telegram_polling": {
        "label": "Telegram รับคำสั่ง (Render polling loop)",
        "max_age_seconds": 3 * 60,  # long-poll วนต่อเนื่อง ควรเห็นถี่มาก
    },
    "render_service": {
        "label": "Render (ตัวรับคำสั่ง Telegram — โดน UptimeRobot ping)",
        "max_age_seconds": 10 * 60,  # UptimeRobot ปกติ ping ทุก 5 นาที
    },
}


def heartbeat(component: str):
    """เรียกทุกครั้งที่ component นั้นทำงานสำเร็จจริง (ไม่ใช่ระหว่างเทส) ห้าม throw ออกไปเด็ดขาด"""
    try:
        kv_set(STATUS_BUCKET, component, str(int(time.time())))
    except Exception as e:
        print(f"[status_tracker] บันทึก heartbeat '{component}' ไม่สำเร็จ (ไม่กระทบการทำงานหลัก): {e}")


def _get_last_seen(component: str):
    try:
        val = kv_get(STATUS_BUCKET, component)
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def get_status_report():
    """
    คืน dict สรุปสถานะของทุก component ไว้ให้ /status endpoint ส่งเป็น JSON ตรงๆ
    โครงสร้างต่อ 1 component:
      {
        "label": str,
        "last_seen_unix": int | None,
        "seconds_ago": int | None,
        "ok": bool,          # True ถ้ายังอยู่ในเกณฑ์ max_age_seconds (หรือยังไม่เคยเห็นเลย = False)
        "never_seen": bool,
      }
    """
    now = int(time.time())
    report = {}
    for name, meta in COMPONENTS.items():
        last_seen = _get_last_seen(name)
        if last_seen is None:
            report[name] = {
                "label": meta["label"],
                "last_seen_unix": None,
                "seconds_ago": None,
                "ok": False,
                "never_seen": True,
            }
            continue
        seconds_ago = max(0, now - last_seen)
        report[name] = {
            "label": meta["label"],
            "last_seen_unix": last_seen,
            "seconds_ago": seconds_ago,
            "ok": seconds_ago <= meta["max_age_seconds"],
            "never_seen": False,
        }
    return report
