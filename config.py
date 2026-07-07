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
    "risk_per_trade_pct": 1.0,
    "structure_entry_atr_mult": 0.5,
    "sl_buffer_atr": 0.25,
    "min_rr": 1.2,
    "tp1_rr": 1.5,
    "tp2_rr": 2.5,
    "tp3_rr": 4.0,
    "min_score_to_trade": 50,
    "fib_levels": [0.382, 0.5, 0.618, 0.705, 0.79],
    # --- Secrets: อ่านจาก GitHub Actions Secrets (Environment Variables) ---
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "min_score_to_alert": 70,
    "twelvedata_api_key": os.environ.get("TWELVEDATA_API_KEY", ""),
    "healthchecks_url": os.environ.get("HEALTHCHECKS_URL", ""),
    "kvdb_bucket": os.environ.get("KVDB_BUCKET", ""),


}
