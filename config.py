import os

CONFIG = {
    "swing_lookback": 5,
    "structure_lookback": 50,
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_trend": 200,
    "rsi_period": 14,
    "atr_period": 14,
    "ob_lookback": 30,
    "fvg_min_gap_atr": 0.15,
    "liquidity_lookback": 40,
    "equal_level_tolerance_atr": 0.1,
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
