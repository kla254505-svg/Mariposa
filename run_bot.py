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
import time

import requests
from flask import Flask, jsonify, Response

from config import CONFIG
from telegram_bot import run_polling_loop
from status_tracker import heartbeat, get_status_report
from kvstore import kv_get, kv_set

app = Flask(__name__)


@app.route("/")
def health_check():
    # UptimeRobot ping เข้ามาที่ route นี้ทุก ~5 นาทีอยู่แล้ว — ถือโอกาสบันทึก heartbeat ของ
    # Render service ไปด้วยเลย ไม่ต้องเพิ่ม endpoint แยกให้ UptimeRobot ยิงเพิ่ม
    heartbeat("render_service")
    return "Mariposa Telegram command bot is running.", 200


def _check_upstash_redis():
    """เช็ค Redis สดๆ ตรงๆ (SET แล้ว GET กลับมาเทียบค่า) ไม่ใช้ heartbeat เก่าที่อาจค้างจาก
    รอบก่อนหน้า เพราะอยากรู้สถานะ ณ ตอนเปิด Dashboard จริงๆ"""
    nonce = str(int(time.time() * 1000))
    try:
        ok_set = kv_set("system_status", "_redis_probe", nonce)
        if not ok_set:
            return False, "เขียนค่าลง Upstash Redis ไม่สำเร็จ (เช็ค UPSTASH_REDIS_REST_URL/TOKEN บน Render)"
        val = kv_get("system_status", "_redis_probe")
        if val != nonce:
            return False, "เขียนได้แต่อ่านค่ากลับมาไม่ตรง (Redis อาจไม่เสถียร)"
        return True, "เชื่อมต่อ Upstash Redis สำเร็จ"
    except Exception as e:
        return False, f"เชื่อมต่อ Upstash Redis ไม่สำเร็จ: {e}"


def _check_telegram_api():
    """เช็คว่า Telegram Bot API ยังตอบสนองไหม (getMe — เบามาก ไม่เสียอะไร ไม่ส่งข้อความจริง)"""
    token = CONFIG.get("telegram_token")
    if not token:
        return False, "ยังไม่ได้ตั้งค่า TELEGRAM_TOKEN"
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            username = data.get("result", {}).get("username", "-")
            return True, f"เชื่อมต่อ Telegram Bot API สำเร็จ (@{username})"
        return False, f"Telegram Bot API ตอบ HTTP {resp.status_code}: {data}"
    except Exception as e:
        return False, f"เรียก Telegram Bot API ไม่สำเร็จ: {e}"


@app.route("/status")
def status_json():
    """สรุปสถานะทุกจุดเชื่อมต่อเป็น JSON ให้หน้า Dashboard (/dashboard) มาอ่าน
    แบ่งเป็น 2 กลุ่ม: heartbeats (จากการทำงานจริงของบอท เก็บสะสมไว้) และ live_checks (เช็คสดตอนนี้เลย)"""
    import sheets_log

    ok_sheets, msg_sheets = sheets_log.test_sheets_connection()
    ok_redis, msg_redis = _check_upstash_redis()
    ok_tg, msg_tg = _check_telegram_api()

    return jsonify({
        "generated_at_unix": int(time.time()),
        "heartbeats": get_status_report(),
        "live_checks": {
            "google_sheets": {"ok": ok_sheets, "message": msg_sheets},
            "upstash_redis": {"ok": ok_redis, "message": msg_redis},
            "telegram_api": {"ok": ok_tg, "message": msg_tg},
        },
    })


@app.route("/dashboard")
def dashboard_page():
    return Response(_DASHBOARD_HTML, mimetype="text/html")


