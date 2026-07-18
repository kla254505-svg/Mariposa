from flask import Flask
from threading import Thread

# สร้างเว็บเซิร์ฟเวอร์จำลองเพื่อให้ Render ไม่ปิดบอท
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

# รันเว็บเซิร์ฟเวอร์แยกออกมา
t = Thread(target=run)
t.start()

# --- โค้ดเดิมของคุณ (ส่วนที่รันบอท Telegram) เริ่มตรงนี้ ---
# เช่น updater.start_polling() หรือ bot.polling()
import os
from telegram.ext import Updater # หรือ import ที่คุณใช้งานจริง

# ... (โค้ด Flask ที่คุณมีอยู่แล้ว) ...

# --- โค้ดส่วนรันบอท Telegram ---
TOKEN = os.environ.get('TOKEN')
# สมมติว่าคุณใช้ python-telegram-bot เวอร์ชันมาตรฐาน:
updater = Updater(TOKEN, use_context=True)

# เพิ่มคำสั่งต่างๆ ของคุณที่นี่ (เช่น updater.dispatcher.add_handler(...))

updater.start_polling()
updater.idle()

