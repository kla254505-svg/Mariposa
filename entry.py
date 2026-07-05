from orderblock import find_order_blocks, get_nearest_unmitigated_ob
from fvg import find_fvgs, get_nearest_unfilled_fvg
from liquidity import find_liquidity_pools
from fibo import calc_fib_levels, is_in_ote_zone


def evaluate_entry(df, structure, config):
    current_price = df["close"].iloc[-1]
    trend = structure["trend"]
    event = structure["event"]

    result = {
        "valid": False, "direction": None, "entry_price": None,
        "reasons": [], "ob": None, "fvg": None, "liquidity": None, "fib_levels": None,
    }

    if trend == "sideway" or event is None:
        result["reasons"].append("ตลาดยังไม่มีโครงสร้างชัดเจน (sideway หรือไม่มี BOS/CHoCH) — ข้าม")
        return result

    direction = "bullish" if trend == "bullish" else "bearish"
    result["direction"] = direction
    result["reasons"].append(f"โครงสร้างตลาดเป็น {trend} และเพิ่งเกิด {event} ยืนยันทิศทาง")

    obs = find_order_blocks(df, config)
    ob = get_nearest_unmitigated_ob(obs, direction, current_price)
    if ob:
        result["ob"] = ob
        result["reasons"].append(f"พบ {direction} Order Block ที่ยังไม่ถูกแตะ บริเวณ {ob['bottom']:.4f}-{ob['top']:.4f}")

    fvgs = find_fvgs(df, config)
    fvg = get_nearest_unfilled_fvg(fvgs, direction, current_price)
    if fvg:
        result["fvg"] = fvg
        result["reasons"].append(f"พบ {direction} FVG ที่ยังไม่ถูกเติมเต็ม บริเวณ {fvg['bottom']:.4f}-{fvg['top']:.4f}")

    liquidity = find_liquidity_pools(df, config)
    result["liquidity"] = liquidity
    if direction == "bullish" and liquidity["equal_lows"]:
        result["reasons"].append("มี Equal Lows (liquidity) ด้านล่างที่อาจถูกกวาดมาแล้วก่อนกลับตัวขึ้น")
    if direction == "bearish" and liquidity["equal_highs"]:
        result["reasons"].append("มี Equal Highs (liquidity) ด้านบนที่อาจถูกกวาดมาแล้วก่อนกลับตัวลง")

    swings = structure["last_swings"]
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]
    if highs and lows:
        swing_high_price = highs[-1]["price"]
        swing_low_price = lows[-1]["price"]
        fib_levels = calc_fib_levels(swing_low_price, swing_high_price, direction, config)
        result["fib_levels"] = fib_levels
        if is_in_ote_zone(current_price, fib_levels):
            result["reasons"].append("ราคาปัจจุบันอยู่ในโซน OTE (Fib 0.618-0.79) ซึ่งเป็นโซน entry ที่ดี")

    if ob or fvg:
        result["valid"] = True
        zone_edges = []
        if ob:
            zone_edges.append(ob["top"] if direction == "bullish" else ob["bottom"])
        if fvg:
            zone_edges.append(fvg["top"] if direction == "bullish" else fvg["bottom"])
        result["entry_price"] = sum(zone_edges) / len(zone_edges)
    else:
        result["reasons"].append("ไม่พบ Order Block หรือ FVG รองรับ — ยังไม่แนะนำเข้า")

    return result
