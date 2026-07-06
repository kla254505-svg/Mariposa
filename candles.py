"""
candles.py
จับรูปแบบแท่งเทียน (Engulfing, Pin Bar) และหา Divergence / MACD Cross
ใช้เสริมการวิเคราะห์ SMC เดิม ไม่ใช่ตัวตัดสินใจหลัก
"""

import pandas as pd


def detect_engulfing(df):
    """
    เชคว่าแท่งล่าสุด (แท่งสุดท้าย) เป็น Bullish หรือ Bearish Engulfing ไหม
    Bullish Engulfing: แท่งก่อนหน้าเป็นแท่งแดง, แท่งล่าสุดเป็นแทงเขียวที่ตัว (body)
                       ใหญ่คลุมตวแท่งก่อนหน้าทั้งหมด
    Bearish Engulfing: ตรงข้ามกน
    คืนค่า "bullish" / "bearish" / None
    """
    if len(df) < 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    if prev_bearish and curr_bullish:
        if curr["close"] >= prev["open"] and curr["open"] <= prev["close"]:
            return "bullish"

    prev_bullish = prev["close"] > prev["open"]
    curr_bearish = curr["close"] < curr["open"]
    if prev_bullish and curr_bearish:
        if curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
            return "bearish"

    return None


def detect_pin_bar(df, wick_ratio=2.0):
    """
    เช็คว่าแท่งล่าสุดเป็น Pin Bar ไหม (ไส้เทียนยาวกว่าตัว body อย่างน้อย wick_ratio เท่า)
    คืนค่า "bullish" (ไส้ล่างยาว = แรงซื้อกลับ) / "bearish" (ไส้บนยาว = แรงขายกลับ) / None
    """
    if len(df) < 1:
        return None

    candle = df.iloc[-1]
    body = abs(candle["close"] - candle["open"])
    if body == 0:
        body = 0.0001  # กันหารด้วยศูนย์

    upper_wick = candle["high"] - max(candle["close"], candle["open"])
    lower_wick = min(candle["close"], candle["open"]) - candle["low"]

    if lower_wick >= body * wick_ratio and lower_wick > upper_wick:
        return "bullish"
    if upper_wick >= body * wick_ratio and upper_wick > lower_wick:
        return "bearish"

    return None


def detect_macd_cross(df):
    """
    เช็คว่าเพิ่งเกิด MACD Cross ในแท่งล่าสุดไหม (เสน MACD ตัดเส้น Signal)
    คืนค่า "bullish" (ตัดขึ้น) / "bearish" (ตัดลง) / None
    """
    if len(df) < 2 or "macd" not in df.columns:
        return None

    prev_diff = df["macd"].iloc[-2] - df["macd_signal"].iloc[-2]
    curr_diff = df["macd"].iloc[-1] - df["macd_signal"].iloc[-1]

    if prev_diff < 0 and curr_diff > 0:
        return "bullish"
    if prev_diff > 0 and curr_diff < 0:
        return "bearish"

    return None


def detect_rsi_divergence(df, lookback=20):
    """
    หา RSI Divergence แบบง่าย เทียบ swing point ล่าสุด 2 จุดของราคากับ RSI
    Bearish Divergence: ราคาทำ Higher High แต่ RSI ทำ Lower High (โมเมนตมอ่อนลง ทังที่ราคาขึ้น)
    Bullish Divergence: ราคาทำ Lower Low แต่ RSI ทำ Higher Low (โมเมนตัมแข็งขึ้น ทั้งที่ราคาลง)
    คืนค่า "bullish" / "bearish" / None
    """
    if len(df) < lookback or "rsi" not in df.columns:
        return None

    sub = df.iloc[-lookback:]

    # หาจุดสูงสุด 2 จุด (ราคา) เทียบ RSI ณ จุดเดียวกัน แบบง่าย: แบ่งครึ่งช่วงเวลา
    half = lookback // 2
    first_half = sub.iloc[:half]
    second_half = sub.iloc[half:]

    price_high_1 = first_half["high"].max()
    price_high_2 = second_half["high"].max()
    rsi_high_1 = first_half["rsi"].max()
    rsi_high_2 = second_half["rsi"].max()

    price_low_1 = first_half["low"].min()
    price_low_2 = second_half["low"].min()
    rsi_low_1 = first_half["rsi"].min()
    rsi_low_2 = second_half["rsi"].min()

    if price_high_2 > price_high_1 and rsi_high_2 < rsi_high_1:
        return "bearish"

    if price_low_2 < price_low_1 and rsi_low_2 > rsi_low_1:
        return "bullish"

    return None
