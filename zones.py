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


def find_opposing_zone_in_path(df, config, direction, entry_price, target_price):
    """
    เช็คว่าระหว่าง entry_price กับ target_price (TP) มี "กำแพง" ของฝั่งตรงข้ามขวางอยู่ไหม — คือ Order
    Block/FVG ของฝั่งตรงข้ามที่ยังไม่ถูกแตะ (unmitigated/unfilled) หรือ Liquidity Pool (Equal High/Low)
    ของฝั่งตรงข้าม ที่ตำแหน่งอยู่ระหว่างสองราคานี้

    ใช้เสริมให้แผนที่ 2 (Breakout) และแผนที่ 4 (Daily Continuation) เท่านั้น — สองแผนนี้อิงแค่ "ราคาทะลุ
    swing point ไปแรงๆ" ไม่เคยเช็คเลยว่าทางที่จะเดินไปเป้าหมายมีกำแพงอุปสงค์/อุปทานจริงขวางอยู่หรือเปล่า
    (ต่างจากแผนที่ 1/5/6 ที่ใช้ OB/FVG/Liquidity เป็นส่วนหนึ่งของการหา entry อยู่แล้ว) เจอเคสจริงที่ราคา
    ทะลุแนวเดิมไปแรงๆ ตามเงื่อนไขเดิมของแผน 2 แต่ดันวิ่งไปชนโซนใหญ่ที่ไม่เคยถูกตรวจสอบเลย แล้วสวนกลับ
    ชน SL ทันที — ฟังก์ชันนี้แค่ "ตรวจ" ให้ผู้เรียกเห็นก่อน ไม่ได้ตัดสินใจแทนว่าจะข้ามสัญญาณหรือไม่

    direction: "bullish" -> มองหาโซน/ระดับฝั่ง "bearish" ที่ขวางทางขึ้น
               "bearish" -> มองหาโซน/ระดับฝั่ง "bullish" ที่ขวางทางลง

    คืน None ถ้าไม่เจออะไรขวางทาง (หรือคำนวณไม่ได้ — กันเหนียว ไม่ทำให้ผู้เรียกพัง)
    คืน dict {"kind","top","bottom","count"} ของกำแพงที่ใกล้ entry_price ที่สุด ถ้าเจออย่างน้อย 1 อัน
    (count = จำนวนกำแพงทั้งหมดที่เจอในช่วงนี้ เผื่อผู้เรียกอยากรู้ว่าหนาแค่ไหน)
    """
    if entry_price is None or target_price is None:
        return None

    try:
        lo = min(entry_price, target_price)
        hi = max(entry_price, target_price)
        if hi <= lo:
            return None

        opposing_type = "bearish" if direction == "bullish" else "bullish"
        blockers = []

        try:
            from orderblock import find_order_blocks
            for ob in find_order_blocks(df, config):
                if ob.get("type") == opposing_type and not ob.get("mitigated"):
                    mid = (ob["top"] + ob["bottom"]) / 2.0
                    if lo < mid < hi:
                        blockers.append({"kind": "Order Block", "top": ob["top"], "bottom": ob["bottom"]})
        except Exception:
            pass

        try:
            from fvg import find_fvgs
            for fv in find_fvgs(df, config):
                if fv.get("type") == opposing_type and not fv.get("filled"):
                    mid = (fv["top"] + fv["bottom"]) / 2.0
                    if lo < mid < hi:
                        blockers.append({"kind": "FVG", "top": fv["top"], "bottom": fv["bottom"]})
        except Exception:
            pass

        try:
            from liquidity import find_liquidity_pools
            pools = find_liquidity_pools(df, config)
            opposing_levels = pools.get("equal_highs", []) if direction == "bullish" else pools.get("equal_lows", [])
            for level in opposing_levels:
                if lo < level < hi:
                    blockers.append({"kind": "Liquidity Pool (Equal High/Low)", "top": level, "bottom": level})
        except Exception:
            pass

        if not blockers:
            return None

        # เอาอันที่ใกล้ entry_price ที่สุดมารายงาน (ตัวแรกที่ราคาจะเจอระหว่างทางไปเป้าหมาย)
        blockers.sort(key=lambda b: abs(((b["top"] + b["bottom"]) / 2.0) - entry_price))
        nearest = blockers[0]
        return {
            "kind": nearest["kind"], "top": nearest["top"], "bottom": nearest["bottom"],
            "count": len(blockers),
        }
    except Exception:
        return None  # กันเหนียว — เช็คเสริมตัวนี้ต้องไม่มีทางทำให้ผู้เรียกหลักพัง
