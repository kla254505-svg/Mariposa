import pandas as pd


def find_swings(df, lookback=5):
    df = df.copy()
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    highs = df["high"].values
    lows = df["low"].values

    for i in range(lookback, n - lookback):
        window_high = highs[i - lookback: i + lookback + 1]
        window_low = lows[i - lookback: i + lookback + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            swing_high[i] = True
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            swing_low[i] = True

    df["swing_high"] = swing_high
    df["swing_low"] = swing_low
    return df


def get_last_swings(df, n_points=4):
    points = []
    for i in range(len(df)):
        if df["swing_high"].iloc[i]:
            points.append({"index": i, "price": df["high"].iloc[i], "type": "high"})
        if df["swing_low"].iloc[i]:
            points.append({"index": i, "price": df["low"].iloc[i], "type": "low"})
    points.sort(key=lambda p: p["index"])
    return points[-n_points:] if len(points) >= n_points else points
