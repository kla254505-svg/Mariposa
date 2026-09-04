"""
session_range_entry.py — "Plan 9 (Session Range Breakout)" ผู้สมัคร — ยังไม่เปิดใช้งานจริง

แนวคิด: มาร์คกรอบสูง-ต่ำของช่วง Asian Session (00:00-07:00 UTC ตามที่ตกลงกันไว้) แล้วรอราคาทะลุกรอบนั้น
อย่างมีนัยสำคัญ (ปิดเลยกรอบไปอย่างน้อย breakout_confirm_atr_mult * ATR — เกณฑ์เดียวกับที่แผนที่ 2 ใช้
ยืนยัน Breakout กันสัญญาณหลอกจากแค่ไส้เทียนแตะกรอบ) คนละมุมกับแผนที่ 2 เดิม (อิง swing high/low แบบ
Structure) เพราะอันนี้อิง "ช่วงเวลา" (session) ล้วนๆ — ทองมักมีจังหวะ breakout จาก Asian range ตอน
London เปิด (07:00 UTC เป็นต้นไป ตรงกับ killzones_utc เดิมในระบบพอดี) ค่อนข้างสม่ำเสมอ

*** สถานะตอนนี้: เขียนไว้ก่อน ยังไม่เปิดใช้งานจริง ***
ไม่ถูกเรียกจาก main.py / plan_runner.py / telegram_bot.py เลยแม้แต่จุดเดียว เป็นแค่โมดูล standalone ที่
ทดสอบเองได้ (ดู test_session_range_entry.py) รอผลวิเคราะห์ 8 แผนเดิมก่อน

การเปิดใช้งานจริงในอนาคตจะต้องทำเพิ่ม (ยังไม่ได้ทำในรอบนี้ ตามที่ตกลงกันไว้):
  1. เพิ่มการเรียก find_session_range() -> detect_range_breakout() -> calc_session_range_order() เข้า
     plan_runner.py ทำนองเดียวกับ Plan 5-8 (ดู check_zone_entry_trigger เป็นตัวอย่างโครงสร้าง) พร้อม
     state-based dedup ผ่าน kvdb แบบเดียวกับแผนอื่น (แผนนี้ยังไม่มี dedup guard ใดๆ เลยตอนนี้)
  2. เพิ่ม "plan9_session_range" เข้า orders.py: PLAN_LABEL / PLAN_SHORT
  3. เพิ่มเข้า alert_dispatcher.py จุดที่ telegram_bot.py:_check_all_plans ไล่เช็คทีละแผนสำหรับ /order
"""

from datetime import datetime, timezone, timedelta

ASIAN_RANGE_START_HOUR_UTC = 0
ASIAN_RANGE_END_HOUR_UTC = 7
PLAN_KEY = "plan9_session_range"


