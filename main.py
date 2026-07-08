import requests
import pandas as pd
import numpy as np


from datetime import datetime, timezone

from config import CONFIG
from indicator import add_indicators, is_atr_contracting
from trend import analyze_structure, analyze_internal_structure
from entry import evaluate_entry
from risk import calc_stop_loss, calc_position_size
from tp import calc_take_profits, calc_risk_reward
from score import calc_confidence_score
from report import print_report
from notify import send_telegram_alert, format_alert_message, send_or_edit_message
from scenario import build_hourly_briefing
from dashboard import build_dashboard_message
from session import get_session_info
from orders import add_order, update_orders_status, build_orders_dashboard
from zones import calc_premium_discount_zone, check_bias_pd_confirm, calc_daily_bias
from cooldown import is_in_cooldown, mark_alert_sent


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
                  higher_tf_trend=None, session_info=None, daily_bias=None):
    df = add_indicators(df, config)
    structure = analyze_structure(df, config)
    internal_structure = analyze_internal_structure(df, config)
    pd_zone = calc_premium_discount_zone(df, config.get("pd_lookback", 20))
    entry_signal = evaluate_entry(df, structure, config)

    # --- กรองสัญญาณที่สวนทางกับเทรนด์เฟรมใหญ่ (1H) ---
    if entry_signal["valid"] and higher_tf_trend not in (None, "sideway"):
        if entry_signal["direction"] != higher_tf_trend:
            entry_signal["reasons"].append(
                f"สัญญาณ 15m เป็น {entry_signal['direction']} แต่เทรนด์ 1H เป็น {higher_tf_trend} "
                f"(สวนทางกัน) — ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False

    # --- กรองด้วย Daily Bias (4H) — ชั้น bias สูงสุด เหนือกว่า 1H ---
    if entry_signal["valid"] and config.get("daily_bias_filter_enabled") and daily_bias not in (None, "neutral"):
        daily_bias_direction = "bullish" if daily_bias == "bullish" else "bearish"
        if entry_signal["direction"] != daily_bias_direction:
            entry_signal["reasons"].append(
                f"สัญญาณ 15m เป็น {entry_signal['direction']} แต่ Daily Bias (4H) เป็น {daily_bias} "
                f"(สวนทางกับภาพใหญ่สุด) — ไม่แนะนำเข้า"
            )
            entry_signal["valid"] = False

    # --- กรองด้วย Premium/Discount Zone — Buy ต้องอยู่ Discount, Sell ต้องอยู่ Premium ---
    if entry_signal["valid"] and config.get("pd_zone_filter_enabled"):
        if not check_bias_pd_confirm(entry_signal["direction"], pd_zone):
            entry_signal["reasons"].append(
                f"ราคาปัจจุบันอยู่โซน {pd_zone['zone']} ({pd_zone['position_pct']}% ของช่วง) "
                f"ไม่เหมาะกับสัญญาณ {entry_signal['direction']} — ไม่แนะนำเข้า"
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
        elif is_in_cooldown(config["kvdb_bucket"], symbol, entry_signal["direction"],
                             config.get("signal_cooldown_minutes", 45)):
            # --- กัน Alert ยิงถี่เกินไปตอนราคาแกว่งกลับไปมาในทิศทางเดียวกันในกรอบเวลาสั้นๆ ---
            entry_signal["reasons"].append(
                f"เพิ่งส่ง Alert ทิศทาง {entry_signal['direction']} ไปเมื่อไม่ถึง "
                f"{config.get('signal_cooldown_minutes', 45)} นาทีที่แล้ว (Cooldown) — ยังไม่ส่งซ้ำ"
            )
            entry_signal["alert_ready"] = False
        else:
            entry_signal["alert_ready"] = True

    print_report(symbol, timeframe, structure, entry_signal, stop_loss,
                 take_profits, position, rr, confidence, config)

    # --- ส่ง Telegram Alert เมื่อ signal ผ่านเกณฑ์กฎหลัก "และ" ผ่านเกณฑ์คะแนนขั้นต่ำ ---
    if entry_signal.get("alert_ready"):
        msg = format_alert_message(symbol, timeframe, structure, entry_signal,
                                    stop_loss, take_profits, rr, confidence,
                                    daily_bias=daily_bias, pd_zone=pd_zone,
                                    internal_structure=internal_structure)
        sent = send_telegram_alert(config["telegram_token"], config["telegram_chat_id"], msg)
        print("[Telegram] ส่งแจ้งเตือนสำเร็จ" if sent else "[Telegram] ส่งแจ้งเตือนล้มเหลว")

        # --- บันทึกออเดอร์ใหม่ลง kvdb.io เพื่อติดตามผลย้อนหลัง (win/loss/running) ---
        add_order(
            config["kvdb_bucket"], symbol, entry_signal["direction"],
            entry_signal["entry_price"], stop_loss, take_profits, confidence["score"]
        )

        # --- บันทึกเวลาที่ส่ง Alert ไป เพื่อเช็ค Cooldown ในรอบถัดไป ---
        mark_alert_sent(config["kvdb_bucket"], symbol, entry_signal["direction"])

    # --- Dashboard: ส่งทุกรอบ แบบแก้ทับข้อความเดิม (ไม่สแปมแชท) ---
    dashboard_text = build_dashboard_message(
        symbol, timeframe, df, structure, entry_signal, confidence, session_info, config
    )
    send_or_edit_message(
        config["telegram_token"], config["telegram_chat_id"], dashboard_text,
        config["kvdb_bucket"], key=f"dashboard_{symbol}"
    )

    # --- Order Dashboard: อัพเดทสถานะออเดอร์เก่า (running/win/loss) แล้วส่งแยกจาก Dashboard หลัก ---
    current_price = df["close"].iloc[-1]
    orders = update_orders_status(config["kvdb_bucket"], symbol, current_price)
    orders_dashboard_text = build_orders_dashboard(symbol, orders, current_price)
    send_or_edit_message(
        config["telegram_token"], config["telegram_chat_id"], orders_dashboard_text,
        config["kvdb_bucket"], key=f"orders_dashboard_{symbol}"
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

                # ดึงเฟรม 4H มาคำนวณ Daily Bias — ชั้น bias สูงสุด เหนือกว่า 1H
                df_4h = fetch_twelvedata(
                    symbol=td_symbol, interval="4h", outputsize=300,
                    api_key=CONFIG["twelvedata_api_key"]
                )
            except Exception as e:
                print(f"[Data Error] {display_symbol}: {e}")
                continue  # ข้ามคู่เงินนี้ไป แต่คู่อื่น/ping ยังทำงานต่อได้

            df_1h_ind = add_indicators(df_1h, CONFIG)
            structure_1h = analyze_structure(df_1h_ind, CONFIG)
            higher_tf_trend = structure_1h["trend"]

            daily_bias = calc_daily_bias(df_4h, CONFIG)

            run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000,
                         higher_tf_trend=higher_tf_trend, session_info=session_info,
                         daily_bias=daily_bias)

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
