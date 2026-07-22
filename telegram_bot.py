"""
telegram_bot.py
ระบบรับคำสั่งจาก Telegram (Interactive Commands) เพิ่มเติมจากที่บอทส่งแจ้งเตือนอัตโนมัติอยู่แล้ว
รองรับ: /order /order1 /order2 /order3 /trend /news /status /summary /stats /confirm1 /confirm2 /confirm3
(/order, /order1-3 บันทึกลง Order Dashboard ให้อัตโนมัติทันทีที่เจอจุดเข้าอยู่แล้ว ส่วน /confirm1-3
ใช้กดยืนยัน/บันทึกซ้ำแบบเจาะจงเองได้อีกที มีระบบกันบันทึกซ้ำร่วมกันทั้งหมด ไม่ทำให้ข้อมูลซ้ำซ้อน)

ข้อจำกัดสำคัญที่ควรรู้ก่อนใช้: บอทนี้รันบน GitHub Actions แบบ cron (ไม่ใช่ server ที่ฟังตลอดเวลา)
คำสั่งที่พิมพ์จะถูกประมวลผล "ตอนที่บอทรันรอบถัดไป" เท่านั้น ไม่ใช่ตอบทันที ถ้า cron ตั้งไว้ทุก 5 นาที
การตอบสนองจะช้าสุดประมาณ 5 นาที ไม่ใช่ real-time เป๊ะๆ — ถ้าต้องการตอบทันทีจริงต้องเปลี่ยนไปรันบน
server ที่ฟัง webhook ตลอดเวลาแทน (คนละสถาปัตยกรรมกับที่ใช้อยู่ตอนนี้)

ความปลอดภัย: ประมวลผลคำสั่งจาก TELEGRAM_OWNER_ID (เจ้าของบอท ใช้ได้ทุกที่) และจากใครก็ตามที่พิมพ์
มาจากกลุ่มที่ตั้งไว้ใน TELEGRAM_GROUP_CHAT_ID เท่านั้น (ดู config.py) คนนอกเหนือจากนี้พิมพ์คำสั่ง
จะถูกเมินเงียบๆ ไม่มีการตอบกลับใดๆ ทั้งสิ้น
"""

import time
import json
import uuid
import requests
from datetime import datetime, timedelta, timezone

from kvstore import kv_get, kv_set
from orders import (
    load_orders, add_order, update_orders_status, build_orders_dashboard,
    calc_stats, build_stats_message,
)
from news import fetch_usd_calendar_events
from news_scheduler import THAI_TZ, is_in_news_blackout
from scenario import (
    detect_breakout_trigger, detect_counter_trend_trigger,
    calc_breakout_order, calc_counter_trend_order,
    get_breakout_status, get_counter_trend_status,
)
from zones import calc_premium_discount_zone

TREND_LABEL = {"bullish": "ขาขึ้น", "bearish": "ขาลง", "sideway": "Sideway"}
STRENGTH_LABEL = {"strong": "(Strong)", "weak": "(Weak — กำลังก่อตัว)", "none": ""}

# --- Cache ผลลัพธ์ command context ไว้ในหน่วยความจำ กันคนพิมพ์คำสั่งถี่ๆ ยิง TwelveData ซ้ำจนชนโควตา ---
# (เจอจริง: /trend ถูกพิมพ์รัวๆ หลายครั้งติดกัน รวมกับ main.py บน GitHub Actions ที่ใช้ API key เดียวกัน
# ทำให้ชนเพดาน 8 requests/นาทีของแผนฟรี Twelve Data จน error 429 "run out of API credits")
_CONTEXT_CACHE = {}  # symbol -> (fetched_at_epoch, ctx)
_CONTEXT_CACHE_TTL_SECONDS = 120  # ภายใน 2 นาที คำสั่งซ้ำใช้ข้อมูลเดิม ไม่ยิง API ใหม่

# --- Lock กันตอบซ้ำตอน Render zero-downtime deploy (2 instance คาบเกี่ยวกันชั่วขณะ) ---
_INSTANCE_ID = uuid.uuid4().hex[:8]   # ID สุ่มต่อ process กันจำ instance ตัวเองสับสน
LOCK_KEY = "telegram_poll_lock"
LOCK_TTL_SECONDS = 45  # ต้องมากกว่า long-poll timeout (30s) + buffer กันเช็คไม่ทัน

# --- กันไล่ตอบ backlog คำสั่งเก่าตอน instance เพิ่งเริ่ม/resume จาก suspend ---
# ถ้า service ถูก suspend ไว้นาน (เช่นบน Render free tier) offset ใน kvdb จะค้างอยู่ตำแหน่งเดิม
# พอ resume กลับมา getUpdates จะคืนคำสั่งเก่าที่ค้างคิวมาทั้งหมดให้ตอบรัวๆ ทั้งที่ผู้ใช้ไม่ได้พิมพ์อะไรใหม่
# ป้องกันด้วยการเช็ค timestamp ของแต่ละข้อความ (Telegram ให้มาเป็น field "date" หน่วยวินาที)
# ถ้าเก่าเกิน STALE_MESSAGE_SECONDS ให้ข้ามไปเงียบๆ (ยังคง advance offset ปกติ กันไม่ให้ค้างวนซ้ำ)
STALE_MESSAGE_SECONDS = 90  # นานกว่านี้ถือว่า "ตกยุค" ต้องพิมพ์คำสั่งใหม่เอง ไม่ไล่ตอบย้อนหลัง


