import pandas as pd
from pattern import find_swings


def find_liquidity_pools(df, config):
    lookback = config["liquidity_lookback"]
    tolerance_mult = config["equal_level_tolerance_atr"]

    sub_df = df.iloc[-lookback:].copy() if len(df) > lookback else df.copy()
    sub_df = find_swings(sub_df, lookback=config["swing_lookback"])

    atr_val = df["atr"].iloc[-1] if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]) else df["close"].iloc[-1] * 0.001
    tolerance = tolerance_mult * atr_val

    highs = sub_df.loc[sub_df["swing_high"], "high"].tolist()
    lows = sub_df.loc[sub_df["swing_low"], "low"].tolist()

    def cluster(levels):
        clusters = []
        used = [False] * len(levels)
        for i, lvl in enumerate(levels):
            if used[i]:
                continue
            group = [lvl]
            used[i] = True
            for j in range(i + 1, len(levels)):
                if not used[j] and abs(levels[j] - lvl) <= tolerance:
                    group.append(levels[j])
                    used[j] = True
            if len(group) >= 2:
                clusters.append(sum(group) / len(group))
        return clusters

    eqh = cluster(highs)
    eql = cluster(lows)
    return {"equal_highs": eqh, "equal_lows": eql}


def detect_liquidity_sweep(df, pools, direction, lookback=10):
    """
    เช็คว่าในแท่งล่าสุด (lookback แท่ง) มีการ 'กวาด Liquidity แล้วกลับตัว' จริงไหม
    ไม่ใช่แค่มี Equal High/Low อยู่ใกล้ๆเฉยๆ
    Bullish: ราคาแตะ/ทะลุใต้ระดับ Equal Low ด้วยไส้ (wick) แล้วปิดกลับขึ้นเหนือระดับนั้น
    Bearish: ราคาแตะ/ทะลุเหนือระดับ Equal High ด้วยไส้ แล้วปิดกลับลงใต้ระดับนั้น
    คืนค่า dict {'level': ราคา, 'index': ตำแหน่งแท่งที่เกิด} หรือ None ถ้าไม่เจอ
    """
    recent = df.iloc[-lookback:] if len(df) > lookback else df

    if direction == "bullish":
        levels = pools.get("equal_lows", [])
        for level in levels:
            for idx in range(len(recent)):
                candle = recent.iloc[idx]
                if candle["low"] < level and candle["close"] > level:
                    return {"level": level, "index": recent.index[idx]}
        return None
    else:
        levels = pools.get("equal_highs", [])
        for level in levels:
            for idx in range(len(recent)):
                candle = recent.iloc[idx]
                if candle["high"] > level and candle["close"] < level:
                    return {"level": level, "index": recent.index[idx]}
        return None
