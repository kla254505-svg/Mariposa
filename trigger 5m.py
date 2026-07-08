"""
trigger_5m.py
ชั้น Trigger เข้าไม้จริง (5M) — อยู่ล่างสุดของ MTF pipeline
รอราคาวิ่งเข้ามาแตะโซนที่ 15M ระบุไว้ (OB/FVG/Structure zone) ก่อน แล้วค่อยดู reaction กลับตัวจริง
(mini BOS/CHoCH หรือ engulfing candle) แทนที่จะยิง entry ทันทีที่เห็นโซนบน 15M เฉยๆ
"""

import pandas as pd
from pattern import find_swings, get_last_swings


def _is_engulfing(prev_candle, candle, direction):
    if direction == "bullish":
        return (
            candle["close"] > candle["open"]
            and prev_candle["close"] < prev_candle["open"]
            and candle["close"] >= prev_candle["open"]
            and candle["open"] <= prev_candle["close"]
        )
    else:
        return (
            candle["close"] < candle["open"]
            and prev_candle["close"] > prev_candle["open"]
            and candle["close"] <= prev_candle["open"]
            and candle["open"] >= prev_candle["close"]
        )


def _mini_choch(df, direction, lookback):
    """เช็ค mini BOS/CHoCH กลับทิศบนเฟรม 5M ใน lookback แท่งล่าสุด"""
    sub = df.iloc[-lookback:].copy() if len(df) > lookback else df.copy()
    if len(sub) < 7:
        return False
    sub = find_swings(sub, lookback=3)
    swings = get_last_swings(sub, n_points=2)
    if len(swings) < 2:
        return False

    last_close = df["close"].iloc[-1]
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    # ขาขึ้น: ราคาปิดทะลุ swing high ล่าสุดขึ้นไป = สัญญาณกลับตัวขึ้นจริงบน 5M
    if direction == "bullish" and highs and last_close > highs[-1]["price"]:
        return True
    # ขาลง: ราคาปิดทะลุ swing low ล่าสุดลงไป = สัญญาณกลับตัวลงจริงบน 5M
    if direction == "bearish" and lows and last_close < lows[-1]["price"]:
        return True
    return False


def find_5m_trigger(df_5m, zone_top, zone_bottom, direction, config):
    """
    รับ df เฟรม 5M + ขอบบน-ล่างของโซนที่ 15M ระบุ (OB/FVG/Structure zone)
    เช็คว่า:
      1. ราคาในไม่กี่แท่งล่าสุด เข้ามาแตะโซนนี้จริงไหม
      2. มี reaction กลับตัวยืนยัน: engulfing candle สวนทิศเดิม หรือ mini BOS/CHoCH บน 5M
    คืนค่า dict:
      confirmed    : bool — ยืนยันเข้าไม้ได้หรือยัง
      reason       : str — เหตุผลล่าสุด (ไว้โชว์ใน report/alert)
      trigger_price/trigger_low/trigger_high : จุดกลับตัวจริงบน 5M (ใช้ลด SL ให้แคบลง)
    """
    result = {
        "confirmed": False,
        "reason": "ยังไม่มีราคาเข้ามาแตะโซนบน 5M — รอราคาวิ่งเข้าโซนก่อน",
        "trigger_price": None,
        "trigger_low": None,
        "trigger_high": None,
    }

    if df_5m is None or len(df_5m) < 2 or zone_top is None or zone_bottom is None:
        return result

    lookback = config.get("trigger5m_lookback", 6)
    recent = df_5m.iloc[-lookback:] if len(df_5m) > lookback else df_5m

    touched = any(
        recent.iloc[idx]["low"] <= zone_top and recent.iloc[idx]["high"] >= zone_bottom
        for idx in range(len(recent))
    )
    if not touched:
        return result

    last = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]

    engulfing = _is_engulfing(prev, last, direction)
    mini_choch = _mini_choch(df_5m, direction, config.get("trigger5m_choch_lookback", 20))

    if engulfing or mini_choch:
        reason_bits = []
        if engulfing:
            reason_bits.append("เจอ Engulfing Candle สวนทิศเดิมในโซน")
        if mini_choch:
            reason_bits.append("เจอ Mini BOS/CHoCH กลับทิศบน 5M")
        result["confirmed"] = True
        result["reason"] = " และ ".join(reason_bits) + " — ยืนยันเข้าไม้"
        result["trigger_price"] = float(last["close"])
        result["trigger_low"] = float(last["low"])
        result["trigger_high"] = float(last["high"])
    else:
        result["reason"] = "ราคาแตะโซนแล้วแต่ยังไม่มี reaction กลับตัวชัดเจน (รอ engulfing/mini CHoCH)"

    return result
