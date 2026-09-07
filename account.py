"""
account.py — จัดการ "ทุนเทรด" (Account Balance) ที่ผู้ใช้ตั้งเองผ่าน Telegram (/setbalance) แทนค่าที่
เคย hardcode ไว้ในโค้ด (main.py เดิม account_balance=1000 ตรงๆ ใน __main__ block)

ทำไมต้องมีไฟล์นี้แยก: ก่อนหน้านี้ทุนเทรดเป็นตัวเลขคงที่ในโค้ด ต้องแก้โค้ด+deploy ใหม่ทุกครั้งที่อยากเปลี่ยน
ทั้งที่การเปลี่ยนทุนเป็นเรื่องปกติที่ผู้ใช้ควรทำได้เองจาก Telegram (เช่น เริ่มด้วย $100 แล้วค่อยเพิ่มทีหลัง)
เก็บลง kvdb (Upstash) เหมือนข้อมูลอื่นๆ ของระบบ (orders, pending offset ฯลฯ) อ่าน/เขียนได้จากทั้ง
main.py (GitHub Actions cron) และ telegram_bot.py (Render polling) เพราะใช้ kvdb bucket เดียวกัน —
ตั้งค่าผ่าน Telegram แล้วรอบ cron ถัดไป (สูงสุด 5 นาที) จะใช้ทุนใหม่ทันที ไม่ต้องแก้โค้ด/deploy ใหม่

หมายเหตุ: เป็นทุนของ "บัญชีเดียว" ไม่ได้แยกต่อคู่เงิน (ต่างจาก orders.py ที่แยก key ตาม symbol) เพราะ
ในทางปฏิบัติผู้ใช้เทรดจากบัญชีเดียวกัน ทุนที่มีจึงเป็นก้อนเดียวกันไม่ว่าจะเปิดไม้คู่เงินไหน
"""

from kvstore import kv_get, kv_set

DEFAULT_ACCOUNT_BALANCE = 1000.0
ACCOUNT_BALANCE_KEY = "account_balance"


def get_account_balance(bucket, default=DEFAULT_ACCOUNT_BALANCE):
    """อ่านทุนเทรดปัจจุบันจาก kvdb คืนค่า default ถ้ายังไม่เคยตั้ง (ผู้ใช้ยังไม่เคยพิมพ์ /setbalance)
    หรือถ้าค่าที่เก็บไว้แปลงเป็นตัวเลขไม่ได้/ติดลบ/ศูนย์ (ข้อมูลเพี้ยน) — กันเหนียว ไม่ให้ทั้งระบบ
    (position sizing ของทุกแผน) พังเพราะจุดนี้จุดเดียว"""
    raw = kv_get(bucket, ACCOUNT_BALANCE_KEY)
    if not raw:
        return default
    try:
        value = float(raw)
        if value <= 0:
            return default
        return value
    except (TypeError, ValueError):
        return default


def set_account_balance(bucket, amount):
    """บันทึกทุนเทรดใหม่ลง kvdb คืน True/False ตามผลจริง (ตามแพทเทิร์นเดียวกับ save_orders ใน
    orders.py — ผู้เรียก (telegram_bot.py) ต้องเช็คค่าที่คืนมาก่อนบอกผู้ใช้ว่า 'บันทึกสำเร็จ')"""
    return kv_set(bucket, ACCOUNT_BALANCE_KEY, str(float(amount)))
