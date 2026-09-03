"""
telegram_bot.py
ระบบรับคำสั่งจาก Telegram (Interactive Commands) เพิ่มเติมจากที่บอทส่งแจ้งเตือนอัตโนมัติอยู่แล้ว
รองรับ: /order /trend /news /status /aicheck

/order รวมทั้ง 8 แผนไว้คำสั่งเดียว: เช็คเงื่อนไขของแผนที่ 1-8 พร้อมกันในรอบเดียว แล้วสรุปผลรวมเป็น
ข้อความเดียว (ยาว อาจถูกแบ่งส่งหลายข้อความถ้าเกินลิมิตของ Telegram — ดู _reply/_split_message)
ไม่มีระบบ Confirm และไม่บันทึกลง Order Dashboard อีกต่อไป (ตัดออกทั้งหมดตามที่ผู้ใช้ร้องขอ เพราะ
ระบบยืนยันเดิมทำให้ตัดสินใจเข้าเทรดช้า/ไม่ยอมเทรด และการเขียนข้อมูลทุกครั้งที่เช็คทำให้ตอบช้า/หน่วง)
/order แสดงผลตามสภาพจริงล้วนๆ (detect-and-display) — การบันทึกข้อมูลออเดอร์ ผู้ใช้แยกไปทำเองข้างนอก
แล้ว ทุกแผนคำนวณ "คะแนน" (Score) ของตัวเองเทียบกันเสมอ (ดู plan_score.py) เรียงจากมากไปน้อย พร้อม
ไฮไลต์แผนที่ทิศทางตรงกับเทรนด์หลัก (4H Bias หรือ 15M Structure) ให้เห็นชัดเวลาสัญญาณสวนทางกัน

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
import html
import requests
from datetime import datetime, timedelta, timezone

from kvstore import kv_get, kv_set
from news import fetch_usd_calendar_events
from news_scheduler import THAI_TZ, is_in_news_blackout
from scenario import (
    detect_breakout_trigger, detect_counter_trend_trigger,
    calc_breakout_order, calc_counter_trend_order,
    get_daily_bias_and_range, detect_plan4_signal, calc_plan4_order,
)
from zones import calc_premium_discount_zone
from zone_entry import find_zone_entry, calc_zone_entry_order
from liquidity_sweep_entry import find_sweep_entry, calc_sweep_entry_order
from qm_pattern_entry import find_qm_pattern, calc_qm_entry_order
from flag_pattern_entry import find_flag_pattern, calc_flag_entry_order
from plan_score import generic_plan_score, determine_master_trend
from config import get_symbol_config
import ai_layer
import sheets_log

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

# --- คู่เงินที่ /order รองรับ (พิมพ์ /order เฉยๆ = คู่เงินหลักของ instance นี้ ตามที่ตั้งไว้ตอนรัน
# run_polling_loop(config, symbol=...) พิมพ์ /order gold หรือ /order eth เพื่อเจาะจงคู่เงินได้ตรงๆ)
# SYMBOL_ALIASES: คำที่ผู้ใช้พิมพ์ (lowercase) -> display symbol ที่ระบบใช้ภายใน (ตรงกับ key ใน
# TD_SYMBOL_MAP ด้านล่าง ซึ่งแปลงต่อเป็น symbol จริงที่ยิงหา TwelveData)
SYMBOL_ALIASES = {
    "gold": "XAUUSD", "xau": "XAUUSD", "xauusd": "XAUUSD", "xau/usd": "XAUUSD",
    "eth": "ETHUSDT", "ethusdt": "ETHUSDT", "ethusd": "ETHUSDT", "eth/usdt": "ETHUSDT",
}

# --- คู่เงินที่ "ปิดใช้งานชั่วคราว" — โค้ด/ตรรกะทั้งหมด (SYMBOL_ALIASES, TD_SYMBOL_MAP, per-symbol
# config override ฯลฯ) ยังอยู่ครบ แค่บล็อกไม่ให้เลือกใช้ผ่าน /order /trend ตอนนี้เท่านั้น เผื่อวันหลัง
# อยากกลับมาเปิดใช้ใหม่ แค่ลบ symbol ออกจากเซ็ตนี้ ไม่ต้องเขียนโค้ดใหม่เลย (ตามที่ขอ "ซ่อนไว้ก่อน")
DISABLED_SYMBOLS = {"ETHUSDT"}

# --- คำสั่งที่รับ argument เลือกคู่เงินได้ (เช่น "/order eth", "/trend gold", หรือพิมพ์ติดกัน
# "/ordereth" "/trendgold") ส่วนคำสั่งอื่น (/news /status) ยังผูกกับคู่เงินหลักของ instance เหมือนเดิม
# ไม่รับ argument — เพิ่มคำสั่งใหม่เข้าชุดนี้ได้เลยถ้าอยากให้เลือกคู่เงินได้ด้วย
SYMBOL_AWARE_COMMANDS = {"order", "trend"}

# --- คำสั่งที่ตั้งใจให้ทำงานหนัก/ใช้เวลานานเป็นปกติ (ดึงข้อมูลหลาย timeframe + เรียก API ภายนอก
# หลายเจ้า) — ได้รับการยกเว้นจากตัวกรอง STALE_MESSAGE_SECONDS และจะถูกตอบรับทันทีก่อนเริ่มทำงานจริง
# (ดู run_polling_loop) ไม่งั้นจะโดนข้ามทิ้งเงียบๆ ตอนทำงานเกิน 90 วิ ทั้งที่ผู้ใช้เพิ่งกดสดๆ
SLOW_COMMANDS = {"test"}

# --- display symbol -> label สั้นๆ ที่ใช้ขึ้นหัวข้อความตอบกลับ ให้เห็นชัดว่าผลลัพธ์นี้ของคู่เงินไหน
# (กันสับสนตอนสลับดู /order gold กับ /order eth ถี่ๆ ในแชทเดียวกัน) ---
SYMBOL_DISPLAY_LABEL = {"XAUUSD": "GOLD (XAUUSD)", "ETHUSDT": "ETH (ETHUSDT)"}


def _symbol_label(symbol):
    return SYMBOL_DISPLAY_LABEL.get(symbol, symbol)

# --- display symbol (ที่ระบบใช้ภายใน/ตั้งชื่อไฟล์ kvdb) -> symbol จริงที่ยิงหา TwelveData API ---
# ใช้ตัวเดียวกันทั้ง _build_command_context() และ _fetch_plan4_context() กันสองจุดนี้ไหลออกจากกัน
# (เคยเป็น dict แยกกันคนละที่ 2 จุด ถ้าเพิ่มคู่เงินใหม่แล้วแก้ไม่ครบทั้งคู่ /order ปกติจะทำงาน แต่
# แผนที่ 4 อย่างเดียวจะพังเงียบๆ เพราะยังส่ง "ETHUSDT" ตรงๆ ไป TwelveData แทนที่จะเป็น "ETH/USD")
TD_SYMBOL_MAP = {"XAUUSD": "XAU/USD", "ETHUSDT": "ETH/USD"}


def _resolve_symbol_arg(args, default_symbol):
    """แปลง argument ของคำสั่ง (เช่น "/order eth" -> args=["eth"]) เป็น display symbol ที่ระบบรู้จัก
    ผ่าน SYMBOL_ALIASES ไม่ใส่ argument เลย (เช่น "/order" เฉยๆ) -> ใช้ default_symbol ของ instance นี้
    เหมือนพฤติกรรมเดิมทุกประการ (ไม่ breaking change สำหรับคนที่ยังพิมพ์ /order เฉยๆ อยู่)

    เช็ค DISABLED_SYMBOLS ก่อนเสมอ (แม้ default_symbol เองก็เช็คด้วย เผื่อวันหลัง default ไปเป็นคู่เงิน
    ที่ปิดใช้งานอยู่โดยไม่ตั้งใจ) — คืนข้อความอธิบายที่ต่างจากกรณี "ไม่รู้จักคู่เงินเลย" ให้ชัดว่าปิดไว้
    ชั่วคราว ไม่ใช่ไม่มีคู่เงินนี้ในระบบ

    คืนค่า (symbol, None) ถ้าแปลงได้ หรือ (None, ข้อความอธิบาย) ถ้าพิมพ์คู่เงินที่ไม่รู้จัก/ปิดใช้งานอยู่
    — เอาไป _reply() ตรงๆ ได้เลยแทนที่จะโยน error"""
    if not args:
        if default_symbol in DISABLED_SYMBOLS:
            return None, f"คู่เงินหลักของบอทตอนนี้ ({_symbol_label(default_symbol)}) ปิดใช้งานชั่วคราวอยู่ครับ"
        return default_symbol, None
    key = args[0].lower()
    resolved = SYMBOL_ALIASES.get(key)
    if resolved and resolved in DISABLED_SYMBOLS:
        return None, f"{_symbol_label(resolved)} ปิดใช้งานชั่วคราวอยู่ครับ (โฟกัสที่ GOLD (XAUUSD) ก่อน)"
    if resolved:
        return resolved, None
    return None, (
        f"ไม่รู้จักคู่เงิน \"{args[0]}\" ครับ ตอนนี้รองรับ:\n"
        f"  /order gold — XAUUSD\n"
        f"  /order (ไม่ใส่คำต่อท้าย) — คู่เงินหลักของบอทตอนนี้"
    )


def _normalize_order_shorthand(command, args):
    """รองรับพิมพ์ติดกันแบบ "/ordereth" "/trendgold" เป็นทางลัดของ "/order eth" "/trend gold"
    (เผื่อพิมพ์เร็วๆ ไม่ทันเว้นวรรค) — เทียบส่วนที่ต่อท้ายชื่อคำสั่งใน SYMBOL_AWARE_COMMANDS กับ
    SYMBOL_ALIASES ถ้าตรงกับคู่เงินที่รู้จัก จะแปลงเป็นคำสั่งนั้นพร้อม argument ให้อัตโนมัติ ไม่กระทบ
    คำสั่งอื่นที่ไม่ได้อยู่ใน SYMBOL_AWARE_COMMANDS เลย (เช่น /news /status /aicheck)"""
    if command in SYMBOL_AWARE_COMMANDS:
        return command, args
    for base in SYMBOL_AWARE_COMMANDS:
        if command.startswith(base):
            suffix = command[len(base):]
            if suffix in SYMBOL_ALIASES:
                return base, [suffix] + args
    return command, args


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


def _get_cached_higher_tf_trend(config, symbol):
    """เหมือน _get_cached_bias_4h() แต่ดึงฟิลด์ higher_tf_trend (1H) จาก kv payload เดียวกัน
    ใช้เสริมความแม่นยำของคะแนนแผนที่ 1 (bonus 1H alignment ใน score.py) โดยไม่ต้องยิง TwelveData เพิ่ม"""
    raw = kv_get(config["kvdb_bucket"], f"htf_ctx_{symbol}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("higher_tf_trend")
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

    # ใช้ config เฉพาะคู่เงินนี้ (ถ้ามี override — ดู SYMBOL_CONFIG_OVERRIDES ใน config.py เช่น
    # session_filter_enabled/spread_buffer/min_sl_distance ของ ETHUSDT ต่างจากทอง) ไม่กระทบคู่เงินที่
    # ไม่มี override (ยังได้ config ตัวเดิมเป๊ะ)
    config = get_symbol_config(config, symbol)

    from fetch_data import fetch_twelvedata
    from indicator import add_indicators
    from trend import analyze_structure
    from entry import evaluate_entry
    from bias_4h import analyze_4h_bias
    from session import get_session_info

    td_symbol = TD_SYMBOL_MAP.get(symbol, symbol)

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
        "higher_tf_trend": _get_cached_higher_tf_trend(config, symbol),
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


# --- Telegram: กันข้อความยาว/ตัวอักษรพิเศษตีกันจนส่งไม่สำเร็จ ---
# /order รวม 8 แผนในข้อความเดียวอาจยาวเกิน 4096 ตัวอักษรที่ Telegram รับได้ต่อข้อความง่ายๆ เลย
# TELEGRAM_MAX_LEN เผื่อ buffer ไว้ต่ำกว่าลิมิตจริงเล็กน้อย กันกรณีนับความยาวคลาดเคลื่อน (unicode/emoji)
TELEGRAM_MAX_LEN = 3800


def _split_message(text, max_len=TELEGRAM_MAX_LEN):
    """แบ่งข้อความยาวเป็นหลายก้อน ตัดที่ขอบบรรทัด (ไม่ตัดกลางคำ/กลาง HTML tag) แต่ละก้อนที่ได้
    ยังคง <b>...</b> ปิดครบในตัวเอง เพราะข้อความต้นทางเปิด-ปิด tag ในบรรทัดเดียวกันเสมอ ไม่มี tag
    ที่คร่อมข้ามหลายบรรทัด จึงตัดตรงขอบบรรทัดได้อย่างปลอดภัย"""
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""
    for line in text.split("\n"):
        # บรรทัดเดียวยาวเกิน max_len เอง (เคสหายาก) — ตัดดิบๆ ตามความยาวไปก่อน กันข้อความไม่ถูกส่งเลย
        while len(line) > max_len:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_len])
            line = line[max_len:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate

    if current:
        parts.append(current)
    return parts


def _esc(value):
    """HTML-escape ค่าที่จะแทรกลงข้อความ Telegram (parse_mode=HTML) กันตัวอักษรพิเศษ (&, <, >)
    ในข้อมูลไดนามิก (เช่น symbol/label ที่อาจมาจากภายนอกในอนาคต) ไปตีกับ tag ที่เราคุมเองจนส่งไม่สำเร็จ
    ใช้กับ "ค่าที่แทรกเข้าไป" เท่านั้น ห้ามใช้ครอบทั้งข้อความที่มี <b> ของเราเองอยู่แล้ว"""
    return html.escape(str(value), quote=False)


def _send_single(token, chat_id, text, parse_mode="HTML"):
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            if parse_mode:
                # มักเป็นเพราะตัวอักษรพิเศษตีกับ HTML parsing — ลองส่งใหม่แบบ plain text (ตัด parse_mode
                # ทิ้ง) กันข้อความหายไปเฉยๆ แค่ tag ตัวหนา/ตัวเอียงจะหายไปด้วย ยังดีกว่าไม่ส่งอะไรเลย
                print(f"[Telegram Bot Error] sendMessage แบบ HTML ล้มเหลว ({data.get('description')}) "
                      f"— ลองส่งใหม่แบบ plain text")
                return _send_single(token, chat_id, text, parse_mode=None)
            print(f"[Telegram Bot Error] sendMessage ล้มเหลว: {data}")
            return False
        return True
    except Exception as e:
        print(f"[Telegram Bot Error] ส่งข้อความตอบกลับล้มเหลว: {e}")
        return False


def _reply(token, chat_id, text):
    """ส่งข้อความตอบกลับ — แบ่งเป็นหลายก้อนอัตโนมัติถ้ายาวเกินลิมิตของ Telegram (ดู _split_message)
    และ fallback เป็น plain text อัตโนมัติถ้า HTML parsing พัง (ดู _send_single) กันข้อความยาวๆ
    อย่าง /order (รวม 8 แผน) ส่งไม่สำเร็จเงียบๆ

    รับได้ทั้ง str (ข้อความเดียว) และ list/tuple ของ str (หลายข้อความแยกกัน) — คำสั่งที่อยากส่งหลาย
    ข้อความแยกฟองกันใน Telegram (เช่น /test ที่ส่งสรุปผลทดสอบ + ผลวิเคราะห์ AI 4 ข้อความ) แค่ return
    เป็น list กลับมาได้เลย ไม่ต้องแก้ dispatcher ทั้ง 2 จุดแยกกัน"""
    parts = text if isinstance(text, (list, tuple)) else [text]
    for part in parts:
        if not part:
            continue
        for chunk in _split_message(str(part)):
            _send_single(token, chat_id, chunk)


def _fetch_plan4_context(symbol, config):
    """
    ดึงข้อมูลที่แผนที่ 4 ต้องใช้เพิ่มเติมจากที่ ctx ปกติมีอยู่แล้ว (Daily range + 5 นาทีล่าสุด)
    แยกออกมาเป็นฟังก์ชันต่างหาก ไม่รวมเข้า _build_command_context หลัก เพราะ /order
    ส่วนใหญ่ไม่ต้องใช้ข้อมูลนี้ กันไม่ให้ทุกคำสั่งช้าลง/ยิง API เพิ่มโดยไม่จำเป็น
    คืน (daily_range, df_5m) หรือ (None, None) ถ้าดึงไม่สำเร็จ
    """
    try:
        from fetch_data import fetch_twelvedata
        from indicator import add_indicators

        td_symbol = TD_SYMBOL_MAP.get(symbol, symbol)

        daily_df = fetch_twelvedata(symbol=td_symbol, interval="1day", outputsize=3,
                                     api_key=config["twelvedata_api_key"])
        daily_range = get_daily_bias_and_range(daily_df)
        if not daily_range:
            return None, None

        df_5m = fetch_twelvedata(symbol=td_symbol, interval="5min", outputsize=20,
                                  api_key=config["twelvedata_api_key"])
        df_5m = add_indicators(df_5m, config)
        return daily_range, df_5m
    except Exception as e:
        print(f"[Plan 4 Context Error] {e}")
        return None, None


def _current_atr(ctx):
    df_ind = ctx["df_ind"]
    config = ctx["config"]
    atr_period = config.get("sl_atr_avg_period", 20)
    return df_ind["atr"].tail(atr_period).mean() if "atr" in df_ind.columns and len(df_ind) else 0


# ===========================================================================
# /order รวม — เช็คทั้ง 8 แผนพร้อมกันในรอบเดียว ไม่มี Confirm/ไม่บันทึกลง Order Dashboard
# (ตัดออกทั้งหมดตามที่ผู้ใช้ร้องขอ — ดูรายละเอียดเหตุผลที่หัวไฟล์) แต่ละแผนคืนผลลัพธ์เป็น dict
# รูปแบบเดียวกัน (ดู _plan_result() ด้านล่าง) เพื่อให้ประกอบเป็นข้อความเดียว + จัดอันดับคะแนนได้ง่าย
#   num, label, active(bool), direction("bullish"/"bearish"/None), score(float หรือ None),
#   breakdown(dict คะแนนย่อย), lines(list บรรทัดรายละเอียดตอน active), note(str บรรทัดเดียวตอนไม่ active)
# ===========================================================================


def _plan_result(num, label, active, direction=None, score=None, breakdown=None, lines=None, note=None):
    return {
        "num": num, "label": label, "active": bool(active), "direction": direction,
        "score": score, "breakdown": breakdown or {}, "lines": lines or [], "note": note,
    }


def _build_recommendation(active_sorted, master_trend):
    """สร้างบรรทัด "🎯 แนะนำ" สำหรับ /order — จัดอันดับจากข้อมูลที่คำนวณไว้แล้วเท่านั้น (คะแนน +
    ทิศทางเทียบเทรนด์หลัก) ไม่ได้คิดตัวเลข Entry/SL/TP ใหม่ และไม่ได้เรียก AI — เป็นแค่การ "สรุปสิ่งที่
    มีอยู่แล้วให้อ่านง่ายขึ้น" ตอบโจทย์เวลาหลายแผนขึ้นพร้อมกันแล้วไม่รู้จะเลือกอันไหน

    ลำดับการตัดสิน (สำคัญ: ทิศทางมาก่อนคะแนนเสมอ):
      1. ถ้ามีเทรนด์หลักชัดเจน -> เลือกเฉพาะแผนที่ "ตามเทรนด์หลัก" มาจัดอันดับก่อน แล้วเอาคะแนนสูงสุด
         ในกลุ่มนั้น — เหตุผล: จากข้อมูลเทรดจริงที่วิเคราะห์ร่วมกัน ไม้ที่สวนเทรนด์หลักแพ้เยอะกว่าชัดเจน
         (โดยเฉพาะ Plan 2 ที่แพ้ 5/5 และมีโน้ต "สวนเทรนด์" ติดมาด้วย) คะแนนสูงแต่สวนเทรนด์จึงไม่ควร
         ถูกแนะนำเหนือแผนที่ตามเทรนด์
      2. ถ้าไม่มีแผนไหนตามเทรนด์หลักเลย (active ทุกอันสวนเทรนด์) -> เตือนให้ระวังเป็นพิเศษ
      3. ถ้าเทรนด์หลักเป็น sideway (ไม่มีทิศทางชัด) -> ใช้คะแนนล้วนๆ ตัดสิน พร้อมหมายเหตุกำกับ

    คืนค่าเป็น list ของบรรทัดข้อความ (อาจว่างเปล่าถ้าไม่มีอะไรให้แนะนำ)"""
    if not active_sorted:
        return []

    aligned = [r for r in active_sorted if master_trend and r["direction"] == master_trend]
    against = [r for r in active_sorted if master_trend and r["direction"] != master_trend]
    directions = {r["direction"] for r in active_sorted}
    has_conflict = len(directions) > 1

    lines = ["🎯 <b>แนะนำ</b>"]

    if not master_trend:
        top = active_sorted[0]
        dir_th = "LONG" if top["direction"] == "bullish" else "SHORT"
        lines.append(f"{top['label']} — {dir_th} (คะแนนสูงสุด {top['score']})")
        lines.append("⚠️ ตอนนี้เทรนด์หลักเป็น Sideway ไม่มีทิศทางชัดเจน — ตัดสินจากคะแนนล้วนๆ "
                     "ความน่าเชื่อถือต่ำกว่าปกติ ควรพิจารณาให้รอบคอบเป็นพิเศษ")
        return lines

    if aligned:
        top = aligned[0]
        dir_th = "LONG" if top["direction"] == "bullish" else "SHORT"
        reason = "คะแนนสูงสุดในกลุ่มที่ตามเทรนด์หลัก" if len(aligned) > 1 else "ตามเทรนด์หลัก"
        lines.append(f"{top['label']} — {dir_th} ({reason}, คะแนน {top['score']}) ⭐")
        if has_conflict and against:
            names = ", ".join(r["label"] for r in against)
            lines.append(f"ข้ามแผนที่สวนเทรนด์หลักไปก่อน: {names} ⚠️")
        elif len(aligned) > 1:
            lines.append(f"(แผนที่เหลืออีก {len(aligned) - 1} แผนไปทางเดียวกัน = ยืนยันซึ่งกันและกัน "
                         f"เลือกอันที่ RR ถูกใจได้เลย)")
    else:
        top = active_sorted[0]
        dir_th = "LONG" if top["direction"] == "bullish" else "SHORT"
        lines.append(f"⚠️ ทุกแผนที่เข้าเงื่อนไขตอนนี้ <b>สวนเทรนด์หลักทั้งหมด</b> — ถ้าจะเข้าจริง "
                     f"ควรลดขนาดไม้/ระวังเป็นพิเศษ")
        lines.append(f"อันที่คะแนนดีสุดคือ {top['label']} — {dir_th} (คะแนน {top['score']})")

    return lines


def _fmt_score_line(score, breakdown):
    if score is None:
        return "คะแนน: -"
    parts = " + ".join(f"{k} {v}" for k, v in breakdown.items())
    return f"คะแนน: {score} ({parts})" if parts else f"คะแนน: {score}"


def _check_plan1_all(ctx):
    """แผนที่ 1 (Pullback) — ใช้ entry_signal ที่คำนวณไว้แล้วใน ctx คะแนนมาจาก score.py
    (calc_confidence_score) ของเดิมเป๊ะๆ ไม่ได้แก้สูตร (ดู plan_score.py หัวไฟล์สำหรับหมายเหตุเรื่อง
    สเกลคะแนนที่ต่างจากแผนอื่นเล็กน้อย)"""
    from risk import calc_stop_loss
    from tp import calc_take_profits, calc_risk_reward
    from score import calc_confidence_score

    entry_signal = ctx["entry_signal"]
    structure = ctx["structure"]
    config = ctx["config"]
    label = "แผนที่ 1 (Pullback)"

    if not (entry_signal.get("valid") and entry_signal.get("direction") == structure["trend"]):
        return _plan_result(1, label, False, note="ยังไม่เข้าเงื่อนไข (รอ pullback ตามเทรนด์หลัก 15M)")

    direction = entry_signal["direction"]
    try:
        current_atr = _current_atr(ctx)
        stop_loss = calc_stop_loss(entry_signal, current_atr, config)
        take_profits = calc_take_profits(entry_signal["entry_price"], stop_loss, direction, config)
        rr = {name: calc_risk_reward(entry_signal["entry_price"], stop_loss, price)
              for name, price in take_profits.items()}
        rr_tp1 = next(iter(rr.values()), 0)
        conf = calc_confidence_score(entry_signal, structure, ctx["df_ind"], config, rr_tp1,
                                      bias_4h=ctx.get("bias_4h"), higher_tf_trend=ctx.get("higher_tf_trend"))
    except Exception as e:
        return _plan_result(1, label, True, direction=direction,
                             note=f"เข้าเงื่อนไข: {'LONG' if direction == 'bullish' else 'SHORT'} "
                                  f"แต่คำนวณ SL/TP/คะแนนไม่สำเร็จ: {e}")

    confirmed = bool(entry_signal.get("trigger", {}).get("confirmed"))
    lines = [f"Entry: {entry_signal['entry_price']:.4f}", f"SL: {stop_loss:.4f}"]
    for name, price in take_profits.items():
        lines.append(f"{name}: {price:.4f} (RR {rr[name]})")
    if not confirmed:
        lines.append("⚠️ ยังไม่ยืนยัน 5M Trigger — ราคาอาจยังไม่กลับตัวจริง เข้าก่อนเวลาอาจโดนสวนได้")
    return _plan_result(1, label, True, direction=direction, score=conf["score"],
                         breakdown=conf["breakdown"], lines=lines)


def _check_plan2_all(ctx):
    """แผนที่ 2 (Breakout)"""
    label = "แผนที่ 2 (Breakout)"
    df_ind, structure, config = ctx["df_ind"], ctx["structure"], ctx["config"]
    breakout = detect_breakout_trigger(df_ind, structure, config)
    if not breakout:
        return _plan_result(2, label, False, note="ยังไม่ทะลุระดับ swing ที่มีนัยสำคัญตอนนี้")

    direction = breakout["direction"]
    order = calc_breakout_order(breakout, structure, df_ind, config)
    if not order:
        score, breakdown = generic_plan_score(direction, None, ctx.get("bias_4h"), structure, config)
        return _plan_result(2, label, True, direction=direction, score=score, breakdown=breakdown,
                             lines=[f"ทะลุ {breakout['level']:.4f} ที่ราคา {breakout['price']:.4f}",
                                    "(หาข้อมูล swing ไม่พอสำหรับคำนวณ SL/TP ของแผนนี้)"])

    score, breakdown = generic_plan_score(direction, order["rr"], ctx.get("bias_4h"), structure, config)
    lines = [
        f"ทะลุ {breakout['level']:.4f} ที่ราคา {breakout['price']:.4f}",
        f"Entry: {order['entry_price']:.4f}", f"SL: {order['stop_loss']:.4f}",
        f"TP (Measured move): {order['take_profit']:.4f} (RR {order['rr']})",
    ]
    return _plan_result(2, label, True, direction=direction, score=score, breakdown=breakdown, lines=lines)


def _check_plan3_all(ctx):
    """แผนที่ 3 (สวนเทรนด์)"""
    label = "แผนที่ 3 (สวนเทรนด์)"
    df_ind, structure, config = ctx["df_ind"], ctx["structure"], ctx["config"]
    counter = detect_counter_trend_trigger(df_ind, structure)
    if not counter:
        return _plan_result(3, label, False, note="Checklist สวนเทรนด์ยังไม่ครบ 3/3 ข้อ")

    direction = counter["direction"]
    order = calc_counter_trend_order(counter, df_ind, config)
    if not order:
        score, breakdown = generic_plan_score(direction, None, ctx.get("bias_4h"), structure, config)
        return _plan_result(3, label, True, direction=direction, score=score, breakdown=breakdown,
                             lines=["Checklist ครบ 3/3 ข้อ", "(คำนวณ SL/TP ของแผนนี้ไม่สำเร็จ)"])

    score, breakdown = generic_plan_score(direction, order["rr"], ctx.get("bias_4h"), structure, config)
    lines = [
        "Checklist ครบ 3/3 ข้อ",
        f"Entry: {order['entry_price']:.4f}", f"SL: {order['stop_loss']:.4f}",
        f"TP (Equilibrium): {order['take_profit']:.4f} (RR {order['rr']})",
        "⚠️ แผนสวนเทรนด์เสี่ยงสูงกว่าแผนอื่น ควรลดขนาดไม้",
    ]
    return _plan_result(3, label, True, direction=direction, score=score, breakdown=breakdown, lines=lines)


def _check_plan4_all(ctx):
    """แผนที่ 4 (Daily Continuation) — ต้องดึง Daily range + 5M เพิ่ม (ดู _fetch_plan4_context)"""
    label = "แผนที่ 4 (Daily Continuation)"
    daily_range, df_5m = _fetch_plan4_context(ctx["symbol"], ctx["config"])
    if not daily_range or df_5m is None:
        return _plan_result(4, label, False, note="ดึงข้อมูล Daily/5M สำหรับแผนนี้ไม่สำเร็จตอนนี้")

    signal = detect_plan4_signal(df_5m)
    if not (signal and signal["direction"] == daily_range["bias"]):
        bias_th = "LONG (discount)" if daily_range["bias"] == "bullish" else "SHORT (premium)"
        return _plan_result(4, label, False,
                             note=f"ยังไม่เข้าเงื่อนไข (Bias ตอนนี้: {bias_th}, "
                                  f"รอ pattern ต่อเนื่องตามทิศทาง bias ก่อน)")

    order = calc_plan4_order(signal, daily_range)
    direction = signal["direction"]
    if not order:
        score, breakdown = generic_plan_score(direction, None, ctx.get("bias_4h"), ctx["structure"], ctx["config"])
        return _plan_result(4, label, True, direction=direction, score=score, breakdown=breakdown,
                             lines=["(คำนวณ SL/TP ของแผนนี้ไม่สำเร็จ)"])

    score, breakdown = generic_plan_score(direction, order["rr"], ctx.get("bias_4h"), ctx["structure"], ctx["config"])
    lines = [
        f"Entry: {order['entry_price']:.4f}", f"SL: {order['stop_loss']:.4f}",
        f"TP (ขอบ Daily range ฝั่งตรงข้าม): {order['take_profit']:.4f} (RR {order['rr']})",
        "⚠️ แผนนี้ถือยาวเป็นชั่วโมง-วัน ไม่ใช่ day-trade แบบแผน 1-3",
    ]
    return _plan_result(4, label, True, direction=direction, score=score, breakdown=breakdown, lines=lines)


def _check_zone_style_plan(num, label, find_fn, calc_fn, ctx, entry_tag="Limit", needs_bias=True):
    """runner กลาง ใช้ร่วมกันสำหรับแผนกลุ่ม Set & Forget ที่หน้าตาเหมือนกันหมด (5/6/7/8) — ต่างกันแค่
    find_fn/calc_fn ที่ใช้ และว่าต้องส่ง bias_4h เข้าไปด้วยหรือไม่ (แผน 7/8 ไม่ใช้ 4H bias)
    find_fn ที่ส่งเข้ามาต้องรับ arg ตามลำดับ (bias_4h, df, config) เมื่อ needs_bias=True หรือ
    (df, config) เมื่อ needs_bias=False เสมอ (ดู _find_zone_adapter/_find_sweep_adapter ด้านล่าง —
    find_zone_entry/find_sweep_entry ของจริงรับลำดับ arg ไม่ตรงกัน ต้องมี adapter ห่อให้ตรงกันก่อน)"""
    df_ind, config = ctx["df_ind"], ctx["config"]
    args = (ctx.get("bias_4h"), df_ind, config) if needs_bias else (df_ind, config)
    result = find_fn(*args)
    if not result["valid"]:
        short_reason = result["reasons"][-1] if result["reasons"] else "ยังไม่เข้าเงื่อนไข"
        return _plan_result(num, label, False, note=short_reason)

    direction = result["direction"]
    order = calc_fn(result, df_ind, config) if needs_bias else calc_fn(result, config)
    if not order:
        score, breakdown = generic_plan_score(direction, None, ctx.get("bias_4h"), ctx["structure"], config)
        lines = list(result["reasons"]) + ["(เจอโอกาสแล้ว แต่ RR ที่คำนวณได้ต่ำกว่าเกณฑ์ขั้นต่ำ — ยังไม่คุ้มเสี่ยง)"]
        return _plan_result(num, label, True, direction=direction, score=score, breakdown=breakdown, lines=lines)

    score, breakdown = generic_plan_score(direction, order["rr"], ctx.get("bias_4h"), ctx["structure"], config)
    lines = list(result["reasons"]) + [
        f"Entry ({entry_tag}): {order['entry_price']:.4f}",
        f"SL: {order['stop_loss']:.4f}",
        f"TP: {order['take_profit']:.4f} (RR {order['rr']})",
    ]
    return _plan_result(num, label, True, direction=direction, score=score, breakdown=breakdown, lines=lines)


def _find_zone_adapter(bias_4h, df, config):
    """find_zone_entry ของจริงรับ arg ตรงลำดับ (bias_4h, df, config) อยู่แล้ว — adapter นี้แค่ทำให้
    ลายเซ็นตรงกับ find_sweep_entry ด้านล่างเป๊ะๆ (ทั้งคู่ต้องรับ (bias_4h, df, config) เพื่อให้
    _check_zone_style_plan เรียกได้แบบเดียวกันทั้งคู่)"""
    return find_zone_entry(bias_4h, df, config)


def _find_sweep_adapter(bias_4h, df, config):
    """find_sweep_entry ของจริงรับ arg คนละลำดับ (df, bias_4h, config) — สลับให้ตรงกับ adapter
    ของแผนที่ 5 ด้านบน (bias_4h, df, config) กันเผลอส่ง dict bias_4h เข้าไปแทน DataFrame ผิดตำแหน่ง
    (บั๊กที่เจอตอน smoke test: ทำให้ find_swings อ่านคอลัมน์ 'high' จาก dict ไม่เจอ)"""
    return find_sweep_entry(df, bias_4h, config)


def _check_all_plans(ctx):
    """เช็คทั้ง 8 แผนพร้อมกัน คืน list ของผลลัพธ์ (ดู _plan_result) เรียงตามเลขแผน 1-8 เสมอ
    (การจัดอันดับคะแนน/ไฮไลต์เทรนด์หลักทำที่ชั้น format ไม่ใช่ชั้นนี้)"""
    results = [
        _check_plan1_all(ctx),
        _check_plan2_all(ctx),
        _check_plan3_all(ctx),
        _check_plan4_all(ctx),
        _check_zone_style_plan(5, "แผนที่ 5 (SMC Zone Entry, Set & Forget)",
                                _find_zone_adapter, calc_zone_entry_order, ctx, needs_bias=True),
        _check_zone_style_plan(6, "แผนที่ 6 (Liquidity Sweep + Displacement, Set & Forget)",
                                _find_sweep_adapter, calc_sweep_entry_order, ctx, needs_bias=True),
        _check_zone_style_plan(7, "แผนที่ 7 (Quasimodo Pattern, Set & Forget)",
                                find_qm_pattern, calc_qm_entry_order, ctx, needs_bias=False),
        _check_zone_style_plan(8, "แผนที่ 8 (Flag Pattern, Set & Forget)",
                                find_flag_pattern, calc_flag_entry_order, ctx, needs_bias=False, entry_tag="Stop"),
    ]
    return results


def _cmd_order_all(ctx):
    """คำสั่ง /order — รวมทั้ง 8 แผนไว้คำสั่งเดียว: เช็คเงื่อนไขทุกแผนพร้อมกันรอบเดียว ไม่มี Confirm
    และไม่บันทึกลง Order Dashboard (ตัดออกทั้งหมดตามที่ผู้ใช้ร้องขอ) แสดงผลตามสภาพจริงล้วนๆ

    ทุกแผนที่ active จะคำนวณคะแนนของตัวเอง (แม้จะไม่ถึง 100) แล้วจัดอันดับจากมากไปน้อยไว้ในหัวข้อ
    "อันดับคะแนน" ก่อน ตามด้วยรายละเอียดเต็มของแต่ละแผนตามลำดับคะแนนเดียวกัน และท้ายสุดคือแผนที่ยัง
    ไม่เข้าเงื่อนไข (บรรทัดเดียวสั้นๆ ต่อแผน) — ไฮไลต์แผนที่ทิศทางตรงกับ "เทรนด์หลัก" (4H Bias ถ้ามี
    ไม่งั้น fallback ไปเทรนด์ 15M) ด้วย ⭐ และแผนที่สวนเทรนด์หลักด้วย ⚠️ ให้เห็นชัดเวลาสัญญาณตีกัน
    """
    results = _check_all_plans(ctx)
    master_trend, master_source = determine_master_trend(ctx.get("bias_4h"), ctx["structure"])

    active = [r for r in results if r["active"]]
    inactive = [r for r in results if not r["active"]]
    active_sorted = sorted(active, key=lambda r: (r["score"] if r["score"] is not None else -1), reverse=True)

    lines = [f"📥 <b>เช็คโอกาสเข้าไม้ทั้ง 8 แผน — {_symbol_label(ctx['symbol'])}</b>", ""]

    if master_trend:
        trend_th = "ขาขึ้น (LONG)" if master_trend == "bullish" else "ขาลง (SHORT)"
        lines.append(f"🧭 เทรนด์หลักตอนนี้ ({master_source}): {trend_th}")
    else:
        lines.append("🧭 เทรนด์หลักตอนนี้: Sideway (ยังไม่มีทิศทางหลักชัดเจนทั้ง 4H และ 15M)")
    lines.append("")

    if not active_sorted:
        lines.append("📭 ยังไม่มีแผนไหนเข้าเงื่อนไขเลยตอนนี้ครับ (เช็คครบทั้ง 8 แผนแล้ว)")
    else:
        lines.append("🏆 <b>อันดับคะแนน (มาก → น้อย)</b>")
        for r in active_sorted:
            direction_th = "LONG" if r["direction"] == "bullish" else "SHORT"
            if master_trend and r["direction"] == master_trend:
                tag = "⭐ ตามเทรนด์หลัก"
            elif master_trend and r["direction"] != master_trend:
                tag = "⚠️ สวนเทรนด์หลัก"
            else:
                tag = ""
            score_text = r["score"] if r["score"] is not None else "-"
            lines.append(f"{r['num']}. {r['label']} — {direction_th} | คะแนน {score_text} {tag}".rstrip())
        lines.append("")
        recommendation = _build_recommendation(active_sorted, master_trend)
        if recommendation:
            lines.extend(recommendation)
            lines.append("")
        lines.append("💡 หมายเหตุ: คะแนนแผนที่ 1 คำนวณจากสูตร Confidence Score ละเอียดเฉพาะของแผนนั้น "
                      "(เต็ม ~120) ส่วนแผนที่ 2-8 คำนวณแบบทั่วไป (เต็ม 100) จากสัญญาณ+RR+ความสอดคล้อง "
                      "เทรนด์ — ใช้เทียบ \"ลำดับความน่าสนใจสัมพัทธ์\" ระหว่างแผน ไม่ใช่ % ความแม่นยำที่เทียบ"
                      "ตรงตัวกันเป๊ะๆ ข้ามแผน โดยเฉพาะเวลาสัญญาณ Buy/Sell สวนกัน ให้ใช้ ⭐/⚠️ ประกอบการตัดสินใจด้วย")
        lines.append("")
        lines.append("📋 <b>รายละเอียดแต่ละแผนที่เข้าเงื่อนไข</b>")
        for r in active_sorted:
            direction_th = "LONG" if r["direction"] == "bullish" else "SHORT"
            lines.append("")
            lines.append(f"✅ <b>{r['label']}</b> — {direction_th}")
            lines.append(_fmt_score_line(r["score"], r["breakdown"]))
            for detail in r["lines"]:
                lines.append(f"   {detail}")

    if inactive:
        lines.append("")
        lines.append("💤 <b>แผนที่ยังไม่เข้าเงื่อนไข</b>")
        for r in inactive:
            lines.append(f"{r['num']}. {r['label']}: {r['note']}")

    if ctx.get("news_blackout", (False, None))[0]:
        lines.append("")
        lines.append("⛔ หมายเหตุ: ตอนนี้อยู่ในช่วงห้ามเทรดรอบข่าวสำคัญ (±60 นาที) ควรพิจารณาความเสี่ยงเพิ่มเติม")

    lines.append("")
    lines.append("(แสดงผลตามสภาพจริง ไม่มีการบันทึกลง Order Dashboard อัตโนมัติแล้ว — ตัดสินใจ/บันทึกเอง)")

    return "\n".join(lines)


def _cmd_trend(ctx):
    structure = ctx["structure"]
    bias_4h = ctx["bias_4h"] or {}
    pd_zone = calc_premium_discount_zone(ctx["df_ind"], ctx["config"].get("structure_lookback", 50))

    lines = [
        f"📈 <b>สรุปแนวโน้ม — {_symbol_label(ctx['symbol'])}</b>",
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


def _cmd_aicheck(ctx):
    """เช็คว่า Central AI Layer (Choice B — ai_layer.py) ใช้งานได้จริงไหม ทำ 2 อย่าง:
    1. ทดสอบเรียก Claude API จริงๆ สั้นๆ (ประหยัด token ไม่ใช้ prompt เต็มของ Central AI Layer)
    2. โชว์สถานะล่าสุดที่ Central AI Layer เคยทำงานจริงจากฝั่ง cron (ai_layer.get_ai_memory_snapshot)
       ถ้ายังไม่เคยมีเลย (ยังไม่มีแผนไหน active ในช่วงเวลาที่อนุญาตมาก่อน) จะบอกตรงๆ ว่ายังไม่เคยเรียก"""
    config = ctx["config"]
    ok, message = ai_layer.test_ai_connection(config)
    icon = "✅" if ok else "❌"
    lines = [f"{icon} <b>เช็คสถานะ AI</b>", "", message]

    memory = ai_layer.get_ai_memory_snapshot(config, ctx["symbol"])
    lines.append("")
    if memory:
        lines.append("📋 <b>ครั้งล่าสุดที่ Central AI Layer ทำงานจริง (ฝั่ง cron)</b>")
        lines.append(f"เวลา: {memory.get('last_ai_call_iso', '-')}")
        lines.append(f"สถานะ: {memory.get('ai_state', '-')}")
        log = memory.get("ai_log") or []
        if log:
            lines.append(f"จำนวนครั้งที่วิเคราะห์สำเร็จ (เก็บย้อนหลังไว้): {len(log)} ครั้งล่าสุด")
    else:
        lines.append("📋 Central AI Layer ยังไม่เคยถูกเรียกจริงเลย (ยังไม่มี Event ที่น่าสนใจเกิดขึ้น "
                      "ในช่วงเวลาที่อนุญาตให้ทำงานมาก่อน)")

    hours = config.get("ai_time_filter_hours", (10, 22))
    lines.append("")
    lines.append(f"ช่วงเวลาที่ AI ทำงาน: จ-ศ {hours[0]}:00-{hours[1]}:00 เวลาไทย")
    lines.append(f"เปิดใช้งานอยู่: {'ใช่' if config.get('ai_analysis_enabled', True) else 'ปิดอยู่'}")

    return "\n".join(lines)


def _cmd_sheetscheck(ctx):
    """เช็คว่า Google Sheets Logging (sheets_log.py) เชื่อมต่อได้จริงไหม — ทดสอบเปิด Spreadsheet จริง
    (ไม่ใช้ cache) + เช็คว่าเจอ worksheet ครบ 3 อัน (Signal_Log/Signal_Context/AI_Log) เพื่อบอกสาเหตุ
    ที่พบบ่อยแยกเป็นข้อความชัดเจน (env var หาย/JSON เพี้ยน/ยังไม่ Share สิทธิ์/ชื่อ Sheet ไม่ตรง)
    แทนที่จะให้เดาว่าทำไม Sheets ไม่มีข้อมูลขึ้น"""
    ok, message = sheets_log.test_sheets_connection()
    icon = "✅" if ok else "❌"
    lines = [f"{icon} <b>เช็คสถานะ Google Sheets</b>", "", message]

    if ok:
        lines.append("")
        lines.append("💡 หมายเหตุ: เชื่อมต่อได้ไม่ได้แปลว่ามีข้อมูลขึ้นทันที — Sheets Log ทำงานแบบ "
                      "Event-Driven เหมือน AI Layer (บันทึกเฉพาะตอนมี Signal ใหม่/เปลี่ยนสถานะจริงๆ "
                      "ผ่าน orders.py เท่านั้น) ถ้ายังไม่มีแผนไหน active เลย ก็ยังไม่มีอะไรให้บันทึก "
                      "เป็นเรื่องปกติ ไม่ใช่บั๊ก")

    return "\n".join(lines)




def _cmd_test(ctx):
    """คำสั่ง /test — End-to-End Test ของทั้งระบบในคำสั่งเดียว

    ต่างจาก /aicheck และ /sheetscheck ที่เช็คแค่ "เชื่อมต่อได้ไหม" ทีละส่วน — /test จะไล่ทดสอบทั้งสาย
    จริงๆ ตั้งแต่ดึงข้อมูล -> เช็คแผน 1-8 -> เรียก Claude API จริง -> เขียน Google Sheets จริง
    โดย "บังคับ" ให้ทำงานทุกขั้นตอน แม้ตอนนี้จะไม่มีแผนไหนเข้าเงื่อนไขก็ตาม (ข้าม Event Detection /
    state hash / cooldown / time filter ทั้งหมด) — ใช้ยืนยันว่าระบบยังไม่พังโดยไม่ต้องรอจังหวะตลาดจริง

    พฤติกรรมตามที่ผู้ใช้ระบุ:
      - มีแผน active -> เรียก AI จริง + เขียน Google Sheets จริง + ส่งผลเข้า Telegram ครบทุกส่วน
      - ไม่มีแผน active -> แจ้งใน Telegram อย่างเดียวว่า "ตอนนี้ไม่มีจุดเข้า" ไม่เขียน Sheets
        (ไม่สร้างข้อมูลปลอมลงฐานข้อมูลจริง — Signal_Log/Signal_Context ต้องมีแต่ข้อมูลจริงเท่านั้น
        ไม่งั้นสถิติที่เอาไปวิเคราะห์ทีหลังจะเพี้ยน)

    หมายเหตุ: /test เรียก Claude API จริง = มีค่าใช้จ่ายจริงต่อครั้ง (ประมาณ 0.4-0.5 บาท) ใช้เท่าที่
    จำเป็นตอนอยากยืนยันว่าระบบทำงาน ไม่ใช่กดรัวๆ เล่น"""
    config = ctx["config"]
    symbol = ctx["symbol"]
    lines = [f"🧪 <b>ทดสอบระบบทั้งสาย (End-to-End) — {_symbol_label(symbol)}</b>", ""]

    # --- ขั้นที่ 1: ข้อมูลตลาด (ถ้ามาถึงตรงนี้ได้ = ดึง TwelveData + คำนวณ indicator สำเร็จแล้ว) ---
    structure = ctx["structure"]
    bias_4h = ctx.get("bias_4h") or {}
    df_ind = ctx["df_ind"]
    current_price = float(df_ind["close"].iloc[-1])
    lines.append(f"1️⃣ ข้อมูลตลาด: ✅ ดึงสำเร็จ (ราคาปัจจุบัน {current_price:.2f})")
    lines.append(f"   เทรนด์ 15M: {structure.get('trend')} | 4H Bias: {bias_4h.get('trend') or '-'}")

    # --- ขั้นที่ 2: เช็คแผน 1-8 (ใช้ตรรกะเดียวกับ /order เป๊ะ) ---
    try:
        results = _check_all_plans(ctx)
        active = [r for r in results if r["active"]]
        lines.append(f"2️⃣ เช็คแผน 1-8: ✅ สำเร็จ (เข้าเงื่อนไข {len(active)} จาก 8 แผน)")
        for r in active:
            direction_th = "LONG" if r["direction"] == "bullish" else "SHORT"
            lines.append(f"   • {r['label']}: {direction_th} (คะแนน {r['score']})")
    except Exception as e:
        lines.append(f"2️⃣ เช็คแผน 1-8: ❌ ล้มเหลว — {e}")
        return "\n".join(lines)

    # --- ขั้นที่ 3-4: ทดสอบ AI + Google Sheets ---
    if not active:
        # ไม่มีแผน active -> ทดสอบการเชื่อมต่อทั้งสองส่วน แต่ไม่เขียนข้อมูลลง Sheets จริง
        lines.append("")
        lines.append("📭 <b>ตอนนี้ไม่มีจุดเข้า</b> — ไม่มีแผนไหนเข้าเงื่อนไขเลย")
        lines.append("   (ไม่บันทึกลง Google Sheets — ไม่สร้างข้อมูลปลอมลงฐานข้อมูลจริง)")
        lines.append("")
        lines.append("3️⃣ ทดสอบเรียก Claude API...")
        ok_ai, ai_msg = ai_layer.test_ai_connection(config)
        lines.append(f"   {'✅' if ok_ai else '❌'} {ai_msg}")
        lines.append("")
        lines.append("4️⃣ ทดสอบเชื่อมต่อ Google Sheets...")
        ok_sheets, sheets_msg = sheets_log.test_sheets_connection()
        lines.append(f"   {'✅' if ok_sheets else '❌'} {sheets_msg}")
        lines.append("")
        lines.append("🎉 <b>สรุป: ระบบพร้อมใช้งาน ไม่พัง</b>" if (ok_ai and ok_sheets)
                     else "⚠️ <b>สรุป: มีบางส่วนใช้งานไม่ได้ (ดูรายละเอียดด้านบน)</b>")
        return "\n".join(lines)

    # --- มีแผน active: บังคับเรียก AI วิเคราะห์จริงเต็มรูปแบบ ---
    # เรียก analyze_market_state() ตรงๆ (ไม่ผ่าน run_central_ai_cycle) เพื่อข้าม Event Detection/
    # state hash/cooldown ที่ปกติจะบล็อกไว้ — /test ต้องรันได้ทุกครั้งที่กด ไม่ว่า state จะซ้ำเดิมไหม
    # ใช้ signal_id ที่ขึ้นต้นด้วย "TEST-" ให้แยกออกชัดเจนจากสัญญาณจริงใน AI_Log
    market_context = {
        "current_price": round(current_price, 3),
        "htf_bias": bias_4h.get("trend"),
        "structure_event_4h": bias_4h.get("event"),
        "zone_4h": bias_4h.get("zone"),
        "trend_1h": ctx.get("higher_tf_trend"),
        "trend_15m": structure.get("trend"),
        "structure_event": structure.get("event"),
        "rsi": round(float(df_ind["rsi"].iloc[-1]), 1) if "rsi" in df_ind.columns else None,
        "macd_hist": round(float(df_ind["macd_hist"].iloc[-1]), 4) if "macd_hist" in df_ind.columns else None,
        "adx": round(float(df_ind["adx"].iloc[-1]), 1) if "adx" in df_ind.columns else None,
        "atr_15m": round(float(df_ind["atr"].iloc[-1]), 3) if "atr" in df_ind.columns else None,
        "ema50_15m": round(float(df_ind["ema_slow"].iloc[-1]), 3) if "ema_slow" in df_ind.columns else None,
        "ema200_15m": round(float(df_ind["ema_trend"].iloc[-1]), 3) if "ema_trend" in df_ind.columns else None,
        "ema50_4h": None, "ema200_4h": None, "ema50_1h": None, "ema200_1h": None,
        "ob_fvg_note": None,
    }
    active_plans = [{
        "id": f"TEST-{symbol}-{r['num']}", "plan": r["label"], "direction": r["direction"],
        "entry": None, "sl": None, "tp": None, "rr": None, "signal_state": "TEST",
    } for r in active]

    lines.append("")
    lines.append("3️⃣ เรียก Claude API วิเคราะห์จริง (บังคับ ข้าม Event/cooldown)...")

    ai_payload = ai_layer.analyze_market_state(
        symbol, active_plans, market_context, config, events=["MANUAL_RECHECK"]
    )

    ai_ok = bool(ai_payload and ai_payload.get("ai_state") == "ANALYZED")
    if ai_ok:
        lines.append("   ✅ AI วิเคราะห์สำเร็จ (ผลเต็มอยู่ในข้อความถัดไป)")
        lines.append("   ✅ บันทึกลง Google Sheets (AI_Log) แล้ว — เช็คได้ในชีต")
    else:
        err = (ai_payload or {}).get("error") if ai_payload else "state ซ้ำเดิม/ติด cooldown"
        lines.append(f"   ❌ AI ไม่ได้วิเคราะห์: {err}")

    messages = ["\n".join(lines)]
    if ai_ok:
        messages.extend(ai_layer.format_ai_telegram_messages(symbol, ai_payload))
    return messages

# หมายเหตุ: /order1-8 และ /confirm1-4 ถูกรวมเป็น /order เดียวแล้ว (เช็คทั้ง 8 แผนพร้อมกันในรอบเดียว
# ไม่มี Confirm อีกต่อไป) คำสั่งเก่าที่ไม่รู้จักจะถูกเมินเงียบๆ ตามพฤติกรรมปกติของ handle_telegram_commands()
# /summary และ /stats ถูกถอดออกตามที่ขอ (ไม่มีคนใช้ รกโค้ด) — หมายเหตุ: การบันทึกออเดอร์อัตโนมัติของฝั่ง
# cron (main.py/plan_runner.py) ยังทำงานตามปกติเหมือนเดิมทุกประการ ไม่ได้ตัดออกไปด้วย (คนละส่วนกัน
# — /summary /stats แค่เป็น "หน้าต่างดูข้อมูลที่บันทึกไว้" ผ่าน Telegram เท่านั้น ไม่ใช่ตัวบันทึกเอง)
COMMAND_HANDLERS = {
    "order": _cmd_order_all,
    "trend": _cmd_trend,
    "news": _cmd_news,
    "status": _cmd_status,
    "aicheck": _cmd_aicheck,
    "sheetscheck": _cmd_sheetscheck,
    "test": _cmd_test,
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

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        # Telegram ส่งคำสั่งกลุ่มมาเป็น "/order@BotName" ต้องตัด @BotName ออกก่อนเทียบ
        text_parts = text[1:].split("@")[0].split()
        command = text_parts[0].lower()
        command_args = text_parts[1:]
        command, command_args = _normalize_order_shorthand(command, command_args)

        # ข้ามคำสั่งเก่าที่ค้างคิวมานาน (เช่นตอน Render suspend ไปนานแล้วเพิ่ง resume) ไม่ไล่ตอบย้อนหลัง
        # ยกเว้น SLOW_COMMANDS ที่ใช้เวลานานเป็นปกติ (ดูเหตุผลเต็มใน run_polling_loop ด้านล่าง)
        msg_age = time.time() - message.get("date", time.time())
        if msg_age > STALE_MESSAGE_SECONDS and command not in SLOW_COMMANDS:
            continue
        handler = COMMAND_HANDLERS.get(command)
        chat_id = message["chat"]["id"]

        if handler:
            # คำสั่งที่ใช้เวลานาน: ตอบรับทันทีก่อนเริ่มทำงานจริง (ดูเหตุผลใน run_polling_loop)
            if command in SLOW_COMMANDS:
                _reply(token, chat_id, "⏳ กำลังทดสอบระบบทั้งสาย... (อาจใช้เวลา 1-2 นาที)")
            try:
                # /order และ /trend รับ argument เลือกคู่เงินได้ (เช่น "/order gold", "/trend eth")
                # ดู SYMBOL_AWARE_COMMANDS — เหตุผลเดียวกับใน run_polling_loop() ด้านล่าง
                if command in SYMBOL_AWARE_COMMANDS:
                    target_symbol, resolve_err = _resolve_symbol_arg(command_args, ctx["symbol"])
                    if resolve_err:
                        reply_text = resolve_err
                    else:
                        reply_text = handler(_build_command_context(target_symbol, config))
                else:
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

                text = (message.get("text") or "").strip()
                if not text.startswith("/"):
                    continue

                text_parts = text[1:].split("@")[0].split()
                command = text_parts[0].lower()
                command_args = text_parts[1:]
                command, command_args = _normalize_order_shorthand(command, command_args)

                # ข้ามคำสั่งเก่าที่ค้างคิวมาตั้งแต่ก่อน instance นี้เริ่ม (เช่นตอน resume จาก suspend)
                # offset ยัง advance ปกติด้านบนแล้ว แค่ไม่ประมวลผล/ไม่ตอบกลับคำสั่งที่ตกยุคนี้
                #
                # ยกเว้นคำสั่งใน SLOW_COMMANDS (เช่น /test) ที่ตั้งใจให้ทำงานหนักและใช้เวลานานเป็นปกติ
                # (ดึงข้อมูลหลาย timeframe + เช็ค 8 แผน + เรียก Claude API + เขียน Google Sheets) — บน
                # Render แพลนฟรีมักเกิน 90 วิ โดยเฉพาะตอน service เพิ่งตื่นจาก sleep ทำให้เคยโดนตัวกรอง
                # นี้ข้ามทิ้งเงียบๆ ไม่ตอบอะไรเลย ทั้งที่ผู้ใช้เพิ่งกดสดๆ (เจอปัญหานี้จริงตอนใช้งาน)
                msg_age = time.time() - message.get("date", time.time())
                if msg_age > STALE_MESSAGE_SECONDS and command not in SLOW_COMMANDS:
                    print(f"[Telegram Bot] ข้ามคำสั่งเก่า (อายุ {msg_age:.0f} วิ) — เกิน {STALE_MESSAGE_SECONDS} วิ")
                    continue

                command = text_parts[0].lower()
                command_args = text_parts[1:]
                command, command_args = _normalize_order_shorthand(command, command_args)
                handler = COMMAND_HANDLERS.get(command)
                chat_id = message["chat"]["id"]
                if not handler:
                    continue  # คำสั่งไม่รู้จัก เมินเงียบๆ

                # คำสั่งที่ใช้เวลานาน: ตอบรับทันทีก่อนเริ่มทำงานจริง ให้ผู้ใช้รู้ว่าบอทได้รับคำสั่งแล้ว
                # (ไม่งั้นจะเงียบเป็นนาที เข้าใจผิดว่าบอทพัง/ไม่ตอบสนอง)
                if command in SLOW_COMMANDS:
                    _reply(token, chat_id, "⏳ กำลังทดสอบระบบทั้งสาย... (อาจใช้เวลา 1-2 นาที)")

                try:
                    # /order และ /trend รับ argument เลือกคู่เงินได้ (เช่น "/order gold", "/trend eth")
                    # ดู SYMBOL_AWARE_COMMANDS — คำสั่งอื่น (/news /status /aicheck) ยังผูกกับ
                    # คู่เงินหลักของ instance นี้เหมือนเดิม ไม่รับ argument
                    if command in SYMBOL_AWARE_COMMANDS:
                        target_symbol, resolve_err = _resolve_symbol_arg(command_args, symbol)
                        if resolve_err:
                            reply_text = resolve_err
                        else:
                            ctx = _build_command_context(target_symbol, config)
                            reply_text = handler(ctx)
                    else:
                        ctx = _build_command_context(symbol, config)
                        reply_text = handler(ctx)
                except Exception as e:
                    reply_text = f"เกิดข้อผิดพลาดตอนประมวลผลคำสั่ง /{command}: {e}"
                _reply(token, chat_id, reply_text)

        except Exception as e:
            # กัน loop ตายทั้งกระบวนการถ้าเน็ตสะดุด/Telegram ล่มชั่วคราว รอสักพักแล้วลองใหม่
            print(f"[Telegram Bot Error] polling loop error: {e}")
            time.sleep(5)
