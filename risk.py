def calc_stop_loss(entry_signal, current_atr, config):
    direction = entry_signal["direction"]
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
        base = entry_signal["entry_price"]



    if direction == "bullish":
        return base - buffer
    else:
        return base + buffer


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
