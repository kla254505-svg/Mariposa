WEIGHTS = {
    "structure_event": 20, "order_block": 20, "fvg": 15,
    "liquidity_sweep": 15, "ote_zone": 15, "rsi_confirm": 10, "rr_quality": 5,
}


def calc_confidence_score(entry_signal, structure, df, config, rr_tp1):
    score = 0
    breakdown = {}

    if structure.get("event") in ("BOS", "CHoCH"):
        score += WEIGHTS["structure_event"]
        breakdown["structure_event"] = WEIGHTS["structure_event"]

    if entry_signal.get("ob"):
        score += WEIGHTS["order_block"]
        breakdown["order_block"] = WEIGHTS["order_block"]

    if entry_signal.get("fvg"):
        score += WEIGHTS["fvg"]
        breakdown["fvg"] = WEIGHTS["fvg"]

    liquidity = entry_signal.get("liquidity") or {}
    direction = entry_signal.get("direction")
    if direction == "bullish" and liquidity.get("equal_lows"):
        score += WEIGHTS["liquidity_sweep"]
        breakdown["liquidity_sweep"] = WEIGHTS["liquidity_sweep"]
    elif direction == "bearish" and liquidity.get("equal_highs"):
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

    return {"score": score, "breakdown": breakdown}
