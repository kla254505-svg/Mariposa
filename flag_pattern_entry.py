"""
flag_pattern_entry.py — กลุ่ม B (เริ่มจาก Flag ก่อน — ดูเหตุผลด้านล่าง): Bullish/Bearish Flag
Continuation Pattern แบบ Set & Forget

กลุ่ม B เดิมมี 20 chart pattern ตามภาพที่ผู้ใช้ส่งมา แต่การตรวจจับ pattern เชิงเรขาคณิตแบบนี้เป็น
สิ่งที่ยากที่สุดในบรรดา 4 กลุ่ม — เสี่ยง false positive สูงมาก (แม้แต่แพลตฟอร์มมืออาชีพที่มีทีมทำ
เรื่องนี้โดยเฉพาะยังทำได้ไม่แม่นเป๊ะ) จึงเริ่มจาก Flag ก่อนเพียงแบบเดียว เพราะมีนิยามชัดเจนที่สุด
(pole แรงๆ + พักตัวแคบๆ + breakout) ส่วนอีก 19 แบบ (Triangle, Wedge, H&S, Cup&Handle ฯลฯ) ต้องมี
ตัวจับเส้นแนวโน้ม (trendline fitting) ที่ซับซ้อนกว่านี้มาก เก็บไว้เป็นงานต่อยอดทีละแบบ

**สำคัญ — Flag ต่างจากกลุ่ม A/C/D ตรงจุดพื้นฐาน:** กลุ่ม A/C/D ทั้งหมดเป็น "Limit order" (รอราคาย่อ
กลับมาที่โซนถูกกว่าปัจจุบัน) แต่ Flag เป็น "Stop order" โดยธรรมชาติ (ต้องรอราคาทะลุกรอบพักตัวไปทาง
เดียวกับ pole ก่อน ถึงจะยืนยันว่า breakout จริง ไม่ใช่แค่หลอกแล้วพักตัวต่อ) — entry_price จึงอยู่
"สูงกว่า" ราคาปัจจุบันสำหรับ Bullish Flag (ต่างจากกลุ่ม A/C/D ที่ entry อยู่ต่ำกว่าเสมอ) ใช้ระบบ
entry_side ใน orders.py ที่แก้ไขให้รองรับ Stop order แล้วโดยเฉพาะสำหรับกลุ่มนี้

ขั้นตอน:
1. หา "Pole" — การเคลื่อนไหวแรงๆ ทางเดียวกันในช่วงก่อนหน้า (แรงพอ = ระยะทางมากกว่า ATR คูณค่าที่ตั้งไว้
   และแท่งเทียนส่วนใหญ่ในช่วงนั้นไปทางเดียวกัน)
2. หา "Flag" — ช่วงพักตัวแคบๆ ทันทีหลัง pole (แคบพอ = ช่วงราคาต้องแคบกว่า pole มาก ไม่ใช่แค่ sideway
   ธรรมดาที่กว้างพอๆ กับ pole เอง ซึ่งนั่นไม่ใช่ flag)
3. Entry ที่ขอบกรอบ Flag ฝั่งเดียวกับทิศทาง pole (Breakout), SL ฝั่งตรงข้ามของกรอบ Flag, TP แบบ
   Measured Move (ระยะเท่ากับความสูงของ pole ต่อจากจุด breakout)
"""