def _get_cached_bias_4h(config, symbol):
    """
    พยายามยืม Bias 4H ที่ main.py (GitHub Actions cron) cache ไว้ใน kvdb อยู่แล้วก่อน (คนละ process
    แต่ใช้ kvdb bucket เดียวกัน) ประหยัด TwelveData call ไป 1 ครั้งต่อคำสั่ง โดยไม่ต้องดึง 4H เอง
    ไม่เข้มงวดเรื่องความสดเกินไป (แค่ต้องมีข้อมูล) เพราะ main.py รีเฟรชค่านี้ทุก 30 นาทีอยู่แล้ว
    ซึ่งสดพอสำหรับแสดงผลใน /trend
    """
    raw = kv_get(config["kvdb_bucket"], f"htf_ctx_{symbol}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("bias_4h")
    except Exception:
        return None


def _build_command_context(symbol, config):
    """
    ดึงข้อมูลสำหรับตอบคำสั่ง Telegram โดยประหยัด TwelveData quota 2 ทาง:
      1. Cache ผลลัพธ์ทั้งก้อนในหน่วยความจำ (_CONTEXT_CACHE) 120 วิ — คำสั่งถี่ๆ ในช่วงนี้ไม่ยิง API ซ้ำเลย
      2. ยืม Bias 4H จาก kvdb ที่ main.py cache ไว้อยู่แล้ว แทนการดึง 4H เอง (เหลือดึงแค่ 15M ต่อคำสั่ง
         แทนที่จะเป็น 15M+4H เหมือนเดิม — ลดจาก 2 requests/คำสั่งเหลือ 1, และเป็น 0 เวลา cache hit)
    """
    now = time.time()
    cached = _CONTEXT_CACHE.get(symbol)
    if cached and (now - cached[0]) < _CONTEXT_CACHE_TTL_SECONDS:
        return cached[1]

    from fetch_data import fetch_twelvedata
    from indicator import add_indicators
    from trend import analyze_structure
    from entry import evaluate_entry
    from bias_4h import analyze_4h_bias
    from session import get_session_info

    symbol_map = {"XAUUSD": "XAU/USD"}
    td_symbol = symbol_map.get(symbol, symbol)

    df = fetch_twelvedata(symbol=td_symbol, interval="15min", outputsize=300, api_key=config["twelvedata_api_key"])
    df_ind = add_indicators(df, config)
    structure = analyze_structure(df_ind, config)
    entry_signal = evaluate_entry(df_ind, structure, config)

    bias_4h = _get_cached_bias_4h(config, symbol)
    if bias_4h is None:
        # cache ไม่มี/parse ไม่ได้ -> ยอมดึงสดเป็น fallback (ยังถูกกว่าไม่มีข้อมูลเลย)
        df_4h = fetch_twelvedata(symbol=td_symbol, interval="4h", outputsize=300, api_key=config["twelvedata_api_key"])
        df_4h_ind = add_indicators(df_4h, config)
        bias_4h = analyze_4h_bias(df_4h_ind, config)

    ctx = {
        "symbol": symbol,
        "config": config,
        "df_ind": df_ind,
        "structure": structure,
        "entry_signal": entry_signal,
        "bias_4h": bias_4h,
        "session_info": get_session_info(config),
        "news_blackout": is_in_news_blackout(config["kvdb_bucket"], symbol),
    }
    _CONTEXT_CACHE[symbol] = (now, ctx)
    return ctx


def _get_updates(token, offset=None, timeout=5):
    """เรียก Telegram getUpdates เพื่อดึงข้อความ/คำสั่งใหม่ตั้งแต่ offset ที่ให้มา"""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=timeout + 10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        print(f"[Telegram Bot Error] getUpdates ล้มเหลว: {e}")
    return []


def _reply(token, chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[Telegram Bot Error] ส่งข้อความตอบกลับล้มเหลว: {e}")


def _has_similar_running_order(bucket, symbol, plan, direction, entry_price, threshold):
    """เช็คว่ามีออเดอร์ที่ยัง running อยู่ ของ plan/ทิศทางเดียวกัน ราคาใกล้เคียงกัน (ภายใน threshold) แล้วหรือยัง
    ใช้กันไม่ให้ /order, /order1-3 บันทึกออเดอร์ซ้ำถ้าเช็คซ้ำหลายครั้ง หรือของเดิม main.py บันทึก
    อัตโนมัติไปแล้ว (Plan 2/3 ที่ trigger จริงจะถูก main.py บันทึกเองด้วยอยู่แล้ว)"""
    for o in load_orders(bucket, symbol):
        if (o["status"] == "running" and o.get("plan") == plan
                and o["direction"] == direction
                and abs(o["entry_price"] - entry_price) < threshold):
            return True
    return False


def _cmd_order(ctx):
    """
    เช็คทั้ง 3 แผน คืนเฉพาะแผนที่ตอนนี้เข้าเงื่อนไขจริงๆ เท่านั้น ตามที่ระบุไว้ในดีไซน์

    ทั้ง 3 แผนคำนวณ Entry/SL/TP ให้พร้อมตั้ง Limit Order ได้ทันที และ "บันทึกลง Order Dashboard
    ให้อัตโนมัติทันที" ที่เจอจุดเข้า (ไม่ต้องพิมพ์คำสั่งยืนยันแยกต่างหากอีกแล้ว) มีระบบกันบันทึกซ้ำ
    (_has_similar_running_order) ถ้าเช็คซ้ำหลายครั้งขณะจุดเข้ายังไม่เปลี่ยน จะไม่บันทึกซ้ำเข้าไปอีก:
      - แผนที่ 1 (Pullback): สูตรเดียวกับที่ main.py ใช้ส่ง Alert อัตโนมัติจริง (ATR เฉลี่ยย้อนหลัง)
        ถ้ายังไม่ยืนยัน 5M Trigger จะบันทึกแยกเป็น plan "plan1_pullback_early" (คนละกลุ่มกับที่ยืนยัน
        แล้ว "plan1_pullback") เพื่อให้ /stats เทียบได้ว่าเข้าก่อนยืนยันกับรอยืนยันแล้วเข้า อันไหนแม่นกว่า
      - แผนที่ 2 (Breakout) และแผนที่ 3 (สวนเทรนด์): ใช้ calc_breakout_order/calc_counter_trend_order
        จาก scenario.py — จุดเดียวกับที่ main.py ใช้คำนวณตอน trigger จริงเพื่อบันทึกลง Order Dashboard
        (กันตรรกะคำนวณ SL/TP ซ้ำซ้อนสองที่ ถ้าแก้สูตรต้องแก้ที่ scenario.py จุดเดียว ทั้งคู่จะได้ตัวเลข
        ตรงกันเป๊ะเสมอ)
    """
    lines = ["📥 <b>เช็คโอกาสเข้าไม้ตอนนี้</b>", ""]
    found_any = False

    entry_signal = ctx["entry_signal"]
    config = ctx["config"]
    df_ind = ctx["df_ind"]
    structure = ctx["structure"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    if entry_signal.get("valid") and entry_signal.get("direction") == structure["trend"]:
        direction_th = "LONG" if entry_signal["direction"] == "bullish" else "SHORT"
        lines.append(f"✅ แผนที่ 1 (Pullback): {direction_th}")

        try:
            from risk import calc_stop_loss
            from tp import calc_take_profits, calc_risk_reward

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            stop_loss = calc_stop_loss(entry_signal, current_atr, config)
            take_profits = calc_take_profits(
                entry_signal["entry_price"], stop_loss, entry_signal["direction"], config
            )
            rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
                  for name, price in take_profits.items()}

            lines.append(f"   Entry: {entry_signal['entry_price']:.4f}")
            lines.append(f"   SL: {stop_loss:.4f}")
            for name, price in take_profits.items():
                lines.append(f"   {name}: {price:.4f} (RR {rr[name]})")

            confirmed = bool(entry_signal.get("trigger", {}).get("confirmed"))
            plan_key = "plan1_pullback" if confirmed else "plan1_pullback_early"
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, plan_key, entry_signal["direction"],
                                           entry_signal["entry_price"], threshold):
                lines.append("   📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, entry_signal["direction"], entry_signal["entry_price"],
                          stop_loss, take_profits, score=None, plan=plan_key)
                tag = "ยืนยันแล้ว" if confirmed else "เข้าก่อนยืนยัน"
                lines.append(f"   📌 บันทึกลง Order Dashboard แล้ว ({tag})")
        except Exception as e:
            lines.append(f"   (คำนวณ/บันทึก SL/TP ไม่สำเร็จ: {e})")

        if not entry_signal.get("trigger", {}).get("confirmed"):
            lines.append(
                "   ⚠️ ยังไม่ยืนยัน 5M Trigger — ราคาอาจยังไม่กลับตัวจริง เข้าก่อนเวลาอาจโดนสวนได้"
            )
        found_any = True

    breakout = detect_breakout_trigger(df_ind, structure, config)
    if breakout:
        direction_th = "LONG" if breakout["direction"] == "bullish" else "SHORT"
        lines.append(
            f"✅ แผนที่ 2 (Breakout): {direction_th} ทะลุ {breakout['level']:.4f} "
            f"ที่ราคา {breakout['price']:.4f}"
        )
        plan2_order = calc_breakout_order(breakout, structure, df_ind, config)
        if plan2_order:
            lines.append(f"   Entry: {plan2_order['entry_price']:.4f}")
            lines.append(f"   SL: {plan2_order['stop_loss']:.4f}")
            lines.append(f"   TP (Measured move): {plan2_order['take_profit']:.4f} (RR {plan2_order['rr']})")

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, "plan2_breakout", plan2_order["direction"],
                                           plan2_order["entry_price"], threshold):
                lines.append("   📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, plan2_order["direction"], plan2_order["entry_price"],
                          plan2_order["stop_loss"], {"TP1": plan2_order["take_profit"]},
                          score=None, plan="plan2_breakout")
                lines.append("   📌 บันทึกลง Order Dashboard แล้ว")
        else:
            lines.append("   (หาข้อมูล swing ไม่พอสำหรับคำนวณ SL/TP ของแผนนี้)")
        found_any = True

    counter = detect_counter_trend_trigger(df_ind, structure)
    if counter:
        direction_th = "LONG" if counter["direction"] == "bullish" else "SHORT"
        lines.append(f"✅ แผนที่ 3 (สวนเทรนด์): {direction_th} — Checklist ครบ 3/3 ข้อ")
        plan3_order = calc_counter_trend_order(counter, df_ind, config)
        if plan3_order:
            lines.append(f"   Entry: {plan3_order['entry_price']:.4f}")
            lines.append(f"   SL: {plan3_order['stop_loss']:.4f}")
            lines.append(f"   TP (Equilibrium): {plan3_order['take_profit']:.4f} (RR {plan3_order['rr']})")
            lines.append("   ⚠️ แผนสวนเทรนด์เสี่ยงสูงกว่าแผนอื่น ควรลดขนาดไม้")

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, "plan3_counter_trend", plan3_order["direction"],
                                           plan3_order["entry_price"], threshold):
                lines.append("   📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, plan3_order["direction"], plan3_order["entry_price"],
                          plan3_order["stop_loss"], {"TP1": plan3_order["take_profit"]},
                          score=None, plan="plan3_counter_trend")
                lines.append("   📌 บันทึกลง Order Dashboard แล้ว")
        else:
            lines.append("   (คำนวณ SL/TP ของแผนนี้ไม่สำเร็จ)")
        found_any = True

    if ctx.get("news_blackout", (False, None))[0]:
        lines.append("")
        lines.append("⛔ หมายเหตุ: ตอนนี้อยู่ในช่วงห้ามเทรดรอบข่าวสำคัญ (±60 นาที) Alert อัตโนมัติจะถูกระงับไว้ก่อน")

    if not found_any:
        return "📥 ตอนนี้ยังไม่มีจุดเข้าไม้ที่เข้าเงื่อนไขเลยครับ (เช็คครบทั้งแผนที่ 1-3 แล้ว)"

    return "\n".join(lines)


