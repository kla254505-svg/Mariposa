from quality import (
    score_order_block_quality,
    score_fvg_quality,
    calc_ema_distance_score,
    calc_macd_slope,
)

WEIGHTS = {
    "structure_event": 13,
    "order_block": 12,        # scale ตามคุณภาพ (ขนาดเทียบ ATR)
    "fvg": 8,                 # scale ตามคุณภาพ
    "structure_pullback": 8,
    "liquidity_sweep": 13,    # ต้องเจอ "สวีปแล้วกลับตัว" จริง
    "ote_zone": 10,
    "rsi_confirm": 6,
    "rr_quality": 5,
    "ema_distance": 9,        # scale ตามระยะห่าง EMA fast-slow เทียบ ATR
    "macd_slope_confirm": 8,  # เต็มถ้า MACD Histogram ไปทางเดียวกับทิศทางที่จะเข้า (สุทธิ ไม่ต้องชันทุกแท่ง)
    "ema_bias_confluence": 8, # ใหม่: EMA Bias ตรงกับ Structure Trend หรือไม่
}


def calc_confidence_score(entry_signal, structure, df, config, rr_tp1):
    score = 0
    breakdown = {}

    direction = entry_signal.get("direction")
    atr_val = df["atr"].iloc[-1] if "atr" in df.columns else 0
    last_row = df.iloc[-1]

    if structure.get("event") in ("BOS", "CHoCH"):
        score += WEIGHTS["structure_event"]
        breakdown["structure_event"] = WEIGHTS["structure_event"]

    ob = entry_signal.get("ob")
    if ob:
        quality = score_order_block_quality(ob, atr_val)
        pts = round(WEIGHTS["order_block"] * quality, 1)
        if pts > 0:
            score += pts
            breakdown["order_block"] = pts

    fvg = entry_signal.get("fvg")
    if fvg:
        quality = score_fvg_quality(fvg, atr_val)
        pts = round(WEIGHTS["fvg"] * quality, 1)
        if pts > 0:
            score += pts
            breakdown["fvg"] = pts

    if entry_signal.get("structure_zone"):
        score += WEIGHTS["structure_pullback"]
        breakdown["structure_pullback"] = WEIGHTS["structure_pullback"]

    # --- Liquidity Sweep: ต้องเจอสวีปแล้วกลับตัวจริง ---
    if entry_signal.get("liquidity_sweep"):
        score += WEIGHTS["liquidity_sweep"]
        breakdown["liquidity_sweep"] = WEIGHTS["liquidity_sweep"]

    fib_levels = entry_signal.get("fib_levels")
    if fib_levels:
        from fibo import is_in_ote_zone
        current_price = df["close"].iloc[-1]
        if is_in_ote_zone(current_price, fib_levels):
            score += WEIGHTS["ote_zone"]
            breakdown["ote_zone"] = WEIGHTS["ote_zone"]

    rsi_val = df["rsi"].iloc[-1] if "rsi" in df.columns else 50
    if direction == "bullish" and rsi_val < 65:
        score += WEIGHTS["rsi_confirm"]
        breakdown["rsi_confirm"] = WEIGHTS["rsi_confirm"]
    elif direction == "bearish" and rsi_val > 35:
        score += WEIGHTS["rsi_confirm"]
        breakdown["rsi_confirm"] = WEIGHTS["rsi_confirm"]

    if rr_tp1 >= config["min_rr"]:
        score += WEIGHTS["rr_quality"]
        breakdown["rr_quality"] = WEIGHTS["rr_quality"]

    # --- EMA Distance: ยิ่ง EMA Fast/Slow ห่างกันเทียบ ATR มาก = เทรนด์แข็งแรงจริง ---
    ema_dist_ratio = calc_ema_distance_score(last_row, atr_val)
    ema_pts = round(WEIGHTS["ema_distance"] * ema_dist_ratio, 1)
    if ema_pts > 0:
        score += ema_pts
        breakdown["ema_distance"] = ema_pts

    # --- MACD Slope: โมเมนตัมสุทธิต้องไปทางเดียวกับทิศทางที่จะเข้า ---
    slope = calc_macd_slope(df)
    if (direction == "bullish" and slope == "rising") or (direction == "bearish" and slope == "falling"):
        score += WEIGHTS["macd_slope_confirm"]
        breakdown["macd_slope_confirm"] = WEIGHTS["macd_slope_confirm"]

    # --- EMA Bias Confluence: EMA Bias ต้องตรงกับ Structure Trend ---
    ema_bias = structure.get("ema_trend")
    if ema_bias == direction:
        score += WEIGHTS["ema_bias_confluence"]
        breakdown["ema_bias_confluence"] = WEIGHTS["ema_bias_confluence"]

    return {"score": round(score, 1), "breakdown": breakdown}
