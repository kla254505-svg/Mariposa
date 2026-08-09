import pandas as pd

from score import calc_confidence_score, WEIGHTS

BASE_CONFIG = {"min_rr": 1.2, "premium_discount_filter_enabled": True}


def make_df(rows=30, atr=2.0, rsi=50, macd_hist_trend="flat", ema_fast=4110.0, ema_slow=4100.0):
    """สร้าง df ปลอมขั้นต่ำที่ calc_confidence_score ต้องการ (atr, rsi, macd_hist, close, ema cols)"""
    if macd_hist_trend == "rising":
        macd_hist = [i * 0.1 for i in range(rows)]
    elif macd_hist_trend == "falling":
        macd_hist = [-i * 0.1 for i in range(rows)]
    else:
        macd_hist = [0.0] * rows

    df = pd.DataFrame({
        "close": [4120.0] * rows,
        "atr": [atr] * rows,
        "rsi": [rsi] * rows,
        "macd_hist": macd_hist,
        "ema_fast": [ema_fast] * rows,
        "ema_slow": [ema_slow] * rows,
    })
    return df


def test_weak_signal_scores_low():
    """สัญญาณที่แทบไม่มีอะไรยืนยันเลย (ไม่มี OB/FVG/sweep) ควรได้คะแนนต่ำมาก"""
    entry_signal = {"direction": "bullish"}
    structure = {"event": "-", "trend_strength": None, "ema_trend": None}
    df = make_df(rsi=50, macd_hist_trend="flat")
    result = calc_confidence_score(entry_signal, structure, df, BASE_CONFIG, rr_tp1=1.0)
    assert result["score"] < 20


def test_strong_signal_scores_high_when_everything_aligns():
    """สัญญาณที่ตรงทุกปัจจัย (OB ใหญ่, sweep, เทรนด์แข็งแรง, 4H/1H สอดคล้อง) ควรได้คะแนนสูง"""
    entry_signal = {
        "direction": "bullish",
        "ob": {"top": 4110.0, "bottom": 4106.0},  # ขนาด 4.0, เท่ากับ 2*atr -> เต็ม cap
        "liquidity_sweep": True,
        "structure_zone": {"top": 4111.0, "bottom": 4109.0},
    }
    structure = {"event": "BOS", "trend_strength": "strong", "ema_trend": "bullish"}
    df = make_df(atr=2.0, rsi=55, macd_hist_trend="rising", ema_fast=4112.0, ema_slow=4108.0)
    bias_4h = {"trend": "bullish", "zone": "discount"}

    result = calc_confidence_score(entry_signal, structure, df, BASE_CONFIG, rr_tp1=2.0,
                                    bias_4h=bias_4h, higher_tf_trend="bullish")
    assert result["score"] > 70
    assert "bias4h_alignment" in result["breakdown"]
    assert "htf_1h_alignment" in result["breakdown"]


def test_conflicting_4h_bias_no_longer_zeroes_the_score():
    """
    Regression test: นี่คือบั๊กที่เคยเกิดจริง (Score รวม 0/100 ทั้งที่ ADX/Structure แข็งแรงมาก
    เพราะ bias_4h เคย hard-veto ก่อนคำนวณสกอร์เลยด้วยซ้ำ) — bias_4h ที่สวนทางตอนนี้
    ต้องแค่ "ไม่ได้แต้มโบนัส" ไม่ใช่ทำให้สกอร์รวมเป็น 0
    """
    entry_signal = {
        "direction": "bearish",
        "structure_zone": {"top": 4111.75, "bottom": 4111.51},
    }
    structure = {"event": "-", "trend_strength": "strong", "ema_trend": "bullish"}
    df = make_df(atr=0.25, rsi=52)
    bias_4h = {"trend": "bullish", "zone": "discount"}  # สวนทางกับสัญญาณ bearish โดยตั้งใจ

    result = calc_confidence_score(entry_signal, structure, df, BASE_CONFIG, rr_tp1=1.5,
                                    bias_4h=bias_4h, higher_tf_trend="bullish")
    assert result["score"] > 0
    assert "bias4h_alignment" not in result["breakdown"]
    assert "htf_1h_alignment" not in result["breakdown"]


def test_weights_dict_has_no_negative_values():
    assert all(v > 0 for v in WEIGHTS.values())
