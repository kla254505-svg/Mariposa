def calc_take_profits(entry_price, stop_loss, direction, config):
    risk_distance = abs(entry_price - stop_loss)
    tp_rr = {"TP1": config["tp1_rr"], "TP2": config["tp2_rr"], "TP3": config["tp3_rr"]}
    tps = {}
    for name, rr in tp_rr.items():
        if direction == "bullish":
            tps[name] = entry_price + risk_distance * rr
        else:
            tps[name] = entry_price - risk_distance * rr
    return tps


def calc_risk_reward(entry_price, stop_loss, tp_price):
    risk = abs(entry_price - stop_loss)
    reward = abs(tp_price - entry_price)
    if risk == 0:
        return 0.0
    return round(reward / risk, 2)
