"""
qm_pattern_entry.py — กลุ่ม D: Quasimodo (QM) Pattern Entry (Set & Forget)

จากภาพที่ผู้ใช้ส่งมา: หาโครงสร้างสวิง 4 จุดเรียงกัน H -> L -> HH (ทะลุขึ้นเหนือ H เดิม แบบ fakeout/
manipulation) -> LL (ทะลุลงใต้ L เดิมแรงๆ แบบการเคลื่อนไหวจริง) แล้วรอราคาย่อกลับมาทดสอบระดับ H เดิม
(เรียกว่า "QML" — Quasimodo Line) ซึ่งกลายเป็นแนวต้านใหม่ — เข้า Sell ที่ QML, SL เหนือ HH, TP ที่ LL
(ฝั่ง Bullish เป็นภาพกระจก: L -> H -> LL (fakeout) -> HH (breakout จริง), QML = ระดับ L เดิม)

ต่างจากกลุ่ม C (Liquidity Sweep) ตรงที่กลุ่ม C มองแค่ "กวาดแล้วกลับตัวทันที" (sweep 1 จุด + FVG ใกล้ๆ)
แต่กลุ่ม D ต้องมีโครงสร้างสวิง 4 จุดเรียงลำดับเวลาถูกต้องก่อน ถึงจะนับเป็น QM pattern จริง — ใช้
pattern.py's find_swings() ที่มีอยู่แล้วเป็นฐาน แล้วไล่หาลำดับ high/low สลับกันที่ตรงเงื่อนไข
"""
from pattern import find_swings
from fvg import find_fvgs, get_nearest_unfilled_fvg
from orderblock import find_order_blocks, get_nearest_unmitigated_ob


def _get_ordered_swing_points(df, lookback):
    """คืน list ของจุด swing high/low ทั้งหมด เรียงตามเวลา (index) จากเก่าไปใหม่
    แต่ละจุดเป็น dict {"index", "price", "type"} โดย type คือ 'high' หรือ 'low'"""
    swings = find_swings(df, lookback=lookback)
    points = []
    for idx, row in swings.iterrows():
        if row["swing_high"]:
            points.append({"index": idx, "price": row["high"], "type": "high"})
        if row["swing_low"]:
            points.append({"index": idx, "price": row["low"], "type": "low"})
    points.sort(key=lambda p: p["index"])
    return points


