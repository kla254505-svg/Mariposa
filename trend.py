import pandas as pd
from pattern import find_swings, get_last_swings


def get_ema_trend(row):
    if row["close"] > row["ema_fast"] > row["ema_slow"] > row["ema_trend"]:
        return "bullish"
    if row["close"] < row["ema_fast"] < row["ema_slow"] < row["ema_trend"]:
        return "bearish"
    return "sideway"


def analyze_structure(df, config):
    df = find_swings(df, lookback=config["swing_lookback"])
    swings = get_last_swings(df, n_points=4)

    result = {
        "trend": "sideway",
        "event": None,
        "event_price": None,
        "last_swings": swings,
        "ema_trend": get_ema_trend(df.iloc[-1]) if len(df) else "sideway",
    }

    if len(swings) < 4:
        return result

    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    trend = "sideway"
    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1]["price"] > highs[-2]["price"]
        higher_low = lows[-1]["price"] > lows[-2]["price"]
        lower_low = lows[-1]["price"] < lows[-2]["price"]
        lower_high = highs[-1]["price"] < highs[-2]["price"]

        if higher_high and higher_low:
            trend = "bullish"
        elif lower_low and lower_high:
            trend = "bearish"

    result["trend"] = trend

    last_close = df["close"].iloc[-1]
    last_swing_high = highs[-1]["price"] if highs else None
    last_swing_low = lows[-1]["price"] if lows else None

    if trend == "bullish" and last_swing_low is not None and last_close < last_swing_low:
        result["event"] = "CHoCH"
        result["event_price"] = last_swing_low
    elif trend == "bearish" and last_swing_high is not None and last_close > last_swing_high:
        result["event"] = "CHoCH"
        result["event_price"] = last_swing_high
    elif trend == "bullish" and last_swing_high is not None and last_close > last_swing_high:
        result["event"] = "BOS"
        result["event_price"] = last_swing_high
    elif trend == "bearish" and last_swing_low is not None and last_close < last_swing_low:
        result["event"] = "BOS"
        result["event_price"] = last_swing_low

    return result
