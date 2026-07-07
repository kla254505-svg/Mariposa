"""
quality.py
ให้คะแนนคุณภาพ (0.0-1.0) ของ Order Block / FVG แทนการนับแค่ "เจอ/ไม่เจอ"
ยิ่งโซนใหญ่เทียบ ATR ยิ่งแปลว่าแรงส่ง (displacement) แรง = คุณภาพสูง
ยังรวมตัวช่วยวัด EMA Distance และ MACD Slope สำหรับให้คะแนนความแข็งแรงของเทรนด์
"""


def score_order_block_quality(ob, atr_val, cap_atr_mult=1.5):
    """คืนค่า 0.0-1.0 ตามขนาด OB เทียบ ATR (ยิ่งใหญ่ ยิ่งมีคุณภาพ จนถึงจุดอิ่มตัวที่ cap_atr_mult)"""
    if not ob or not atr_val:
        return 0.0
    size = ob["top"] - ob["bottom"]
    return min(size / (atr_val * cap_atr_mult), 1.0)


def score_fvg_quality(fvg, atr_val, cap_atr_mult=1.0):
    """คืนค่า 0.0-1.0 ตามขนาด Gap ของ FVG เทียบ ATR"""
    if not fvg or not atr_val:
        return 0.0
    size = fvg["top"] - fvg["bottom"]
    return min(size / (atr_val * cap_atr_mult), 1.0)


def calc_ema_distance_score(row, atr_val, cap_atr_mult=1.0):
    """
    วัดระยะห่างระหว่าง EMA Fast กับ EMA Slow เทียบ ATR
    ยิ่งห่างมาก = เทรนด์แข็งแรง ไม่ใช่แค่เรียงตัวถูกทิศแต่จนแต่ยังไม่มีแรง
    คืนค่า 0.0-1.0
    """
    if not atr_val:
        return 0.0
    dist = abs(row.get("ema_fast", 0) - row.get("ema_slow", 0))
    return min(dist / (atr_val * cap_atr_mult), 1.0)


def calc_macd_slope(df, lookback=3):
    """
    เช็คทิศทางความชันของ MACD Histogram ในช่วง lookback แท่งหลังสุด
    คืนค่า 'rising' (โมเมนตัมเพิ่มขึ้นต่อเนื่อง) / 'falling' (ลดลงต่อเนื่อง) / 'flat' (ไม่ชัดเจน)
    """
    if "macd_hist" not in df.columns or len(df) < lookback + 1:
        return "flat"
    recent = df["macd_hist"].iloc[-(lookback + 1):]
    diffs = recent.diff().dropna()
    if len(diffs) == 0:
        return "flat"
    if (diffs > 0).all():
        return "rising"
    if (diffs < 0).all():
        return "falling"
    return "flat"
