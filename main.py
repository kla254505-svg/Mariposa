import requests
import pandas as pd
import numpy as np


from datetime import datetime, timezone

from config import CONFIG
from indicator import add_indicators, is_atr_contracting
from trend import analyze_structure
from entry import evaluate_entry, get_entry_zone_bounds
from risk import calc_stop_loss, calc_position_size
from tp import calc_take_profits, calc_risk_reward
from score import calc_confidence_score
from report import print_report
from notify import send_telegram_alert, format_alert_message, send_or_edit_message
from scenario import build_hourly_briefing
from dashboard import build_dashboard_message
from session import get_session_info
from bias_4h import analyze_4h_bias, is_bias_aligned
from trigger_5m import find_5m_trigger


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


def run_pipeline(df, symbol="SYMBOL", timeframe="15m", account_balance=1000.0, config=CONFIG,
                  higher_tf_trend=None, session_info=None, bias_4h=None, df_5m=None):
    df = add_indicators(df, config)
    structure = analyze_structure(df, config)
    entry_signal = evaluate_entry(df, structure, config)

    # --- กรองด้วย Bias เฟรม 4H (เทรนด์ใหญ่สุด + โซน Premium/Discount) — ชั้นบนสุดของ MTF ---
    if entry_signal["valid"] and config.get("bias4h_filter_enabled", True) and bias_4h:
        aligned, reason = is_bias_aligned(entry_signal["direction"], bias_4h, config)
        if not aligned:
            entry_signal["reasons"].append(f"[4H Bias] {reason}")
            entry_signal["valid"] = False
        else:
            zone_label = {"premium": "Premium", "discount": "Discount", "equilibrium": "Equilibrium"}.get(
                bias_4h.get("zone"), "-"
            )
            entry_signal["reasons"].append(
                f"[4H Bias] เทรนด์ 4H: {bias_4h.get('trend')} | โซนราคา: {zone_label} — สอดคล้องกับสัญญาณ 15M"
            )

    # --- กรองสัญญาณที่สวนทางกับเทรนด์เฟรมใหญ่ (1H) ---
    if entry_signal["valid"] and higher_tf_trend not in (None, "sideway"):
        if entry_signal["direction"] != higher_tf_trend:
            entry_signal["reasons"].append(
                f"สัญญาณ 15m เป็น {entry_signal['direction']} แต่เทรนด์ 1H เป็น {higher_tf_trend} "
                f"(สวนทางกัน) — ไม่แนะนำเข้า"
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

    # --- กรองตลาดนิ่งผิดปกติด้วย ATR Contraction ---
    if entry_signal["valid"] and config.get("atr_contraction_filter_enabled", True):
        if is_atr_contracting(df, config.get("atr_contraction_lookback", 50),
                               config.get("atr_contraction_ratio", 0.7)):
            entry_signal["reasons"].append(
                "ATR หดตัวต่ำกว่าค่าเฉลี่ยมาก (ความผันผวนแทบไม่มี) — ตลาดนิ่งผิดปกติ ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False

    # --- กรองด้วย Trading Session (London/NY) ---
    if entry_signal["valid"] and config.get("session_filter_enabled") and session_info:
        if not session_info["in_session"]:
            entry_signal["reasons"].append(
                f"เวลาปัจจุบัน {session_info['utc_hour']}:00 UTC อยู่นอกช่วง London/NY session "
                f"(สภาพคล่องต่ำ) — ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False
        elif session_info["in_killzone"]:
            entry_signal["reasons"].append(
                f"อยู่ใน Kill Zone ({session_info['utc_hour']}:00 UTC) — ช่วงเวลาที่มักเคลื่อนไหวแรง"
            )

    stop_loss, take_profits, rr, position = None, {}, {}, {}
    confidence = {"score": 0, "breakdown": {}}

    if entry_signal["valid"]:
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
        elif confidence["score"] < config["min_score_to_alert"]:
            # --- กรองด้วยคะแนนความมั่นใจ ก่อนตัดสินใจส่ง Telegram Alert ---
            entry_signal["reasons"].append(
                f"Score ({confidence['score']}/100) ต่ำกว่าเกณฑ์แจ้งเตือน "
                f"({config['min_score_to_alert']}) — ยังไม่ส่ง Alert"
            )
            entry_signal["alert_ready"] = False
        else:
            entry_signal["alert_ready"] = True

        # --- ชั้น 5M Trigger: รอราคาวิ่งเข้าโซนที่ 15M ระบุ + มี reaction กลับตัวจริงก่อนยิง entry ---
        if config.get("trigger5m_filter_enabled", True) and df_5m is not None:
            zone = get_entry_zone_bounds(entry_signal)
            if zone:
                trigger = find_5m_trigger(
                    df_5m, zone["top"], zone["bottom"], entry_signal["direction"], config
                )
                entry_signal["trigger"] = trigger
                entry_signal["reasons"].append(f"[5M Trigger] {trigger['reason']}")

                if trigger["confirmed"]:
                    # เจอจุดกลับตัวจริงบน 5M แล้ว → ลอง "ลด SL ให้แคบลง" ถ้าจุดกลับตัวจริงใกล้กว่าโซน 15M เดิม
                    tightened_base = (
                        trigger["trigger_low"] if entry_signal["direction"] == "bullish"
                        else trigger["trigger_high"]
                    )
                    buffer = config["sl_buffer_atr"] * current_atr
                    tightened_sl = (
                        tightened_base - buffer if entry_signal["direction"] == "bullish"
                        else tightened_base + buffer
                    )
                    is_tighter = (
                        tightened_sl > stop_loss if entry_signal["direction"] == "bullish"
                        else tightened_sl < stop_loss
                    )
                    if is_tighter:
                        stop_loss = tightened_sl
                        take_profits = calc_take_profits(
                            entry_signal["entry_price"], stop_loss, entry_signal["direction"], config
                        )
                        rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
                              for name, price in take_profits.items()}
                        position = calc_position_size(
                            account_balance, entry_signal["entry_price"], stop_loss, config
                        )
                        entry_signal["reasons"].append(
                            f"SL ถูกปรับให้แคบลงตามจุดกลับตัวจริงบน 5M ({stop_loss:.4f})"
                        )
                elif entry_signal.get("alert_ready"):
                    entry_signal["reasons"].append(
                        "ยังไม่ยิง Alert เพราะรอการยืนยันกลับตัวบน 5M ก่อน (setup ผ่านเกณฑ์แล้ว แต่ยังไม่ trigger)"
                    )
                    entry_signal["alert_ready"] = False
            else:
                entry_signal["reasons"].append(
                    "[5M Trigger] ไม่มีโซน OB/FVG/Structure จาก 15M ให้ตรวจสอบ reaction"
                )

    print_report(symbol, timeframe, structure, entry_signal, stop_loss,
                 take_profits, position, rr, confidence, config)

    # --- ส่ง Telegram Alert เมื่อ signal ผ่านเกณฑ์กฎหลัก "และ" ผ่านเกณฑ์คะแนนขั้นต่ำ ---
    if entry_signal.get("alert_ready"):
        msg = format_alert_message(symbol, timeframe, structure, entry_signal,
                                    stop_loss, take_profits, rr, confidence, bias_4h=bias_4h)
        sent = send_telegram_alert(config["telegram_token"], config["telegram_chat_id"], msg)
        print("[Telegram] ส่งแจ้งเตือนสำเร็จ" if sent else "[Telegram] ส่งแจ้งเตือนล้มเหลว")

    # --- Dashboard: ส่งทุกรอบ แบบแก้ทับข้อความเดิม (ไม่สแปมแชท) ---
    dashboard_text = build_dashboard_message(
        symbol, timeframe, df, structure, entry_signal, confidence, session_info, config, bias_4h=bias_4h
    )
    send_or_edit_message(
        config["telegram_token"], config["telegram_chat_id"], dashboard_text,
        config["kvdb_bucket"], key=f"dashboard_{symbol}"
    )


if __name__ == "__main__":
    from fetch_data import fetch_twelvedata

    symbols = [
        ("XAU/USD", "XAUUSD"),
    ]

    try:
        for td_symbol, display_symbol in symbols:
            # --- ห่อการดึงข้อมูลด้วย try/except กันคู่เงินนี้พังแล้วลากทั้งสคริปต์ตายไปด้วย ---
            try:
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

                # ดึงเฟรม 4H มาคำนวณ Bias ใหญ่สุด (เทรนด์ + Premium/Discount) — ชั้นบนสุดของ MTF
                df_4h = fetch_twelvedata(
                    symbol=td_symbol, interval="4h", outputsize=300,
                    api_key=CONFIG["twelvedata_api_key"]
                )

                # ดึงเฟรม 5M มาหา Trigger เข้าไม้จริง (reaction ในโซนที่ 15M ระบุ)
                df_5m = fetch_twelvedata(
                    symbol=td_symbol, interval="5min", outputsize=200,
                    api_key=CONFIG["twelvedata_api_key"]
                )
            except Exception as e:
                print(f"[Data Error] {display_symbol}: {e}")
                continue  # ข้ามคู่เงินนี้ไป แต่คู่อื่น/ping ยังทำงานต่อได้

            df_1h_ind = add_indicators(df_1h, CONFIG)
            structure_1h = analyze_structure(df_1h_ind, CONFIG)
            higher_tf_trend = structure_1h["trend"]

            df_4h_ind = add_indicators(df_4h, CONFIG)
            bias_4h = analyze_4h_bias(df_4h_ind, CONFIG)

            run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000,
                         higher_tf_trend=higher_tf_trend, session_info=session_info,
                         bias_4h=bias_4h, df_5m=df_5m)

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
    finally:
        # ping บอก Healthchecks.io เสมอ ไม่ว่าข้างบนจะสำเร็จหรือมี error ก็ตาม
        # (นี่คือหน้าที่จริงของ Dead Man's Switch — ต้องรู้ว่าบอทยังไม่ตายแม้ตอน API ล่ม)
        ping_healthcheck(CONFIG["healthchecks_url"])
