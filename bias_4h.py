"""
bias_4h.py
ชั้น Bias เฟรมใหญ่สุด (4H) — อยู่บนสุดของ MTF pipeline
รับผิดชอบ 3 อย่างตามหลัก SMC:
  1. เทรนด์หลักจริงๆ (HH/HL = bullish, LH/LL = bearish) ใช้ analyze_structure เดิม
  2. โซน Premium/Discount ของ swing ล่าสุด (Buy ควรอยู่ Discount, Sell ควรอยู่ Premium)
  3. Liquidity pool ใหญ่ (equal highs/lows) ที่รอถูกกวาด
"""

from trend import analyze_structure
from liquidity import find_liquidity_pools


def analyze_4h_bias(df, config):
    """
    รับ df เฟรม 4H ที่ผ่าน add_indicators() มาแล้ว
    คืนค่า dict:
      trend               : "bullish" / "bearish" / "sideway"
      event               : BOS/CHoCH ล่าสุดบน 4H (ถ้ามี)
      zone                : "premium" / "discount" / "equilibrium" / None
      equilibrium_price   : จุดกึ่งกลาง swing high-low ล่าสุด
      swing_high/low      : ขอบบน-ล่างของ range ที่ใช้คำนวณโซน
      liquidity           : equal highs/lows ใหญ่บน 4H
    """
    structure = analyze_structure(df, config)

    result = {
        "trend": structure["trend"],
        "event": structure["event"],
        "zone": None,
        "equilibrium_price": None,
        "swing_high": None,
        "swing_low": None,
        "liquidity": None,
    }

    swings = structure["last_swings"]
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]

    if highs and lows:
        swing_high = highs[-1]["price"]
        swing_low = lows[-1]["price"]
        if swing_high > swing_low:
            equilibrium = (swing_high + swing_low) / 2
            current_price = df["close"].iloc[-1]

            result["swing_high"] = swing_high
            result["swing_low"] = swing_low
            result["equilibrium_price"] = equilibrium

            if current_price > equilibrium:
                result["zone"] = "premium"
            elif current_price < equilibrium:
                result["zone"] = "discount"
            else:
                result["zone"] = "equilibrium"

    if len(df):
        result["liquidity"] = find_liquidity_pools(df, config)

    return result


def is_bias_aligned(direction, bias_4h, config):
    """
    เช็คว่าสัญญาณ 15M (direction) สอดคล้องกับ Bias 4H หรือไม่
      1. เทรนด์ 4H ต้องไม่สวนทาง (ถ้า 4H เป็น sideway จะไม่กรอง เพราะยังไม่มี bias ชัดเจน)
      2. ถ้าเปิด premium_discount_filter_enabled: Buy ห้ามอยู่โซน Premium, Sell ห้ามอยู่โซน Discount
    คืนค่า (aligned: bool, reason: str|None)
    """
    trend_4h = bias_4h.get("trend")
    if trend_4h not in (None, "sideway") and trend_4h != direction:
        return False, (
            f"เทรนด์ 4H เป็น {trend_4h} แต่สัญญาณ 15M เป็น {direction} (สวนทางกับภาพใหญ่) — ไม่แนะนำเข้า"
        )

    if config.get("premium_discount_filter_enabled", True):
        zone = bias_4h.get("zone")
        if zone == "premium" and direction == "bullish":
            return False, "ราคาปัจจุบันอยู่โซน Premium ของ 4H (แพงเกินไปสำหรับ Buy) — ไม่แนะนำเข้า"
        if zone == "discount" and direction == "bearish":
            return False, "ราคาปัจจุบันอยู่โซน Discount ของ 4H (ถูกเกินไปสำหรับ Sell) — ไม่แนะนำเข้า"

    return True, None
