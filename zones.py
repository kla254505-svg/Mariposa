from indicator import ema


def calc_premium_discount_zone(df, lookback):
    """
    หาโซน Premium/Discount จาก high/low ย้อนหลัง `lookback` แท่ง
    - Equilibrium (จุดกึ่งกลาง) = (high สูงสุด + low ต่ำสุด) / 2
    - ราคาอยู่เหนือ Equilibrium = Premium (แพง เหมาะกับการมองหา Sell)
    - ราคาอยู่ใต้ Equilibrium = Discount (ถูก เหมาะกับการมองหา Buy)
    """
    sub = df.iloc[-lookback:] if len(df) > lookback else df
    zone_high = sub["high"].max()
    zone_low = sub["low"].min()
    equilibrium = (zone_high + zone_low) / 2.0
    current_price = df["close"].iloc[-1]

    if zone_high == zone_low:
        position_pct = 50.0
    else:
        position_pct = (current_price - zone_low) / (zone_high - zone_low) * 100.0

    zone = "premium" if current_price > equilibrium else "discount"

    return {
        "zone_high": round(float(zone_high), 3),
        "zone_low": round(float(zone_low), 3),
        "equilibrium": round(float(equilibrium), 3),
        "current_price": round(float(current_price), 3),
        "position_pct": round(float(position_pct), 1),
        "zone": zone,
    }


def check_bias_pd_confirm(direction, pd_zone):
    """
    ยืนยันว่าสัญญาณเกิดในโซนที่ถูกต้องตามหลัก SMC
    - Buy (bullish) ควรอยู่โซน Discount เท่านั้น
    - Sell (bearish) ควรอยู่โซน Premium เท่านั้น
    """
    if direction == "bullish":
        return pd_zone["zone"] == "discount"
    return pd_zone["zone"] == "premium"


def calc_daily_bias(df_htf, config):
    """
    หา Bias หลักจากกรอบเวลาใหญ่ (แนะนำ 4H) โดยใช้ EMA fast/slow
    ตรรกะเดียวกับ trend engine หลัก แต่ใช้เป็นชั้น bias บนสุด เหนือกว่า 1H
    คืนค่า "bullish" / "bearish" / "neutral"
    """
    if len(df_htf) < config["ema_slow"]:
        return "neutral"

    e_fast = ema(df_htf["close"], config["ema_fast"]).iloc[-1]
    e_slow = ema(df_htf["close"], config["ema_slow"]).iloc[-1]
    last_close = df_htf["close"].iloc[-1]

    if last_close > e_fast > e_slow:
        return "bullish"
    if last_close < e_fast < e_slow:
        return "bearish"
    return "neutral"