def _cmd_order1(ctx):
    """แสดงเฉพาะสถานะแผนที่ 1 (Pullback) — ถ้าเข้าเงื่อนไข จะบันทึกลง Order Dashboard ให้อัตโนมัติทันที
    ถ้ายังไม่เข้าเงื่อนไข จะบอกเหตุผล/สถานะปัจจุบันแทน"""
    entry_signal = ctx["entry_signal"]
    structure = ctx["structure"]
    config = ctx["config"]
    df_ind = ctx["df_ind"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    lines = ["📥 <b>แผนที่ 1 (Pullback)</b>", ""]
    active = entry_signal.get("valid") and entry_signal.get("direction") == structure["trend"]

    if active:
        direction_th = "LONG" if entry_signal["direction"] == "bullish" else "SHORT"
        lines.append(f"✅ เข้าเงื่อนไข: {direction_th}")
        try:
            from risk import calc_stop_loss
            from tp import calc_take_profits, calc_risk_reward

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            stop_loss = calc_stop_loss(entry_signal, current_atr, config)
            take_profits = calc_take_profits(
                entry_signal["entry_price"], stop_loss, entry_signal["direction"], config
            )
            rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
                  for name, price in take_profits.items()}

            lines.append(f"Entry: {entry_signal['entry_price']:.4f}")
            lines.append(f"SL: {stop_loss:.4f}")
            for name, price in take_profits.items():
                lines.append(f"{name}: {price:.4f} (RR {rr[name]})")

            confirmed = bool(entry_signal.get("trigger", {}).get("confirmed"))
            plan_key = "plan1_pullback" if confirmed else "plan1_pullback_early"
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, plan_key, entry_signal["direction"],
                                           entry_signal["entry_price"], threshold):
                lines.append("📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, entry_signal["direction"], entry_signal["entry_price"],
                          stop_loss, take_profits, score=None, plan=plan_key)
                tag = "ยืนยันแล้ว" if confirmed else "เข้าก่อนยืนยัน"
                lines.append(f"📌 บันทึกลง Order Dashboard แล้ว ({tag})")
        except Exception as e:
            lines.append(f"(คำนวณ/บันทึก SL/TP ไม่สำเร็จ: {e})")

        if not entry_signal.get("trigger", {}).get("confirmed"):
            lines.append("⚠️ ยังไม่ยืนยัน 5M Trigger — ราคาอาจยังไม่กลับตัวจริง เข้าก่อนเวลาอาจโดนสวนได้")
    else:
        trend_th = TREND_LABEL.get(structure.get("trend"), "-")
        lines.append(f"ยังไม่เข้าเงื่อนไขตอนนี้ครับ (เทรนด์หลัก 15M ตอนนี้: {trend_th})")
        reasons = entry_signal.get("reasons", [])
        if reasons:
            lines.append("")
            lines.append("สถานะปัจจุบัน:")
            for r in reasons[:3]:
                lines.append(f"- {r}")

    return "\n".join(lines)


