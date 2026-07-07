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
from session import get_session_info


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


 def run_pipeline(df, symbol="SYMBOL", timeframe="15m", account_balance=1000.0, config=CONFIG, higher_tf_trend=None session_info=None):
    df = add_indicators(df, config)
    structure = analyze_structure(df, config)
    entry_signal = evaluate_entry(df, structure, config)

    # --- กรองสัญญาณที่สวนทางกับเทรนด์เฟรมใหญ่ (1H) ---
    if entry_signal["valid"] and higher_tf_trend not in (None, "sideway"):
        if entry_signal["direction"] != higher_tf_trend:
            entry_signal["reasons"].append(
                f"สัญญาณ 15m เป็น {entry_signal['direction']} แต่เทรนด์ 1H เป็น {higher_tf_trend} "
                f"(สวนทางกัน) — ไม่แนะนำเขา"
            )
            entry_signal["valid"] = False

        # --- กรองตลาด choppy/sideway ด้วย ADX ---
    if entry_signal["valid"]:
        current_adx = df["adx"].iloc[-1]
        if current_adx < config["adx_min_trend"]:
            entry_signal["reasons"].append(
                f"ADX ({current_adx:.1f}) ต่ำกว่าเกณฑ์ ({config['adx_min_trend']}) "
                f"ตลาดยังไม่มีเทรนด์ชัดเจน (choppy) — ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False

        # --- กรองด้วย Trading Session (London/NY) ---
    if entry_signal["valid"] and config.get("session_filter_enabled") and session_info:
        if not session_info["in_session"]:
            entry_signal["reasons"].append(
                f"เวลาปัจจุบัน {session_info['utc_hour']}:00 UTC อยู่นอกชวง London/NY session "
                f"(สภาพคล่องต่ำ) — ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False
        elif session_info["in_killzone"]:
            entry_signal["reasons"].append(
                f"อยู่ใน Kill Zone ({session_info['utc_hour']}:00 UTC) — ชวงเวลาที่มักเคลื่อนไหวแรง"
            )



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

    # --- ส่ง Telegram Alert เมื่อ signal ผานเกณฑ์กฎหลัก (เทรนด์ + zone + RR + ไมสวนเทรนด์ 1H) ---
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

        session_info = get_session_info(CONFIG)
        
        # ดึงเฟรม 1H มาคำนวณเทรนด์ ใช้เป็นตัวกรองก่อนส่ง Alert
        df_1h = fetch_twelvedata(
            symbol=td_symbol, interval="1h", outputsize=300,
            api_key=CONFIG["twelvedata_api_key"]
        )
        df_1h_ind = add_indicators(df_1h, CONFIG)
        structure_1h = analyze_structure(df_1h_ind, CONFIG)
        higher_tf_trend = structure_1h["trend"]

        run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000,
                     higher_tf_trend=higher_tf_trend)

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
