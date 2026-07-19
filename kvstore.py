import requests

KVDB_BASE = "https://kvdb.io"


def kv_get(bucket, key):
    """อ่านค่าจาก kvdb.io ถ้าไม่มีหรือ error จะคืนค่า None"""
    try:
        resp = requests.get(f"{KVDB_BASE}/{bucket}/{key}", timeout=10)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception:
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
    """
    try:
        resp = requests.post(f"{KVDB_BASE}/{bucket}/{key}", data=str(value), timeout=10)
        return resp.status_code < 400
    except Exception:
        return False