_DASHBOARD_HTML = """<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mariposa · System Status</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px 16px 60px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0b0d12; color: #e8eaf0;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b93a7; font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; gap: 10px; }
  .card {
    background: #161923; border: 1px solid #232838; border-radius: 12px;
    padding: 14px 16px; display: flex; align-items: flex-start; gap: 12px;
  }
  .dot {
    width: 12px; height: 12px; border-radius: 50%; margin-top: 4px; flex-shrink: 0;
  }
  .dot.ok { background: #34d058; box-shadow: 0 0 8px #34d05888; }
  .dot.bad { background: #ff5c5c; box-shadow: 0 0 8px #ff5c5c88; }
  .dot.unknown { background: #8b93a7; }
  .name { font-size: 15px; font-weight: 600; }
  .detail { font-size: 13px; color: #a7adbd; margin-top: 2px; line-height: 1.4; }
  .section-title { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
    color: #6b7488; margin: 24px 0 8px; }
  .refresh { margin-top: 24px; text-align: center; }
  button {
    background: #2b64ff; color: white; border: none; border-radius: 10px;
    padding: 12px 20px; font-size: 15px; font-weight: 600; width: 100%;
  }
  .loading { color: #6b7488; font-size: 13px; text-align: center; padding: 30px 0; }
</style>
</head>
<body>
  <h1>🦋 Mariposa · System Status</h1>
  <div class="sub" id="updated-at">กำลังโหลด...</div>

  <div class="section-title">การทำงานอัตโนมัติ (จากประวัติจริง)</div>
  <div class="grid" id="heartbeats"><div class="loading">กำลังโหลด...</div></div>

  <div class="section-title">เช็คสดตอนนี้</div>
  <div class="grid" id="live"><div class="loading">กำลังโหลด...</div></div>

  <div class="refresh"><button onclick="load()">🔄 รีเฟรช</button></div>

<script>
function fmtAgo(sec) {
  if (sec === null || sec === undefined) return "ไม่เคยเห็นเลย";
  if (sec < 60) return sec + " วินาทีที่แล้ว";
  if (sec < 3600) return Math.floor(sec/60) + " นาทีที่แล้ว";
  if (sec < 86400) return Math.floor(sec/3600) + " ชม.ที่แล้ว";
  return Math.floor(sec/86400) + " วันที่แล้ว";
}

function card(name, ok, detail, neverSeen) {
  var dotClass = neverSeen ? "unknown" : (ok ? "ok" : "bad");
  return '<div class="card"><div class="dot ' + dotClass + '"></div>' +
    '<div><div class="name">' + name + '</div>' +
    '<div class="detail">' + detail + '</div></div></div>';
}

function load() {
  document.getElementById("updated-at").textContent = "กำลังโหลด...";
  fetch("/status").then(function(r){ return r.json(); }).then(function(data){
    var updated = new Date(data.generated_at_unix * 1000);
    document.getElementById("updated-at").textContent = "อัปเดตล่าสุด: " + updated.toLocaleTimeString("th-TH");

    var hbHtml = "";
    for (var key in data.heartbeats) {
      var c = data.heartbeats[key];
      var detail = c.never_seen ? "ยังไม่เคยเห็นเลย" : ("เห็นล่าสุด: " + fmtAgo(c.seconds_ago));
      hbHtml += card(c.label, c.ok, detail, c.never_seen);
    }
    document.getElementById("heartbeats").innerHTML = hbHtml;

    var liveHtml = "";
    var liveLabels = {
      google_sheets: "Google Sheets",
      upstash_redis: "Upstash Redis",
      telegram_api: "Telegram Bot API"
    };
    for (var lkey in data.live_checks) {
      var lc = data.live_checks[lkey];
      liveHtml += card(liveLabels[lkey] || lkey, lc.ok, lc.message, false);
    }
    document.getElementById("live").innerHTML = liveHtml;
  }).catch(function(e){
    document.getElementById("updated-at").textContent = "โหลดไม่สำเร็จ: " + e;
  });
}

load();
</script>
</body>
</html>
"""


def start_polling_in_background():
    thread = threading.Thread(target=run_polling_loop, args=(CONFIG, "XAUUSD"), daemon=True)
    thread.start()


# เริ่ม polling loop ทันทีที่ import ไฟล์นี้ (ไม่ใช่แค่ตอนรันใน __main__) เพราะ Render/Gunicorn
# มักไม่ได้เรียกผ่าน "python run_bot.py" ตรงๆ แต่ผ่าน WSGI server ที่ import ตัวแปร app ไปใช้เอง
start_polling_in_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
