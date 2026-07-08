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
                             bias_4h=None):
    last = df.iloc[-1]
    price = last["close"]
    adx_val = last.get("adx", 0)
    atr_val = last.get("atr", 0)
    rsi_val = last.get("rsi", 50)

    RSI_LABEL = "Oversold ⚠️" if rsi_val <= 30 else ("Overbought ⚠️" if rsi_val >= 70 else "ปกติ")

    STRENGTH_LABEL = {"strong": " (Strong)", "weak": " (Weak — กำลังก่อตัว)", "none": ""}
    trend_label = TREND_LABEL.get(structure.get("trend"), structure.get("trend"))
    trend_label += STRENGTH_LABEL.get(structure.get("trend_strength"), "")
    ema_bias_label = TREND_LABEL.get(structure.get("ema_trend"), structure.get("ema_trend"))
    slope_label = SLOPE_LABEL.get(calc_macd_slope(df), "-")

    atr_contract = is_atr_contracting(
        df, config.get("atr_contraction_lookback", 50), config.get("atr_contraction_ratio", 0.7)
    )

    lines = [
        f"📋 <b>Dashboard: {symbol} ({timeframe})</b>",
        f"ราคาปัจจุบัน: {price:.4f}",
        "",
    ]

    if bias_4h:
        zone_label = {"premium": "Premium", "discount": "Discount", "equilibrium": "Equilibrium"}.get(
            bias_4h.get("zone"), "-"
        )
        lines.append(
            f"🧭 4H Bias: {TREND_LABEL.get(bias_4h.get('trend'), bias_4h.get('trend'))} | โซนราคา: {zone_label}"
        )
        lines.append("")

    lines += [
        f"เทรนด์ (Structure): {trend_label} | Event: {structure.get('event') or '-'}",
        f"เทรนด์ (EMA Bias): {ema_bias_label}",
        f"ADX: {adx_val:.1f} ({'มีเทรนด์' if adx_val >= config['adx_min_trend'] else 'Choppy'})",
        f"RSI: {rsi_val:.1f} ({RSI_LABEL})",
        f"ATR: {atr_val:.4f} ({'หดตัว/นิ่งผิดปกติ' if atr_contract else 'ปกติ'})",
        f"MACD Slope: {slope_label}",
        "",
    ]

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

    trigger = entry_signal.get("trigger")
    if trigger:
        lines.append(f"5M Trigger: {'ยืนยันแล้ว ✅' if trigger.get('confirmed') else 'รอ reaction ⏳'}")
    lines.append("")

    lines.append(f"<b>Score รวม: {confidence['score']}/100</b>")
    for factor, pts in confidence["breakdown"].items():
        lines.append(f" • {factor}: +{pts}")
    lines.append("")

    if entry_signal.get("alert_ready"):
        lines.append(f"<b>สถานะ: พร้อมเข้าไม้ ({direction})</b>")
        lines.append(f"Entry: {entry_signal['entry_price']:.4f}")
    elif entry_signal.get("valid"):
        lines.append(f"<b>สถานะ: มี setup ({direction}) แต่ยังไม่ผ่านเกณฑ์ครบ / รอ trigger</b>")
        lines.append(f"Entry (คาดการณ์): {entry_signal['entry_price']:.4f}")
        if entry_signal.get("reasons"):
            lines.append("เหตุผลล่าสุด:")
            for r in entry_signal["reasons"][-3:]:
                lines.append(f" • {r}")
    else:
        lines.append("<b>สถานะ: ยังไม่มีจังหวะเข้าไม้ที่ผ่านเกณฑ์ทั้งหมด</b>")
        if entry_signal.get("reasons"):
            lines.append("เหตุผลล่าสุด:")
            for r in entry_signal["reasons"][-3:]:
                lines.append(f" • {r}")

    return "\n".join(lines)
