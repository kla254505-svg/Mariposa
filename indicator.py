import pandas as pd
import numpy as np


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val.fillna(50)


def atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()

def adx(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    tr_atr = atr(df, period)  # ใช้ ATR ที่มีอยู่แล้ว

    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr_atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    adx_val = dx.ewm(alpha=1 / period, adjust=False).mean()

    return adx_val.fillna(0)


def macd(series, fast=12, slow=26, signal=9):
    """คืนคา (macd_line, signal_line, histogram)"""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series, period=20, std_mult=2):
    """คืนค่า (upper_band, middle_band, lower_band)"""
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = middle + (std * std_mult)
    lower = middle - (std * std_mult)
    return upper, middle, lower


def is_atr_contracting(df, lookback=50, contraction_ratio=0.7):
    """
    เช็คว่า ATR ปัจจุบันหดตัวเทียบค่าเฉลี่ยย้อนหลังมากไหม (ตลาดนิ่ง/sideway ผิดปกติ)
    ต่างจาก ADX ตรงที่วัด 'ความผันผวนสัมบูรณ์' ไม่ใช่ 'ความแรงของเทรนด์'
    คืนค่า True ถ้า ATR หดตัวต่ำกว่า contraction_ratio ของค่าเฉลี่ย (ควรหลีกเลี่ยงเข้าเทรด)
    """
    if "atr" not in df.columns or len(df) < lookback:
        return False
    current_atr = df["atr"].iloc[-1]
    avg_atr = df["atr"].iloc[-lookback:].mean()
    if not avg_atr:
        return False
    return current_atr < avg_atr * contraction_ratio


def add_indicators(df, config):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], config["ema_fast"])
    df["ema_slow"] = ema(df["close"], config["ema_slow"])
    df["ema_trend"] = ema(df["close"], config["ema_trend"])
    df["rsi"] = rsi(df["close"], config["rsi_period"])
    df["atr"] = atr(df, config["atr_period"])
    df["adx"] = adx(df, config["adx_period"])


    macd_line, signal_line, hist = macd(df["close"])
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"] = hist

    bb_upper, bb_mid, bb_lower = bollinger_bands(df["close"])
    df["bb_upper"] = bb_upper
    df["bb_mid"] = bb_mid
    df["bb_lower"] = bb_lower

    return df

