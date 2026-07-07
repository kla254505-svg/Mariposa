
from fibo import calc_fib_levels, is_in_ote_zone
from fvg import find_fvgs, get_nearest_unfilled_fvg
from liquidity import find_liquidity_pools
from orderblock import find_order_blocks, get_nearest_unmitigated_ob
from structure_entry import find_structure_entry


def evaluate_entry(df, structure, config):
    current_price = df["close"].iloc[-1]
    trend = structure["trend"]
    event = structure["event"]

    # 1. เพิ่ม "structure_zone": None ตรงนี้ตามที่ต้องการได้เลยครับ
    result = {
        "valid": False,
        "direction": None,
        "entry_price": None,
        "reasons": [],
        "ob": None,
        "fvg": None,
        "structure_zone": None,  # เพิ่มเข้ามาตรงนี้แล้ว
        "liquidity": None,
        "liquidity_sweep": None,
        "fib_levels": None,
    }

    if trend == "sideway":
        result["reasons"].append(
            "ตลาดยังไม่มีโครงสร้างชัดเจน (sideway) — ข้าม"
        )
        return result

    direction = "bullish" if trend == "bullish" else "bearish"
    result["direction"] = direction
    if event:
        result["reasons"].append(
            f"โครงสร้างตลาดเป็น {trend} และเพิ่งเกิด {event} ยืนยันทิศทาง"
        )
    else:
        result["reasons"].append(
            f"โครงสร้างตลาดเป็น {trend} ต่อเนื่อง (ยังไม่มี BOS/CHoCH ใหม่ในรอบนี้)"
        )

    obs = find_order_blocks(df, config)
    ob = get_nearest_unmitigated_ob(obs, direction, current_price)
    if ob:
        result["ob"] = ob
        result["reasons"].append(
            f"พบ {direction} Order Block ที่ยังไม่ถูกแตะ บริเวณ {ob['bottom']:.4f}-{ob['top']:.4f}"
        )

    fvgs = find_fvgs(df, config)
    fvg = get_nearest_unfilled_fvg(fvgs, direction, current_price)
    if fvg:
        result["fvg"] = fvg
        result["reasons"].append(
            f"พบ {direction} FVG ที่ยังไม่ถูกเติมเต็ม บริเวณ {fvg['bottom']:.4f}-{fvg['top']:.4f}"
        )

    liquidity = find_liquidity_pools(df, config)
    result["liquidity"] = liquidity

    from liquidity import detect_liquidity_sweep
    sweep = detect_liquidity_sweep(df, liquidity, direction, lookback=config.get("liquidity_sweep_lookback", 10))
    result["liquidity_sweep"] = sweep
    if sweep:
        result["reasons"].append(
            f"เจอการกวาด Liquidity ที่ระดับ {sweep['level']:.4f} แล้วราคาปิดกลับตัวตามทิศทาง {direction} "
            f"(ยืนยันแรงกวาดสภาพคล่องจริง ไม่ใช่แค่มีแนวอยู่ใกล้ๆ)"
        )

    swings = structure["last_swings"]
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]
    if highs and lows:
        swing_high_price = highs[-1]["price"]
        swing_low_price = lows[-1]["price"]
        fib_levels = calc_fib_levels(
            swing_low_price, swing_high_price, direction, config
        )
        result["fib_levels"] = fib_levels
        if is_in_ote_zone(current_price, fib_levels):
            result["reasons"].append(
                "ราคาปัจจุบันอยู่ในโซน OTE (Fib 0.618-0.79) ซึ่งเป็นโซน entry ที่ดี"
            )

    # 2. ย้ายการดึงค่า structure_zone มาไว้ตรงนี้ (ย้ายออกจากบล็อก if ของ fib ด้านบน)
    # เพื่อให้เงื่อนไข if ข้างล่างสามารถเรียกใช้ตัวแปรนี้ได้เสมอ
    structure_zone = None
    if not ob and not fvg:
        structure_zone = find_structure_entry(df, structure, config)
        if structure_zone:
            result["structure_zone"] = structure_zone
            result["reasons"].append(
                f"ไม่เจอ OB/FVG แต่ราคาอยู่ใกล้ {structure_zone['label']} "
                f"บริเวณ {structure_zone['bottom']:.4f}-{structure_zone['top']:.4f}"
            )

    if ob or fvg or structure_zone:
        result["valid"] = True
        zone_edges = []
        if ob:
            zone_edges.append(ob["top"] if direction == "bullish" else ob["bottom"])
        if fvg:
            zone_edges.append(fvg["top"] if direction == "bullish" else fvg["bottom"])
        if structure_zone:
            zone_edges.append(
                structure_zone["top"]
                if direction == "bullish"
                else structure_zone["bottom"]
            )
        result["entry_price"] = sum(zone_edges) / len(zone_edges)
    else:
        result["reasons"].append(
            "ไม่พบ Order Block, FVG หรือจุด Pullback ตามโครงสร้าง — ยังไม่แนะนำเข้า"
        )

    return result
