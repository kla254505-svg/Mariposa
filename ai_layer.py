"""
ai_layer.py — Central AI Second Opinion Layer (Choice B)

สถาปัตยกรรม:
    Market Data -> Plan 1-8 (Strategy, ตัดสินใจ) -> Signal Detector (orders.py, มีอยู่แล้ว)
    -> Memory/State Checker (ไฟล์นี้) -> [Meaningful Event?] -> Central AI (ไฟล์นี้, ให้ความเห็น)
    -> Memory Update -> Telegram (ข้อความเสริม แยกจาก Strategy Alert เดิม)

หลักการที่ต้องคงไว้เสมอ (ห้ามผิด แม้ตอนแก้ไฟล์นี้ในอนาคต):
  - PLAN = DECIDE, AI = REVIEW, MEMORY = REMEMBER, TELEGRAM = NOTIFY
  - AI ห้ามสร้าง/แก้ Entry, SL, TP, Direction, RR ใหม่เด็ดขาด — Strategy (Plan 1-8 ผ่าน orders.py)
    เป็นเจ้าของค่าพวกนี้เพียงผู้เดียว ไฟล์นี้แค่ "อ่าน" ออเดอร์ที่ Strategy สร้างไว้แล้วส่งไปให้ AI
    ประเมิน ไม่เคยส่งค่าที่ AI ตอบกลับมาไปเขียนทับ order ใดๆ ทั้งสิ้น
  - AI Error/Timeout/Rate-limit ต้องไม่ทำให้ Strategy Alert หายไปหรือ Main Loop ล้ม — ทุกฟังก์ชันใน
    ไฟล์นี้ถูกเรียกจาก main.py ภายใต้ try/except ของตัวเองอยู่แล้ว (ดู main.py จุดเรียก) และฟังก์ชัน
    หลัก analyze_market_state() เองก็ไม่โยน exception ออกไปเลย คืนค่า None หรือ dict ที่มี "error" เท่านั้น
  - เรียก AI ได้สูงสุด 1 ครั้งต่อรอบ (ต่อ symbol) ไม่ว่าจะมีกี่แผน active พร้อมกันก็ตาม (ดู
    analyze_market_state ซึ่งเป็นจุดเรียกเดียว ไม่มีฟังก์ชันอื่นเรียก Claude API เลย)
"""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from kvstore import kv_get, kv_set

AI_MEMORY_KEY_PREFIX = "ai_market_state"
AI_LOG_MAX_ENTRIES = 20  # เก็บประวัติการวิเคราะห์ล่าสุดกี่รายการ (bounded — กัน kvdb value โตไม่หยุด)
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

ALLOWED_BIAS = {"BULLISH", "BEARISH", "NEUTRAL"}
ALLOWED_ASSESSMENT = {"VALID", "WEAK", "NEUTRAL", "INVALID"}
ALLOWED_RISK = {"LOW", "MEDIUM", "HIGH"}

SYSTEM_PROMPT = """คุณเป็น "Second Opinion" ให้บอทเทรด ไม่ใช่ตัวตัดสินใจหลัก

กติกาที่ห้ามฝ่าฝืนเด็ดขาด:
1. ห้ามเสนอ Entry, SL, TP, Direction, หรือ RR ใหม่ใดๆ ทั้งสิ้น ค่าพวกนี้ Strategy (แผนการเทรด)
   ตัดสินใจไปแล้วและเป็นค่าสุดท้าย หน้าที่คุณคือประเมินคุณภาพของสัญญาณที่ Strategy สร้างไว้แล้วเท่านั้น
2. ห้ามสร้างข้อมูลตลาดขึ้นมาเอง (ห้ามเดา) ใช้เฉพาะข้อมูลที่ได้รับมาในข้อความเท่านั้น ถ้าข้อมูลไหน
   เป็น null หรือ "not_available" ให้ระบุว่าไม่มีข้อมูลส่วนนั้น อย่าคาดเดาแทน
3. ต้องตอบเป็น JSON ที่ถูกต้องเท่านั้น ห้ามมีข้อความอื่นนอก JSON ห้ามใส่ ```json หรือ markdown fence ใดๆ
   ห้ามมีคำอธิบายก่อน/หลัง JSON

รูปแบบ JSON ที่ต้องตอบ (ทุก field บังคับ):
{
  "overall_bias": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal_assessment": "VALID" | "WEAK" | "NEUTRAL" | "INVALID",
  "confidence": <จำนวนเต็ม 0-100>,
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "conflict": "<ข้อความสั้นๆ ภาษาไทย อธิบายว่ามีแผนไหนขัดกันไหม หรือ 'ไม่มี' ถ้าไม่มี>",
  "reason": "<เหตุผลสั้นๆ ภาษาไทย 1-3 ประโยค ว่าทำไมประเมินแบบนี้ อ้างอิงข้อมูลที่ได้รับมาเท่านั้น>",
  "key_observation": "<ภาษาไทย สิ่งที่ควรจับตาดูต่อไป เช่น เงื่อนไขที่จะยืนยัน/ยกเลิกสมมติฐานนี้>",
  "next_event_to_watch": "<ภาษาไทย เหตุการณ์ถัดไปที่ควรรอดู>"
}
"""


def _now_bangkok():
    return datetime.now(timezone(timedelta(hours=7)))


