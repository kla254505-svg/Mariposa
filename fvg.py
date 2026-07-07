import pandas as pd


def find_fvgs(df, config):
    fvgs = []
    min_gap = config["fvg_min_gap_atr"]
    lookback = config.get("fvg_lookback", 60)
    start = max(2, len(df) - lookback)

    for i in range(start, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]
        atr_val = df["atr"].iloc[i] if "atr" in df.columns else 0

        gap_up = c3["low"] - c1["high"]
        if gap_up > 0 and (atr_val == 0 or gap_up >= min_gap * atr_val):
            fvgs.append({"type": "bullish", "index": i, "top": c3["low"],
                         "bottom": c1["high"], "filled": False})

        gap_down = c1["low"] - c3["high"]
        if gap_down > 0 and (atr_val == 0 or gap_down >= min_gap * atr_val):
            fvgs.append({"type": "bearish", "index": i, "top": c1["low"],
                         "bottom": c3["high"], "filled": False})

    for fvg in fvgs:
        for j in range(fvg["index"] + 1, len(df)):
            price_low = df["low"].iloc[j]
            price_high = df["high"].iloc[j]
            if price_low <= fvg["bottom"] and price_high >= fvg["top"]:
                fvg["filled"] = True
                break

    return fvgs


def get_nearest_unfilled_fvg(fvgs, direction, current_price):
    candidates = [f for f in fvgs if f["type"] == direction and not f["filled"]]
    if not candidates:
        return None
    if direction == "bullish":
        below = [f for f in candidates if f["top"] <= current_price]
        if not below:
            return None
        return max(below, key=lambda f: f["top"])
    else:
        above = [f for f in candidates if f["bottom"] >= current_price]
        if not above:
            return None
        return min(above, key=lambda f: f["bottom"])
