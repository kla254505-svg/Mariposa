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
from datetime import datetime, timedelta, timezone

import requests

from kvstore import kv_get, kv_set

AI_MEMORY_KEY_PREFIX = "ai_market_state"
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


def _normalize_market_state(symbol, active_plans, market_context):
    """สร้าง state แบบ normalized (เรียง key/ลำดับคงที่เสมอ) สำหรับเอาไป hash เทียบว่า 'สถานการณ์
    เปลี่ยนไปมีนัยสำคัญไหม' จากรอบก่อนหน้า — เรียง active_plans ตาม plan name กันกรณีลำดับใน list
    สลับกันเฉยๆ (ไม่ได้มีอะไรเปลี่ยนจริง) ทำให้ hash เปลี่ยนโดยไม่จำเป็น"""
    plans_normalized = sorted(
        [{"plan": p.get("plan"), "direction": p.get("direction")} for p in active_plans],
        key=lambda p: (p["plan"] or "", p["direction"] or ""),
    )
    state = {
        "symbol": symbol,
        "active_plans": plans_normalized,
        "htf_bias": market_context.get("htf_bias"),
        "trend_1h": market_context.get("trend_1h"),
        "trend_15m": market_context.get("trend_15m"),
        "structure_event": market_context.get("structure_event"),
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
        "max_tokens": 700,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": context_text}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
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
        text = body["content"][0]["text"]
    except Exception as e:
        return None, "ERROR", f"อ่านโครงสร้างผลลัพธ์จาก Claude API ไม่ได้: {e}"

    try:
        parsed = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as e:
        return None, "ERROR", f"Claude ตอบมาไม่ใช่ JSON ที่ parse ได้: {e}"

    if not _validate_ai_response(parsed):
        return None, "ERROR", "JSON ที่ Claude ตอบมาขาด field หรือค่าไม่ตรงสเปกที่กำหนด"

    return parsed, "ANALYZED", None


def _build_ai_context_text(symbol, active_plans, market_context):
    """ประกอบข้อมูลจริงทั้งหมดเป็นข้อความให้ Claude อ่าน — ใส่เฉพาะข้อมูลที่มีจริง ค่าไหนไม่มีใส่
    'not_available' ตรงๆ (ห้าม AI เดาแทนค่าที่หายไป ตามกติกาใน SYSTEM_PROMPT)"""
    def _fmt(v):
        return "not_available" if v is None else v

    lines = [f"Symbol: {symbol}", ""]
    lines.append("=== Market Context ===")
    lines.append(f"Current Price: {_fmt(market_context.get('current_price'))}")
    lines.append(f"HTF Bias (4H): {_fmt(market_context.get('htf_bias'))}")
    lines.append(f"Trend 1H: {_fmt(market_context.get('trend_1h'))}")
    lines.append(f"Trend 15M: {_fmt(market_context.get('trend_15m'))}")
    lines.append(f"Structure Event (15M): {_fmt(market_context.get('structure_event'))}")
    lines.append(f"RSI (15M): {_fmt(market_context.get('rsi'))}")
    lines.append(f"MACD Histogram (15M): {_fmt(market_context.get('macd_hist'))}")
    lines.append(f"ADX (15M): {_fmt(market_context.get('adx'))}")
    lines.append("")
    lines.append("=== Strategy Signals (Plan 1-8) — ค่าพวกนี้ตัดสินใจแล้ว ห้ามเสนอค่าใหม่ ===")
    if not active_plans:
        lines.append("(ไม่มีแผนไหน active ในรอบนี้)")
    for p in active_plans:
        lines.append(
            f"- {p.get('plan')}: {p.get('direction')} | Entry {p.get('entry')} | "
            f"SL {p.get('sl')} | TP {p.get('tp')} | RR {p.get('rr')}"
        )
    return "\n".join(lines)


def analyze_market_state(symbol, active_plans, market_context, config):
    """จุดเรียกเดียวของ Central AI Layer ทั้งระบบ — เรียกจาก main.py หลังเช็คครบ 8 แผนแล้วเท่านั้น

    active_plans: list ของ dict {plan, direction, entry, sl, tp, rr} — อ่านมาจาก orders.py (Strategy
    เป็นคนสร้างค่าพวกนี้ ฟังก์ชันนี้แค่ "อ่าน" ไม่เคยแก้ไข)
    market_context: dict ข้อมูลตลาดปัจจุบัน (ดู _build_ai_context_text ด้านบนว่าใช้ field ไหนบ้าง)

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

        normalized = _normalize_market_state(symbol, active_plans, market_context)
        current_hash = _compute_state_hash(normalized)

        if memory.get("last_state_hash") == current_hash and memory.get("ai_state") == "ANALYZED":
            return None  # SKIPPED — state เดิมเป๊ะ เคยวิเคราะห์สำเร็จไปแล้ว ไม่เรียกซ้ำ

        # Cooldown กันเรียก AI ซ้อนกันเฉพาะกรณีผิดปกติ (เช่น cron รันซ้อน/เรียกถี่ผิดจังหวะ) — ต้อง
        # ตั้งค่าไว้ "สั้นกว่า" รอบ cron จริงเสมอ (ดูเหตุผลเต็มใน config.py: ai_cooldown_minutes) ไม่งั้น
        # จะไปกันสัญญาณใหม่ที่เกิดขึ้นจริงในรอบถัดไปด้วยโดยไม่ตั้งใจ — ไม่นับรวมตอน SKIPPED ด้านบน
        cooldown_minutes = config.get("ai_cooldown_minutes", 10)
        last_call_iso = memory.get("last_ai_call_iso")
        if last_call_iso:
            try:
                last_call = datetime.fromisoformat(last_call_iso)
                if datetime.now(timezone.utc) - last_call < timedelta(minutes=cooldown_minutes):
                    return None  # ยังอยู่ใน cooldown แม้ state จะเปลี่ยนไปแล้วก็ตาม
            except Exception:
                pass

        context_text = _build_ai_context_text(symbol, active_plans, market_context)
        ai_result, ai_state, error = _call_claude_api(context_text, config)

        memory["last_ai_call_iso"] = datetime.now(timezone.utc).isoformat()
        memory["ai_state"] = ai_state

        if ai_state == "ANALYZED":
            memory["last_state_hash"] = current_hash  # อัปเดต hash เฉพาะตอนสำเร็จเท่านั้น กัน error
            # ค้างสถานะไว้เป็น "เหมือนวิเคราะห์ไปแล้ว" ทั้งที่จริงยังไม่สำเร็จ (รอบหน้าจะได้ลองใหม่ถ้า
            # state ยังต่างจาก last_state_hash เดิมอยู่)
            memory["last_ai_analysis"] = ai_result
            _save_ai_memory(bucket, symbol, memory)
            return {"ai_result": ai_result, "active_plans": active_plans, "ai_state": "ANALYZED"}

        _save_ai_memory(bucket, symbol, memory)
        print(f"[AI Layer] {symbol}: เรียก AI ไม่สำเร็จ ({ai_state}): {error}")
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