def find_session_range(df, now=None):
    """คืน dict {range_high, range_low, range_start, range_end, is_complete} จากช่วง Asian Session
    (00:00-07:00 UTC) ที่ "เพิ่งจบล่าสุด" — ถ้าตอนนี้ยังอยู่ในช่วง 00:00-07:00 UTC ของวันนี้เอง ให้ใช้
    ของเมื่อวานแทน (กันเอา range ที่ยังไม่จบมาใช้ ซึ่งยังไม่ใช่กรอบที่แท้จริง — ราคาอาจวิ่งต่อได้อีก)

    df: ต้อง index เป็น datetime (UTC) — fetch_twelvedata คืนคอลัมน์ "datetime" มาให้ ต้อง
    pd.to_datetime(...) แล้ว set_index ก่อนส่งเข้าฟังก์ชันนี้ (ดู _prepare_df_index ใน
    test_session_range_entry.py เป็นตัวอย่าง)

    คืน None ถ้าไม่มีแท่งอยู่ในช่วงที่ต้องการเลย (ข้อมูลไม่พอ เช่น outputsize สั้นเกินไปจนย้อนไม่ถึง)"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    today_start = now.replace(hour=ASIAN_RANGE_START_HOUR_UTC, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=ASIAN_RANGE_END_HOUR_UTC, minute=0, second=0, microsecond=0)

    if now < today_end:
        range_start = today_start - timedelta(days=1)
        range_end = today_end - timedelta(days=1)
    else:
        range_start = today_start
        range_end = today_end

    window = df[(df.index >= range_start) & (df.index < range_end)]
    if window.empty:
        return None

    return {
        "range_high": float(window["high"].max()),
        "range_low": float(window["low"].min()),
        "range_start": range_start,
        "range_end": range_end,
        "is_complete": now >= range_end,
    }


def detect_range_breakout(df, session_range, config):
    """เช็คว่าราคาแท่งล่าสุดทะลุกรอบ Asian Range อย่างมีนัยสำคัญไหม (ปิดเลยกรอบไป >=
    breakout_confirm_atr_mult * ATR — เกณฑ์เดียวกับแผนที่ 2 กันสัญญาณหลอกจากแค่ไส้เทียนแตะกรอบเฉยๆ)

    คืน dict {direction, close, breakout_level} หรือ None ถ้ายังไม่ทะลุกรอบอย่างมีนัยสำคัญ (หรือ
    session_range ยังไม่ complete / เป็น None)"""
    if not session_range or not session_range.get("is_complete"):
        return None

    last_row = df.iloc[-1]
    last_close = float(last_row["close"])
    atr_val = float(last_row["atr"]) if "atr" in df.columns else 0.0
    confirm_buffer = config.get("breakout_confirm_atr_mult", 0.3) * atr_val

    range_high = session_range["range_high"]
    range_low = session_range["range_low"]

    if last_close > range_high + confirm_buffer:
        return {"direction": "bullish", "close": last_close, "breakout_level": range_high}
    if last_close < range_low - confirm_buffer:
        return {"direction": "bearish", "close": last_close, "breakout_level": range_low}
    return None


def calc_session_range_order(df, session_range, breakout, config):
    """คำนวณ Entry/SL/TP สำหรับ Session Range Breakout

    Entry = ราคาปิดแท่งที่ยืนยัน breakout (เข้าตลาดทันที ไม่ใช่ pending order รอ retest แบบแผน 5-8)
    SL = ฝั่งตรงข้ามของกรอบ Asian Range (buffer ด้วย sl_buffer_atr + min_sl_distance เหมือนแผนอื่นๆ
    ทุกแผน — เอาความกว้างทั้งกรอบ session มาเป็นระยะเสี่ยง ซึ่งมักกว้างกว่า SL ของแผนอื่นพอสมควร เพราะ
    Asian range บางวันแคบบางวันกว้างไม่แน่นอน)
    TP = ใช้ tp.py มาตรฐานเดียวกับทุกแผน (tp1_rr/tp2_rr/tp3_rr จาก config)"""
    from tp import calc_take_profits, calc_risk_reward

    direction = breakout["direction"]
    entry_price = breakout["close"]
    last_row = df.iloc[-1]
    atr_val = float(last_row["atr"]) if "atr" in df.columns else 0.0
    buffer = config.get("sl_buffer_atr", 0.25) * atr_val

    if direction == "bullish":
        stop_loss = session_range["range_low"] - buffer
    else:
        stop_loss = session_range["range_high"] + buffer

    min_distance = config.get("min_sl_distance", 0)
    current_distance = abs(entry_price - stop_loss)
    if min_distance and current_distance < min_distance:
        stop_loss = (entry_price - min_distance) if direction == "bullish" else (entry_price + min_distance)

    take_profits = calc_take_profits(entry_price, stop_loss, direction, config)
    rr_tp1 = calc_risk_reward(entry_price, stop_loss, take_profits.get("TP1"))

    return {
        "plan": PLAN_KEY,
        "direction": direction,
        "entry_price": round(entry_price, 3),
        "stop_loss": round(stop_loss, 3),
        "take_profits": {k: round(v, 3) for k, v in take_profits.items()},
        "rr_tp1": rr_tp1,
        "range_high": round(session_range["range_high"], 3),
        "range_low": round(session_range["range_low"], 3),
    }


def score_session_range_order(order, bias_4h, structure, config):
    """ให้คะแนนแบบเดียวกับแผนที่ 2-8 (ใช้ plan_score.py:generic_plan_score ตัวเดิม ไม่สร้างสูตรใหม่
    แยกต่างหาก เพื่อให้เทียบคะแนนข้ามแผนได้แบบเดียวกับที่ /order ทำอยู่แล้วตอนนี้)"""
    from plan_score import generic_plan_score

    return generic_plan_score(order["direction"], order.get("rr_tp1"), bias_4h, structure, config)
