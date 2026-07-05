import requests
import pandas as pd


def fetch_binance(symbol="BTCUSDT", interval="15m", limit=500):
    url = "https://api.binance.com/api/v3/klines"
    params = dict(symbol=symbol.upper(), interval=interval, limit=limit)
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tbbav", "tbqav", "ignore",
    ])
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.reset_index(drop=True)


def fetch_yahoo(symbol="GC=F", interval="15m", range_="5d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = dict(interval=interval, range=range_)
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = data["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]

    df = pd.DataFrame({
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"],
        "volume": quote["volume"],
    })
    df = df.dropna().reset_index(drop=True)
    return df


def fetch_twelvedata(symbol="XAU/USD", interval="15min", outputsize=300, api_key=""):
    url = "https://api.twelvedata.com/time_series"
    params = dict(symbol=symbol, interval=interval, outputsize=outputsize, apikey=api_key)
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "values" not in data:
        raise ValueError(f"Twelve Data Error: {data}")

    df = pd.DataFrame(data["values"])

    # ทอง/Forex มักไม่มีคอลัมน์ volume ส่งมา ต้องเช็คก่อน
    if "volume" not in df.columns:
        df["volume"] = 0

    price_cols = ["open", "high", "low", "close", "volume"]
    df[price_cols] = df[price_cols].astype(float)

    df = df.sort_values("datetime").reset_index(drop=True)  # Twelve Data ส่งมาใหม่->เก่า ต้องกลับลำดับ
    return df[price_cols]
