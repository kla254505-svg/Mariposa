"""
zone_entry.py — กลุ่ม A: Set & Forget SMC Zone Entry

รวม 3 รูปแบบที่ผู้ใช้ส่งมา (4H Bias -> Premium/Discount -> Confluence -> Limit Order) ซึ่งจริงๆ
เป็น engine เดียวกันเป๊ะ ต่างกันแค่วิธีแบ่งไม้เข้า (เข้าทีเดียว/สองทาง/แบ่ง 3 ก้อนตาม Fib — ส่วนแบ่งไม้
ปล่อยให้ผู้ใช้ตัดสินใจเองตอนวาง Limit จริง ไม่ได้ผูกไว้ในโค้ด)

ต่างจากแผนที่ 1 (entry.py) 2 จุดสำคัญ:
  1. ใช้ 4H bias (บริบทภาพใหญ่) เป็นตัวกำหนดทิศทาง ไม่ใช่ 15M structure trend
  2. เป็น "Set & Forget" — แจ้งเตือนทันทีที่เจอ zone/confluence โดยไม่ต้องรอราคาแตะ + มี reaction
     ยืนยันด้วยแท่งเทียนก่อนแบบแผนที่ 1 — บันทึกเป็นสถานะ 'pending' (ผ่าน add_pending_order() ใน
     orders.py) แทน 'running' ทันที เพราะราคาอาจยังไม่เดินทางมาถึงจริง

ใช้ building block เดิมที่มีอยู่แล้วทั้งหมด (find_order_blocks, find_fvgs, find_structure_entry,
analyze_4h_bias, risk.calc_stop_loss) — ไม่ได้เขียน detection ใหม่จากศูนย์ แค่ประกอบสูตรใหม่
"""
from fvg import find_fvgs, get_nearest_unfilled_fvg
from orderblock import find_order_blocks, get_nearest_unmitigated_ob


def find_zone_entry(bias_4h, df_entry_tf, config):
    """
    หาโอกาสเข้าไม้กลุ่ม A แบบ Set & Forget:
    1. เช็ค 4H bias (เทรนด์ + โซน Premium/Discount) — รับ bias_4h ที่คำนวณไว้แล้วโดยตรง (จาก
       bias_4h.analyze_4h_bias()) ไม่คำนวณเองในนี้ เพราะ telegram_bot.py มี cache ของ 4H context
       อยู่แล้ว (ยืมจาก kvdb ที่ main.py cache ไว้) — เรียกซ้ำจะเสีย TwelveData quota ฟรีๆ
    2. ถ้า 4H ยัง sideway ไม่มีโอกาส ข้าม
    3. ราคาปัจจุบันต้องอยู่โซนที่ถูกต้องตามหลัก SMC (Buy ต้องอยู่ Discount, Sell ต้องอยู่ Premium)
       ถ้ายัง ไม่ใช่จังหวะ ต้องรอราคาย่อกลับมาก่อน
    4. หา Confluence (OB/FVG) บน timeframe เข้าไม้ (15M/30M) ตามทิศทางจาก 4H bias — reuse ฟังก์ชัน
       เดิมเป๊ะกับที่แผนที่ 1 ใช้ แค่ direction มาจาก 4H ไม่ใช่ 15M

    หมายเหตุ: ตั้งใจไม่ fallback ไป find_structure_entry() แบบแผนที่ 1 เพราะฟังก์ชันนั้นเช็คทิศทาง
    จาก structure trend ของ timeframe เข้าไม้เอง (ไม่รับ direction จากภายนอก) — ตอน pullback จริง
    โครงสร้างระยะสั้นมักจะดู "สวนทาง" กับเทรนด์ใหญ่ชั่วคราว (เพราะมันคือการย่อ) ถ้าเอามาใช้จะกรอง
    ทิ้งเคสที่ต้องการที่สุดออกไปเอง — กลุ่ม A จึงใช้แค่ OB/FVG (ทั้งคู่รับ direction จากภายนอกได้ตรงๆ
    ไม่ผูกกับ trend ของ timeframe เข้าไม้เอง) ถ้าไม่เจอทั้งคู่ ถือว่ายังไม่มี zone ที่ชัดเจนพอ

    คืนค่า dict รูปแบบเดียวกับ entry.evaluate_entry() (valid/direction/entry_price/reasons/ob/fvg)
    บวก 'bias_4h' เก็บบริบท 4H ไว้ให้ calc_zone_entry_order() ใช้หา TP ต่อ
    """
    result = {
        "valid": False,
        "direction": None,
        "entry_price": None,
        "reasons": [],
        "ob": None,
        "fvg": None,
        "bias_4h": bias_4h,
    }

    trend_4h = bias_4h["trend"]
    if trend_4h not in ("bullish", "bearish"):
        result["reasons"].append("4H ยังไม่มีเทรนด์ชัดเจน (sideway) — ยังไม่มีโอกาสเข้ากลุ่ม A ตอนนี้")
        return result

    direction = trend_4h
    result["direction"] = direction

    zone = bias_4h["zone"]
    expected_zone = "discount" if direction == "bullish" else "premium"
    if zone != expected_zone:
        result["reasons"].append(
            f"4H เทรนด์ {direction} แต่ราคาตอนนี้อยู่โซน {zone or 'ไม่ทราบ'} "
            f"(ต้องรอราคาย่อกลับมาโซน {expected_zone} ก่อนถึงจะมีจุดวาง Limit)"
        )
        return result

    result["reasons"].append(
        f"4H bias {direction} + ราคาอยู่ในโซน {expected_zone} ตามหลัก SMC (สอดคล้องกับภาพใหญ่)"
    )

    current_price = df_entry_tf["close"].iloc[-1]

    obs = find_order_blocks(df_entry_tf, config)
    ob = get_nearest_unmitigated_ob(obs, direction, current_price)
    if ob:
        result["ob"] = ob
        result["reasons"].append(f"พบ {direction} Order Block บริเวณ {ob['bottom']:.4f}-{ob['top']:.4f}")

    fvgs = find_fvgs(df_entry_tf, config)
    fvg = get_nearest_unfilled_fvg(fvgs, direction, current_price)
    if fvg:
        result["fvg"] = fvg
        result["reasons"].append(f"พบ {direction} FVG บริเวณ {fvg['bottom']:.4f}-{fvg['top']:.4f}")

    if ob or fvg:
        result["valid"] = True
        zone_edges = []
        if ob:
            zone_edges.append(ob["top"] if direction == "bullish" else ob["bottom"])
        if fvg:
            zone_edges.append(fvg["top"] if direction == "bullish" else fvg["bottom"])
        result["entry_price"] = sum(zone_edges) / len(zone_edges)
    else:
        result["reasons"].append(
            "อยู่ในโซนที่ถูกต้องแล้ว แต่ยังไม่เจอ OB/FVG ที่ชัดเจนพอให้วาง Limit"
        )

    return result


