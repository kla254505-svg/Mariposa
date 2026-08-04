import time

import requests

KVDB_BASE = "https://kvdb.io"

# ลอง 3 ครั้งรวมครั้งแรก เว้นช่วงเพิ่มขึ้นก่อนรีทราย (1 วิ, แล้ว 2 วิ) กันเคส kvdb.io free tier
# โดน rate limit (429) หรือ error ชั่วคราว (5xx/timeout) เป็นพักๆ — โดยเฉพาะตอนคำสั่งเดียวเช็คหลาย
# แผนพร้อมกันแล้วยิง write ติดกันเร็วๆ (เช่น /order ที่เช็คแผน 1-3 ในคำสั่งเดียว)
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2]


def kv_get(bucket, key):
    """
    อ่านค่าจาก kvdb.io ถ้าไม่มีหรือ error จะคืนค่า None

    retry เฉพาะตอนที่น่าจะเป็นปัญหาชั่วคราว (429/5xx/network error) เท่านั้น — ถ้า key ไม่มีจริง (404)
    หรือ error อื่นที่ไม่ใช่ rate limit (เช่น 403) จะคืน None ทันทีโดยไม่ retry เพราะ retry ไปก็ไม่ช่วย
    เสียเวลาเปล่า (งานรันบน GitHub Actions ทุก 15-30 นาที ไม่อยากให้แต่ละ run ช้าโดยไม่จำเป็น)
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.get(f"{KVDB_BASE}/{bucket}/{key}", timeout=10)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code != 429 and resp.status_code < 500:
                return None
        except Exception:
            pass
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_SECONDS[attempt])
    return None


def kv_set(bucket, key, value):
    """
    เขียนค่าลง kvdb.io คืน True เฉพาะตอนเขียนสำเร็จจริงเท่านั้น

    บั๊กเดิม: requests.post() ไม่ throw exception ตอนที่ server ตอบ HTTP error กลับมา
    (เช่น 429 Too Many Requests ตอนโดน rate limit, 403, 500) เพราะ HTTP error ไม่ใช่
    exception ในตัวของ requests เอง (ต้องเรียก .raise_for_status() เองถึงจะ throw)
    เดิมโค้ดคืน True ทันทีหลังยิง request โดยไม่เช็ค status code เลย ทำให้ผู้เรียกเข้าใจผิดว่า
    เขียนสำเร็จ ทั้งที่จริงๆ ค่าไม่ถูกบันทึกลง kvdb.io เลย (พบจริงกับ telegram_last_update_id
    ที่เขียนไม่ผ่านตอนโดน rate limit แล้วทำให้ offset ไม่ขยับ บอทเลยไล่ตอบคำสั่งเดิมซ้ำไปเรื่อยๆ)

    ตอนนี้ retry ในตัวเอง (429/5xx/network error เท่านั้น) แทนที่จะให้ผู้เรียกแต่ละจุดทำ retry เอง
    ซ้ำๆ กัน — เป็น mitigation ชั่วคราวระหว่างรอย้ายออกจาก kvdb.io ไป Upstash Redis (แก้ที่ต้นตอจริง)
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.post(f"{KVDB_BASE}/{bucket}/{key}", data=str(value), timeout=10)
            if resp.status_code < 400:
                return True
            if resp.status_code != 429 and resp.status_code < 500:
                return False
        except Exception:
            pass
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_SECONDS[attempt])
    return False
