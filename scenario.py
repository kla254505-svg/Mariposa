"""
scenario.py
สร้างรายงานวิเคราะห์ 3 แผนสำหรับ Hourly Briefing
"""

from candles import detect_engulfing, detect_pin_bar, detect_macd_cross, detect_rsi_divergence
from risk import calc_stop_loss
from tp import calc_take_profits, calc_risk_reward
from zones import calc_premium_discount_zone


def build_pullback_plan(df, structure, entry_signal, config):
    trend = structure["trend"]
    if trend == "sideway":
        return "ตลาดยัง sideway ไม่มีเทรนดชัดเจนให้รอ Pullback ตาม"

    direction_th = "ขึ้น" if trend == "bullish" else "ลง"
    lines = [f"เทรนด์หลักตอนนี้คือ {direction_th}"]

    if entry_signal.get("valid") and entry_signal.get("direction") == trend:
        lines.append(f"รอราคาย่อมาที่ Entry ~{entry_signal['entry_price']:.4f} ตามโซน OB/FVG ที่เจอ")

        # คำนวณ SL/TP ด้วยสูตรเดียวกับที่ใช้ตอนยิง Alert จริง (risk.py / tp.py) ให้ดูล่วงหน้าได้เลย
        # ใช้ ATR เฉลี่ยย้อนหลัง เหมือนกับที่ main.py ใช้คำนวณ SL จริงตอนยิง Alert (สอดคล้องกัน)
        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = df["atr"].tail(atr_period).mean() if "atr" in df.columns and len(df) else 0
        stop_loss = calc_stop_loss(entry_signal, current_atr, config)
        take_profits = calc_take_profits(entry_signal["entry_price"], stop_loss, entry_signal["direction"], config)
        rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
              for name, price in take_profits.items()}

        lines.append(f"SL (คาดการณ์): {stop_loss:.4f}")
        for name, price in take_profits.items():
            lines.append(f"{name}: {price:.4f} (RR {rr[name]})")

        current_price = df["close"].iloc[-1] if len(df) else None
        stale_threshold = 2 * current_atr if current_atr else config.get("min_sl_distance", 10.0)
        price_line = f"ราคา ณ ตอนสร้าง Briefing นี้: {current_price:.4f}" if current_price is not None else ""
        lines.append(
            f"หมายเหตุ: ตัวเลข SL/TP นี้คำนวณจากโซนปัจจุบัน ({price_line}) ถ้าราคายังไม่ย่อมาเข้าโซนจริง "
            "โซน OB/FVG อาจขยับได้ก่อนราคาจะมาถึง — และ Plan 1 นี้ไม่ผ่านฟิลเตอร์ 4H/1H/Session แบบ Alert จริง "
            f"ถ้าราคาตอนที่คุณอ่านอันนี้ห่างจากราคาข้างต้นเกิน {stale_threshold:.2f} ให้ถือว่าข้อมูลนี้เก่าไปแล้ว "
            "ควรเช็ค Dashboard ล่าสุดก่อนเข้าไม้เสมอ ไม่ควรเข้าตามตัวเลขนี้ตรงๆ"
        )
    else:
        lines.append("ยังไม่เจอโซน OB/FVG ที่ชัดเจนพอให้รอเข้าตามเทรนด์นี้ในตอนนี้")

    return " | ".join(lines[:2]) + ("\n" + "\n".join(lines[2:]) if len(lines) > 2 else "")


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


def _evaluate_counter_trend_checklist(df, structure):
    """ตัวช่วยกลาง: คำนวณ checklist สวนเทรนด์ครั้งเดียว ใช้ร่วมกันทั้ง build_counter_trend_plan (ข้อความ)
    และ detect_counter_trend_trigger (เช็คว่าควรยิง Alert จริงไหม) กันตรรกะซ้ำซ้อนสองที่"""
    trend = structure["trend"]
    if trend == "sideway":
        return None

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
    return counter_direction, checklist