def calc_zone_entry_order(entry_signal, df_entry_tf, config):
    """
    คำนวณ SL/TP ของโอกาสกลุ่ม A:
    - SL: ใช้สูตรเดียวกับแผนที่ 1 (risk.calc_stop_loss) เพราะ entry_signal มีรูปแบบ ob/fvg/
      structure_zone เหมือนกันเป๊ะ (ขยาย OB/FVG width + ATR buffer + min_sl_distance floor)
    - TP: ใช้ปลายอีกฝั่งของ swing 4H (bias_4h.swing_high สำหรับ Buy / swing_low สำหรับ Sell)
      แทนการใช้ RR คงที่แบบแผนที่ 1 — เพราะเป้าหมายจริงตามภาพคือ "ขอบตรงข้ามของ range" ที่มาจาก
      โครงสร้างจริง ไม่ใช่สัดส่วนคงที่ที่อาจไม่ตรงกับแนวรับ-ต้านจริงเลย (จุดที่เคยติงไว้เรื่องแผนที่ 1)
    คืน None ถ้าหา TP จากโครงสร้างไม่ได้ หรือ RR ที่ได้ต่ำกว่า min_rr ที่ตั้งไว้ (ไม่คุ้มเสี่ยง)
    """
    from risk import calc_stop_loss
    from tp import calc_risk_reward

    direction = entry_signal["direction"]
    bias_4h = entry_signal["bias_4h"]

    take_profit = bias_4h.get("swing_high") if direction == "bullish" else bias_4h.get("swing_low")
    if take_profit is None:
        return None

    atr_period = config.get("sl_atr_avg_period", 20)
    current_atr = (
        df_entry_tf["atr"].tail(atr_period).mean()
        if "atr" in df_entry_tf.columns and len(df_entry_tf)
        else 0
    )
    stop_loss = calc_stop_loss(entry_signal, current_atr, config)
    entry_price = entry_signal["entry_price"]

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
