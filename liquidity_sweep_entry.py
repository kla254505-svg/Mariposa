"""
liquidity_sweep_entry.py — กลุ่ม C: Liquidity Sweep + Displacement Entry (Set & Forget)

รวม 2 รูปแบบที่ผู้ใช้ส่งมา (Liquidity Sweep ทั่วไป และ AMD Model ที่จำกัดกรอบเฉพาะ Asia session)
ที่จริงเป็น engine เดียวกัน ต่างกันแค่ "อะไรกำหนดกรอบ liquidity ที่จะเฝ้าดู"

รอบนี้ implement เฉพาะเวอร์ชันทั่วไป (Equal Highs/Lows จาก swing point — ใช้ liquidity.py ที่มีอยู่
แล้วทั้งดุ้น ไม่ต้องเขียนใหม่เลย) ส่วนเวอร์ชัน AMD (จำกัดกรอบเฉพาะ Asia session) ต้องมีตัวคำนวณ
Asia session range ก่อน (ยังไม่มีในโค้ดเดิม — session.py มีแค่เช็คว่าตอนนี้อยู่ session ไหน ไม่ได้
คำนวณ high/low ของ session ที่ผ่านมา) เก็บไว้เป็นงานต่อยอดในตระกูลเดียวกัน (คนละ plan_key เช่น
plan6_sweep_general / plan6_sweep_asia แต่ engine ข้างในเหมือนกัน)

ขั้นตอน:
1. หา Liquidity Pool (Equal Highs/Lows) — find_liquidity_pools() (มีอยู่แล้ว)
2. เช็คว่ามีการ "กวาดแล้วกลับตัว" จริงไหม (ไส้ทะลุระดับ + ปิดกลับเข้ามา) — detect_liquidity_sweep()
   (มีอยู่แล้ว)
3. ยืนยัน Displacement — ต้องเจอ FVG ที่เกิด "หลัง" จุดกวาด ไปทางเดียวกับทิศทางกลับตัวเท่านั้น
   (กัน false positive จาก FVG เก่าที่ไม่เกี่ยวกับการกวาดครั้งนี้)
4. Entry ที่กึ่งกลาง FVG นั้น, SL ใต้/เหนือไส้เทียนที่กวาดจริง (ไม่ใช่แค่ระดับ Equal High/Low เฉยๆ —
   ไส้มักยื่นเลยระดับไปอีกหน่อย ถ้าวาง SL ที่ระดับพอดีเสี่ยงโดนสะบัดซ้ำ), TP ที่ liquidity pool ฝั่ง
   ตรงข้าม (ตาม concept SMC ว่าราคามักวิ่งไปกวาดสภาพคล่องอีกฝั่งต่อ) หรือ fallback เป็นปลาย swing 4H
"""
from fvg import find_fvgs, get_nearest_unfilled_fvg
from liquidity import find_liquidity_pools, detect_liquidity_sweep