def build_counter_trend_plan(df, structure):
    result = _evaluate_counter_trend_checklist(df, structure)
    if result is None:
        return "ตลาด sideway ไม่มเทรนด์หลักให้สวน ข้ามแผนนี้"

    _, checklist = result
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


def detect_breakout_trigger(df, structure, config):
    """
    เช็คว่าราคา 'ทะลุแรงๆ' ตาม Plan 2 (Breakout) จริงหรือยัง
    'แรงๆ' = ปิดเลยระดับ swing high/low ล่าสุดไปเกิน buffer (คูณ ATR) ไม่ใช่แค่ wick แตะผ่านนิดเดียว
    คืนค่า None ถ้ายังไม่ทะลุ, หรือ dict {"direction","level","price"} ถ้าทะลุแล้ว
    """
    swings = structure.get("last_swings", [])
    if len(swings) < 2 or not len(df):
        return None

    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    current_price = df["close"].iloc[-1]
    atr_val = df["atr"].iloc[-1] if "atr" in df.columns else 0
    buffer = config.get("breakout_confirm_atr_mult", 0.3) * atr_val

    if highs:
        last_high = highs[-1]["price"]
        if current_price > last_high + buffer:
            return {"direction": "bullish", "level": last_high, "price": current_price}

    if lows:
        last_low = lows[-1]["price"]
        if current_price < last_low - buffer:
            return {"direction": "bearish", "level": last_low, "price": current_price}

    return None


def detect_counter_trend_trigger(df, structure):
    """เช็คว่า Checklist สวนเทรนด์ (Plan 3) ผ่านครบทุกข้อแล้วหรือยัง
    คืนค่า None ถ้ายังไม่ครบ, หรือ dict {"direction","checklist"} ถ้าครบแล้ว"""
    result = _evaluate_counter_trend_checklist(df, structure)
    if result is None:
        return None

    counter_direction, checklist = result
    if all(checklist.values()):
        return {"direction": counter_direction, "checklist": checklist}
    return None


def calc_breakout_order(breakout, structure, df, config):
    """
    คำนวณ Entry/SL/TP ให้แผนที่ 2 (Breakout) — จุดเดียวที่ใช้ร่วมกันทั้ง main.py (ตอน trigger จริง
    เพื่อบันทึกออเดอร์ลง Order Dashboard) และ telegram_bot.py (ตอนแสดงผลใน /order)

    - SL แบบอิงจุดที่ทะลุ (structure-based): วางไว้เลยระดับที่ทะลุ (breakout['level']) กลับไปอีกฝั่ง
      นิดหน่อย (buffer = sl_buffer_atr × ATR เฉลี่ย ตัวเดียวกับที่ main.py ใช้ตอน tighten SL ของ Plan 1)
      ตรรกะ: ถ้าราคาย้อนกลับผ่านระดับเดิมไปอีกฝั่ง breakout ครั้งนี้ถือว่าล้มเหลว (false breakout)
    - TP แบบ Measured move: วัดความสูงของ range ที่ราคาสะสมตัวก่อนทะลุ (จาก swing ฝั่งตรงข้ามล่าสุด
      ก่อนถึงระดับที่ทะลุ ณ ตอนนี้เก็บไว้ใน structure['last_swings']) แล้ว project ระยะเท่ากันจากจุดที่
      ทะลุจริงเป็นเป้าหมาย เป็นวิธีมาตรฐานของ breakout trading ที่ผูกเป้าหมายกับโครงสร้างราคาจริง
      ไม่ใช่ตัวเลข RR ลอยๆ (สอดคล้องกับตรรกะเดียวกับที่ใช้เลือก SL ด้านบน)

    คืน None ถ้าหาข้อมูล swing ที่จำเป็นไม่ได้ (กันไม่ให้ผู้เรียกพังเพราะแผนเสริมคำนวณไม่ได้)
    """
    try:
        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = df["atr"].tail(atr_period).mean() if "atr" in df.columns and len(df) else 0
        buffer = config.get("sl_buffer_atr", 0.5) * current_atr

        level = breakout["level"]
        direction = breakout["direction"]
        entry_price = breakout["price"]
        swings = structure.get("last_swings", [])

        if direction == "bullish":
            stop_loss = level - buffer
            lows = [p["price"] for p in swings if p["type"] == "low"]
            if not lows:
                return None
            range_height = level - lows[-1]
            take_profit = entry_price + range_height
        else:
            stop_loss = level + buffer
            highs = [p["price"] for p in swings if p["type"] == "high"]
            if not highs:
                return None
            range_height = highs[-1] - level
            take_profit = entry_price - range_height

        if range_height <= 0:
            return None  # range หาความสูงไม่ได้จริง (swing ผิดปกติ) กันเผื่อไว้

        rr = calc_risk_reward(entry_price, stop_loss, take_profit)
        return {
            "direction": direction, "entry_price": entry_price,
            "stop_loss": stop_loss, "take_profit": take_profit, "rr": rr,
        }
    except Exception:
        return None


