"""
dashboard.py
รวมทุกค่าที่ระบบอ่านได้ ณ ขณะนั้น (Trend, ADX, ATR, Session, EMA/MACD, Liquidity Sweep, OB/FVG, Score)
เป็นข้อความเดียว ส่งแบบ "แก้ทับของเดิม" (เหมือน Hourly Briefing) ไม่สแปมแชท
"""

from quality import calc_macd_slope
from indicator import is_atr_contracting

TREND_LABEL = {"bullish": "ขาขึ้น", "bearish": "ขาลง", "sideway": "Sideway"}
SLOPE_LABEL = {"rising": "เพิ่มขึ้น ↑", "falling": "ลดลง ↓", "flat": "แบนราบ"}


def build_dashboard_message(symbol, timeframe, df, structure, entry_signal, confidence, session_info, config,
                             daily_bias=None, pd_zone=None, internal_structure=None):
    last = df.iloc[-1]
    price = last["close"]
    adx_val = last.get("adx", 0)
    atr_val = last.get("atr", 0)

    trend_label = TREND_LABEL.get(structure.get("trend"), structure.get("trend"))
    ema_bias_label = TREND_LABEL.get(structure.get("ema_trend"), structure.get("ema_trend"))
    slope_label = SLOPE_LABEL.get(calc_macd_slope(df), "-")

    atr_contract = is_atr_contracting(
        df, config.get("atr_contraction_lookback", 50), config.get("atr_contraction_ratio", 0.7)
    )

    lines = [
        f"📋 <b>Dashboard: {symbol} ({timeframe})</b>",
        f"ราคาปัจจุบัน: {price:.4f}",
        "",
        f"เทรนด์ (Structure): {trend_label} | Event: {structure.get('event') or '-'}",
        f"เทรนด์ (EMA Bias): {ema_bias_label}",
        f"ADX: {adx_val:.1f} ({'มีเทรนด์' if adx_val >= config['adx_min_trend'] else 'Choppy'})",
        f"ATR: {atr_val:.4f} ({'หดตัว/นิ่งผิดปกติ' if atr_contract else 'ปกติ'})",
        f"MACD Slope: {slope_label}",
        "",
    ]

    if daily_bias is not None:
        daily_bias_label = TREND_LABEL.get(daily_bias, daily_bias)
        lines.append(f"Daily Bias (4H): {daily_bias_label}")

    if internal_structure is not None:
        internal_label = TREND_LABEL.get(internal_structure.get("trend"), internal_structure.get("trend"))
        lines.append(f"Internal Structure: {internal_label} | Event: {internal_structure.get('event') or '-'}")

    if pd_zone is not None:
        zone_label = "Premium (แพง)" if pd_zone["zone"] == "premium" else "Discount (ถูก)"
        lines.append(f"PD Zone: {zone_label} ({pd_zone['position_pct']}% ของช่วง {pd_zone['zone_low']}-{pd_zone['zone_high']})")

    if daily_bias is not None or pd_zone is not None or internal_structure is not None:
        lines.append("")

    if session_info:
        session_line = "อยู่ใน London/NY Session" if session_info["in_session"] else "นอก Session (สภาพคล่องต่ำ)"
        if session_info.get("in_killzone"):
            session_line += " | Kill Zone ⚡"
        lines.append(f"Session: {session_line}")
        lines.append("")

    sweep = entry_signal.get("liquidity_sweep")
    lines.append(f"Liquidity Sweep: {'พบการกวาดแล้วกลับตัว ✅' if sweep else 'ยังไม่พบ'}")

    direction = entry_signal.get("direction") or "-"
    ob = entry_signal.get("ob")
    fvg = entry_signal.get("fvg")
    lines.append(f"Order Block: {'พบ (' + direction + ')' if ob else 'ไม่พบ'}")
    lines.append(f"FVG: {'พบ (' + direction + ')' if fvg else 'ไม่พบ'}")
    lines.append("")

    lines.append(f"<b>Score รวม: {confidence['score']}/100</b>")
    for factor, pts in confidence["breakdown"].items():
        lines.append(f" • {factor}: +{pts}")
    lines.append("")

    if entry_signal.get("valid"):
        lines.append(f"<b>สถานะ: มีจังหวะเข้าไม้ ({direction})</b>")
        lines.append(f"Entry: {entry_signal['entry_price']:.4f}")
    else:
        lines.append("<b>สถานะ: ยังไม่มีจังหวะเข้าไม้ที่ผ่านเกณฑ์ทั้งหมด</b>")
        if entry_signal.get("reasons"):
            lines.append("เหตุผลล่าสุด:")
            for r in entry_signal["reasons"][-3:]:
                lines.append(f" • {r}")

    return "\n".join(lines)
