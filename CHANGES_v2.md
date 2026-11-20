# Mariposa v2 — สรุปการแก้ (19–20 พ.ย. 2026)

## ไฟล์ที่เพิ่ม/แก้
- **orderai.py (ใหม่)** — /orderai, ปุ่ม ✅/❌, ติดตามออเดอร์
- test_orderai.py (ใหม่, 19 เทส)
- telegram_bot.py — /orderai + รับปุ่ม + ส่งตัวเลขดิบ Entry/SL/TP ของทุกแผนออกมา (trade)
- main.py — เรียก track_orders ทุกรอบ cron, Risk ตามแผนของ P1
- config.py — โมเดล claude-sonnet-5-5, ปิด AI อัตโนมัติ, ปิด P2, Risk ตามแผน, ค่า /orderai
- risk.py, trade_quality.py, plan_runner.py — โหมด Risk "plan", ข้าม P2
- ai_layer.py — โมเดลเริ่มต้น + แสดง error HTTP เต็มขึ้น (600 ตัวอักษร)
- sheets_log.py — แท็บ Order_AI (สร้างเองถ้าไม่มี)
- .github/workflows/run.yml — เพิ่ม env TELEGRAM_OWNER_ID

## ต้องตั้งค่า (ทำครั้งเดียว)
1. GitHub Secrets: เพิ่ม `TELEGRAM_OWNER_ID` (ตัวเลข ID ของคุณ ตัวเดียวกับที่ตั้งบน Render) และตั้ง `AI_MODEL` = `claude-sonnet-5-5`
2. Render Environment: ตั้ง `AI_MODEL` = `claude-sonnet-5-5`
3. ชีตใหม่: อัปโหลด Trading_Bot_Database_v2.xlsx เป็น Google Sheet → Share ให้ service account → ตั้ง GOOGLE_SHEETS_ID (GitHub) / GOOGLE_SHEET_ID (Render)
4. GitHub > Actions > Gold Trading Bot > กด Enable workflow (และทำทุกเดือน)
5. Deploy Render ใหม่ แล้วพิมพ์ /aicheck และ /sheetscheck เพื่อทดสอบ

## วิธีใช้ /orderai
พิมพ์ /orderai → AI ตอบ เข้า/รอก่อน/ไม่เข้า + เหตุผล + จุดเข้า (ถ้ามี) → กด ✅ วางแล้ว (ผมวางเองในโบรก) หรือ ❌ ไม่วาง
บอทไม่ยิงออเดอร์จริง ติดตามทุก ~5 นาที: ✅ เข้าแล้ว / 🎯❌ ปิด / ⚠️ ควรยกเลิก (ตกรถ, แตะ SL ก่อนเข้า, 4H/15M พลิก, ข่าวแรง, หมดอายุ)

## ปรับค่าได้ใน config.py
plans_disabled, risk_pct_by_plan, orderai_button_ttl_minutes, orderai_track_expiry_hours, orderai_missed_progress
สถิติแผนที่ป้อนให้ AI: orderai.py > PLAN_STATS