def find_sweep_entry(df, bias_4h, config):
    """
    หาโอกาสเข้าไม้กลุ่ม C: กวาด Liquidity แล้วกลับตัว + มี FVG ยืนยัน (Displacement)

    เช็คทั้ง 2 ทิศทาง ไม่ผูกกับเทรนด์ 4H เป็นพิเศษ (ต่างจากกลุ่ม A) เพราะโดยธรรมชาติของกลยุทธ์นี้คือ
    "หาจุดกลับตัวจากการกวาดสภาพคล่อง" ซึ่งอาจเป็นได้ทั้งการกลับตัวตามเทรนด์ใหญ่ (pullback ที่กวาด
    equal low ก่อนไปต่อ) หรือกลับตัวสวนเทรนด์ใหญ่ (reversal จริง) ก็ได้ — bias_4h ยังเก็บไว้ใน
    ผลลัพธ์เผื่อใช้หา TP fallback เท่านั้น ไม่ได้ใช้กรองทิศทางเหมือนกลุ่ม A

    คืนค่า dict: valid/direction/entry_price/reasons/fvg/sweep/pools/bias_4h
    """
    pools = find_liquidity_pools(df, config)

    result = {
        "valid": False,
        "direction": None,
        "entry_price": None,
        "reasons": [],
        "fvg": None,
        "sweep": None,
        "pools": pools,
        "bias_4h": bias_4h,
    }

    lookback = config.get("liquidity_sweep_lookback", 10)

    for direction in ("bullish", "bearish"):
        sweep = detect_liquidity_sweep(df, pools, direction, lookback=lookback)
        if not sweep:
            continue

        fvgs = find_fvgs(df, config)
        sweep_idx = sweep["index"]
        # เอาเฉพาะ FVG ที่เกิด "หลัง" จุดกวาด และทิศทางตรงกับการกลับตัว (ยืนยัน Displacement จริง
        # ไม่ใช่ FVG เก่าที่เกิดก่อนหน้าซึ่งไม่เกี่ยวข้องกับการกวาดครั้งนี้เลย)
        fvgs_after_sweep = [f for f in fvgs if f["index"] > sweep_idx and f["type"] == direction]
        if not fvgs_after_sweep:
            continue

        current_price = df["close"].iloc[-1]
        fvg = get_nearest_unfilled_fvg(fvgs_after_sweep, direction, current_price)
        if not fvg:
            continue

        result["valid"] = True
        result["direction"] = direction
        result["fvg"] = fvg
        result["sweep"] = sweep
        result["entry_price"] = (fvg["top"] + fvg["bottom"]) / 2
        result["reasons"].append(
            f"กวาด Liquidity ที่ {sweep['level']:.4f} แล้วกลับตัว {direction} + เจอ FVG ยืนยัน "
            f"Displacement บริเวณ {fvg['bottom']:.4f}-{fvg['top']:.4f}"
        )
        break  # เจอทิศทางแรกที่ผ่านครบทุกเงื่อนไขแล้ว พอ (ปกติไม่ควรเจอสองทิศพร้อมกันในทางปฏิบัติ)

    if not result["valid"]:
        result["reasons"].append("ยังไม่เจอการกวาด Liquidity + Displacement ที่ชัดเจนพอตอนนี้")

    return result


def calc_sweep_entry_order(entry_signal, df, config):
    """
    คำนวณ SL/TP ของโอกาสกลุ่ม C:
    - SL: ใต้/เหนือไส้เทียนที่เกิดการกวาดจริง (ดึงราคาจาก df ตรงๆ ไม่ใช่แค่ระดับ Equal High/Low
      ที่ detect_liquidity_sweep() คืนมา เพราะไส้มักยื่นเลยระดับไปอีกหน่อย) + buffer เล็กน้อย กัน
      ราคาแกว่งกลับไปแตะไส้เดิมซ้ำแล้วโดน stop ทั้งที่ยังเป็นโอกาสเดิมอยู่
    - TP: liquidity pool ฝั่งตรงข้าม (ถ้ามี — ตาม concept SMC ว่าราคามักวิ่งไปกวาดสภาพคล่องอีกฝั่ง
      ต่อ) หรือ fallback เป็นปลาย swing 4H ถ้าไม่มี pool ฝั่งตรงข้ามที่ใช้ได้
    คืน None ถ้าหา TP ไม่ได้เลย หรือ RR ต่ำกว่า min_rr ที่ตั้งไว้
    """
    from tp import calc_risk_reward

    direction = entry_signal["direction"]
    sweep = entry_signal["sweep"]
    entry_price = entry_signal["entry_price"]
    pools = entry_signal["pools"]
    bias_4h = entry_signal["bias_4h"]

    sweep_candle = df.loc[sweep["index"]]
    buffer = config.get("sweep_sl_buffer", 1.0)
    if direction == "bullish":
        stop_loss = sweep_candle["low"] - buffer
    else:
        stop_loss = sweep_candle["high"] + buffer

    opposite_pools = pools.get("equal_highs" if direction == "bullish" else "equal_lows", [])
    candidate_tps = [
        p for p in opposite_pools
        if (p > entry_price if direction == "bullish" else p < entry_price)
    ]
    if candidate_tps:
        take_profit = min(candidate_tps) if direction == "bullish" else max(candidate_tps)
    else:
        take_profit = bias_4h.get("swing_high") if direction == "bullish" else bias_4h.get("swing_low")

    if take_profit is None:
        return None

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
