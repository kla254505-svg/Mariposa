"""
scenario.py
สร้างรายงานวิเคราะห์ 3 แผนสำหรับ Hourly Briefing
"""

from candles import detect_engulfing, detect_pin_bar, detect_macd_cross, detect_rsi_divergence


def build_pullback_plan(df, structure, entry_signal):
    trend = structure["trend"]
    if trend == "sideway":
        return "ตลาดยัง sideway ไม่มีเทรนดชัดเจนให้รอ Pullback ตาม"

    direction_th = "ขึ้น" if trend == "bullish" else "ลง"
    lines = [f"เทรนด์หลักตอนนี้คือ {direction_th}"]

    if entry_signal.get("valid") and entry_signal.get("direction") == trend:
        lines.append(f"รอราคาย่อมาที่ Entry ~{entry_signal['entry_price']:.4f} ตามโซน OB/FVG ที่เจอ")
    else:
        lines.append("ยังไม่เจอโซน OB/FVG ที่ชัดเจนพอให้รอเข้าตามเทรนด์นี้ในตอนนี้")

    return " | ".join(lines)


def build_breakout_plan(df, structure):
    swings = structure.get("last_swings", [])
    if len(swings) < 2:
        return "ข้อมูล swing ยังไม่พอสำหรับวางแผน Breakout"

    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    lines = []
    if highs:
        last_high = highs[-1]["price"]
        lines.append(f"ถ้าราคาทะลุ {last_high:.4f} ขึ้นไปแรงๆ = trigger เข้า LONG ตาม breakout")
    if lows:
        last_low = lows[-1]["price"]
        lines.append(f"ถ้าราคาหลุด {last_low:.4f} ลงมาแรงๆ = trigger เข้า SHORT ตาม breakout")

    return " | ".join(lines) if lines else "ยังไม่มีจุด trigger ที่ชดเจน"


def build_counter_trend_plan(df, structure):
    trend = structure["trend"]
    if trend == "sideway":
        return "ตลาด sideway ไม่มเทรนด์หลักให้สวน ข้ามแผนนี้"

    counter_direction = "bearish" if trend == "bullish" else "bullish"

    divergence = detect_rsi_divergence(df)
    engulfing = detect_engulfing(df)
    pin_bar = detect_pin_bar(df)
    macd_cross = detect_macd_cross(df)
    candle_signal = engulfing or pin_bar

    checklist = {
        "RSI Divergence สวนเทรนด์": divergence == counter_direction,
        "รปแบบแท่งเทียนกลับตัว": candle_signal == counter_direction,
        "MACD Cross สวนเทรนด์": macd_cross == counter_direction,
    }

    passed = sum(checklist.values())
    total = len(checklist)

    lines = [f"Checklist ผ่าน {passed}/{total} ข้อ:"]
    for name, ok in checklist.items():
        mark = "ผ่าน" if ok else "ไม่ผ่าน"
        lines.append(f"- {name}: {mark}")

    if passed == total:
        lines.append("=> ครบทุกเงื่อนไข พอพิจารณาสวนเทรนด์ได้ (ยังต้องระวัง เทรนดหลักยังไม่กลับจริง)")
    else:
        lines.append("=> ยังไม่ครบเงื่อนไข ไม่แนะนำสวนเทรนดตอนนี้")

    return "\n".join(lines)


def build_summary(structure, entry_signal):
    trend = structure["trend"]
    event = structure["event"]

    if trend == "sideway":
        return "ตลาด sideway แนะนำรอสัญญาณที่ชัดเจนกว่านี ยังไม่ควร scalp"

    if entry_signal.get("valid"):
        direction_th = "LONG" if entry_signal["direction"] == "bullish" else "SHORT"
        return f"เทรนด์ {trend} ({event}) มีโซนเข้าไม้ {direction_th} ให้รอตามแผนที่ 1"

    return f"เทรนด์ {trend} ({event}) แต่ยังไม่เจอโซนเข้าไม้ชัดเจน แนะนำรอดูก่อน"


def build_hourly_briefing(symbol, timeframe, df, structure, entry_signal):
    plan1 = build_pullback_plan(df, structure, entry_signal)
    plan2 = build_breakout_plan(df, structure)
    plan3 = build_counter_trend_plan(df, structure)
    summary = build_summary(structure, entry_signal)

    lines = [
        f"📊 <b>Hourly Briefing: {symbol} ({timeframe})</b>",
        "",
        "<b>แผนที่ 1 — รอ Pullback ตามเทรนด์</b>",
        plan1,
        "",
        "<b>แผนที่ 2 — Breakout</b>",
        plan2,
        "",
        "<b>แผนที่ 3 — สวนเทรนด์ (ต้องผ่าน Checklist)</b>",
        plan3,
        "",
        "<b>สรุป: ตอนนี้ควรทำอะไร</b>",
        summary,
    ]
    return "\n".join(lines)
