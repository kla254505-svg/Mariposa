def calc_fib_levels(swing_low_price, swing_high_price, direction, config):
    diff = swing_high_price - swing_low_price
    levels = {}
    for lvl in config["fib_levels"]:
        if direction == "bullish":
            levels[lvl] = swing_high_price - diff * lvl
        else:
            levels[lvl] = swing_low_price + diff * lvl
    return levels


def is_in_ote_zone(price, fib_levels, low_bound=0.618, high_bound=0.79):
    prices = [p for lvl, p in fib_levels.items() if low_bound <= lvl <= high_bound]
    if not prices:
        return False
    return min(prices) <= price <= max(prices)