def calc_counter_trend_order(counter, df, config):
    """
    คำนวณ Entry/SL/TP ให้แผนที่ 3 (สวนเทรนด์) — จุดเดียวที่ใช้ร่วมกันทั้ง main.py และ telegram_bot.py

    - SL: วางไว้เลยจุด high/low ล่าสุด (จุดที่ราคาเพิ่งเด้งกลับมา เป็นสัญญาณของการสวนเทรนด์) ไปอีกนิดหน่อย
      (buffer เดียวกับแผนที่ 2) ถ้าราคาทะลุจุดนั้นไปอีก แปลว่าที่คาดว่าจะกลับตัวยังไม่เกิดขึ้นจริง
    - TP: เล็งที่ Equilibrium ของ Premium/Discount zone (สูตรเดียวกับที่ /trend ใช้แสดงผล) เพราะการ
      สวนเทรนด์มักเป็นแค่การพักตัวระยะสั้นกลับไปแถวกึ่งกลาง range ไม่ใช่การกลับเทรนด์เต็มรูปแบบ
      การตั้งเป้าไกลแบบ RR multiple เหมือน Plan 1 อาจเกินจริงสำหรับการเทรดสวนเทรนด์
    """
    try:
        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = df["atr"].tail(atr_period).mean() if "atr" in df.columns and len(df) else 0
        buffer = config.get("sl_buffer_atr", 0.5) * current_atr

        direction = counter["direction"]
        entry_price = df["close"].iloc[-1]  # สวนเทรนด์เข้าที่ตลาด ไม่มีโซนรอเหมือน Plan 1

        pd_zone = calc_premium_discount_zone(df, config.get("structure_lookback", 50))
        take_profit = pd_zone["equilibrium"]

        if direction == "bullish":
            stop_loss = df["low"].tail(20).min() - buffer
        else:
            stop_loss = df["high"].tail(20).max() + buffer

        rr = calc_risk_reward(entry_price, stop_loss, take_profit)
        return {
            "direction": direction, "entry_price": entry_price,
            "stop_loss": stop_loss, "take_profit": take_profit, "rr": rr,
        }
    except Exception:
        return None


def build_summary(structure, entry_signal):
    trend = structure["trend"]
    event = structure["event"]

    if trend == "sideway":
        return "ตลาด sideway แนะนำรอสัญญาณที่ชัดเจนกว่านี ยังไม่ควร scalp"

    if entry_signal.get("valid"):
        direction_th = "LONG" if entry_signal["direction"] == "bullish" else "SHORT"
        return f"เทรนด์ {trend} ({event}) มีโซนเข้าไม้ {direction_th} ให้รอตามแผนที่ 1"

    return f"เทรนด์ {trend} ({event}) แต่ยังไม่เจอโซนเข้าไม้ชัดเจน แนะนำรอดูก่อน"


def build_hourly_briefing(symbol, timeframe, df, structure, entry_signal, config):
    plan1 = build_pullback_plan(df, structure, entry_signal, config)
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