def is_within_ai_time_window(config, now=None):
    """เช็คว่าตอนนี้อยู่ในช่วงเวลาที่อนุญาตให้เรียก AI ไหม (จันทร์-ศุกร์ 10:00-22:00 เวลาไทย ตาม
    ค่า config ai_time_filter_days/ai_time_filter_hours) — Time Filter นี้คุมเฉพาะ "การเรียก AI/ส่ง
    AI Alert" เท่านั้น ไม่เกี่ยวกับ Strategy (Plan 1-8) ที่ยังทำงาน/ยิง Alert ปกติตลอด 24 ชม.เหมือนเดิม
    ทุกประการ ห้ามเอาไปใช้ gate Strategy logic เด็ดขาด (ตามที่ระบุไว้ในสเปก)"""
    now = now or _now_bangkok()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone(timedelta(hours=7)))
    else:
        now = now.astimezone(timezone(timedelta(hours=7)))

    allowed_days = config.get("ai_time_filter_days", {0, 1, 2, 3, 4})  # Mon=0 ... Sun=6
    start_hour, end_hour = config.get("ai_time_filter_hours", (10, 22))

    if now.weekday() not in allowed_days:
        return False
    return start_hour <= now.hour < end_hour


def _normalize_market_state(symbol, active_plans, market_context, events=None):
    """สร้าง state แบบ normalized (เรียง key/ลำดับคงที่เสมอ) สำหรับเอาไป hash เทียบว่า 'สถานการณ์
    เปลี่ยนไปมีนัยสำคัญไหม' จากรอบก่อนหน้า — เรียง active_plans ตาม plan name กันกรณีลำดับใน list
    สลับกันเฉยๆ (ไม่ได้มีอะไรเปลี่ยนจริง) ทำให้ hash เปลี่ยนโดยไม่จำเป็น

    ตั้งใจไม่ใส่ current_price ดิบๆ ลงใน state เลย (แม้จะมีอยู่ใน market_context) เพราะราคาขยับเล็กน้อย
    ทุก 5 นาทีไม่ควรทำให้ hash เปลี่ยนทุกรอบ — จุดที่ราคาขยับมีนัยสำคัญจริง (เข้าใกล้ entry) ถูกจับด้วย
    event "PRICE_APPROACH_ENTRY" ต่างหากแทน (ดู detect_events) ซึ่งจะรวมอยู่ใน events ที่ส่งเข้ามาที่นี่
    events: sorted list ของ event ที่ detect_events ตรวจเจอในรอบนี้ (None = โหมดเดิมก่อนมี event system)
    """
    plans_normalized = sorted(
        [{"plan": p.get("plan"), "direction": p.get("direction"),
          "signal_state": p.get("signal_state")} for p in active_plans],
        key=lambda p: (p["plan"] or "", p["direction"] or "", p["signal_state"] or ""),
    )
    state = {
        "symbol": symbol,
        "active_plans": plans_normalized,
        "htf_bias": market_context.get("htf_bias"),
        "trend_1h": market_context.get("trend_1h"),
        "trend_15m": market_context.get("trend_15m"),
        "structure_event": market_context.get("structure_event"),
        "structure_event_4h": market_context.get("structure_event_4h"),
        "events": sorted(events) if events else None,
    }
    return state