def _cmd_order2(ctx):
    """แสดงเฉพาะสถานะแผนที่ 2 (Breakout) — ถ้าทะลุแล้ว จะบันทึกลง Order Dashboard ให้อัตโนมัติทันที
    ถ้ายังไม่ทะลุ จะบอกระยะห่างจากจุด trigger ทั้งสองฝั่งแทน"""
    df_ind = ctx["df_ind"]
    structure = ctx["structure"]
    config = ctx["config"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    lines = ["📥 <b>แผนที่ 2 (Breakout)</b>", ""]
    breakout = detect_breakout_trigger(df_ind, structure, config)

    if breakout:
        direction_th = "LONG" if breakout["direction"] == "bullish" else "SHORT"
        lines.append(
            f"✅ เข้าเงื่อนไข: {direction_th} ทะลุ {breakout['level']:.4f} ที่ราคา {breakout['price']:.4f}"
        )
        order = calc_breakout_order(breakout, structure, df_ind, config)
        if order:
            lines.append(f"Entry: {order['entry_price']:.4f}")
            lines.append(f"SL: {order['stop_loss']:.4f}")
            lines.append(f"TP (Measured move): {order['take_profit']:.4f} (RR {order['rr']})")

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, "plan2_breakout", order["direction"],
                                           order["entry_price"], threshold):
                lines.append("📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
                          {"TP1": order["take_profit"]}, score=None, plan="plan2_breakout")
                lines.append("📌 บันทึกลง Order Dashboard แล้ว")
        else:
            lines.append("(หาข้อมูล swing ไม่พอสำหรับคำนวณ SL/TP ของแผนนี้)")
    else:
        status = get_breakout_status(df_ind, structure, config)
        if not status:
            lines.append("ข้อมูล swing ยังไม่พอสำหรับเช็คแผนนี้ตอนนี้")
        else:
            lines.append("ยังไม่ทะลุตอนนี้ครับ สถานะปัจจุบัน:")
            if "up_distance" in status:
                lines.append(
                    f"- ฝั่งขึ้น: ห่างจากจุดทะลุ ({status['up_target']:.4f}) อีก {status['up_distance']:.4f}"
                )
            if "down_distance" in status:
                lines.append(
                    f"- ฝั่งลง: ห่างจากจุดทะลุ ({status['down_target']:.4f}) อีก {status['down_distance']:.4f}"
                )

    return "\n".join(lines)


def _cmd_order3(ctx):
    """แสดงเฉพาะสถานะแผนที่ 3 (สวนเทรนด์) — ถ้าครบ checklist แล้ว จะบันทึกลง Order Dashboard ให้อัตโนมัติทันที
    ถ้ายังไม่ครบ จะบอกว่าขาดข้อไหนอยู่แทน"""
    df_ind = ctx["df_ind"]
    structure = ctx["structure"]
    config = ctx["config"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    lines = ["📥 <b>แผนที่ 3 (สวนเทรนด์)</b>", ""]
    counter = detect_counter_trend_trigger(df_ind, structure)

    if counter:
        direction_th = "LONG" if counter["direction"] == "bullish" else "SHORT"
        lines.append(f"✅ เข้าเงื่อนไข: {direction_th} — Checklist ครบ 3/3 ข้อ")
        order = calc_counter_trend_order(counter, df_ind, config)
        if order:
            lines.append(f"Entry: {order['entry_price']:.4f}")
            lines.append(f"SL: {order['stop_loss']:.4f}")
            lines.append(f"TP (Equilibrium): {order['take_profit']:.4f} (RR {order['rr']})")
            lines.append("⚠️ แผนสวนเทรนด์เสี่ยงสูงกว่าแผนอื่น ควรลดขนาดไม้")

            atr_period = config.get("sl_atr_avg_period", 20)
            current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
            threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)
            if _has_similar_running_order(bucket, symbol, "plan3_counter_trend", order["direction"],
                                           order["entry_price"], threshold):
                lines.append("📌 (มีออเดอร์ลักษณะเดียวกันบันทึกไว้แล้ว ไม่บันทึกซ้ำ)")
            else:
                add_order(bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
                          {"TP1": order["take_profit"]}, score=None, plan="plan3_counter_trend")
                lines.append("📌 บันทึกลง Order Dashboard แล้ว")
        else:
            lines.append("(คำนวณ SL/TP ของแผนนี้ไม่สำเร็จ)")
    else:
        status = get_counter_trend_status(df_ind, structure)
        if status is None:
            lines.append("ตลาด sideway ไม่มีเทรนด์หลักให้สวนตอนนี้")
        else:
            passed = sum(status["checklist"].values())
            total = len(status["checklist"])
            lines.append(f"ยังไม่ครบเงื่อนไขตอนนี้ครับ ({passed}/{total} ข้อ)")
            for name, ok in status["checklist"].items():
                mark = "✅" if ok else "❌"
                lines.append(f"- {name}: {mark}")

    return "\n".join(lines)


