import pandas as pd

from scenario import detect_breakout_trigger, detect_counter_trend_trigger

STRUCTURE_BULLISH = {
    "trend": "bullish",
    "last_swings": [
        {"type": "low", "price": 4100.0},
        {"type": "high", "price": 4115.0},
    ],
}


def test_breakout_not_triggered_within_buffer():
    """ราคาโผล่พ้นระดับนิดเดียว (ยังไม่เกิน buffer) ไม่ควรนับว่า 'ทะลุแรงๆ'"""
    df = pd.DataFrame({"close": [4115.2], "atr": [2.0]})  # buffer = 0.3*2.0 = 0.6
    result = detect_breakout_trigger(df, STRUCTURE_BULLISH, {"breakout_confirm_atr_mult": 0.3})
    assert result is None


def test_breakout_triggered_beyond_buffer():
    """ราคาปิดเลยระดับ + buffer ไปแล้วจริง ควรนับว่าทะลุแรงๆ และคืนทิศทาง/ระดับ/ราคาที่ถูกต้อง"""
    df = pd.DataFrame({"close": [4116.0], "atr": [2.0]})
    result = detect_breakout_trigger(df, STRUCTURE_BULLISH, {"breakout_confirm_atr_mult": 0.3})
    assert result is not None
    assert result["direction"] == "bullish"
    assert result["level"] == 4115.0


def test_breakout_none_when_not_enough_swings():
    df = pd.DataFrame({"close": [4200.0], "atr": [2.0]})
    result = detect_breakout_trigger(df, {"trend": "bullish", "last_swings": []}, {})
    assert result is None


def test_counter_trend_none_when_market_sideway():
    df = pd.DataFrame({"close": [4110.0] * 10})
    result = detect_counter_trend_trigger(df, {"trend": "sideway"})
    assert result is None
