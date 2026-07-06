"""
structure_entry.py
กลยุทธ์เข้าไม้แบบ Classic Price Action: รอราคาย่อกลับมาที่จุด
Higher Low (ขาขึ้น) หรือ Lower High (ขาลง) แล้วเด้งกลับตามเทรนด์
ใช้เป็นแผนสำรองเมื่อไม่เจอ Order Block หรือ FVG
"""

import pandas as pd


def find_structure_entry(df, structure, config):
    trend = structure["trend"]
    if trend == "sideway":
        return None

    swings = structure["last_swings"]
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    atr_val = df["atr"].iloc[-1] if "atr" in df.columns and not pd.isna(df["atr"].iloc[-1]) else df["close"].iloc[-1] * 0.001
    tolerance = config.get("structure_entry_atr_mult", 0.5) * atr_val

    current_price = df["close"].iloc[-1]

    if trend == "bullish" and lows:
        swing_low = lows[-1]["price"]
        if abs(current_price - swing_low) <= tolerance * 3:
            return {
                "type": "bullish",
                "top": swing_low + tolerance,
                "bottom": swing_low - tolerance,
                "label": "Structure Pullback (Higher Low)",
            }

    if trend == "bearish" and highs:
        swing_high = highs[-1]["price"]
        if abs(current_price - swing_high) <= tolerance * 3:
            return {
                "type": "bearish",
                "top": swing_high + tolerance,
                "bottom": swing_high - tolerance,
                "label": "Structure Pullback (Lower High)",
            }

    return None
