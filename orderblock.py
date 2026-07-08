import pandas as pd


def is_high_volatility_bar(candle, atr_val, mult=2.0):
    """
    เช็คว่าแท่งเทียนนี้ 'ผันผวนผิดปกติ' ไหม (range >= mult * ATR)
    แท่งแบบนี้มักเกิดจากข่าว/สภาพคล่องกระชาก ไม่ใช่การสะสมของสถาบันจริง
    ใช้กรอง Order Block ที่ไม่น่าเชื่อถือออกไป
    """
    if atr_val <= 0:
        return False
    return (candle["high"] - candle["low"]) >= (mult * atr_val)


def find_order_blocks(df, config):
    lookback = config["ob_lookback"]
    start = max(0, len(df) - lookback)
    obs = []

    vol_filter_enabled = config.get("ob_volatility_filter_enabled", True)
    vol_mult = config.get("ob_volatility_atr_mult", 2.0)

    for i in range(start, len(df) - 1):
        candle = df.iloc[i]
        next_candle = df.iloc[i + 1]
        atr_val = df["atr"].iloc[i] if "atr" in df.columns and not pd.isna(df["atr"].iloc[i]) else 0

        # --- กรอง Order Block ที่เกิดจากแท่งเทียนผันผวนผิดปกติออกไปก่อน ---
        if vol_filter_enabled and is_high_volatility_bar(candle, atr_val, vol_mult):
            continue

        is_bearish_candle = candle["close"] < candle["open"]
        is_bullish_candle = candle["close"] > candle["open"]
        impulsive_up = next_candle["close"] > candle["high"]
        impulsive_down = next_candle["close"] < candle["low"]

        if is_bearish_candle and impulsive_up:
            obs.append({"type": "bullish", "index": i, "top": candle["high"],
                        "bottom": candle["low"], "mitigated": False})
        elif is_bullish_candle and impulsive_down:
            obs.append({"type": "bearish", "index": i, "top": candle["high"],
                        "bottom": candle["low"], "mitigated": False})

    for ob in obs:
        for j in range(ob["index"] + 1, len(df)):
            price_low = df["low"].iloc[j]
            price_high = df["high"].iloc[j]
            if price_low <= ob["top"] and price_high >= ob["bottom"]:
                ob["mitigated"] = True
                break

    return obs


def get_nearest_unmitigated_ob(obs, direction, current_price):
    candidates = [ob for ob in obs if ob["type"] == direction and not ob["mitigated"]]
    if not candidates:
        return None
    if direction == "bullish":
        below = [ob for ob in candidates if ob["top"] <= current_price]
        if not below:
            return None
        return max(below, key=lambda ob: ob["top"])
    else:
        above = [ob for ob in candidates if ob["bottom"] >= current_price]
        if not above:
            return None
        return min(above, key=lambda ob: ob["bottom"])
