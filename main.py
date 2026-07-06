import pandas as pd
import numpy as np

from config import CONFIG
from indicator import add_indicators
from trend import analyze_structure
from entry import evaluate_entry
from risk import calc_stop_loss, calc_position_size
from tp import calc_take_profits, calc_risk_reward
from score import calc_confidence_score
from report import print_report
from notify import send_telegram_alert, format_alert_message


def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV ขาดคอลัมน์: {missing}")
    return df.reset_index(drop=True)


def generate_synthetic_data(n=400, seed=42):
    rng = np.random.default_rng(seed)
    price = 100.0
    rows = []
    trend_bias = 0.0
    for i in range(n):
        if i % 60 == 0:
            trend_bias = rng.choice([-0.15, 0.15, 0.0])
        change = rng.normal(loc=trend_bias, scale=1.0)
        open_p = price
        close_p = price + change
        high_p = max(open_p, close_p) + abs(rng.normal(0, 0.5))
        low_p = min(open_p, close_p) - abs(rng.normal(0, 0.5))
        volume = rng.integers(100, 1000)
        rows.append([open_p, high_p, low_p, close_p, volume])
        price = close_p
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def run_pipeline(df, symbol="SYMBOL", timeframe="15m", account_balance=1000.0, config=CONFIG):
    df = add_indicators(df, config)
    structure = analyze_structure(df, config)
    entry_signal = evaluate_entry(df, structure, config)

    if not entry_signal["valid"]:
        print_report(symbol, timeframe, structure, entry_signal,
                      stop_loss=None, take_profits={}, position={},
                      rr={}, confidence={"score": 0, "breakdown": {}}, config=config)
        return

    current_atr = df["atr"].iloc[-1]
    stop_loss = calc_stop_loss(entry_signal, current_atr, config)
    take_profits = calc_take_profits(entry_signal["entry_price"], stop_loss,
                                      entry_signal["direction"], config)
    rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
          for name, price in take_profits.items()}
    position = calc_position_size(account_balance, entry_signal["entry_price"], stop_loss, config)
    confidence = calc_confidence_score(entry_signal, structure, df, config, rr["TP1"])

    if rr["TP1"] < config["min_rr"]:
        entry_signal["reasons"].append(
            f"RR ของ TP1 ({rr['TP1']}) ต่ำกว่าเกณฑ์ขั้นต่ำ ({config['min_rr']}) — ไม่แนะนำเข้า"
        )
        entry_signal["valid"] = False

    print_report(symbol, timeframe, structure, entry_signal, stop_loss,
                 take_profits, position, rr, confidence, config)

    # --- ส่ง Telegram Alert เมื่อ signal ถูกต้อง และ Score ผ่านเกณฑ์ ---
    if entry_signal["valid"] and confidence["score"] >= config["min_score_to_alert"]:
        msg = format_alert_message(symbol, timeframe, structure, entry_signal,
                                    stop_loss, take_profits, rr, confidence)
        sent = send_telegram_alert(config["telegram_token"], config["telegram_chat_id"], msg)
        print("[Telegram] ส่งแจ้งเตือนสำเร็จ" if sent else "[Telegram] ส่งแจ้งเตือนล้มเหลว")


if __name__ == "__main__":
    from fetch_data import fetch_twelvedata

    symbols = [
        ("XAU/USD", "XAUUSD"),
        ("EUR/USD", "EURUSD"),
        ("GBP/USD", "GBPUSD"),
    ]

    for td_symbol, display_symbol in symbols:
        df = fetch_twelvedata(
            symbol=td_symbol, interval="15min", outputsize=300,
            api_key=CONFIG["twelvedata_api_key"]
        )
        run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000)

