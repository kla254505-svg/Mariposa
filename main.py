import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone

from config import CONFIG
from indicator import add_indicators
from trend import analyze_structure
from entry import evaluate_entry
from risk import calc_stop_loss, calc_position_size
from tp import calc_take_profits, calc_risk_reward
from score import calc_confidence_score
from report import print_report
from notify import send_telegram_alert, format_alert_message, send_or_edit_message
from scenario import build_hourly_briefing


def ping_healthcheck(url):
    """ยิงสัญญาณบอกว่าบอทรันสำเร็จ ถ้าไม่มี URL หรือยิงพลาด จะไม่ทำให้บอทหลักพัง"""
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except Exception:
        pass  # ไม่ต้องทำอะไร แค่ไม่อยากให้ ping พังแล้วบอทหลักพังตาม


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

    # --- ส่ง Telegram Alert เมื่อ signal ผ่านเกณฑ์กฎหลัก (เทรนด์ + zone + RR) ---
    # หมายเหตุ: ไม่ใช confidence score มากรองซอีกชั้นแล้ว เพราะ entry_signal["valid"]
    # เป็น checklist แบบ pass/fail ที่ผ่านมาตรฐานอยู่แล้ว การกรองซ้ำด้วย score
    # ทให้สัญญาณที่ถูกต้องหลุดออกไปโดยไม่จำเป็น
    if entry_signal["valid"]:
        msg = format_alert_message(symbol, timeframe, structure, entry_signal,
                                    stop_loss, take_profits, rr, confidence)
        sent = send_telegram_alert(config["telegram_token"], config["telegram_chat_id"], msg)
        print("[Telegram] ส่งแจ้งเตือนสำเร็จ" if sent else "[Telegram] ส่งแจ้งเตือนล้มเหลว")


if __name__ == "__main__":
    from fetch_data import fetch_twelvedata

    symbols = [
        ("XAU/USD", "XAUUSD"),
    ]

    for td_symbol, display_symbol in symbols:
        df = fetch_twelvedata(
            symbol=td_symbol, interval="15min", outputsize=300,
            api_key=CONFIG["twelvedata_api_key"]
        )
        run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000)

        # ส่ง Hourly Briefing เฉพาะรอบที่ตรงกับต้นชั่วโมง (นาที 0-14 ของทุกชั่วโมง)
        if datetime.now(timezone.utc).minute < 15:
            df_ind = add_indicators(df, CONFIG)
            structure = analyze_structure(df_ind, CONFIG)
            entry_signal = evaluate_entry(df_ind, structure, CONFIG)
            briefing_text = build_hourly_briefing(display_symbol, "15m", df_ind, structure, entry_signal)
            send_or_edit_message(
                CONFIG["telegram_token"], CONFIG["telegram_chat_id"], briefing_text,
                CONFIG["kvdb_bucket"], key=f"briefing_{display_symbol}"
            )

    # ping บอก Healthchecks.io ว่ารันสำเร็จครบทุกคู่เงินแล้ว (ทำเป็นลำดับสุดท้ายเสมอ)
    ping_healthcheck(CONFIG["healthchecks_url"])
