from risk import calc_stop_loss, calc_position_size

BASE_CONFIG = {
    "sl_buffer_atr": 0.5,
    "min_sl_distance": 10.0,
}


def test_stop_loss_uses_order_block_when_present_bullish():
    entry_signal = {
        "direction": "bullish",
        "entry_price": 4120.0,
        "ob": {"top": 4110.0, "bottom": 4105.0},
    }
    sl = calc_stop_loss(entry_signal, current_atr=2.0, config=BASE_CONFIG)
    # base = ob bottom (4105) - buffer(0.5*2.0=1.0) = 4104.0
    assert sl == 4104.0


def test_stop_loss_uses_fvg_when_no_ob_bearish():
    entry_signal = {
        "direction": "bearish",
        "entry_price": 4120.0,
        "ob": None,
        "fvg": {"top": 4125.0, "bottom": 4122.0},
    }
    sl = calc_stop_loss(entry_signal, current_atr=2.0, config=BASE_CONFIG)
    # base = fvg top (4125) + buffer(1.0) = 4126.0 -> ห่างจาก entry แค่ 6.0
    # แคบกว่า min_sl_distance(10.0) จึงถูกดันออกไปเป็น entry + 10.0 = 4130.0
    assert sl == 4130.0


def test_stop_loss_falls_back_to_entry_price_when_no_zone():
    entry_signal = {"direction": "bullish", "entry_price": 4120.0}
    sl = calc_stop_loss(entry_signal, current_atr=2.0, config=BASE_CONFIG)
    # base = entry_price (ไม่มี zone) - buffer(1.0) = 4119.0 -> แคบกว่า min_sl_distance(10.0)
    # จึงต้องถูกดันออกไปเป็น entry - 10.0 = 4110.0
    assert sl == 4110.0


def test_stop_loss_respects_min_sl_distance_even_with_tight_zone():
    entry_signal = {
        "direction": "bullish",
        "entry_price": 4120.0,
        "ob": {"top": 4119.5, "bottom": 4119.0},  # zone แคบมาก ใกล้ entry เกินไป
    }
    sl = calc_stop_loss(entry_signal, current_atr=0.1, config=BASE_CONFIG)
    assert sl == 4110.0  # ถูกดันให้ห่างอย่างน้อย min_sl_distance เสมอ


def test_position_size_normal_case():
    result = calc_position_size(account_balance=1000, entry_price=4120.0, stop_loss=4110.0,
                                 config={"risk_per_trade_pct": 1.0})
    assert result["risk_amount"] == 10.0
    assert result["sl_distance"] == 10.0
    assert result["position_size"] == 1.0


def test_position_size_zero_distance_does_not_divide_by_zero():
    result = calc_position_size(account_balance=1000, entry_price=4120.0, stop_loss=4120.0,
                                 config={"risk_per_trade_pct": 1.0})
    assert result["position_size"] == 0
    assert result["sl_distance"] == 0
