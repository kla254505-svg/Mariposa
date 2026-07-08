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
    # ดึง pool ใหญ่กว่าแยกไว้เฉพาะคำนวณเทรนด์ เพราะ high/low ไม่จำเป็นต้องสลับกันเป๊ะภายใน 4 จุดล่าสุด
    # (เช่น อาจเจอ high ติดกัน 3 จุดแล้วมี low แค่จุดเดียว) ถ้าใช้ n_points=4 ตรงๆ จะเจอ "sideway" บ่อยเกินจริง
    # ส่วน "last_swings" (n_points=4) ยังคงเดิมไว้ให้โมดูลอื่น (bias_4h, entry, scenario) ใช้ต่อ ไม่กระทบพฤติกรรมเดิม
    swing_pool = get_last_swings(df, n_points=20)

    result = {
        "trend": "sideway",
        # "strong"  = HH+HL (หรือ LL+LH) ครบทั้งคู่ -> เทรนด์ confirm เต็มรูปแบบ
        # "weak"    = confirm แค่ฝั่งเดียว (เช่น high ทำจุดสูงใหม่ แต่ low ยังแกว่งเท่าเดิม) -> โมเมนตัมกำลังก่อตัว ยังไม่ confirm ครบ
        # "none"    = สวนทางกันเอง (high สูงขึ้นแต่ low ก็ต่ำลงด้วย) หรือข้อมูลไม่พอ -> sideway จริง
        "trend_strength": "none",
        "event": None,
        "event_price": None,
        "last_swings": swings,
        "ema_trend": get_ema_trend(df.iloc[-1]) if len(df) else "sideway",
    }

    if len(swing_pool) < 4:
        return result

    highs = [p for p in swing_pool if p["type"] == "high"][-2:]
    lows = [p for p in swing_pool if p["type"] == "low"][-2:]

    trend = "sideway"
    strength = "none"
    if len(highs) == 2 and len(lows) == 2:
        higher_high = highs[-1]["price"] > highs[-2]["price"]
        higher_low = lows[-1]["price"] > lows[-2]["price"]
        lower_low = lows[-1]["price"] < lows[-2]["price"]
        lower_high = highs[-1]["price"] < highs[-2]["price"]

        if higher_high and higher_low:
            trend, strength = "bullish", "strong"
        elif lower_low and lower_high:
            trend, strength = "bearish", "strong"
        else:
            # ไม่ confirm ครบทั้งคู่ -> อาจเป็น "ฝั่งเดียว confirm" (weak trend จริง) หรือ "สวนทางกันเอง"
            # (high สูงขึ้นแต่ low ก็ต่ำลงด้วย = ขยาย range, หรือ low สูงขึ้นแต่ high ต่ำลง = บีบ range)
            # ใช้ swing ล่าสุดสุด (ที่เพิ่งเกิดขึ้นจริงตามเวลา ไม่ว่าจะเป็น high หรือ low) เป็นตัวชี้โมเมนตัมปัจจุบัน
            last_point = highs[-1] if highs[-1]["index"] > lows[-1]["index"] else lows[-1]
            if last_point["type"] == "high":
                trend, strength = ("bullish", "weak") if higher_high else ("bearish", "weak")
            else:
                trend, strength = ("bullish", "weak") if higher_low else ("bearish", "weak")

    result["trend"] = trend
    result["trend_strength"] = strength

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