def _compute_state_hash(normalized_state):
    payload = json.dumps(normalized_state, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_ai_memory(bucket, symbol):
    """โหลด Memory ของ Central AI Layer (คนละก้อนกับ orders.py — อันนั้นคือ Signal Memory ต่อออเดอร์
    แต่ละใบ, อันนี้คือ Market-state Memory ภาพรวมของ symbol นั้น ใช้กันเรียก AI ซ้ำ)"""
    raw = kv_get(bucket, f"{AI_MEMORY_KEY_PREFIX}_{symbol}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_ai_memory(bucket, symbol, memory):
    return kv_set(bucket, f"{AI_MEMORY_KEY_PREFIX}_{symbol}", json.dumps(memory, ensure_ascii=False))


def _strip_json_fence(text):
    """เผื่อ Claude ตอบมาแบบมี ```json ... ``` ห่อ ทั้งที่ system prompt บอกห้ามแล้วก็ตาม (กันเหนียว)"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()


def _validate_ai_response(data):
    """เช็คว่า JSON ที่ Claude ตอบกลับมาครบ field และค่าที่ enum อยู่ในขอบเขตที่กำหนดไว้จริง กัน
    response ที่ parse ผ่านเป็น JSON ได้ แต่โครงสร้าง/ค่าไม่ตรงสเปก (เช่น พิมพ์ 'bullish' ตัวเล็ก
    หรือลืม field) หลุดไปสร้างข้อความ Telegram ที่พังหรือเข้าใจผิดได้"""
    required = {"overall_bias", "signal_assessment", "confidence", "risk_level",
                "conflict", "reason", "key_observation", "next_event_to_watch"}
    if not required.issubset(data.keys()):
        return False
    if data["overall_bias"] not in ALLOWED_BIAS:
        return False
    if data["signal_assessment"] not in ALLOWED_ASSESSMENT:
        return False
    if data["risk_level"] not in ALLOWED_RISK:
        return False
    try:
        conf = int(data["confidence"])
        if not (0 <= conf <= 100):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _call_claude_api(context_text, config):
    """ยิงไป Claude API ตรงๆ (raw requests เหมือนที่โค้ดส่วนอื่นในโปรเจกต์เรียก TwelveData/Telegram
    — ไม่ใช้ SDK เพิ่ม dependency) คืนค่า (parsed_dict, ai_state, error_message)
    ai_state: "ANALYZED" | "ERROR" | "TIMEOUT" — ห้ามโยน exception ออกจากฟังก์ชันนี้เด็ดขาด"""
    api_key = config.get("anthropic_api_key")
    if not api_key:
        return None, "ERROR", "ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY"

    payload = {
        "model": config.get("ai_model", "claude-sonnet-5"),
        # ตั้ง 2000 (เดิม 700 น้อยเกินไป) — เจอปัญหาจริงตอนใช้งาน: โมเดลรุ่นใหม่ใช้ thinking token
        # ก่อนตอบจริง พอโควตาหมดไปกับการคิด เลยไม่เหลือให้เขียน JSON ออกมาเลย ได้ response ที่ไม่มี
        # text block (stop_reason=max_tokens) ทำให้ Central AI Layer ใช้งานไม่ได้ทั้งระบบ
        # JSON ที่ต้องการจริงยาวแค่ ~300-400 token ที่เหลือเผื่อไว้ให้ thinking โดยเฉพาะ
        # หมายเหตุเรื่องต้นทุน: จ่ายตาม token ที่ใช้จริงเท่านั้น ไม่ใช่ตามค่า max_tokens ที่ตั้งไว้
        # การเพิ่มเพดานตรงนี้จึงไม่ได้ทำให้ค่าใช้จ่ายต่อครั้งเพิ่มขึ้นถ้าโมเดลตอบสั้นเท่าเดิม
        "max_tokens": 2000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": context_text}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=45)
    except requests.exceptions.Timeout:
        return None, "TIMEOUT", "เรียก Claude API timeout"
    except requests.exceptions.RequestException as e:
        return None, "ERROR", f"เรียก Claude API ไม่สำเร็จ (network): {e}"

    if resp.status_code == 429:
        return None, "ERROR", "Claude API ตอบ 429 (rate limit) — จะลองใหม่รอบถัดไปที่ state เปลี่ยน"
    if resp.status_code != 200:
        return None, "ERROR", f"Claude API ตอบ HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        body = resp.json()
        # content เป็น list ของ block หลายชนิด (text, thinking, tool_use ฯลฯ) — block แรกไม่จำเป็น
        # ต้องเป็น type "text" เสมอ (เจอปัญหาจริงตอนใช้งาน: KeyError 'text' เพราะไปหยิบ content[0]
        # ตรงๆ แล้วบังเอิญเป็น block ชนิดอื่น) ต้องไล่หา block ที่เป็น text จริงๆ แล้วต่อกันทั้งหมด
        text_parts = [
            b.get("text", "") for b in body.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        # ต่อกันตรงๆ ไม่ใส่ "\n" คั่น — ถ้า Claude แบ่ง JSON ก้อนเดียวออกเป็นหลาย text block
        # (เกิดขึ้นได้กับ response ยาว/streaming) การใส่ newline คั่นกลางจะทำให้ JSON พังทันที
        text = "".join(text_parts).strip()
        if not text:
            stop_reason = body.get("stop_reason")
            return None, "ERROR", (
                f"Claude API ตอบกลับมาแต่ไม่มีข้อความ (stop_reason={stop_reason}) — "
                f"อาจโดนตัดกลางคันเพราะ max_tokens ต่ำไป"
            )
    except Exception as e:
        return None, "ERROR", f"อ่านโครงสร้างผลลัพธ์จาก Claude API ไม่ได้: {e}"

    try:
        parsed = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as e:
        return None, "ERROR", f"Claude ตอบมาไม่ใช่ JSON ที่ parse ได้: {e}"

    if not _validate_ai_response(parsed):
        return None, "ERROR", "JSON ที่ Claude ตอบมาขาด field หรือค่าไม่ตรงสเปกที่กำหนด"

    return parsed, "ANALYZED", None


def _build_ai_context_text(symbol, active_plans, market_context, events=None):
    """ประกอบข้อมูลจริงทั้งหมดเป็นข้อความให้ Claude อ่าน — ใส่เฉพาะข้อมูลที่มีจริง ค่าไหนไม่มีใส่
    'not_available' ตรงๆ (ห้าม AI เดาแทนค่าที่หายไป ตามกติกาใน SYSTEM_PROMPT) รวม MTF context เท่าที่
    ระบบมีข้อมูลจริง — 30M ไม่ได้ถูกดึงที่ไหนในระบบเลยตอนนี้ (มีแค่ 5M/15M/1H(เทรนด์)/4H) จึงใส่
    not_available ทั้งชุดตามกติกา แทนที่จะยิง TwelveData เพิ่มเพียงเพื่อ AI Context (ผิดหลักการ
    'ห้ามยิง API เพิ่มโดยไม่จำเป็น')"""
    def _fmt(v):
        return "not_available" if v is None else v

    lines = [f"Symbol: {symbol}", ""]

    if events:
        lines.append(f"=== Event ที่ทำให้เรียกวิเคราะห์รอบนี้ === \n{', '.join(sorted(events))}")
        lines.append("")

    lines.append("=== 4H ===")
    lines.append(f"Trend: {_fmt(market_context.get('htf_bias'))}")
    lines.append(f"Structure Event: {_fmt(market_context.get('structure_event_4h'))}")
    lines.append(f"Zone: {_fmt(market_context.get('zone_4h'))}")
    lines.append(f"EMA50: {_fmt(market_context.get('ema50_4h'))}")
    lines.append(f"EMA200: {_fmt(market_context.get('ema200_4h'))}")
    lines.append("")
    lines.append("=== 1H ===")
    lines.append(f"Trend: {_fmt(market_context.get('trend_1h'))}")
    lines.append(f"EMA50: {_fmt(market_context.get('ema50_1h'))}")
    lines.append(f"EMA200: {_fmt(market_context.get('ema200_1h'))}")
    lines.append("")
    lines.append("=== 30M ===")
    lines.append("(ไม่มีข้อมูล — ระบบไม่ได้ดึงกรอบเวลานี้เลยตอนนี้) not_available ทั้งหมด")
    lines.append("")
    lines.append("=== 15M ===")
    lines.append(f"Trend: {_fmt(market_context.get('trend_15m'))}")
    lines.append(f"Structure Event: {_fmt(market_context.get('structure_event'))}")
    lines.append(f"EMA50: {_fmt(market_context.get('ema50_15m'))}")
    lines.append(f"EMA200: {_fmt(market_context.get('ema200_15m'))}")
    lines.append(f"RSI: {_fmt(market_context.get('rsi'))}")
    lines.append(f"MACD Histogram: {_fmt(market_context.get('macd_hist'))}")
    lines.append(f"ADX: {_fmt(market_context.get('adx'))}")
    lines.append(f"Order Block / FVG นับเป็น Supply-Demand: {_fmt(market_context.get('ob_fvg_note'))}")
    lines.append(f"Volume: not_available (ฟีด forex/gold ที่ใช้ไม่มี volume จริง)")
    lines.append("")
    lines.append("=== Current Price ===")
    lines.append(f"{_fmt(market_context.get('current_price'))}")
    lines.append("")
    lines.append("=== Strategy Signals (Plan 1-8) — ค่าพวกนี้ตัดสินใจแล้ว ห้ามเสนอค่าใหม่ ===")
    if not active_plans:
        lines.append("(ไม่มีแผนไหน active ในรอบนี้)")
    for p in active_plans:
        lines.append(
            f"- {p.get('plan')} [{p.get('signal_state', 'unknown')}]: {p.get('direction')} | "
            f"Entry {p.get('entry')} | SL {p.get('sl')} | TP {p.get('tp')} | RR {p.get('rr')}"
        )
    return "\n".join(lines)


def _load_orders_safe(bucket, symbol):
    """เรียก orders.load_orders() แบบกันเหนียว — import ในฟังก์ชันกันปัญหา circular import (orders.py
    ไม่ import ai_layer.py กลับมา แต่กันไว้เผื่ออนาคต) อ่านอย่างเดียว ไม่เคยเขียนกลับ"""
    try:
        from orders import load_orders
        return load_orders(bucket, symbol)
    except Exception as e:
        print(f"[AI Layer] {symbol}: อ่าน orders.py ไม่สำเร็จ: {e}")
        return []


def _append_ai_log(memory, symbol, events, ai_result):
    """เก็บประวัติการวิเคราะห์แต่ละครั้งแบบ append (ไม่ overwrite ของเก่าทิ้ง) ตามหลักการ 'ห้าม
    overwrite historical AI analysis' — เก็บแบบ bounded (ล่าสุด AI_LOG_MAX_ENTRIES รายการ) ใน kvdb
    (Runtime Memory) สำหรับ /aicheck อ่านแบบเร็วๆ โดยไม่ต้องยิง Google Sheets API — ประวัติแบบเต็ม/
    ไม่จำกัดจำนวนอยู่ใน Google Sheets AI_Log แทน (ดู _log_ai_to_sheets ด้านล่าง เขียนคู่ขนานกัน)"""
    log = memory.get("ai_log", [])
    log.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "events": sorted(events) if events else [],
        "overall_bias": ai_result.get("overall_bias"),
        "signal_assessment": ai_result.get("signal_assessment"),
        "confidence": ai_result.get("confidence"),
    })
    memory["ai_log"] = log[-AI_LOG_MAX_ENTRIES:]


def _log_ai_to_sheets(active_plans, events, ai_result, config, ai_status, error_message):
    """เรียก sheets_log.py แบบกันเหนียวสุดขีด (เหมือน orders.py._log_to_sheets) — ไม่ให้ Google Sheets
    Logging (ฟีเจอร์เสริม) มีทางทำให้ Central AI Layer (ฟีเจอร์หลักของไฟล์นี้) พังได้เลยไม่ว่ากรณีไหน"""
    try:
        import sheets_log
        signal_ids = [p.get("id") for p in (active_plans or []) if p.get("id")]
        sheets_log.log_ai_analysis(
            signal_ids, events, ai_result, config.get("ai_model", "claude-sonnet-5"),
            ai_status=ai_status, error_message=error_message,
        )
    except Exception as e:
        print(f"[AI Layer] เรียก Sheets Log (AI_Log) ไม่สำเร็จ (ไม่กระทบการทำงานหลัก): {e}")


def detect_events(symbol, config, market_context, current_price, memory=None):
    """ตรวจจับ Event ที่มีความหมายทั้งหมดเทียบกับที่เก็บไว้ใน Memory จากรอบก่อนหน้า:
      - NEW_SIGNAL / ENTRY_HIT / TP_HIT / SL_HIT: จาก diff สถานะออเดอร์ใน orders.py (อ่านอย่างเดียว
        ไม่เคยเขียนกลับไปที่ orders.py เลย — Signal Lifecycle ยังคงเป็นของ Strategy/orders.py เพียง
        ผู้เดียวตามเดิม ที่นี่แค่ "สังเกต" การเปลี่ยนสถานะที่เกิดขึ้นแล้วเท่านั้น)
      - PRICE_APPROACH_ENTRY: ราคาปัจจุบันเข้าใกล้ entry ของออเดอร์ที่ยัง pending ภายใน threshold (x ATR)
      - MARKET_STRUCTURE_CHANGE: BOS/CHoCH ใหม่ (15M หรือ 4H) ต่างจากที่เก็บไว้ล่าสุด
      - HTF_BIAS_CHANGE: เทรนด์ 4H หรือ 1H เปลี่ยนจากที่เก็บไว้ล่าสุด (ไม่นับตอนเพิ่งเริ่มมีข้อมูล
        ครั้งแรกที่ยังไม่มีค่าเก่าให้เทียบ)

    active_plans ที่คืนมาคือออเดอร์ที่ยัง pending/running อยู่ (relevant ต่อเนื่องไม่ว่าจะสร้างมานานแค่
    ไหนแล้ว — ไม่ตัดด้วยเวลาสร้างอีกต่อไป) รวมกับออเดอร์ที่เพิ่งเปลี่ยนสถานะไปเมื่อรอบนี้เอง (win/loss/
    expired ก็ยังส่งให้ AI เห็นผลได้ในรอบที่มันเพิ่งปิดจบพอดี)

    คืนค่า (events: set[str], active_plans: list[dict], updated_memory: dict) — ผู้เรียก
    (run_central_ai_cycle) เป็นคนบันทึก updated_memory กลับเสมอ ไม่ว่าจะมี event เกิดขึ้นหรือไม่ก็ตาม
    เพื่อให้ diff ในรอบถัดไปแม่นยำ"""
    memory = dict(memory or {})
    events = set()

    orders_list = _load_orders_safe(config.get("kvdb_bucket"), symbol)
    prev_snapshot = memory.get("order_status_snapshot", {})
    current_snapshot = {}
    active_plans = []

    for o in orders_list:
        oid = o.get("id")
        status = o.get("status")
        current_snapshot[oid] = status
        prev_status = prev_snapshot.get(oid)
        just_transitioned = False

        if prev_status is None and status in ("pending", "running"):
            events.add("NEW_SIGNAL")
            just_transitioned = True
            # บันทึก Market Snapshot ลง Google Sheets (Signal_Context) ตอนที่เพิ่งเกิด Signal ใหม่นี่
            # แหละคือจังหวะที่ถูกต้องที่สุด (ภาพตลาด ณ ตอนนั้นจริงๆ) — Signal_Log (Signal ID เอง) ถูก
            # บันทึกแยกต่างหากแล้วที่ orders.py (_log_to_sheets ใน add_order/add_pending_order) ไม่ต้อง
            # ทำซ้ำที่นี่ กันเขียนซ้ำ 2 รอบโดยไม่จำเป็น — ห่อ try/except กันเหนียว (ดูเหตุผลเดียวกับ
            # _log_ai_to_sheets ด้านบน: ฟีเจอร์เสริมต้องไม่มีทางทำให้ Event Detection หลักพังได้)
            try:
                import sheets_log
                sheets_log.log_signal_context(oid, symbol, market_context)
            except Exception as e:
                print(f"[AI Layer] เรียก Sheets Log (Signal_Context) ไม่สำเร็จ (ไม่กระทบการทำงานหลัก): {e}")
        elif prev_status == "pending" and status == "running":
            events.add("ENTRY_HIT")
            just_transitioned = True
        elif prev_status == "running" and status == "win":
            events.add("TP_HIT")
            just_transitioned = True
        elif prev_status == "running" and status == "loss":
            events.add("SL_HIT")
            just_transitioned = True

        if status in ("pending", "running") or just_transitioned:
            take_profits = o.get("take_profits") or {}
            tp = take_profits.get("TP1") or (next(iter(take_profits.values())) if take_profits else None)
            active_plans.append({
                "id": oid, "plan": o.get("plan"), "direction": o.get("direction"),
                "entry": o.get("entry_price"), "sl": o.get("stop_loss"), "tp": tp,
                "rr": o.get("rr_tp1"), "signal_state": status,
            })

        if status == "pending" and current_price is not None:
            atr = market_context.get("atr_15m")
            entry_price = o.get("entry_price")
            if atr and entry_price is not None:
                threshold = config.get("ai_price_approach_atr_mult", 0.5) * atr
                if abs(current_price - entry_price) <= threshold:
                    events.add("PRICE_APPROACH_ENTRY")

    memory["order_status_snapshot"] = current_snapshot

    prev_struct_15m = memory.get("last_structure_event_15m")
    cur_struct_15m = market_context.get("structure_event")
    if cur_struct_15m and cur_struct_15m != prev_struct_15m:
        events.add("MARKET_STRUCTURE_CHANGE")
    memory["last_structure_event_15m"] = cur_struct_15m

    prev_struct_4h = memory.get("last_structure_event_4h")
    cur_struct_4h = market_context.get("structure_event_4h")
    if cur_struct_4h and cur_struct_4h != prev_struct_4h:
        events.add("MARKET_STRUCTURE_CHANGE")
    memory["last_structure_event_4h"] = cur_struct_4h

    prev_bias = memory.get("last_htf_bias")
    cur_bias = market_context.get("htf_bias")
    if cur_bias and prev_bias and cur_bias != prev_bias:
        events.add("HTF_BIAS_CHANGE")
    memory["last_htf_bias"] = cur_bias

    prev_1h = memory.get("last_trend_1h")
    cur_1h = market_context.get("trend_1h")
    if cur_1h and prev_1h and cur_1h != prev_1h:
        events.add("HTF_BIAS_CHANGE")
    memory["last_trend_1h"] = cur_1h

    return events, active_plans, memory


def run_central_ai_cycle(symbol, config, market_context, current_price, manual_recheck=False):
    """จุดเรียกหลักจาก main.py (แทนที่การเรียก analyze_market_state() ตรงๆ) — ตรวจ Event ก่อนเสมอ
    (detect_events, ซึ่ง log Signal_Context ไป Google Sheets ให้เองตอนเจอ NEW_SIGNAL ด้วย) แล้วค่อย
    ตัดสินใจว่าควรเรียก AI ไหม บันทึก event-tracking memory ไว้ทุกครั้งไม่ว่าจะเรียก AI จริงหรือไม่
    (กัน diff รอบถัดไปพลาด) ไม่โยน exception ออกไปเลย

    manual_recheck=True: บังคับให้ถือว่ามี event "MANUAL_RECHECK" เพิ่ม แม้ detect_events จะไม่เจอ
    อะไรเปลี่ยนเลยก็ตาม (ยังต้องมี active_plans อย่างน้อย 1 อันอยู่ดีถึงจะเรียก AI จริง — ไม่มีอะไรให้
    AI ดูก็ไม่มีประโยชน์จะเรียก)"""
    bucket = config.get("kvdb_bucket")
    try:
        memory = _load_ai_memory(bucket, symbol)
        events, active_plans, memory = detect_events(symbol, config, market_context, current_price, memory)

        if manual_recheck:
            events.add("MANUAL_RECHECK")

        _save_ai_memory(bucket, symbol, memory)  # บันทึก event-tracking เสมอ ไม่ว่าจะเรียก AI หรือไม่

        if not events or not active_plans:
            return None

        return analyze_market_state(symbol, active_plans, market_context, config, events=sorted(events))
    except Exception as e:
        print(f"[AI Layer] {symbol}: เกิดข้อผิดพลาดใน run_central_ai_cycle: {e}")
        return {"error": str(e), "ai_state": "ERROR"}


def analyze_market_state(symbol, active_plans, market_context, config, events=None):
    """จุดเรียกเดียวของ Central AI Layer ทั้งระบบ — เรียกจาก run_central_ai_cycle() ด้านล่างเท่านั้น
    (ซึ่ง main.py เรียกอีกที) หลังเช็คครบ 8 แผนแล้วเท่านั้น

    active_plans: list ของ dict {plan, direction, entry, sl, tp, rr, signal_state} — อ่านมาจาก
    orders.py (Strategy เป็นคนสร้างค่าพวกนี้ ฟังก์ชันนี้แค่ "อ่าน" ไม่เคยแก้ไข)
    market_context: dict ข้อมูลตลาดปัจจุบัน (ดู _build_ai_context_text ด้านบนว่าใช้ field ไหนบ้าง)
    events: sorted list ของ event ที่ detect_events ตรวจเจอ (None = เรียกแบบไม่มี event system,
    เก็บไว้เพื่อ backward-compat กับตอนเรียกฟังก์ชันนี้ตรงๆ โดยไม่ผ่าน run_central_ai_cycle)

    คืนค่า None ถ้า: ไม่มีแผน active เลย / state ไม่เปลี่ยนจากรอบก่อน (SKIPPED) / อยู่ใน cooldown
    คืนค่า dict {"ai_result":..., "active_plans":..., "ai_state": "ANALYZED"} ถ้าเรียก AI สำเร็จ
    คืนค่า dict {"error": ..., "ai_state": "ERROR"/"TIMEOUT"} ถ้าเรียก AI แล้วพัง (ผู้เรียกควร log
    ไว้เฉยๆ ไม่ต้องส่ง Telegram ต่อ — Strategy Alert เดิมไม่เกี่ยวข้อง ส่งไปแล้วตามปกติอยู่แล้ว)

    ไม่โยน exception ออกจากฟังก์ชันนี้เด็ดขาด (ห่อทุกอย่างด้วย try/except ภายใน)
    """
    bucket = config.get("kvdb_bucket")

    if not active_plans:
        return None  # ไม่มีอะไรให้ AI ดู ไม่เรียก ไม่เสียเงิน

    try:
        memory = _load_ai_memory(bucket, symbol)

        normalized = _normalize_market_state(symbol, active_plans, market_context, events=events)
        current_hash = _compute_state_hash(normalized)

        if memory.get("last_state_hash") == current_hash and memory.get("ai_state") == "ANALYZED":
            return None  # SKIPPED — state เดิมเป๊ะ เคยวิเคราะห์สำเร็จไปแล้ว ไม่เรียกซ้ำ

        # Cooldown กันเรียก AI ซ้อนกันเฉพาะกรณีผิดปกติ (เช่น cron รันซ้อน/เรียกถี่ผิดจังหวะ) — ต้อง
        # ตั้งค่าไว้ "สั้นกว่า" รอบ cron จริงเสมอ (ดูเหตุผลเต็มใน config.py: ai_cooldown_minutes) ไม่งั้น
        # จะไปกันสัญญาณใหม่ที่เกิดขึ้นจริงในรอบถัดไปด้วยโดยไม่ตั้งใจ — ไม่นับรวมตอน SKIPPED ด้านบน
        cooldown_minutes = config.get("ai_cooldown_minutes", 2)
        last_call_iso = memory.get("last_ai_call_iso")
        if last_call_iso:
            try:
                last_call = datetime.fromisoformat(last_call_iso)
                if datetime.now(timezone.utc) - last_call < timedelta(minutes=cooldown_minutes):
                    return None  # ยังอยู่ใน cooldown แม้ state จะเปลี่ยนไปแล้วก็ตาม
            except Exception:
                pass

        context_text = _build_ai_context_text(symbol, active_plans, market_context, events=events)
        ai_result, ai_state, error = _call_claude_api(context_text, config)

        memory["last_ai_call_iso"] = datetime.now(timezone.utc).isoformat()
        memory["ai_state"] = ai_state

        if ai_state == "ANALYZED":
            memory["last_state_hash"] = current_hash  # อัปเดต hash เฉพาะตอนสำเร็จเท่านั้น กัน error
            # ค้างสถานะไว้เป็น "เหมือนวิเคราะห์ไปแล้ว" ทั้งที่จริงยังไม่สำเร็จ (รอบหน้าจะได้ลองใหม่ถ้า
            # state ยังต่างจาก last_state_hash เดิมอยู่)
            memory["last_ai_analysis"] = ai_result
            memory["last_error"] = None  # เคลียร์ error เก่าทิ้งเมื่อรอบล่าสุดสำเร็จ
            _append_ai_log(memory, symbol, events, ai_result)
            _save_ai_memory(bucket, symbol, memory)
            _log_ai_to_sheets(active_plans, events, ai_result, config, "SUCCESS", None)
            return {"ai_result": ai_result, "active_plans": active_plans, "ai_state": "ANALYZED"}

        # เก็บข้อความ error ไว้ใน memory ด้วย (เดิมแค่ print ไปที่ Render log อย่างเดียว มองไม่เห็นผ่าน
        # Telegram) เพื่อให้ /aicheck ดึงมาโชว์ได้ตรงๆ ว่ารอบ cron ล่าสุดพังเพราะอะไร
        memory["last_error"] = error
        _save_ai_memory(bucket, symbol, memory)
        print(f"[AI Layer] {symbol}: เรียก AI ไม่สำเร็จ ({ai_state}): {error}")
        _log_ai_to_sheets(active_plans, events, None, config, ai_state, error)
        return {"error": error, "ai_state": ai_state}

    except Exception as e:
        # กันเหนียวสุดท้าย — ไม่ว่าจะพังตรงไหนในนี้ ต้องไม่หลุดขึ้นไปกระทบ main loop
        print(f"[AI Layer] {symbol}: เกิดข้อผิดพลาดไม่คาดคิดใน analyze_market_state: {e}")
        return {"error": str(e), "ai_state": "ERROR"}


def format_ai_telegram_messages(symbol, ai_payload):
    """แปลงผลลัพธ์จาก analyze_market_state() เป็นข้อความ Telegram (list ของข้อความ — ส่งหลายข้อความ
    แยกกันได้ตามที่ขอ ไม่ยัดรวมเป็นก้อนเดียว) คืน [] ถ้าไม่มีอะไรให้ส่ง (เช่น ai_payload เป็น error)"""
    if not ai_payload or ai_payload.get("ai_state") != "ANALYZED":
        return []

    ai = ai_payload["ai_result"]
    active_plans = ai_payload["active_plans"]

    bias_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(ai["overall_bias"], "🟡")
    assessment_emoji = {"VALID": "✅", "WEAK": "⚠️", "NEUTRAL": "🟡", "INVALID": "❌"}.get(
        ai["signal_assessment"], "🟡"
    )
    risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(ai["risk_level"], "🟡")

    # ข้อความ 1: ภาพรวมแนวโน้ม (ใช้เป็น reference)
    msg1 = (
        f"🤖 <b>AI Second Opinion — {symbol}</b>\n\n"
        f"{bias_emoji} ภาพรวมแนวโน้ม: <b>{ai['overall_bias']}</b>\n"
        f"{assessment_emoji} คุณภาพสัญญาณ: <b>{ai['signal_assessment']}</b> (มั่นใจ {ai['confidence']}%)\n"
        f"{risk_emoji} ระดับความเสี่ยง: <b>{ai['risk_level']}</b>"
    )

    # ข้อความ 2: Order info (Strategy เป็นคนตัดสินใจ — AI แค่แสดงซ้ำให้เห็นในข้อความเดียวกับความเห็น)
    order_lines = [f"📋 <b>สัญญาณจาก Strategy ({symbol})</b>", ""]
    for p in active_plans:
        dir_th = "LONG" if p.get("direction") == "bullish" else "SHORT"
        order_lines.append(
            f"• {p.get('plan')}: {dir_th}\n"
            f"  Entry {p.get('entry')} | SL {p.get('sl')} | TP {p.get('tp')} | RR {p.get('rr')}"
        )
    msg2 = "\n".join(order_lines)

    # ข้อความ 3: เหตุผลย่อ + conflict
    msg3_parts = [f"💡 <b>เหตุผล</b>\n{ai['reason']}"]
    if ai.get("conflict") and ai["conflict"] not in ("ไม่มี", "None", "none", ""):
        msg3_parts.append(f"\n⚠️ <b>จุดที่ขัดแย้งกัน</b>\n{ai['conflict']}")
    msg3 = "\n".join(msg3_parts)

    # ข้อความ 4: จุดสังเกตต่อไป
    msg4 = (
        f"👀 <b>จุดที่ต้องจับตาต่อ</b>\n{ai['key_observation']}\n\n"
        f"🔮 <b>เหตุการณ์ถัดไปที่ควรรอดู</b>\n{ai['next_event_to_watch']}\n\n"
        f"<i>นี่คือความเห็นเสริมจาก AI ประกอบการตัดสินใจ ไม่ใช่คำแนะนำการลงทุน "
        f"Strategy (Entry/SL/TP ด้านบน) เป็นผู้ตัดสินใจหลักเสมอ</i>"
    )

    return [msg1, msg2, msg3, msg4]

def test_ai_connection(config):
    """ทดสอบว่าตั้งค่า AI ถูกต้องและเรียก Claude API ได้จริงไหม (ใช้โดยคำสั่ง /aicheck) — ยิง prompt
    สั้นที่สุดเท่าที่จะทำได้ ไม่ใช้ SYSTEM_PROMPT เต็มของ Central AI Layer เพื่อประหยัด token ตอนแค่
    ทดสอบการเชื่อมต่อ ไม่ได้วิเคราะห์อะไรจริง คืนค่า (ok: bool, message: str) ไม่โยน exception"""
    api_key = config.get("anthropic_api_key")
    if not api_key:
        return False, "ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY บน Render (Environment Variables)"

    payload = {
        "model": config.get("ai_model", "claude-sonnet-5"),
        # ตั้ง 100 (เดิม 10 น้อยเกินไป) — เหตุผลเดียวกับใน _call_claude_api: โมเดลรุ่นใหม่ใช้ thinking
        # token ก่อนตอบ ถ้าเพดานต่ำมากอาจไม่เหลือให้ตอบข้อความจริงเลย ทำให้ /aicheck รายงานผลเพี้ยน
        # ได้ทั้งที่ API ใช้งานได้ปกติ (ยังถือว่าประหยัดมาก เพราะจ่ายตาม token ที่ใช้จริงเท่านั้น)
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "ตอบคำว่า OK คำเดียวพอ ไม่ต้องพูดอะไรเพิ่ม"}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    start = time.time()
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=45)
    except requests.exceptions.Timeout:
        return False, "เรียก Claude API timeout (เกิน 45 วิ) — เช็คเน็ต/ลองใหม่อีกครั้ง"
    except requests.exceptions.RequestException as e:
        return False, f"เรียก Claude API ไม่สำเร็จ (network error): {e}"
    elapsed_ms = int((time.time() - start) * 1000)

    if resp.status_code == 401:
        return False, "API key ไม่ถูกต้อง (HTTP 401) — เช็คว่าคัดลอกมาครบ/ยังไม่หมดอายุ/ยังไม่ถูกลบ"
    if resp.status_code == 429:
        return False, "โดน Rate Limit (HTTP 429) — คีย์ใช้งานได้ แค่ตอนนี้เรียกถี่ไป ลองใหม่อีกสักครู่"
    if resp.status_code != 200:
        return False, f"Claude API ตอบ HTTP {resp.status_code}: {resp.text[:150]}"

    try:
        # ไล่หา block ที่เป็น type "text" จริงๆ (เหตุผลเดียวกับใน _call_claude_api — block แรกไม่
        # จำเป็นต้องเป็น text เสมอ) ที่นี่ถ้าอ่านไม่ได้ก็ไม่ถือว่าล้มเหลว เพราะ HTTP 200 = เชื่อมต่อสำเร็จ
        # แล้วซึ่งเป็นสิ่งที่ฟังก์ชันนี้ต้องการทดสอบจริงๆ
        blocks = resp.json().get("content", [])
        text = "\n".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip() or "(ตอบกลับไม่มีข้อความ แต่ HTTP 200 คือเชื่อมต่อสำเร็จ)"
    except Exception:
        text = "(อ่านข้อความตอบกลับไม่ได้ แต่ HTTP 200 คือเชื่อมต่อสำเร็จ)"

    return True, f"เชื่อมต่อ Claude API สำเร็จ ({elapsed_ms}ms) — โมเดลตอบกลับ: \"{text[:50]}\""


def get_ai_memory_snapshot(config, symbol):
    """ดึงสถานะล่าสุดของ Central AI Layer สำหรับ symbol นี้ (ใช้โดย /aicheck แสดงประกอบ) — คืน dict
    ว่างเปล่า {} ถ้ายังไม่เคยมีการเรียกอะไรเลย (ยังไม่มีแผนไหน active ในช่วงเวลาที่อนุญาตมาก่อน)"""
    return _load_ai_memory(config.get("kvdb_bucket"), symbol)