def find_qm_pattern(df, config):
    """
    หา QM pattern จาก 4 จุดสวิงล่าสุดที่เรียงลำดับถูกต้อง (ดูเฉพาะช่วง qm_lookback แท่งหลังสุด กัน
    จับ pattern เก่าที่ไม่เกี่ยวข้องกับสถานการณ์ตอนนี้แล้ว):

    Bearish QM: high(H) -> low(L) -> high(HH, HH>H) -> low(LL, LL<L)
      QML = ระดับ H เดิม (แนวต้านที่รอราคาย่อกลับมาทดสอบ)
    Bullish QM (กระจก): low(L) -> high(H) -> low(LL, LL<L) -> high(HH, HH>H)
      QML = ระดับ L เดิม (แนวรับที่รอราคาย่อกลับมาทดสอบ)

    หลังเจอโครงสร้างแล้ว หา OB/FVG ที่ซ้อนทับ/ใกล้ระดับ QML เพื่อความแม่นยำของจุดเข้าเพิ่มเติม
    (ถ้าไม่เจอ ใช้ระดับ QML ตรงๆ เป็นจุดเข้า)
    """
    qm_lookback = config.get("qm_lookback", 60)
    sub_df = df.iloc[-qm_lookback:] if len(df) > qm_lookback else df
    swing_lookback = config.get("swing_lookback", 7)
    points = _get_ordered_swing_points(sub_df, swing_lookback)

    result = {
        "valid": False,
        "direction": None,
        "entry_price": None,
        "reasons": [],
        "qml_level": None,
        "hh_level": None,
        "ll_level": None,
        "ob": None,
        "fvg": None,
    }

    if len(points) < 4:
        result["reasons"].append("ยังไม่มีโครงสร้างสวิงพอที่จะหา QM pattern ได้ตอนนี้")
        return result

    last4 = points[-4:]
    types = [p["type"] for p in last4]

    direction = None
    qml_level = hh_level = ll_level = None

    if types == ["high", "low", "high", "low"] and last4[2]["price"] > last4[0]["price"] \
            and last4[3]["price"] < last4[1]["price"]:
        direction = "bearish"
        qml_level = last4[0]["price"]
        hh_level = last4[2]["price"]
        ll_level = last4[3]["price"]
    elif types == ["low", "high", "low", "high"] and last4[2]["price"] < last4[0]["price"] \
            and last4[3]["price"] > last4[1]["price"]:
        direction = "bullish"
        qml_level = last4[0]["price"]
        ll_level = last4[2]["price"]
        hh_level = last4[3]["price"]

    if direction is None:
        result["reasons"].append("โครงสร้างสวิงล่าสุดไม่ตรงแพทเทิร์น QM (H-L-HH-LL หรือ L-H-LL-HH)")
        return result

    result["direction"] = direction
    result["qml_level"] = qml_level
    result["hh_level"] = hh_level
    result["ll_level"] = ll_level
    result["reasons"].append(
        f"เจอโครงสร้าง QM ({'Bearish' if direction == 'bearish' else 'Bullish'}): "
        f"QML (Left Shoulder) ที่ {qml_level:.4f}"
        + (f", HH ที่ {hh_level:.4f}" if direction == "bearish" else f", LL ที่ {ll_level:.4f}")
    )

    current_price = df["close"].iloc[-1]

    obs = find_order_blocks(df, config)
    ob = get_nearest_unmitigated_ob(obs, direction, current_price)
    fvgs = find_fvgs(df, config)
    fvg = get_nearest_unfilled_fvg(fvgs, direction, current_price)

    # ใช้ OB/FVG เป็นจุดเข้าที่แม่นขึ้นถ้าซ้อนทับกับระดับ QML (ห่างไม่เกิน ~ATR เล็กน้อย) ไม่งั้นใช้ QML ตรงๆ
    tolerance = config.get("qm_confluence_tolerance", 3.0)
    entry_price = qml_level
    if ob and abs(((ob["top"] + ob["bottom"]) / 2) - qml_level) <= tolerance:
        result["ob"] = ob
        entry_price = (ob["top"] + ob["bottom"]) / 2
        result["reasons"].append(f"มี Order Block ซ้อนทับ QML บริเวณ {ob['bottom']:.4f}-{ob['top']:.4f}")
    elif fvg and abs(((fvg["top"] + fvg["bottom"]) / 2) - qml_level) <= tolerance:
        result["fvg"] = fvg
        entry_price = (fvg["top"] + fvg["bottom"]) / 2
        result["reasons"].append(f"มี FVG ซ้อนทับ QML บริเวณ {fvg['bottom']:.4f}-{fvg['top']:.4f}")

    result["valid"] = True
    result["entry_price"] = entry_price
    return result


def calc_qm_entry_order(entry_signal, config):
    """
    คำนวณ SL/TP ของโอกาสกลุ่ม D:
    - SL: เหนือ HH (ฝั่ง Bearish) หรือใต้ LL (ฝั่ง Bullish) + buffer เล็กน้อย
    - TP: ที่ระดับ LL (ฝั่ง Bearish) หรือ HH (ฝั่ง Bullish) — เป้าหมายคือปลายอีกฝั่งของโครงสร้าง QM เอง
    คืน None ถ้า RR ต่ำกว่า min_rr ที่ตั้งไว้
    """
    from tp import calc_risk_reward

    direction = entry_signal["direction"]
    entry_price = entry_signal["entry_price"]
    buffer = config.get("qm_sl_buffer", 1.0)

    if direction == "bearish":
        stop_loss = entry_signal["hh_level"] + buffer
        take_profit = entry_signal["ll_level"]
    else:
        stop_loss = entry_signal["ll_level"] - buffer
        take_profit = entry_signal["hh_level"]

    rr = calc_risk_reward(entry_price, stop_loss, take_profit)
    if rr < config.get("min_rr", 1.2):
        return None

    return {
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
    }