def find_flag_pattern(df, config):
    """
    หา Flag pattern จากแท่งเทียนล่าสุด: [ช่วง pole เก่ากว่า] -> [ช่วง flag ล่าสุดจนถึงตอนนี้]
    คืนค่า dict: valid/direction/entry_price/reasons/flag_upper/flag_lower/pole_move
    """
    pole_bars = config.get("flag_pole_bars", 12)
    flag_bars = config.get("flag_consolidation_bars", 10)
    total_bars = pole_bars + flag_bars

    result = {
        "valid": False,
        "direction": None,
        "entry_price": None,
        "reasons": [],
        "flag_upper": None,
        "flag_lower": None,
        "pole_move": None,
    }

    if len(df) < total_bars + 5:
        result["reasons"].append("ข้อมูลไม่พอสำหรับหา Flag pattern ตอนนี้")
        return result

    recent = df.iloc[-total_bars:]
    pole_section = recent.iloc[:pole_bars]
    flag_section = recent.iloc[pole_bars:]

    pole_move = pole_section["close"].iloc[-1] - pole_section["close"].iloc[0]
    atr = df["atr"].iloc[-1] if "atr" in df.columns and not df["atr"].isna().all() else None
    if not atr or atr <= 0:
        atr = (pole_section["high"].max() - pole_section["low"].min()) / pole_bars

    min_pole_atr_mult = config.get("flag_min_pole_atr_mult", 2.5)
    if abs(pole_move) < atr * min_pole_atr_mult:
        result["reasons"].append("ยังไม่เจอ pole ที่แรงพอในช่วงนี้ (ระยะสั้นกว่าเกณฑ์ที่ตั้งไว้)")
        return result

    direction = "bullish" if pole_move > 0 else "bearish"

    same_dir_count = sum(
        1 for _, row in pole_section.iterrows()
        if (row["close"] > row["open"]) == (direction == "bullish")
    )
    min_ratio = config.get("flag_pole_directional_ratio", 0.6)
    if same_dir_count / pole_bars < min_ratio:
        result["reasons"].append("ช่วง pole มีแท่งเทียนสวนทางเยอะเกินไป ไม่ใช่ pole ที่แข็งแรงจริง")
        return result

    flag_range = flag_section["high"].max() - flag_section["low"].min()
    max_flag_ratio = config.get("flag_max_consolidation_ratio", 0.5)
    if flag_range > abs(pole_move) * max_flag_ratio:
        result["reasons"].append("ช่วงพักตัวหลัง pole กว้างเกินไป ไม่ใช่ flag ที่พักตัวแคบจริง")
        return result

    flag_upper = flag_section["high"].max()
    flag_lower = flag_section["low"].min()
    entry_price = flag_upper if direction == "bullish" else flag_lower

    result["valid"] = True
    result["direction"] = direction
    result["entry_price"] = entry_price
    result["flag_upper"] = flag_upper
    result["flag_lower"] = flag_lower
    result["pole_move"] = pole_move
    direction_th = "Bullish" if direction == "bullish" else "Bearish"
    result["reasons"].append(
        f"เจอ {direction_th} Flag: pole {abs(pole_move):.4f} + พักตัวแคบในกรอบ "
        f"{flag_lower:.4f}-{flag_upper:.4f} — รอราคาทะลุกรอบไปทาง{'บน' if direction == 'bullish' else 'ล่าง'}"
    )
    return result


def calc_flag_entry_order(entry_signal, config):
    """
    คำนวณ SL/TP ของโอกาส Flag:
    - SL: ฝั่งตรงข้ามของกรอบ Flag (กันโดนสะบัดกลับเข้ากรอบก่อนไปต่อจริง) + buffer เล็กน้อย
    - TP: Measured Move — ระยะเท่ากับความสูงของ pole ต่อจากจุด breakout (แนวคิดคลาสสิกของ Flag)
    คืน None ถ้า RR ต่ำกว่า min_rr ที่ตั้งไว้
    """
    from tp import calc_risk_reward

    direction = entry_signal["direction"]
    entry_price = entry_signal["entry_price"]
    pole_move = entry_signal["pole_move"]
    buffer = config.get("flag_sl_buffer", 1.0)

    if direction == "bullish":
        stop_loss = entry_signal["flag_lower"] - buffer
        take_profit = entry_price + abs(pole_move)
    else:
        stop_loss = entry_signal["flag_upper"] + buffer
        take_profit = entry_price - abs(pole_move)

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
