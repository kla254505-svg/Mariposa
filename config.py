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
    "min_score_to_trade": 50,
    # --- SL: กันเคส zone แคบ/ATR หดตัวชั่วคราวจนได้ SL แคบผิดปกติ ---
    "min_sl_distance": 10.0,   # ระยะ SL ขั้นต่ำเป็นราคาจริง (เช่น เข้า 4124 SL ห่างอย่างน้อย 10.0 = 4114)
    "sl_atr_avg_period": 20,   # ใช้ ATR เฉลี่ยย้อนหลังกี่แท่งสำหรับคำนวณ buffer แทน ATR แท่งล่าสุดเป๊ะๆ
    "fib_levels": [0.382, 0.5, 0.618, 0.705, 0.79],
    # --- Secrets: อ่านจาก GitHub Actions Secrets (Environment Variables) ---
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    # กลุ่ม Telegram แยกต่างหาก (optional) — ใช้เฉพาะ "สัญญาณเข้าเทรด" กับ "เตือนข่าวล่วงหน้า 1 ชม."
    # ถ้าไม่ตั้งค่า env ตัวนี้ไว้ ระบบจะไม่ส่งเข้ากลุ่ม (ส่งแค่ telegram_chat_id เดิมตามปกติ)
    "telegram_group_chat_id": os.environ.get("TELEGRAM_GROUP_CHAT_ID", ""),
    "min_score_to_alert": 70,
    "twelvedata_api_key": os.environ.get("TWELVEDATA_API_KEY", ""),
    "healthchecks_url": os.environ.get("HEALTHCHECKS_URL", ""),
    "kvdb_bucket": os.environ.get("KVDB_BUCKET", ""),


}
