def calc_stop_loss(entry_signal, current_atr, config):
    """
    current_atr: ควรส่งเป็น ATR เฉลี่ยย้อนหลัง (ไม่ใช่ ATR แท่งล่าสุดเป๊ะๆ) เพื่อกันเคสตลาดหดตัว
    ผิดปกติชั่วคราวแล้วได้ buffer แคบเกินจริง — ดู main.py/scenario.py ตรงจุดที่เรียกใช้ฟังก์ชันนี้
    """
    direction = entry_signal["direction"]
    entry_price = entry_signal["entry_price"]
    buffer = config["sl_buffer_atr"] * current_atr

    ob = entry_signal.get("ob")
    fvg = entry_signal.get("fvg")

    structure_zone = entry_signal.get("structure_zone")

    if ob:
        base = ob["bottom"] if direction == "bullish" else ob["top"]
    elif fvg:
        base = fvg["bottom"] if direction == "bullish" else fvg["top"]
    elif structure_zone:
        base = structure_zone["bottom"] if direction == "bullish" else structure_zone["top"]
    else:
        base = entry_price

    stop_loss = (base - buffer) if direction == "bullish" else (base + buffer)

    # --- SL ขั้นต่ำ: กันเคส zone แคบ/ATR ต่ำจนได้ SL แคบผิดปกติ เสี่ยงโดนสะบัดออกจาก noise ---
    # เช่นตั้ง min_sl_distance = 10.0 -> เข้า 4124 SL ต้องห่างอย่างน้อย 4114 (ฝั่ง Buy) เสมอ
    min_distance = config.get("min_sl_distance", 0)
    current_distance = abs(entry_price - stop_loss)
    if min_distance and current_distance < min_distance:
        stop_loss = (entry_price - min_distance) if direction == "bullish" else (entry_price + min_distance)

    return stop_loss


def calc_position_size(account_balance, entry_price, stop_loss, config):
    risk_amount = account_balance * (config["risk_per_trade_pct"] / 100)
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return {"risk_amount": risk_amount, "sl_distance": 0, "position_size": 0}
    position_size = risk_amount / sl_distance
    return {
        "risk_amount": round(risk_amount, 2),
        "sl_distance": round(sl_distance, 6),
        "position_size": round(position_size, 6),
    }
