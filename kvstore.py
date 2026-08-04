import os
import time

import requests

# --- Step 3: ย้ายออกจาก kvdb.io มาที่ Upstash Redis (free tier มี SLA จริง ไม่ fail แบบเงียบเหมือน
# kvdb.io) --- interface เดิมทุกประการ: kv_get(bucket, key) / kv_set(bucket, key, value) โค้ดฝั่งอื่น
# (orders.py, main.py, telegram_bot.py, session.py, cooldown.py, news_scheduler.py, dashboard.py ฯลฯ)
# ไม่ต้องแก้อะไรเลย
#
# Upstash ไม่มีแนวคิด "bucket" แยกเหมือน kvdb.io (ที่ path เป็น /bucket/key) — ใช้ namespace ด้วยการ
# เอา bucket มาต่อหน้า key เป็น "bucket:key" แทน (Redis key เดียวแบนราบ)
#
# ใช้ REST "command API" แบบ POST body เป็น JSON array (["SET", key, value]) แทนแบบ path-based
# (/set/key/value) เพราะค่าที่เก็บจริงมี JSON ซ้อน JSON (เช่น order list) ที่มีอักขระพิเศษ (quote,
# brace, ตัวอักษรไทย) เยอะมาก — เอาไปเป็นส่วนหนึ่งของ URL path จะเสี่ยง encode ผิดพลาด/ยาวเกินลิมิต
# ส่ง body-style ปลอดภัยกว่ามาก

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

# ลอง 3 ครั้งรวมครั้งแรก เว้นช่วงเพิ่มขึ้นก่อนรีทราย (1 วิ, แล้ว 2 วิ) — เผื่อเจอ error ชั่วคราว
# (429/5xx/network) เป็นพักๆ (มรดกจาก mitigation สมัย kvdb.io ยังใช้อยู่ ยังมีประโยชน์เหมือนเดิม)
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2]


def _redis_key(bucket, key):
    return f"{bucket}:{key}"


def _redis_command(*args):
    """
    ยิงคำสั่ง Redis ไปที่ Upstash ผ่าน REST command API (POST JSON array ไปที่ URL ฐานตรงๆ)
    คืน (ok, result): ok=True เฉพาะตอน HTTP 200 และ Upstash ไม่คืน error กลับมาเท่านั้น

    ทุก failure path มี log บอกเหตุผลจริง (แต่ไม่ print ค่า UPSTASH_TOKEN เองเด็ดขาด แค่ความยาวพอ
    ให้เดาได้ว่า copy-paste มาไม่ครบหรือเปล่า) — ก่อนหน้านี้ตอน fail จะเงียบสนิท ไม่รู้เลยว่าพังเพราะอะไร
    """
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        print(f"[kvstore] ยังไม่ได้ตั้งค่า UPSTASH_REDIS_REST_URL/TOKEN ครบ "
              f"(URL ตั้งแล้ว: {bool(UPSTASH_URL)}, TOKEN ตั้งแล้ว: {bool(UPSTASH_TOKEN)}, "
              f"ความยาว TOKEN: {len(UPSTASH_TOKEN)} ตัวอักษร — ถ้าสั้นผิดปกติ (ต่ำกว่า ~80) "
              f"น่าจะ copy-paste ตอนตั้ง Secret มาไม่ครบ)")
        return False, None

    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = requests.post(UPSTASH_URL, headers=headers, json=list(args), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    print(f"[kvstore] Upstash ตอบ error: {data['error']}")
                    return False, None  # error ระดับ Redis command เอง (ไม่ใช่ HTTP error) ไม่ต้อง retry
                return True, data.get("result")
            if resp.status_code != 429 and resp.status_code < 500:
                print(f"[kvstore] Upstash ตอบ HTTP {resp.status_code}: {resp.text[:200]} "
                      f"(URL: {UPSTASH_URL}, TOKEN ยาว {len(UPSTASH_TOKEN)} ตัวอักษร)")
                return False, None  # 4xx อื่นที่ไม่ใช่ rate limit (เช่น token ผิด) retry ไปก็ไม่ช่วย
            print(f"[kvstore] Upstash ตอบ HTTP {resp.status_code} (ลอง retry ต่อ)")
        except Exception as e:
            print(f"[kvstore] เรียก Upstash ไม่สำเร็จ: {type(e).__name__}: {e}")
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF_SECONDS[attempt])
    return False, None


def kv_get(bucket, key):
    """อ่านค่าจาก Upstash Redis ถ้าไม่มี key นี้จริง หรือ error จะคืนค่า None (เหมือนพฤติกรรมเดิมทุกประการ)"""
    ok, result = _redis_command("GET", _redis_key(bucket, key))
    if not ok:
        return None
    return result  # Redis คืน null ให้ key ที่ไม่มีอยู่จริงอยู่แล้ว -> result เป็น None พอดี


def kv_set(bucket, key, value):
    """เขียนค่าไปยัง Upstash Redis คืน True เฉพาะตอนเขียนสำเร็จจริงเท่านั้น (Redis ตอบ 'OK')"""
    ok, result = _redis_command("SET", _redis_key(bucket, key), str(value))
    return ok and result == "OK"
