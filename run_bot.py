"""
run_bot.py
ไฟล์นี้คือ Entry Point สำหรับรันบน Render (หรือ server อื่นที่ไม่ตาย ต่างจาก GitHub Actions ที่รันแล้วจบ)

ทำ 2 อย่างพร้อมกัน:
  1. เปิด Flask web server เล็กๆ (แค่ route "/" ตอบ "OK") — Render ต้องการให้ service bind พอร์ต
     และตอบ HTTP ได้ ไม่งั้นจะคิดว่า service ตายแล้วรีสตาร์ทวนไปเรื่อยๆ (Web Service ต้องมี HTTP endpoint)
  2. รัน telegram_bot.py's run_polling_loop() ใน background thread แยกต่างหาก — อันนี้คือตัวที่ทำให้
     บอทตอบคำสั่ง /order /trend /news /status /aicheck ได้จริงแบบเกือบ real-time

⚠️ ไฟล์นี้ "ไม่ได้" รันการวิเคราะห์/ส่ง Alert อัตโนมัติ (นั่นยังเป็นหน้าที่ของ main.py บน GitHub Actions
cron เหมือนเดิม) ไฟล์นี้ทำหน้าที่แค่ตอบคำสั่งที่พิมพ์เข้ามาเท่านั้น สองระบบนี้แชร์ข้อมูลกันผ่าน kvdb
(orders, ปฏิทินข่าว) แต่รันคนละที่คนละจังหวะกัน ไม่ชนกัน
"""

import os
import threading

from flask import Flask
from config import CONFIG
from telegram_bot import run_polling_loop

app = Flask(__name__)


@app.route("/")
def health_check():
    # *** ใหม่ (7 ก.ย. 2026): heartbeat("render_service") ให้หน้า System Status เห็นว่า Render ***
    # instance นี้ยังตอบ HTTP อยู่จริง — route นี้คือจุดที่ UptimeRobot ping เข้ามาทุก ~5 นาทีเพื่อกัน
    # Render free tier sleep อยู่แล้ว เดิมไม่เคยบันทึกอะไรลง status_tracker เลยสักครั้ง ทำให้แถว
    # "Render (ตัวรับคำสั่ง Telegram — โดน UptimeRobot ping)" บน Dashboard ค้างเก่าตลอด แม้ instance
    # จะตอบ ping ปกติทุกรอบก็ตาม — ห่อ try/except กันเหนียว ห้ามให้การบันทึกสถานะทำ endpoint นี้พัง
    # เด็ดขาด (ไม่งั้น Render จะเข้าใจผิดว่า service ตายแล้วรีสตาร์ทวนไปเรื่อยๆ)
    try:
        from status_tracker import heartbeat
        heartbeat("render_service")
    except Exception:
        pass
    return "Mariposa Telegram command bot is running.", 200


def start_polling_in_background():
    thread = threading.Thread(target=run_polling_loop, args=(CONFIG, "XAUUSD"), daemon=True)
    thread.start()


# เริ่ม polling loop ทันทีที่ import ไฟล์นี้ (ไม่ใช่แค่ตอนรันใน __main__) เพราะ Render/Gunicorn
# มักไม่ได้เรียกผ่าน "python run_bot.py" ตรงๆ แต่ผ่าน WSGI server ที่ import ตัวแปร app ไปใช้เอง
start_polling_in_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
