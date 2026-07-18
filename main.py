import requests
import json
import pandas as pd
import numpy as np


from datetime import datetime, timezone, timedelta

from config import CONFIG
from indicator import add_indicators, is_atr_contracting
from trend import analyze_structure
from entry import evaluate_entry, get_entry_zone_bounds
from risk import calc_stop_loss, calc_position_size
from tp import calc_take_profits, calc_risk_reward
from score import calc_confidence_score
from report import print_report
from notify import send_telegram_alert, format_alert_message, send_or_edit_message, send_telegram_photo
from chart import build_entry_chart
from scenario import (
    build_hourly_briefing, build_pullback_plan,
    detect_breakout_trigger, detect_counter_trend_trigger,
)
from dashboard import build_dashboard_message
from session import get_session_info
from bias_4h import analyze_4h_bias, is_bias_aligned
from trigger_5m import find_5m_trigger
from kvstore import kv_get, kv_set
from orders import add_order, update_orders_status, build_orders_dashboard
from telegram_bot import handle_telegram_commands
from news_scheduler import (
    refresh_daily_calendar, build_daily_summary_message, check_and_send_pre_news_warning,
    check_and_send_post_news_result, is_in_news_blackout,
)


def _current_hour_key():
    """คืนค่า string ระบุ 'ชั่วโมงปัจจุบัน' แบบ UTC เช่น '2026-07-08-14' ใช้เทียบว่าข้ามชั่วโมงหรือยัง (สำหรับ Hourly Briefing)"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")


def _htf_cache_bucket_key():
    """
    คืนค่า string ระบุ 'ช่วง 30 นาทีปัจจุบัน' แบบ UTC เช่น '2026-07-08-14-00' หรือ '...-14-30'
    ใช้แยกกับ _current_hour_key() เพราะ cache ของ 1H/4H ควรสดกว่ารอบส่ง Hourly Briefing
    (ตัวกรองสัญญาณควรสดที่สุดเท่าที่ทำได้ ส่วน briefing แค่รายงานผล ไม่จำเป็นต้องถี่ตาม)
    """
    now = datetime.now(timezone.utc)
    bucket_minute = 0 if now.minute < 30 else 30
    return now.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%Y-%m-%d-%H-%M")


def get_cached_htf_context(kvdb_bucket, symbol):
    """
    บอทวิเคราะห์ทุก 5 นาที แต่เทรนด์ 1H และ Bias 4H ไม่จำเป็นต้องดึงข้อมูลใหม่ทุกรอบ
    ฟังก์ชันนี้จะอ่านค่าที่ cache ไว้จาก kvdb ถ้ายังอยู่ใน "ช่วง 30 นาทีเดียวกัน" กับตอนนี้ (ประหยัด API quota)
    คืนค่า (higher_tf_trend, bias_4h) หรือ (None, None) ถ้ายังไม่มี cache/cache หมดอายุแล้ว
    """
    raw = kv_get(kvdb_bucket, f"htf_ctx_{symbol}")
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except Exception:
        return None, None
    if data.get("bucket") != _htf_cache_bucket_key():
        return None, None
    return data.get("higher_tf_trend"), data.get("bias_4h")


def set_cached_htf_context(kvdb_bucket, symbol, higher_tf_trend, bias_4h):
    """บันทึกผล 1H trend + 4H bias ที่เพิ่งคำนวณลง kvdb ให้รอบ 5 นาทีถัดไปในช่วง 30 นาทีเดียวกันใช้ซ้ำได้"""
    payload = json.dumps(
        {"bucket": _htf_cache_bucket_key(), "higher_tf_trend": higher_tf_trend, "bias_4h": bias_4h},
        default=float,
    )
    kv_set(kvdb_bucket, f"htf_ctx_{symbol}", payload)


def should_send_hourly_briefing(kvdb_bucket, symbol):
    """เช็คว่าชั่วโมงนี้เคยส่ง Hourly Briefing ไปแล้วหรือยัง (กันส่งซ้ำตอนวิเคราะห์ทุก 5 นาที)"""
    last_sent_hour = kv_get(kvdb_bucket, f"briefing_hour_{symbol}")
    return last_sent_hour != _current_hour_key()


def mark_hourly_briefing_sent(kvdb_bucket, symbol):
    kv_set(kvdb_bucket, f"briefing_hour_{symbol}", _current_hour_key())


def should_send_daily_summary(kvdb_bucket, symbol):
    """
    เช็คว่าถึงเวลาสรุปผลประจำวันหรือยัง (เวลาไทย 23:55-23:59 — ใกล้เที่ยงคืนที่สุดในกรอบ cron ทุก 5 นาที
    เพราะไม่มีทางชนเวลา 23:59:00 เป๊ะๆ ได้ ใช้หน้าต่าง 5 นาทีสุดท้ายของวันแทน)
    """
    now = datetime.now(timezone(timedelta(hours=7)))
    if not (now.hour == 23 and now.minute >= 55):
        return False
    today_str = now.strftime("%Y-%m-%d")
    last_sent = kv_get(kvdb_bucket, f"daily_summary_date_{symbol}")
    return last_sent != today_str


def mark_daily_summary_sent(kvdb_bucket, symbol):
    today_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")
    kv_set(kvdb_bucket, f"daily_summary_date_{symbol}", today_str)


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

    # --- Bias เฟรม 4H (เทรนด์ใหญ่สุด + โซน Premium/Discount) — ชั้นบนสุดของ MTF ---
    # หมายเหตุ: ไม่ veto การเข้าไม้แล้ว (เปลี่ยนจาก hard filter เป็นส่วนหนึ่งของ confidence score
    # ใน score.py แทน) เพราะเดิมถ้าสวนทางจะตัด entry_signal["valid"] เป็น False ตั้งแต่จุดนี้
    # ทำให้ calc_confidence_score ด้านล่างไม่ถูกเรียกเลย (สกอร์ค้างที่ 0 เสมอ) ทั้งที่สัญญาณ 15M/5M
    # อาจแข็งแรงมาก (เช่น ADX สูง, structure ชัดเจน) เพียงแค่สวนทางกับ 4H เท่านั้น
    if config.get("bias4h_filter_enabled", True) and bias_4h:
        aligned, reason = is_bias_aligned(entry_signal["direction"], bias_4h, config)
        if not aligned:
            entry_signal["reasons"].append(f"[4H Bias] {reason} (ไม่ตัดสิทธิ์ แต่จะไม่ได้แต้ม bias4h_alignment)")
        else:
            zone_label = {"premium": "Premium", "discount": "Discount", "equilibrium": "Equilibrium"}.get(
                bias_4h.get("zone"), "-"
            )
            entry_signal["reasons"].append(
                f"[4H Bias] เทรนด์ 4H: {bias_4h.get('trend')} | โซนราคา: {zone_label} — สอดคล้องกับสัญญาณ 15M"
            )

    # --- เทรนด์เฟรมใหญ่ (1H) — เช่นเดียวกับ 4H ไม่ veto แล้ว แค่มีผลต่อคะแนนใน score.py ---
    if higher_tf_trend not in (None, "sideway"):
        if entry_signal["direction"] != higher_tf_trend:
            entry_signal["reasons"].append(
                f"สัญญาณ 15m เป็น {entry_signal['direction']} แต่เทรนด์ 1H เป็น {higher_tf_trend} "
                f"(สวนทางกัน) — ไม่ได้แต้ม htf_1h_alignment"
            )
        else:
            entry_signal["reasons"].append(
                f"เทรนด์ 1H เป็น {higher_tf_trend} สอดคล้องกับสัญญาณ 15m"
            )

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
        # ใช้ ATR เฉลี่ยย้อนหลัง (ไม่ใช่แท่งล่าสุดเป๊ะๆ) กัน SL แคบผิดปกติตอนตลาดหดตัวชั่วคราว
        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = df["atr"].tail(atr_period).mean() if "atr" in df.columns and len(df) else 0
        stop_loss = calc_stop_loss(entry_signal, current_atr, config)
        take_profits = calc_take_profits(entry_signal["entry_price"], stop_loss,
                                          entry_signal["direction"], config)
        rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
              for name, price in take_profits.items()}
        position = calc_position_size(account_balance, entry_signal["entry_price"], stop_loss, config)
        confidence = calc_confidence_score(entry_signal, structure, df, config, rr["TP1"],
                                            bias_4h=bias_4h, higher_tf_trend=higher_tf_trend)

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
                    # ยืนยันจริงแล้ว -> เคลียร์ pending-marker เดิมทิ้ง กันไม่ให้ค้างไปงงตอน setup รอบหน้า
                    kv_set(config["kvdb_bucket"], f"pending_plan1_{symbol}_{entry_signal['direction']}", "")

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
                    # SL ที่ tighten แล้วก็ยังต้องไม่แคบกว่าขั้นต่ำที่ตั้งไว้ (min_sl_distance) เหมือนกัน
                    min_distance = config.get("min_sl_distance", 0)
                    tightened_distance = abs(entry_signal["entry_price"] - tightened_sl)
                    if min_distance and tightened_distance < min_distance:
                        tightened_sl = (
                            entry_signal["entry_price"] - min_distance if entry_signal["direction"] == "bullish"
                            else entry_signal["entry_price"] + min_distance
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

                    # --- แจ้งเตือนล่วงหน้า (Pending Order): setup ผ่านเกณฑ์ทุกข้อแล้ว รอแค่ 5M ยืนยัน ---
                    # ให้ผู้ใช้ตั้ง Pending Order รอไว้ที่โซนนี้ได้เลย โดยไม่ต้องรอ Alert เต็มรูปแบบ
                    # กันสแปม: เทียบกับ entry_price ที่เคยแจ้งไปล่าสุด ถ้ายังเป็นโซนเดิม (ใกล้เคียงกัน) จะไม่แจ้งซ้ำ
                    try:
                        pending_key = f"pending_plan1_{symbol}_{entry_signal['direction']}"
                        stale_threshold_pending = (2 * current_atr) if current_atr else config.get("min_sl_distance", 10.0)
                        raw_pending = kv_get(config["kvdb_bucket"], pending_key)
                        already_notified = False
                        if raw_pending:
                            try:
                                prev = json.loads(raw_pending)
                                if abs(prev.get("entry_price", 0) - entry_signal["entry_price"]) < stale_threshold_pending:
                                    already_notified = True
                            except Exception:
                                pass

                        if not already_notified:
                            zone = get_entry_zone_bounds(entry_signal)
                            direction_th = "LONG (ซื้อ)" if entry_signal["direction"] == "bullish" else "SHORT (ขาย)"
                            pending_msg = (
                                f"🕒 <b>เตรียมตั้ง Pending Order — {symbol}</b>\n"
                                f"ทิศทาง: {direction_th} | Score: {confidence['score']}/100 (ผ่านเกณฑ์แล้ว)\n"
                                f"Entry (คาดการณ์): {entry_signal['entry_price']:.4f}\n"
                                f"โซนรอเข้า: {zone['bottom']:.4f} - {zone['top']:.4f}\n"
                                f"SL (คาดการณ์): {stop_loss:.4f}\n\n"
                                "หมายเหตุ: setup นี้ผ่านทุกฟิลเตอร์ (4H/1H/ADX/Session/Score) แล้ว "
                                "เหลือแค่รอราคาวิ่งมาแตะโซนแล้วมี reaction กลับตัวจริงบน 5M เพื่อยืนยัน "
                                "ถ้าตั้ง Pending Order ไว้ล่วงหน้าได้ ให้ตั้งตามโซนนี้ แต่ระวัง: ราคาอาจขยับก่อนถึงเวลาจริง "
                                "และ SL/TP ที่ยืนยันจะมาอีกทีตอน Alert เต็มรูปแบบ (เมื่อ 5M trigger ยืนยันแล้ว)"
                            )
                            if config.get("push_notifications_enabled", True):
                                pending_targets = [config["telegram_chat_id"]]
                                if config.get("telegram_group_chat_id"):
                                    pending_targets.append(config["telegram_group_chat_id"])
                                for target_chat_id in pending_targets:
                                    send_telegram_alert(config["telegram_token"], target_chat_id, pending_msg)

                            kv_set(config["kvdb_bucket"], pending_key,
                                   json.dumps({"entry_price": entry_signal["entry_price"]}))
                    except Exception as e:
                        print(f"[Pending Order Notice Error] {e}")
            else:
                entry_signal["reasons"].append(
                    "[5M Trigger] ไม่มีโซน OB/FVG/Structure จาก 15M ให้ตรวจสอบ reaction"
                )

    print_report(symbol, timeframe, structure, entry_signal, stop_loss,
                 take_profits, position, rr, confidence, config)

    # --- ระงับ Alert ชั่วคราวถ้าอยู่ในช่วงห้ามเทรดรอบข่าวสำคัญ (±60 นาที) ---
    if entry_signal.get("alert_ready"):
        try:
            in_blackout, blackout_event = is_in_news_blackout(config["kvdb_bucket"], symbol)
        except Exception:
            in_blackout, blackout_event = False, None
        if in_blackout:
            entry_signal["reasons"].append(
                f"อยู่ในช่วงห้ามเทรดรอบข่าว \"{blackout_event['title']}\" (±60 นาที) — ระงับ Alert ชั่วคราว"
            )
            entry_signal["alert_ready"] = False

    # --- ส่ง Telegram Alert เมื่อ signal ผ่านเกณฑ์กฎหลัก "และ" ผ่านเกณฑ์คะแนนขั้นต่ำ ---
    if entry_signal.get("alert_ready"):
        if config.get("push_notifications_enabled", True):
            # threshold ไว้บอกว่า "ห่างจาก Entry เท่าไหร่ถึงถือว่าสัญญาณหมดอายุ" ใช้ 2x ATR เฉลี่ย
            # (ตัวเดียวกับที่ใช้คำนวณ SL) หรือ fallback เป็น min_sl_distance ถ้า ATR ใช้ไม่ได้
            stale_threshold = (2 * current_atr) if current_atr else config.get("min_sl_distance", 10.0)
            msg = format_alert_message(symbol, timeframe, structure, entry_signal,
                                        stop_loss, take_profits, rr, confidence, bias_4h=bias_4h,
                                        current_price=df["close"].iloc[-1], stale_threshold=stale_threshold)

            # ปลายทางที่จะส่ง Alert: แชทเดิมเสมอ + กลุ่ม (ถ้าตั้งค่า telegram_group_chat_id ไว้)
            alert_targets = [config["telegram_chat_id"]]
            if config.get("telegram_group_chat_id"):
                alert_targets.append(config["telegram_group_chat_id"])

            # แนบกราฟราคาไปด้วย (วาดจากข้อมูลที่ดึงมาอยู่แล้ว ไม่ต้องเรียก API เพิ่ม)
            # ถ้าวาดรูปหรือส่งรูปพลาดด้วยเหตุผลใดก็ตาม ให้ fallback ไปส่งเป็นข้อความล้วนแทน กันไม่ให้ alert หายไปเฉยๆ
            chart_path = None
            try:
                chart_path = build_entry_chart(
                    df, entry_signal, structure, out_path=f"/tmp/mariposa_chart_{symbol}.png"
                )
            except Exception as e:
                print(f"[Chart Error] {e}")

            for target_chat_id in alert_targets:
                sent = False
                if chart_path:
                    sent = send_telegram_photo(config["telegram_token"], target_chat_id, chart_path, caption=msg)
                if not sent:
                    sent = send_telegram_alert(config["telegram_token"], target_chat_id, msg)
                print(f"[Telegram -> {target_chat_id}] ส่งแจ้งเตือนสำเร็จ" if sent else f"[Telegram -> {target_chat_id}] ส่งแจ้งเตือนล้มเหลว")

        # --- บันทึกออเดอร์ไว้ให้ /summary และสรุปรายวันเช็คผล TP/SL ย้อนหลังได้ (ทำเสมอ ไม่ว่าจะ push หรือไม่) ---
        try:
            add_order(config["kvdb_bucket"], symbol, entry_signal["direction"],
                      entry_signal["entry_price"], stop_loss, take_profits, confidence["score"])
        except Exception as e:
            print(f"[Order Tracking Error] {e}")

    # --- Dashboard: ส่งทุกรอบ แบบแก้ทับข้อความเดิม (ไม่สแปมแชท) — ข้ามถ้าปิด push ไว้ (ดูผ่าน /status แทน) ---
    if config.get("push_notifications_enabled", True):
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
                # 15M และ 5M ต้องสดทุกรอบ เพราะเป็นเฟรมที่ใช้หาโซนเข้าไม้ + trigger จริง
                df = fetch_twelvedata(
                    symbol=td_symbol, interval="15min", outputsize=300,
                    api_key=CONFIG["twelvedata_api_key"]
                )
                df_5m = fetch_twelvedata(
                    symbol=td_symbol, interval="5min", outputsize=200,
                    api_key=CONFIG["twelvedata_api_key"]
                )

                session_info = get_session_info(CONFIG)

                # เทรนด์ 1H + Bias 4H ไม่เปลี่ยนทุก 5 นาที -> ใช้ cache ในชั่วโมงเดียวกัน ประหยัด API quota
                higher_tf_trend, bias_4h = get_cached_htf_context(CONFIG["kvdb_bucket"], display_symbol)
                if higher_tf_trend is None or bias_4h is None:
                    df_1h = fetch_twelvedata(
                        symbol=td_symbol, interval="1h", outputsize=300,
                        api_key=CONFIG["twelvedata_api_key"]
                    )
                    df_4h = fetch_twelvedata(
                        symbol=td_symbol, interval="4h", outputsize=300,
                        api_key=CONFIG["twelvedata_api_key"]
                    )

                    df_1h_ind = add_indicators(df_1h, CONFIG)
                    structure_1h = analyze_structure(df_1h_ind, CONFIG)
                    higher_tf_trend = structure_1h["trend"]

                    df_4h_ind = add_indicators(df_4h, CONFIG)
                    bias_4h = analyze_4h_bias(df_4h_ind, CONFIG)

                    set_cached_htf_context(CONFIG["kvdb_bucket"], display_symbol, higher_tf_trend, bias_4h)
            except Exception as e:
                print(f"[Data Error] {display_symbol}: {e}")
                continue  # ข้ามคู่เงินนี้ไป แต่คู่อื่น/ping ยังทำงานต่อได้

            # รัน pipeline ทุกรอบ (ทุก 5 นาที) — ถ้าเจอจังหวะเข้าไม้ที่ผ่านเกณฑ์ จะยิง Telegram Alert ทันที
            # ส่วน Dashboard จะถูก edit ทับข้อความเดิมเสมอ ไม่สร้างข้อความใหม่ ไม่สแปมแชท
            run_pipeline(df, symbol=display_symbol, timeframe="15m", account_balance=1000,
                         higher_tf_trend=higher_tf_trend, session_info=session_info,
                         bias_4h=bias_4h, df_5m=df_5m)

            # --- เช็คราคาปัจจุบันเทียบ SL/TP1 ของออเดอร์ที่ยัง 'running' ทุกรอบ (ให้ /summary ข้อมูลสด) ---
            try:
                update_orders_status(CONFIG["kvdb_bucket"], display_symbol, df["close"].iloc[-1])
            except Exception as e:
                print(f"[Order Status Update Error] {display_symbol}: {e}")

            # --- Plan 2/3 จาก Hourly Briefing (Breakout / สวนเทรนด์): เช็คทุกรอบว่า "เข้าออเดอร์จริง" หรือยัง ---
            # ไม่ใช่แค่ข้อความในบรีฟฟิ่งเฉยๆ แล้ว ถ้าทริกเกอร์จริงจะยิง Telegram (แชทเดิม + กลุ่ม) พร้อมหมายเหตุ
            #
            # กันสแปมแบบ "state-based" แทน cooldown ตามเวลา: เดิมใช้ is_in_cooldown/mark_alert_sent (ตามนาที)
            # แต่พบว่าพอเวลาผ่านไปเกิน cooldown มันเตือนซ้ำระดับ Breakout เดิมที่ยังไม่มีสวิงใหม่เกิดขึ้นจริง
            # (โมเมนตัมจบไปแล้ว แค่ยังไม่มีสวิงไฮ/โลว์ใหม่ให้ระบบอ้างอิง) ตอนนี้เปลี่ยนมา dedup ตาม "เงื่อนไขจริง"
            # แทนเวลา: Plan 2 จะแจ้งซ้ำก็ต่อเมื่อสวิงไฮ/โลว์ที่ทะลุเปลี่ยนเป็นระดับใหม่เท่านั้น, Plan 3 จะเงียบไปจนกว่า
            # checklist จะหลุด (ไม่ครบ 3/3) แล้วกลับมาครบใหม่อีกครั้ง (rising-edge) ไม่ใช่แจ้งซ้ำทุกช่วงเวลาที่ตั้งไว้
            try:
                df_ind_plan = add_indicators(df, CONFIG)
                structure_plan = analyze_structure(df_ind_plan, CONFIG)

                plan_triggers = [
                    ("plan2_breakout", "Breakout (แผนที่ 2)",
                     detect_breakout_trigger(df_ind_plan, structure_plan, CONFIG),
                     "ราคาทะลุระดับ {level:.4f} แรงๆ ที่ราคา {price:.4f}"),
                    ("plan3_counter_trend", "สวนเทรนด์ (แผนที่ 3)",
                     detect_counter_trend_trigger(df_ind_plan, structure_plan),
                     "Checklist สวนเทรนด์ผ่านครบ 3/3 ข้อแล้ว"),
                ]

                # เช็คครั้งเดียวก่อนเข้าลูป ใช้ร่วมกันทั้ง Plan 2/3 (ข่าวเดียวกัน ไม่ต้องเช็คซ้ำต่อแผน)
                plan_blackout, plan_blackout_event = is_in_news_blackout(CONFIG["kvdb_bucket"], display_symbol)

                for plan_key, plan_label, trigger, detail_template in plan_triggers:
                    state_key = f"plan_state_{display_symbol}_{plan_key}"

                    if not trigger:
                        # เงื่อนไขไม่ตรงแล้วในรอบนี้ (breakout ยังไม่มีสวิงใหม่ / checklist หลุดจาก 3/3)
                        # เคลียร์ state ทิ้ง รอบหน้าถ้ากลับมาเป็นจริงใหม่จะได้แจ้งเตือนสดอีกครั้ง (ไม่ใช่ของค้าง)
                        kv_set(CONFIG["kvdb_bucket"], state_key, "")
                        continue

                    if plan_blackout:
                        # อยู่ในช่วงห้ามเทรดรอบข่าว -> ข้ามไปเงียบๆ ไม่ mark state (กันไม่ให้พอข่าวผ่านไปแล้ว
                        # เงื่อนไขเดิมยังจริงอยู่ แต่ถูก dedup ทิ้งเพราะเข้าใจผิดว่าเคยแจ้งไปแล้วตอนที่จริงแค่ถูกระงับ)
                        continue

                    if plan_key == "plan2_breakout":
                        # dedup ตาม "ระดับที่ทะลุ" ไม่ใช่เวลา — แจ้งซ้ำก็ต่อเมื่อมีสวิงไฮ/โลว์ใหม่จริงๆ เท่านั้น
                        dedup_value = f"{trigger['direction']}:{trigger['level']:.4f}"
                    else:
                        # plan3: ตราบใด trigger ไม่ None แปลว่า checklist ครบ 3/3 อยู่แล้วเสมอ (เงื่อนไขตายตัว)
                        # dedup แค่ทิศทาง เพื่อกันไม่ให้แจ้งซ้ำขณะเงื่อนไขยังเป็นจริงต่อเนื่องรอบต่อรอบ
                        dedup_value = trigger["direction"]

                    prev_value = kv_get(CONFIG["kvdb_bucket"], state_key)
                    if prev_value == dedup_value:
                        continue  # เงื่อนไขเดิมที่เคยแจ้งไปแล้ว ไม่แจ้งซ้ำ

                    direction_th = "LONG (ซื้อ)" if trigger["direction"] == "bullish" else "SHORT (ขาย)"
                    detail = detail_template.format(**trigger) if "{" in detail_template else detail_template
                    plan_msg = (
                        f"🚨 <b>ออเดอร์เข้า — {plan_label}</b>\n"
                        f"Symbol: {display_symbol} | ทิศทาง: {direction_th}\n"
                        f"{detail}\n\n"
                        "หมายเหตุ: สัญญาณนี้มาจาก Plan เสริมใน Hourly Briefing ไม่ใช่ระบบ Scoring หลัก "
                        "ไม่ได้ผ่านฟิลเตอร์ 4H Bias/1H Trend/Session เหมือนสัญญาณเข้าเทรดปกติ (Plan 1) "
                        "ควรพิจารณาความเสี่ยงเพิ่มเติมเอง หรือลดขนาดไม้ก่อนเข้า"
                    )

                    if CONFIG.get("push_notifications_enabled", True):
                        plan_alert_targets = [CONFIG["telegram_chat_id"]]
                        if CONFIG.get("telegram_group_chat_id"):
                            plan_alert_targets.append(CONFIG["telegram_group_chat_id"])
                        for target_chat_id in plan_alert_targets:
                            send_telegram_alert(CONFIG["telegram_token"], target_chat_id, plan_msg)

                    kv_set(CONFIG["kvdb_bucket"], state_key, dedup_value)
            except Exception as e:
                print(f"[Plan 2/3 Trigger Error] {display_symbol}: {e}")

            # --- ตอบคำสั่ง Telegram (/order /trend /news /status /summary) ถ้ามีพิมพ์เข้ามารอบนี้ ---
            # แยก try/except ของตัวเอง ไม่พึ่งพาตัวแปรจากบล็อก Plan 2/3 ด้านบน กันกรณีบล็อกนั้น error
            # กลางทางแล้วตัวแปรไม่ครบ (self-contained คำนวณใหม่เองเบาๆ ไม่มี API call เพิ่ม)
            try:
                df_ind_cmd = add_indicators(df, CONFIG)
                structure_cmd = analyze_structure(df_ind_cmd, CONFIG)
                entry_signal_cmd = evaluate_entry(df_ind_cmd, structure_cmd, CONFIG)
                news_blackout_cmd = is_in_news_blackout(CONFIG["kvdb_bucket"], display_symbol)

                cmd_ctx = {
                    "symbol": display_symbol,
                    "config": CONFIG,
                    "df_ind": df_ind_cmd,
                    "structure": structure_cmd,
                    "entry_signal": entry_signal_cmd,
                    "bias_4h": bias_4h,
                    "session_info": session_info,
                    "news_blackout": news_blackout_cmd,
                }
                handle_telegram_commands(CONFIG, cmd_ctx)
            except Exception as e:
                print(f"[Telegram Command Error] {display_symbol}: {e}")

            # --- ข่าว/ปฏิทินเศรษฐกิจ: แค่ข้อมูลประกอบการตัดสินใจ ไม่ยุ่งกับ entry logic ---
            # ห่อทั้งก้อนด้วย try/except เพราะเป็น 3rd-party ฟรี ไม่มี SLA ถ้าพังไม่ให้กระทบบอทหลัก
            try:
                # refresh cache เสมอ (ไม่ว่าจะปิด push หรือไม่) เพราะ /order /status ยังต้องใช้เช็คช่วงห้ามเทรดรอบข่าว
                new_events, new_headlines = refresh_daily_calendar(CONFIG["kvdb_bucket"], display_symbol)

                if CONFIG.get("push_notifications_enabled", True):
                    if new_events is not None:  # เพิ่งขึ้นวันใหม่ (เวลาไทย) -> สรุปข่าวทั้งวันครั้งเดียว
                        summary_text = build_daily_summary_message(display_symbol, new_events, new_headlines)
                        send_or_edit_message(
                            CONFIG["telegram_token"], CONFIG["telegram_chat_id"], summary_text,
                            CONFIG["kvdb_bucket"], key=f"news_summary_{display_symbol}"
                        )
                        # ส่งเข้ากลุ่มด้วย ถ้าตั้งค่าไว้ (แต่ก่อนหน้านี้ตกหล่นไป ทำให้ข่าวไม่เด้งในกลุ่มเลย)
                        if CONFIG.get("telegram_group_chat_id"):
                            send_or_edit_message(
                                CONFIG["telegram_token"], CONFIG["telegram_group_chat_id"], summary_text,
                                CONFIG["kvdb_bucket"], key=f"news_summary_{display_symbol}_group"
                            )

                    # หมายเหตุ: check_and_send_pre_news_warning/post_news_result มาร์ค kvdb ว่า "เตือน/รายงานแล้ว"
                    # ทันทีที่เรียก ถ้าปิด push ไว้จะไม่เรียกเลย กันไม่ให้เสียโอกาสแจ้งเตือนไปฟรีๆ ตอนไม่ได้ส่งจริง
                    # (เผื่อวันหลังเปิด push กลับมา จะได้ยังเตือน/รายงานข่าวตัวเดิมได้อยู่)
                    warning_text = check_and_send_pre_news_warning(CONFIG["kvdb_bucket"], display_symbol)
                    if warning_text:
                        news_targets = [CONFIG["telegram_chat_id"]]
                        if CONFIG.get("telegram_group_chat_id"):
                            news_targets.append(CONFIG["telegram_group_chat_id"])
                        for target_chat_id in news_targets:
                            send_telegram_alert(CONFIG["telegram_token"], target_chat_id, warning_text)

                    # --- ผลข่าวหลังประกาศจริง (actual vs forecast) ---
                    result_text = check_and_send_post_news_result(CONFIG["kvdb_bucket"], display_symbol)
                    if result_text:
                        result_targets = [CONFIG["telegram_chat_id"]]
                        if CONFIG.get("telegram_group_chat_id"):
                            result_targets.append(CONFIG["telegram_group_chat_id"])
                        for target_chat_id in result_targets:
                            send_telegram_alert(CONFIG["telegram_token"], target_chat_id, result_text)
            except Exception as e:
                print(f"[News Scheduler Error] {e}")

            # ส่ง Hourly Briefing (สถานะปกติ) แค่ครั้งเดียวต่อชั่วโมง แม้จะวิเคราะห์ทุก 5 นาทีก็ตาม
            # ข้ามทั้งบล็อกถ้าปิด push ไว้ (ใช้ /trend หรือ /order แทนได้)
            if CONFIG.get("push_notifications_enabled", True) and should_send_hourly_briefing(
                CONFIG["kvdb_bucket"], display_symbol
            ):
                df_ind = add_indicators(df, CONFIG)
                structure = analyze_structure(df_ind, CONFIG)
                entry_signal = evaluate_entry(df_ind, structure, CONFIG)
                briefing_text = build_hourly_briefing(display_symbol, "15m", df_ind, structure, entry_signal, CONFIG)
                sent = send_or_edit_message(
                    CONFIG["telegram_token"], CONFIG["telegram_chat_id"], briefing_text,
                    CONFIG["kvdb_bucket"], key=f"briefing_{display_symbol}"
                )
                if sent:
                    mark_hourly_briefing_sent(CONFIG["kvdb_bucket"], display_symbol)

                # ส่งเฉพาะ "แผนที่ 1" (รอ Pullback) เข้ากลุ่มด้วย ตามที่ขอเพิ่ม
                # ไม่ส่ง Dashboard/Briefing เต็มรูปแบบเข้ากลุ่ม (ยังคงหลักการเดิม: กลุ่มได้แค่ข้อมูล actionable)
                # แต่ Plan 1 เป็นแผนหลักที่มักใช้ตัดสินใจตั้ง Pending Order ไว้ล่วงหน้า เลยควรเห็นในกลุ่มด้วย
                # ใช้ send_or_edit_message เหมือนกัน (edit ทับข้อความเดิมทุกชั่วโมง ไม่สแปมแชทกลุ่ม)
                if CONFIG.get("telegram_group_chat_id"):
                    plan1_text = build_pullback_plan(df_ind, structure, entry_signal, CONFIG)
                    group_plan1_msg = (
                        f"📋 <b>แผนที่ 1 — รอ Pullback ตามเทรนด์ ({display_symbol})</b>\n\n{plan1_text}"
                    )
                    send_or_edit_message(
                        CONFIG["telegram_token"], CONFIG["telegram_group_chat_id"], group_plan1_msg,
                        CONFIG["kvdb_bucket"], key=f"briefing_plan1_{display_symbol}_group"
                    )

            # --- สรุปผลประกอบการประจำวัน (23:55-23:59 เวลาไทย) ครั้งเดียวต่อวัน — ข้ามถ้าปิด push (ใช้ /summary แทน) ---
            try:
                if CONFIG.get("push_notifications_enabled", True) and should_send_daily_summary(
                    CONFIG["kvdb_bucket"], display_symbol
                ):
                    daily_orders = update_orders_status(CONFIG["kvdb_bucket"], display_symbol, df["close"].iloc[-1])
                    daily_text = build_orders_dashboard(display_symbol, daily_orders, df["close"].iloc[-1])
                    daily_text = daily_text.replace(
                        "📋 <b>Order Dashboard:", "📆 <b>สรุปผลประกอบการวันนี้:"
                    )
                    daily_targets = [CONFIG["telegram_chat_id"]]
                    if CONFIG.get("telegram_group_chat_id"):
                        daily_targets.append(CONFIG["telegram_group_chat_id"])
                    for target_chat_id in daily_targets:
                        send_telegram_alert(CONFIG["telegram_token"], target_chat_id, daily_text)
                    mark_daily_summary_sent(CONFIG["kvdb_bucket"], display_symbol)
            except Exception as e:
                print(f"[Daily Summary Error] {display_symbol}: {e}")
    finally:
        # ping บอก Healthchecks.io เสมอ ไม่ว่าข้างบนจะสำเร็จหรือมี error ก็ตาม
        # (นี่คือหน้าที่จริงของ Dead Man's Switch — ต้องรู้ว่าบอทยังไม่ตายแม้ตอน API ล่ม)
        ping_healthcheck(CONFIG["healthchecks_url"])
