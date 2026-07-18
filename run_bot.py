import os
import sys

# ดึงค่าจาก Render มาใส่ให้ระบบจำ
os.environ['TOKEN'] = os.environ.get('TOKEN', '')
os.environ['OWNER_ID'] = os.environ.get('OWNER_ID', '')

# สั่งรัน main.py เหมือนคุณพิมพ์สั่งในเครื่องคอมพิวเตอร์เลย
import main
