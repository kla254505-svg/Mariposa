import os

CONFIG = {
    "swing_lookback": 7,
    "structure_lookback": 50,
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_trend": 200,
    "rsi_period": 14,
    "atr_period": 14,
    "adx_period": 14,
    "adx_min_trend": 20,
    "session_filter_enabled": True,
    "trading_sessions_utc": [(7, 21)],   # London start ถึง NY end รวมกนเป็นช่วงเดียว
    "killzones_utc": [(7, 10), (12, 15)],  # London Open, NY Open
    "ob_lookback": 30,
    "fvg_min_gap_atr": 0.15,
    "fvg_lookback": 60,
    "liquidity_lookback": 40,
    "liquidity_sweep_lookback": 10,
    "equal_level_tolerance_atr": 0.1,
    "atr_contraction_filter_enabled": True,
    "atr_contraction_lookback": 50,
    "atr_contraction_ratio": 0.7,
    # --- 4H Bias (เทรนด์ใหญ่สุด + Premium/Discount) ---
    "bias4h_filter_enabled": True,
    "premium_discount_filter_enabled": True,
    # --- 5M Trigger (รอ reaction กลับตัวจริงก่อนยิง entry) ---
    "trigger5m_filter_enabled": True,
    "trigger5m_lookback": 6,
    "trigger5m_choch_lookback": 20,
    "risk_per_trade_pct": 1.0,
    "structure_entry_atr_mult": 0.5,
    "sl_buffer_atr": 0.25,
    "min_rr": 1.2,
    "tp1_rr": 1.5,
    "tp2_rr": 2.5,
    "tp3_rr": 4.0,
    # หมายเหตุ: ตัวนี้ใช้แค่ตอน print console report (report.py) ว่า setup "น่าสนใจ" พอจะจับตาดูไหม
    # ไม่ใช่ตัวกำหนดว่าจะยิง Telegram Alert หรือไม่ — ตัวที่คุมการยิง Alert จริงคือ min_score_to_alert (ด้านล่าง)
    # ตั้งไว้ต่ำกว่า min_score_to_alert เสมอ (เป็น "เฝ้าดูก่อน" ที่บาร์ต่ำกว่า "พร้อมแจ้งเตือนจริง")
    "min_score_console_watchlist": 30,
    # --- SL: กันเคส zone แคบ/ATR หดตัวชั่วคราวจนได้ SL แคบผิดปกติ ---
    "min_sl_distance": 10.0,   # ระยะ SL ขั้นต่ำเป็นราคาจริง (เช่น เข้า 4124 SL ห่างอย่างน้อย 10.0 = 4114)
    "sl_atr_avg_period": 20,   # ใช้ ATR เฉลี่ยย้อนหลังกี่แท่งสำหรับคำนวณ buffer แทน ATR แท่งล่าสุดเป๊ะๆ
    "fib_levels": [0.382, 0.5, 0.618, 0.705, 0.79],
    # --- Plan 2/3 (Breakout / สวนเทรนด์) จาก Hourly Briefing: เกณฑ์ยืนยันก่อนยิง Alert จริง ---
    "breakout_confirm_atr_mult": 0.3,   # ราคาต้องปิดเลยระดับ swing high/low ไปเกิน 0.3*ATR ถึงจะนับว่า "ทะลุแรงๆ" จริง
    # --- Secrets: อ่านจาก GitHub Actions Secrets (Environment Variables) ---
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    # กลุ่ม Telegram แยกต่างหาก (optional) — ใช้เฉพาะ "สัญญาณเข้าเทรด" กับ "เตือนข่าวล่วงหน้า 1 ชม."
    # ถ้าไม่ตั้งค่า env ตัวนี้ไว้ ระบบจะไม่ส่งเข้ากลุ่ม (ส่งแค่ telegram_chat_id เดิมตามปกติ)
    "telegram_group_chat_id": os.environ.get("TELEGRAM_GROUP_CHAT_ID", ""),
    # ID ผู้ใช้ Telegram ของเจ้าของบอท (ตัวเลข ไม่ใช่ username) — คำสั่ง /order /trend /news /status /summary
    # จะตอบเฉพาะคนนี้เท่านั้น คนอื่นในกลุ่มพิมพ์คำสั่งจะถูกเมินเงียบๆ หาได้จาก @userinfobot บน Telegram
    # ถ้าไม่ตั้งค่านี้ไว้ ระบบจะไม่ประมวลผลคำสั่งใดๆ เลย (ปลอดภัยไว้ก่อน)
    "telegram_owner_id": os.environ.get("TELEGRAM_OWNER_ID", ""),
    # ปิด/เปิดการแจ้งเตือนอัตโนมัติ (Push) ทั้งหมด — ถ้า False บอทจะเงียบสนิท ไม่ส่งอะไรเองเลย
    # ต้องพิมพ์คำสั่ง /order /trend /news /status /summary เอาเองถึงจะได้คำตอบ (Pull-only mode)
    # ตั้งเป็น True เมื่อไหร่ก็ได้ถ้าอยากได้ Push กลับมาเหมือนเดิม ไม่ต้องแก้โค้ดที่อื่นเลย
    "push_notifications_enabled": False,
    # ตัวนี้คุมว่าจะยิง Telegram Alert จริงหรือไม่ (ต่างจาก min_score_console_watchlist ด้านบนที่แค่ print console)
    "min_score_to_alert": 45,
    "twelvedata_api_key": os.environ.get("TWELVEDATA_API_KEY", ""),
    "healthchecks_url": os.environ.get("HEALTHCHECKS_URL", ""),
    "kvdb_bucket": os.environ.get("KVDB_BUCKET", ""),


}