def _cmd_trend(ctx):
    structure = ctx["structure"]
    bias_4h = ctx["bias_4h"] or {}
    pd_zone = calc_premium_discount_zone(ctx["df_ind"], ctx["config"].get("structure_lookback", 50))

    lines = [
        "📈 <b>สรุปแนวโน้ม</b>",
        "",
        f"15M Structure: {TREND_LABEL.get(structure['trend'], structure['trend'])} "
        f"{STRENGTH_LABEL.get(structure.get('trend_strength'), '')} | Event: {structure.get('event') or '-'}",
        f"4H Bias: {TREND_LABEL.get(bias_4h.get('trend'), '-')}",
        f"Premium/Discount (15M, {ctx['config'].get('structure_lookback', 50)} แท่งย้อนหลัง): "
        f"{pd_zone['zone']} ({pd_zone['position_pct']:.0f}% ของ range)",
        f"  Zone High: {pd_zone['zone_high']:.4f} | Equilibrium: {pd_zone['equilibrium']:.4f} | "
        f"Zone Low: {pd_zone['zone_low']:.4f}",
        "",
        "แนวรับ-แนวต้านหลัก (จาก swing ล่าสุดบน 15M):",
    ]

    swings = structure.get("last_swings", [])
    highs = [p for p in swings if p["type"] == "high"]
    lows = [p for p in swings if p["type"] == "low"]
    if highs:
        lines.append(f"  แนวต้าน: {highs[-1]['price']:.4f}")
    if lows:
        lines.append(f"  แนวรับ: {lows[-1]['price']:.4f}")
    if not highs and not lows:
        lines.append("  ยังไม่มีข้อมูล swing พอ")

    return "\n".join(lines)


def _cmd_news(ctx):
    """ดึงปฏิทินสดตอนนี้เลย (ไม่ใช้ cache เที่ยงคืน) เพราะคำสั่งนี้เรียกน้อย ไม่กระทบ rate limit ของ Forex Factory"""
    events = fetch_usd_calendar_events()
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=24)
    upcoming = [e for e in events if now <= e["time"] <= window_end]

    if not upcoming:
        return "📰 ไม่มีข่าว USD สำคัญ (High/Medium Impact) ใน 24 ชม.ข้างหน้าครับ"

    lines = ["📰 <b>ข่าวสำคัญใน 24 ชม.ข้างหน้า</b>", ""]
    for e in upcoming:
        t_thai = e["time"].astimezone(THAI_TZ).strftime("%H:%M")
        icon = "🔴" if e["impact"] == "High" else "🟠"
        lines.append(f"{icon} {t_thai} — {e['title']} (Forecast: {e.get('forecast') or '-'})")
    return "\n".join(lines)


def _cmd_status(ctx):
    config = ctx["config"]
    session_info = ctx.get("session_info") or {}
    lines = ["⚙️ <b>สถานะบอท</b>", ""]

    if session_info:
        lines.append(f"Session: {'อยู่ใน London/NY ✅' if session_info.get('in_session') else 'นอก Session ⛔ (ไม่เทรด)'}")
        if session_info.get("in_killzone"):
            lines.append("Kill Zone: ใช่ ⚡")

    in_blackout, blackout_event = ctx.get("news_blackout", (False, None))
    if in_blackout and blackout_event:
        lines.append(f"⛔ อยู่ในช่วงห้ามเทรดรอบข่าว: {blackout_event['title']}")
    else:
        lines.append("ข่าว: ไม่มีข่าวใกล้ๆ ที่ต้องระวังตอนนี้ ✅")

    structure = ctx.get("structure") or {}
    lines.append(
        f"เทรนด์ 15M ตอนนี้: {TREND_LABEL.get(structure.get('trend'), '-')} "
        f"{STRENGTH_LABEL.get(structure.get('trend_strength'), '')}"
    )
    lines.append("")
    lines.append("บอทกำลังทำงานปกติ — ข้อความนี้คือหลักฐานว่ารันสำเร็จล่าสุด ✅")
    lines.append("(ตอบคำสั่งผ่าน Render polling loop — เกือบ real-time ไม่ใช่รอ cron 5 นาทีแบบเดิมแล้ว)")
    return "\n".join(lines)


def _cmd_summary(ctx):
    config = ctx["config"]
    symbol = ctx["symbol"]
    current_price = ctx["df_ind"]["close"].iloc[-1]
    orders = update_orders_status(config["kvdb_bucket"], symbol, current_price)
    return build_orders_dashboard(symbol, orders, current_price)


def _cmd_stats(ctx):
    """แสดง win rate/expectancy แยกรายแผน (1/2/3) จากออเดอร์ที่ปิดแล้วทั้งหมดใน Order Dashboard"""
    config = ctx["config"]
    symbol = ctx["symbol"]
    current_price = ctx["df_ind"]["close"].iloc[-1]
    orders = update_orders_status(config["kvdb_bucket"], symbol, current_price)
    stats = calc_stats(orders)
    return build_stats_message(symbol, stats)


