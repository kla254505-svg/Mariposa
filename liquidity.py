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