def _cmd_confirm1(ctx):
    """
    บันทึกออเดอร์แผนที่ 1 (Pullback) ที่กำลังเห็นตอนนี้ลง Order Dashboard ทันที (manual confirm)
    ใช้ตอนตัดสินใจตั้ง Limit Order ตามจุด Entry ที่ /order หรือ /order1 แสดงไว้ โดยไม่ต้องรอให้ main.py
    ยิง Alert อัตโนมัติ (ซึ่งจะยิงก็ต่อเมื่อผ่านทุกฟิลเตอร์ 4H/1H/ADX/Session/Score/5M Trigger ครบ)

    ถ้ายังไม่มี 5M Trigger ยืนยัน จะบันทึกแยกเป็น plan "plan1_pullback_early" (คนละกลุ่มกับที่ยืนยัน
    แล้ว "plan1_pullback") เพื่อให้ /stats เปรียบเทียบได้ว่า "เข้าก่อนยืนยัน" กับ "รอยืนยันแล้วค่อยเข้า"
    อันไหนแม่นกว่ากันจริงๆ จากข้อมูลจริงที่สะสมไป — มีระบบกันบันทึกซ้ำ (_has_similar_running_order)
    เดียวกับที่ /order ใช้ ถ้ากด /confirm1 ซ้ำหรือ /order เพิ่งบันทึกจุดเดียวกันไปแล้ว จะไม่บันทึกซ้ำ
    """
    entry_signal = ctx["entry_signal"]
    structure = ctx["structure"]
    config = ctx["config"]
    df_ind = ctx["df_ind"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    if not (entry_signal.get("valid") and entry_signal.get("direction") == structure["trend"]):
        return "📥 ตอนนี้ยังไม่มีจุดเข้าตามแผนที่ 1 ให้ยืนยันครับ ลองเช็ค /order1 ก่อน"

    try:
        from risk import calc_stop_loss
        from tp import calc_take_profits

        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
        stop_loss = calc_stop_loss(entry_signal, current_atr, config)
        take_profits = calc_take_profits(entry_signal["entry_price"], stop_loss, entry_signal["direction"], config)
    except Exception as e:
        return f"คำนวณ SL/TP ไม่สำเร็จ: {e}"

    confirmed = bool(entry_signal.get("trigger", {}).get("confirmed"))
    plan_key = "plan1_pullback" if confirmed else "plan1_pullback_early"
    threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

    if _has_similar_running_order(bucket, symbol, plan_key, entry_signal["direction"],
                                   entry_signal["entry_price"], threshold):
        return "📥 มีออเดอร์ลักษณะเดียวกันที่บันทึกไว้แล้ว (ยัง running อยู่) ไม่บันทึกซ้ำครับ"

    add_order(bucket, symbol, entry_signal["direction"], entry_signal["entry_price"],
              stop_loss, take_profits, score=None, plan=plan_key)

    tag = "ยืนยันแล้ว (5M Trigger)" if confirmed else "เข้าก่อนยืนยัน (early)"
    return (
        f"✅ บันทึกออเดอร์แผนที่ 1 ลง Order Dashboard แล้วครับ ({tag})\n"
        f"Entry: {entry_signal['entry_price']:.4f} | SL: {stop_loss:.4f}\n"
        "เช็คผลได้ที่ /summary และดูสถิติรวมที่ /stats"
    )


def _cmd_confirm2(ctx):
    """บันทึกออเดอร์แผนที่ 2 (Breakout) ที่กำลังทะลุอยู่ตอนนี้ลง Order Dashboard ทันที"""
    df_ind = ctx["df_ind"]
    structure = ctx["structure"]
    config = ctx["config"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    breakout = detect_breakout_trigger(df_ind, structure, config)
    if not breakout:
        return "📥 ตอนนี้ยังไม่ทะลุตามแผนที่ 2 ให้ยืนยันครับ ลองเช็ค /order2 ก่อน"

    order = calc_breakout_order(breakout, structure, df_ind, config)
    if not order:
        return "คำนวณ SL/TP ไม่สำเร็จ (หาข้อมูล swing ไม่พอ)"

    atr_period = config.get("sl_atr_avg_period", 20)
    current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
    threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

    if _has_similar_running_order(bucket, symbol, "plan2_breakout", order["direction"],
                                   order["entry_price"], threshold):
        return "📥 มีออเดอร์ลักษณะเดียวกันที่บันทึกไว้แล้ว (ยัง running อยู่) ไม่บันทึกซ้ำครับ"

    add_order(bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
              {"TP1": order["take_profit"]}, score=None, plan="plan2_breakout")
    return (
        f"✅ บันทึกออเดอร์แผนที่ 2 ลง Order Dashboard แล้วครับ\n"
        f"Entry: {order['entry_price']:.4f} | SL: {order['stop_loss']:.4f} | "
        f"TP: {order['take_profit']:.4f} (RR {order['rr']})"
    )


def _cmd_confirm3(ctx):
    """บันทึกออเดอร์แผนที่ 3 (สวนเทรนด์) ที่ Checklist ครบ 3/3 อยู่ตอนนี้ลง Order Dashboard ทันที"""
    df_ind = ctx["df_ind"]
    structure = ctx["structure"]
    config = ctx["config"]
    symbol = ctx["symbol"]
    bucket = config["kvdb_bucket"]

    counter = detect_counter_trend_trigger(df_ind, structure)
    if not counter:
        return "📥 ตอนนี้ยังไม่ครบเงื่อนไขตามแผนที่ 3 ให้ยืนยันครับ ลองเช็ค /order3 ก่อน"

    order = calc_counter_trend_order(counter, df_ind, config)
    if not order:
        return "คำนวณ SL/TP ไม่สำเร็จ"

    atr_period = config.get("sl_atr_avg_period", 20)
    current_atr = df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0
    threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

    if _has_similar_running_order(bucket, symbol, "plan3_counter_trend", order["direction"],
                                   order["entry_price"], threshold):
        return "📥 มีออเดอร์ลักษณะเดียวกันที่บันทึกไว้แล้ว (ยัง running อยู่) ไม่บันทึกซ้ำครับ"

    add_order(bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
              {"TP1": order["take_profit"]}, score=None, plan="plan3_counter_trend")
    return (
        f"✅ บันทึกออเดอร์แผนที่ 3 ลง Order Dashboard แล้วครับ\n"
        f"Entry: {order['entry_price']:.4f} | SL: {order['stop_loss']:.4f} | "
        f"TP: {order['take_profit']:.4f} (RR {order['rr']})"
    )


COMMAND_HANDLERS = {
    "order": _cmd_order,
    "order1": _cmd_order1,
    "order2": _cmd_order2,
    "order3": _cmd_order3,
    "trend": _cmd_trend,
    "news": _cmd_news,
    "status": _cmd_status,
    "summary": _cmd_summary,
    "stats": _cmd_stats,
    "confirm1": _cmd_confirm1,
    "confirm2": _cmd_confirm2,
    "confirm3": _cmd_confirm3,
}


def handle_telegram_commands(config, ctx):
    """
    เช็คคำสั่งใหม่จาก Telegram (getUpdates) แล้วตอบกลับ ณ รอบที่บอทรันอยู่ตอนนี้ (piggyback บน cron 5 นาที)
    ต้องตั้ง telegram_owner_id ไว้ใน config ไม่งั้นจะไม่ประมวลผลคำสั่งใดๆ เลย (ปลอดภัยไว้ก่อน)
    ctx คือ dict ข้อมูลที่คำนวณไว้แล้วในรอบนี้ (df_ind, structure, entry_signal, bias_4h, session_info,
    news_blackout, symbol, config) ส่งต่อให้ command handler แต่ละตัวใช้ ไม่ต้องคำนวณซ้ำ

    สิทธิ์ใช้คำสั่ง: เจ้าของบอท (telegram_owner_id) ใช้ได้จากทุกที่ (แชทเดี่ยว/กลุ่มไหนก็ได้) และ
    ใครก็ตามที่พิมพ์คำสั่งมาจากกลุ่มที่ตั้งไว้ใน telegram_group_chat_id ก็ใช้คำสั่งได้ด้วยเช่นกัน —
    คนนอกกลุ่มนั้น (ไม่ใช่เจ้าของบอท และไม่ได้พิมพ์จากกลุ่มที่อนุญาต) จะถูกเมินเงียบๆ เหมือนเดิม
    """
    token = config.get("telegram_token")
    owner_id = config.get("telegram_owner_id")
    if not token or not owner_id:
        return

    bucket = config["kvdb_bucket"]
    last_offset_raw = kv_get(bucket, "telegram_last_update_id")
    try:
        last_offset = int(last_offset_raw) if last_offset_raw else None
    except (TypeError, ValueError):
        last_offset = None

    offset = (last_offset + 1) if last_offset is not None else None
    updates = _get_updates(token, offset=offset)
    if not updates:
        return

    max_update_id = last_offset or 0
    for update in updates:
        max_update_id = max(max_update_id, update.get("update_id", 0))
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue

        sender_id = str(message.get("from", {}).get("id", ""))
        chat_id_check = message.get("chat", {}).get("id", "")
        group_chat_id = config.get("telegram_group_chat_id")
        is_owner = sender_id == str(owner_id)
        is_allowed_group = group_chat_id and str(chat_id_check) == str(group_chat_id)
        if not is_owner and not is_allowed_group:
            continue  # ไม่ใช่เจ้าของบอท และไม่ได้พิมพ์จากกลุ่มที่อนุญาต เมินคำสั่งนี้ทิ้งเงียบๆ

        # ข้ามคำสั่งเก่าที่ค้างคิวมานาน (เช่นตอน Render suspend ไปนานแล้วเพิ่ง resume) ไม่ไล่ตอบย้อนหลัง
        msg_age = time.time() - message.get("date", time.time())
        if msg_age > STALE_MESSAGE_SECONDS:
            continue

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        # Telegram ส่งคำสั่งกลุ่มมาเป็น "/order@BotName" ต้องตัด @BotName ออกก่อนเทียบ
        command = text[1:].split("@")[0].split()[0].lower()
        handler = COMMAND_HANDLERS.get(command)
        chat_id = message["chat"]["id"]

        if handler:
            try:
                reply_text = handler(ctx)
            except Exception as e:
                reply_text = f"เกิดข้อผิดพลาดตอนประมวลผลคำสั่ง /{command}: {e}"
            _reply(token, chat_id, reply_text)
        # คำสั่งที่ไม่รู้จัก: เมินเงียบๆ ไม่ตอบอะไร (กันสแปมตอบ error ทุกครั้งที่พิมพ์ผิด)

    # บันทึก offset ลง kvdb — ถ้าเขียนไม่สำเร็จ (rate limit/error ชั่วคราว) จะ log ไว้ให้เห็นใน log
    # (เดิม kv_set คืน True เสมอไม่ว่าจะสำเร็จจริงหรือไม่ ตอนนี้แก้ที่ kvstore.py แล้วให้เช็ค status code จริง)
    if not kv_set(bucket, "telegram_last_update_id", str(max_update_id)):
        print(f"[Telegram Bot Error] บันทึก offset ({max_update_id}) ลง kvdb ไม่สำเร็จ — "
              f"รอบ cron ถัดไปอาจไล่ตอบคำสั่งชุดนี้ซ้ำ")


def _acquire_or_renew_lock(bucket):
    """
    คืน True ถ้า process นี้ "ถือสิทธิ์" ตอบคำสั่ง Telegram อยู่ตอนนี้ (ได้ lock มาใหม่ หรือ renew ของเดิม)
    คืน False ถ้ามี process อื่นถือ lock สดอยู่ — ให้ process นี้เงียบไว้ก่อน ไม่ต้องยิง Telegram API
    กันเคส 2 instance คาบเกี่ยวกันตอน Render deploy ใหม่ (zero-downtime) ตอบคำสั่งซ้ำกัน
    """
    now = time.time()
    raw = kv_get(bucket, LOCK_KEY)
    if raw:
        try:
            data = json.loads(raw)
            holder = data.get("holder")
            ts = data.get("ts", 0)
        except Exception:
            holder, ts = None, 0
        if holder and holder != _INSTANCE_ID and (now - ts) < LOCK_TTL_SECONDS:
            return False  # คนอื่นถือ lock สดอยู่ ไม่แย่ง

    kv_set(bucket, LOCK_KEY, json.dumps({"holder": _INSTANCE_ID, "ts": now}))
    return True


def run_polling_loop(config, symbol="XAUUSD"):
    """
    Loop รันตลอดเวลา (ใช้บน Render/server ที่ไม่ตาย ไม่ใช่ GitHub Actions) ใช้ Telegram long-polling
    (timeout=30 วิ — Telegram จะค้าง connection ไว้จนกว่าจะมีข้อความใหม่หรือครบเวลา ไม่ใช่ busy-loop ถี่ๆ
    ที่กิน CPU/แบนด์วิดท์ฟรี) ตอบคำสั่งได้เกือบทันที (วินาที ไม่ใช่นาที) ต่างจากโหมด cron เดิม

    ⚠️ ห้ามรันคู่กับการเรียก handle_telegram_commands() จาก main.py (cron) พร้อมกัน จะแย่ง offset กัน
    ให้ Render จัดการคำสั่งอย่างเดียว ส่วน GitHub Actions ทำหน้าที่วิเคราะห์ + ส่ง Alert เท่านั้น

    ป้องกันตอบซ้ำตอน Render zero-downtime deploy (2 instance คาบเกี่ยวกันชั่วขณะ) ด้วย lock ผ่าน kvdb:
    ทุกรอบ loop จะแย่ง/ต่ออายุ lock ก่อน มีแค่ instance ที่ถือ lock สดเท่านั้นที่ยิง Telegram API จริง
    ถ้าตัวที่ถือ lock ตายไป lock จะหมดอายุเองใน LOCK_TTL_SECONDS แล้วอีก instance จะรับช่วงต่อทันที

    ป้องกันไล่ตอบ backlog คำสั่งเก่าตอน resume จาก suspend: คำสั่งที่ค้างคิวเกิน STALE_MESSAGE_SECONDS
    จะถูกข้ามเงียบๆ (ไม่ตอบ แต่ offset ยัง advance ตามปกติ) ผู้ใช้ต้องพิมพ์คำสั่งใหม่เอง ไม่ไล่ตอบย้อนหลัง

    ป้องกันตอบซ้ำเมื่อ kvdb.io เขียนพลาด (เช่นโดน rate limit ตอน loop วนถี่ต่อเนื่องหลาย ชม.):
    เดิมโค้ดอ่าน/เขียน offset ผ่าน kvdb ทุกรอบ loop — ถ้า kv_set เขียนไม่สำเร็จแบบเงียบๆ (บั๊กเดิมใน
    kvstore.py ที่คืน True เสมอ) offset จะไม่ขยับ รอบถัดไปเลยไปดึงคำสั่งเดิมซ้ำมาตอบอีก วนซ้ำไปเรื่อยๆ
    ตอนนี้เก็บ offset ไว้ในตัวแปรความจำของ process เอง (known_offset) เป็น "ความจริงหลัก" ระหว่าง
    instance นี้ยังรันอยู่ — อ่านจาก kvdb แค่ครั้งเดียวตอนเริ่ม loop (กู้คืนหลัง restart/deploy ใหม่)
    หลังจากนั้นแต่ละรอบจะเขียนขึ้น kvdb แบบ best-effort เท่านั้น (เผื่อ instance ตายจะได้กู้คืนต่อได้)
    แต่ต่อให้เขียนพลาด ตัวแปรในหน่วยความจำก็ยังจำตำแหน่งล่าสุดถูกต้อง ไม่ทำให้ตอบคำสั่งเดิมซ้ำอีก
    """
    token = config.get("telegram_token")
    owner_id = config.get("telegram_owner_id")
    if not token or not owner_id:
        print("[Telegram Bot] ไม่มี telegram_token หรือ telegram_owner_id — ไม่เริ่ม polling loop")
        return

    bucket = config["kvdb_bucket"]
    print(f"[Telegram Bot] เริ่ม polling loop แล้ว (instance={_INSTANCE_ID})")

    # อ่าน offset เริ่มต้นจาก kvdb แค่ครั้งเดียวตอนเริ่ม instance (กู้คืนหลัง restart/deploy)
    # จากนี้ไปตัวแปรนี้คือ "ความจริงหลัก" ของ instance นี้ ไม่อ่านย้อนกลับจาก kvdb อีกระหว่าง loop
    raw = kv_get(bucket, "telegram_last_update_id")
    try:
        known_offset = int(raw) if raw else None
    except (TypeError, ValueError):
        known_offset = None

    while True:
        try:
            if not _acquire_or_renew_lock(bucket):
                # มี instance อื่นถือ lock สดอยู่ — รอเฉยๆ ไม่ยิง Telegram API ซ้ำ
                time.sleep(3)
                continue

            offset = (known_offset + 1) if known_offset is not None else None
            updates = _get_updates(token, offset=offset, timeout=30)

            for update in updates:
                update_id = update.get("update_id", 0)
                # อัปเดตตัวแปรในหน่วยความจำก่อนเสมอ (เชื่อถือได้ทันที ไม่ต้องรอ kvdb)
                known_offset = max(known_offset or 0, update_id)
                # เขียนขึ้น kvdb แบบ best-effort เผื่อ instance ตายจะได้กู้คืนต่อได้ถูกจุด
                # ถ้าเขียนพลาด (เช่นโดน rate limit) แค่ log ไว้ — ไม่กระทบการทำงานของ instance นี้
                # เพราะ known_offset ในหน่วยความจำยังถูกต้องอยู่ ไม่วนไปตอบคำสั่งเดิมซ้ำแน่นอน
                if not kv_set(bucket, "telegram_last_update_id", str(known_offset)):
                    print(f"[Telegram Bot Error] บันทึก offset ({known_offset}) ลง kvdb ไม่สำเร็จ "
                          f"— ใช้ค่าในหน่วยความจำต่อไปก่อน (ไม่กระทบการตอบคำสั่งรอบนี้)")

                message = update.get("message") or update.get("channel_post")
                if not message:
                    continue

                sender_id = str(message.get("from", {}).get("id", ""))
                chat_id_check = message.get("chat", {}).get("id", "")
                group_chat_id = config.get("telegram_group_chat_id")
                is_owner = sender_id == str(owner_id)
                is_allowed_group = group_chat_id and str(chat_id_check) == str(group_chat_id)
                if not is_owner and not is_allowed_group:
                    continue  # ไม่ใช่เจ้าของบอท และไม่ได้พิมพ์จากกลุ่มที่อนุญาต เมินเงียบๆ

                # ข้ามคำสั่งเก่าที่ค้างคิวมาตั้งแต่ก่อน instance นี้เริ่ม (เช่นตอน resume จาก suspend)
                # offset ยัง advance ปกติด้านบนแล้ว แค่ไม่ประมวลผล/ไม่ตอบกลับคำสั่งที่ตกยุคนี้
                msg_age = time.time() - message.get("date", time.time())
                if msg_age > STALE_MESSAGE_SECONDS:
                    print(f"[Telegram Bot] ข้ามคำสั่งเก่า (อายุ {msg_age:.0f} วิ) — เกิน {STALE_MESSAGE_SECONDS} วิ")
                    continue

                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue

                command = text[1:].split("@")[0].split()[0].lower()
                handler = COMMAND_HANDLERS.get(command)
                chat_id = message["chat"]["id"]
                if not handler:
                    continue  # คำสั่งไม่รู้จัก เมินเงียบๆ

                try:
                    ctx = _build_command_context(symbol, config)
                    reply_text = handler(ctx)
                except Exception as e:
                    reply_text = f"เกิดข้อผิดพลาดตอนประมวลผลคำสั่ง /{command}: {e}"
                _reply(token, chat_id, reply_text)

        except Exception as e:
            # กัน loop ตายทั้งกระบวนการถ้าเน็ตสะดุด/Telegram ล่มชั่วคราว รอสักพักแล้วลองใหม่
            print(f"[Telegram Bot Error] polling loop error: {e}")
            time.sleep(5)
