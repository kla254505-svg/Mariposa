"""
orderai.py — คำสั่ง /orderai (เพิ่มเมื่อ 19 พ.ย. 2026)

ทำอะไร:
  1. พิมพ์ /orderai -> บอทเช็คทั้ง 8 แผน (ใช้ _check_all_plans เดียวกับ /order) แล้วส่งให้ AI ช่วยตัดสิน
  2. AI ตอบสั้นๆ: เข้า / รอก่อน / ไม่เข้า + เหตุผลง่ายๆ + จุดเข้า (ถ้ามี) ท้ายข้อความมีปุ่ม ✅ วางแล้ว / ❌ ไม่วาง
  3. กด "วางแล้ว" -> บอทบันทึกและติดตามออเดอร์ (ทุก 5 นาทีในรอบ cron — ดู track_orders) แจ้งเมื่อเข้า/ปิด/ควรยกเลิก
     กด "ไม่วาง" หรือไม่กด -> ปล่อยผ่าน ไม่แจ้งอะไรอีก

หลักการสำคัญ:
  - ตัวเลข Entry/SL/TP มาจากแผนของบอทเท่านั้น AI ได้แค่ "เลือก/ปฏิเสธ" แผนที่เสนอ ไม่คิดราคาเอง
    และโค้ดตรวจซ้ำอีกชั้น (ทิศ SL/TP, ระยะ SL ขั้นต่ำ, RR ขั้นต่ำ, ราคาหนีไปแล้วหรือยัง)
  - ปุ่มเป็นของเจ้าของบอทคนเดียว หมดอายุตาม config["orderai_button_ttl_minutes"]
  - บอทไม่ได้ยิงออเดอร์จริง "วางแล้ว" = ผู้ใช้วางเองในโบรกแล้ว บอทแค่บันทึก+ติดตาม
  - ถ้า AI ล่ม/ตอบไม่ได้ จะตอบจากกฎของแผนแทน (ไม่เงียบ)
  - AI เรียกเฉพาะตอนพิมพ์ /orderai เท่านั้น การติดตามออเดอร์ใช้กฎในโค้ดล้วน ไม่เรียก AI ซ้ำ
"""

import html
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

from kvstore import kv_get, kv_set

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
THAI_TZ = timezone(timedelta(hours=7))

PLAN_KEY_BY_NUM = {
    1: "plan1_pullback", 2: "plan2_breakout", 3: "plan3_counter_trend", 4: "plan4_daily_continuation",
    5: "plan5_zone_single", 6: "plan6_sweep_general", 7: "plan7_qm_pattern", 8: "plan8_flag_pattern",
}

# สถิติจริงจาก Signal_Log (2 ก.ย.–7 พ.ย. 2026, ไม้ที่ปิดแล้ว 326 ไม้ รวม +181R ถ้าไม่นับ P4 เหลือราวๆ +27R) ป้อนให้ AI ตัดสินจากหลักฐาน
# แก้/อัปเดตได้ที่นี่ เมื่อมีข้อมูลชีตใหม่สะสมมากพอ  (n = จำนวนไม้, win = % ชนะ, net_r = รวม R)
PLAN_STATS = {
    1: {"n": 94, "win": 41, "net_r": 3.5, "note": "เสมอตัว"},
    2: {"n": 83, "win": 22, "net_r": -40.2, "note": "ปิดใช้งานแล้ว"},
    3: {"n": 3, "win": 67, "net_r": -0.6, "note": "ตัวอย่างน้อยมาก"},
    4: {"n": 45, "win": 24, "net_r": 154.7,
        "note": "กำไรมาจากไม่กี่เหตุการณ์ (ไม้เดียว +72R) ค่ากลางคือ -1R ยังเชื่อมากไม่ได้"},
    5: {"n": 65, "win": 22, "net_r": 42.1, "note": "ดี"},
    6: {"n": 6, "win": 17, "net_r": 8.3, "note": "ตัวอย่างน้อย"},
    7: {"n": 16, "win": 25, "net_r": 10.6, "note": "ตัวอย่างน้อย"},
    8: {"n": 14, "win": 36, "net_r": 2.9, "note": "ตัวอย่างน้อย"},
}

VERDICT_LABEL = {
    "ENTER": "✅ <b>ควรเข้า</b>",
    "WAIT": "⏳ <b>รอก่อน</b>",
    "NO": "🚫 <b>ไม่ควรเข้า</b>",
    "RULES": "🧮 <b>สรุปตามกฎของแผน (AI ใช้ไม่ได้ตอนนี้)</b>",
}


def _esc(v):
    return html.escape(str(v), quote=False)


def _now():
    return datetime.now(timezone.utc)


def _dir_th(direction):
    return "LONG (ซื้อ)" if direction == "bullish" else "SHORT (ขาย)"


# ══════════════════════════════════════════════════════
# 1) ตรวจ setup ด้วยกฎในโค้ด (ไม่พึ่ง AI)
# ══════════════════════════════════════════════════════
def validate_trade(direction, entry, sl, tp, price, config):
    """คืน dict: ok, reason, rr, entry_side, progress  — ok=False พร้อมเหตุผลสั้นๆ ถ้าไม่ผ่าน"""
    try:
        entry, sl, tp, price = float(entry), float(sl), float(tp), float(price)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "ตัวเลขราคาไม่ครบ", "rr": None, "entry_side": None, "progress": None}

    bullish = direction == "bullish"
    if bullish and not (sl < entry < tp):
        return {"ok": False, "reason": "SL/TP อยู่ผิดฝั่งของ Entry", "rr": None, "entry_side": None, "progress": None}
    if (not bullish) and not (sl > entry > tp):
        return {"ok": False, "reason": "SL/TP อยู่ผิดฝั่งของ Entry", "rr": None, "entry_side": None, "progress": None}

    sl_dist = abs(entry - sl)
    rr = round(abs(tp - entry) / sl_dist, 2) if sl_dist else 0
    entry_side = "below" if entry <= price else "above"
    progress = (price - entry) / (tp - entry)  # 0 = ที่ Entry, 1 = ถึง TP (ติดลบ = ไปทางตรงข้าม)

    min_sl = config.get("min_sl_distance", 0)
    if min_sl and sl_dist < min_sl - 1e-9:
        return {"ok": False, "reason": f"ระยะ SL แคบเกินไป ({sl_dist:.2f} < ขั้นต่ำ {min_sl})",
                "rr": rr, "entry_side": entry_side, "progress": progress}
    if rr < config.get("min_rr", 1.2):
        return {"ok": False, "reason": f"RR ต่ำกว่าเกณฑ์ ({rr} < {config.get('min_rr', 1.2)})",
                "rr": rr, "entry_side": entry_side, "progress": progress}
    if (bullish and price <= sl) or ((not bullish) and price >= sl):
        return {"ok": False, "reason": "ราคาปัจจุบันถึง SL ไปแล้ว", "rr": rr,
                "entry_side": entry_side, "progress": progress}
    if progress >= config.get("orderai_missed_progress", 0.5):
        return {"ok": False, "reason": f"ราคาวิ่งไปทาง TP แล้ว {progress * 100:.0f}% ตกรถแล้ว",
                "rr": rr, "entry_side": entry_side, "progress": progress}
    return {"ok": True, "reason": None, "rr": rr, "entry_side": entry_side, "progress": progress}


def collect_candidates(results, price, config):
    """แปลงผล _check_all_plans() เป็นรายการผู้สมัคร (candidate) พร้อมผลตรวจกฎ"""
    disabled = config.get("plans_disabled", ()) or ()
    cands = []
    for r in results:
        if not r.get("active"):
            continue
        num = r["num"]
        plan_key = PLAN_KEY_BY_NUM.get(num)
        if plan_key in disabled:
            continue
        t = r.get("trade")
        if not t:
            continue
        v = validate_trade(r["direction"], t["entry"], t["sl"], t["tp"], price, config)
        cands.append({
            "num": num, "plan_key": plan_key, "label": r["label"], "direction": r["direction"],
            "entry": round(float(t["entry"]), 3), "sl": round(float(t["sl"]), 3), "tp": round(float(t["tp"]), 3),
            "rr": v["rr"], "score": r.get("score"), "valid": v["ok"], "invalid_reason": v["reason"],
            "entry_side": v["entry_side"], "note": t.get("note"), "stats": PLAN_STATS.get(num),
        })
    return cands


# ══════════════════════════════════════════════════════
# 2) AI
# ══════════════════════════════════════════════════════
SYSTEM_PROMPT = """คุณเป็นผู้ช่วยตัดสินใจเทรดทอง (XAUUSD) ให้เจ้าของบอทที่เทรดมือบนโบรกเอง เขาอ่านภาษาไทยง่ายๆ ไม่ต้องศัพท์เทคนิคเยอะ
งานของคุณ: ดูข้อมูลตลาดและ "ผู้สมัครออเดอร์" ที่บอทคำนวณมาให้ แล้วตัดสินว่าตอนนี้ควรเข้าไหม

กติกาเหล็ก:
1. ห้ามคิดราคา Entry/SL/TP เอง ให้เลือกได้เฉพาะ "หมายเลขแผน" ที่ valid=true ในรายการผู้สมัครเท่านั้น
2. verdict มี 3 ค่า: ENTER (ควรเข้า — ต้องระบุ pick เป็นหมายเลขแผน), WAIT (ยังไม่ควรเข้าตอนนี้แต่ยังมีลุ้น บอกว่ารออะไร), NO (ไม่ควรเข้า)
3. ถ้าไม่มีผู้สมัคร valid เลย ต้องตอบ WAIT หรือ NO และ pick = null
4. ตัดสินจากหลักฐาน: ทิศเทียบเทรนด์ 4H/1H/15M, สถิติจริงของแต่ละแผน, ข่าวใกล้ๆ, session, โซน premium/discount, ความขัดแย้งระหว่างแผน
   แผนที่สถิติแย่หรือกำไรไม่แน่ชัด ให้ระวังและบอกตรงๆ อย่าเชียร์เกินจริง
5. ห้ามแต่งข้อมูลที่ไม่มีให้ ค่าไหนเป็น not_available ให้บอกว่าไม่มีข้อมูล
6. ตอบเป็น JSON ก้อนเดียว ไม่มี markdown ไม่มีข้อความอื่น:
{"verdict":"ENTER|WAIT|NO","pick":<เลขแผนหรือ null>,"confidence":<0-100>,
 "reasons":["เหตุผลสั้นๆ 1","เหตุผลสั้นๆ 2","เหตุผลสั้นๆ 3 (ไม่บังคับ)"],
 "risk_note":"คำเตือนสั้นๆ 1 บรรทัด หรือสตริงว่าง",
 "no_entry_reason":"ถ้าไม่มีจุดเข้า: สาเหตุเบื้องต้นสั้นๆ 1-2 ประโยค ไม่ยืดเยื้อ ไม่งั้นสตริงว่าง"}
reasons แต่ละข้อไม่เกิน ~110 ตัวอักษร ภาษาไทยอ่านง่าย"""


def _fmt(v):
    return "not_available" if v is None else v


def _upcoming_news(config, symbol):
    """ข่าวแรงที่กำลังจะมาภายใน ~3 ชม. หรือเพิ่งออก จาก cache ปฏิทินใน kvdb (ไม่ยิง API เพิ่ม)"""
    out = []
    try:
        raw = kv_get(config["kvdb_bucket"], f"calendar_events_{symbol}")
        if not raw:
            return out
        payload = json.loads(raw)
        now = _now()
        for e in payload.get("events", []):
            try:
                t = datetime.fromisoformat(e["time"])
            except Exception:
                continue
            diff = (t - now).total_seconds() / 60
            if -30 <= diff <= 180:
                out.append(f"{e.get('title', '?')} ({e.get('impact', '?')}) อีก {diff:.0f} นาที")
    except Exception:
        pass
    return out[:4]


def build_context_text(ctx, cands, inactive, master_trend, master_source, config):
    df = ctx["df_ind"]
    last = df.iloc[-1]

    def col(name, nd=2):
        try:
            return round(float(last[name]), nd) if name in df.columns else None
        except Exception:
            return None

    bias = ctx.get("bias_4h") or {}
    structure = ctx.get("structure") or {}
    price = round(float(last["close"]), 3)
    blackout = ctx.get("news_blackout", (False, None))
    lines = [
        f"Symbol: {ctx['symbol']}",
        f"เวลา (ไทย): {datetime.now(THAI_TZ).strftime('%Y-%m-%d %H:%M')}",
        f"ราคาปัจจุบัน: {price}",
        f"4H: เทรนด์={_fmt(bias.get('trend'))} เหตุการณ์={_fmt(bias.get('event'))} โซน={_fmt(bias.get('zone'))}",
        f"1H เทรนด์: {_fmt(ctx.get('higher_tf_trend'))}",
        f"15M: เทรนด์={_fmt(structure.get('trend'))} เหตุการณ์={_fmt(structure.get('event'))}",
        f"เทรนด์หลักที่บอทสรุป: {_fmt(master_trend)} (จาก {master_source})",
        f"ตัวชี้วัด 15M: RSI={_fmt(col('rsi', 1))} ADX={_fmt(col('adx', 1))} ATR={_fmt(col('atr'))} MACD_hist={_fmt(col('macd_hist', 3))}",
        f"Session: {json.dumps(ctx.get('session_info'), default=str, ensure_ascii=False)}",
        f"ช่วงห้ามเทรดรอบข่าว (±60 นาที): {'ใช่ — ' + str((blackout[1] or {}).get('title')) if blackout[0] else 'ไม่'}",
    ]
    news = _upcoming_news(config, ctx["symbol"])
    lines.append("ข่าวใกล้ๆ: " + ("; ".join(news) if news else "ไม่มีในช่วง 3 ชม. ข้างหน้า"))
    lines.append("")
    lines.append("== ผู้สมัครออเดอร์ (ตัวเลขมาจากบอท ห้ามแก้) ==")
    if not cands:
        lines.append("(ไม่มีแผนไหนเข้าเงื่อนไข)")
    for c in cands:
        st = c["stats"] or {}
        lines.append(
            f"แผน {c['num']} | {c['label']} | {'LONG' if c['direction'] == 'bullish' else 'SHORT'} | "
            f"Entry {c['entry']} SL {c['sl']} TP {c['tp']} RR {c['rr']} | คะแนน {_fmt(c['score'])} | "
            f"valid={str(c['valid']).lower()}"
            + (f" (ไม่ผ่านเพราะ: {c['invalid_reason']})" if not c["valid"] else "")
            + (f" | หมายเหตุ: {c['note']}" if c.get("note") else "")
            + f" | สถิติจริงแผนนี้: ชนะ {st.get('win', '?')}% จาก {st.get('n', '?')} ไม้ รวม {st.get('net_r', '?')}R ({st.get('note', '')})"
        )
    lines.append("")
    lines.append("== แผนที่ยังไม่เข้าเงื่อนไข ==")
    for r in inactive[:8]:
        lines.append(f"แผน {r['num']}: {r.get('note') or '-'}")
    return "\n".join(lines)


def _extract_json(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1 or b < a:
        raise ValueError("ไม่พบ JSON ในคำตอบ")
    return json.loads(t[a:b + 1])


def call_ai(context_text, config):
    """คืน (parsed|None, error|None, usage dict) — ไม่โยน exception"""
    key = config.get("anthropic_api_key")
    if not key:
        return None, "ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY", {}
    payload = {
        "model": config.get("ai_model", "claude-sonnet-5-5"),
        "max_tokens": config.get("orderai_max_tokens", 3000),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": context_text}],
    }
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_API_VERSION, "content-type": "application/json"}
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload,
                             timeout=config.get("orderai_timeout_seconds", 60))
    except requests.exceptions.Timeout:
        return None, "AI ตอบช้าเกินเวลา (timeout)", {}
    except requests.exceptions.RequestException as e:
        return None, f"เชื่อมต่อ AI ไม่ได้: {e}", {}
    if resp.status_code != 200:
        return None, f"AI ตอบ HTTP {resp.status_code}: {resp.text[:300]}", {}
    try:
        body = resp.json()
        text = "".join(b.get("text", "") for b in body.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text").strip()
        usage = body.get("usage") or {}
        if body.get("stop_reason") == "max_tokens" and not text:
            return None, "AI ตอบไม่ครบ (max_tokens)", usage
        return _extract_json(text), None, usage
    except Exception as e:
        return None, f"อ่านคำตอบ AI ไม่ได้: {e}", {}


def sanitize_ai(data, cands):
    """ตรวจคำตอบ AI: verdict ต้องถูกต้อง ENTER ต้องเลือกแผน valid จริง ไม่งั้นลดระดับเป็น NO"""
    verdict = str(data.get("verdict", "")).upper().strip()
    if verdict not in ("ENTER", "WAIT", "NO"):
        raise ValueError("verdict ไม่ถูกต้อง")
    pick = data.get("pick")
    try:
        pick = int(pick) if pick is not None else None
    except (TypeError, ValueError):
        pick = None
    valid_nums = {c["num"] for c in cands if c["valid"]}
    downgraded = None
    if verdict == "ENTER" and pick not in valid_nums:
        verdict, pick = "NO", None
        downgraded = "AI เลือกแผนที่ไม่ผ่านการตรวจของโค้ด จึงปรับเป็นไม่เข้า"
    if verdict != "ENTER":
        pick = None
    reasons = [str(x).strip()[:160] for x in (data.get("reasons") or []) if str(x).strip()][:3]
    try:
        conf = max(0, min(100, int(data.get("confidence"))))
    except (TypeError, ValueError):
        conf = None
    return {
        "verdict": verdict, "pick": pick, "confidence": conf, "reasons": reasons,
        "risk_note": str(data.get("risk_note") or "").strip()[:200],
        "no_entry_reason": str(data.get("no_entry_reason") or "").strip()[:300],
        "downgraded": downgraded,
    }


def rules_only(cands, master_trend):
    """สรุปจากกฎล้วนเมื่อ AI ใช้ไม่ได้: เลือกแผน valid ที่ตามเทรนด์หลักและคะแนนสูงสุด"""
    valid = [c for c in cands if c["valid"]]
    aligned = [c for c in valid if master_trend and c["direction"] == master_trend]
    pool = aligned or []
    pick = max(pool, key=lambda c: (c["score"] if c["score"] is not None else -1)) if pool else None
    reasons = []
    if pick:
        reasons.append(f"แผน {pick['num']} เข้าเงื่อนไข ผ่านการตรวจ และทิศตรงกับเทรนด์หลัก")
    elif valid:
        reasons.append("มีแผนเข้าเงื่อนไข แต่สวนเทรนด์หลัก/เทรนด์ไม่ชัด")
    else:
        reasons.append("ไม่มีแผนไหนผ่านเงื่อนไขและการตรวจของโค้ดตอนนี้")
    return {"verdict": "RULES", "pick": pick["num"] if pick else None, "confidence": None, "reasons": reasons,
            "risk_note": "ยังไม่มี AI ช่วยตรวจบริบท/ข่าว ตัดสินใจเองอีกชั้น", "no_entry_reason": "", "downgraded": None}


# ══════════════════════════════════════════════════════
# 3) ข้อความ + ปุ่ม + Proposal
# ══════════════════════════════════════════════════════
def _stats_line(num):
    st = PLAN_STATS.get(num)
    if not st:
        return ""
    return f"📊 สถิติจริงแผนนี้: ชนะ {st['win']}% จาก {st['n']} ไม้ รวม {st['net_r']:+.1f}R"


def format_message(symbol_label, price, decision, cands, ai_error, config, bucket, sizing_fn=None):
    verdict = decision["verdict"]
    lines = [f"🤖 <b>/orderai — {_esc(symbol_label)}</b>", f"ราคา {price:.2f} · {datetime.now(THAI_TZ).strftime('%H:%M')} น.", "",
             VERDICT_LABEL[verdict]]
    if decision.get("confidence") is not None:
        lines[-1] += f"  (มั่นใจ {decision['confidence']}%)"
    if decision["reasons"]:
        lines.append("")
        lines.append("<b>สาเหตุ</b>")
        for r in decision["reasons"]:
            lines.append(f"• {_esc(r)}")
    if decision.get("downgraded"):
        lines.append(f"⚠️ {_esc(decision['downgraded'])}")

    pick = next((c for c in cands if c["num"] == decision["pick"]), None) if decision["pick"] else None
    lines.append("")
    if pick:
        kind = "Limit (รอราคาย่อมา)" if pick["entry_side"] == "below" else "Stop (รอราคาทะลุไป)"
        lines += [
            f"📍 <b>จุดเข้า — {_esc(pick['label'])} · {_dir_th(pick['direction'])}</b>",
            f"Entry ({kind}): <b>{pick['entry']:.2f}</b>",
            f"SL: <b>{pick['sl']:.2f}</b>  (ห่าง {abs(pick['entry'] - pick['sl']):.1f})",
            f"TP: <b>{pick['tp']:.2f}</b>  (RR {pick['rr']})",
            _stats_line(pick["num"]),
        ]
        if pick.get("note"):
            lines.append(f"⚠️ {_esc(pick['note'])}")
        if sizing_fn:
            lines.append(sizing_fn(pick).strip())
    else:
        lines.append("📍 <b>จุดเข้า: ไม่มี</b>")
        why = decision.get("no_entry_reason")
        if not why:
            invalid = [c for c in cands if not c["valid"]]
            valid = [c for c in cands if c["valid"]]
            if valid:
                why = ("มีแผนเข้าเงื่อนไข (" + ", ".join(f"แผน {c['num']}" for c in valid)
                       + ") แต่ยังไม่ถึงขั้นควรเข้า — ดูสาเหตุด้านบน")
            elif invalid:
                why = "แผนที่เจอไม่ผ่านการตรวจ: " + "; ".join(f"แผน {c['num']} {c['invalid_reason']}" for c in invalid[:3])
            else:
                why = "ยังไม่มีแผนไหนเข้าเงื่อนไข"
        lines.append(_esc(why))
    if decision.get("risk_note"):
        lines.append(f"\n⚠️ {_esc(decision['risk_note'])}")
    if ai_error:
        lines.append(f"\n🛠 AI: {_esc(ai_error)[:160]}")
    if pick:
        lines.append(f"\n👇 ออเดอร์นี้คุณวางเองในโบรกแล้วหรือยัง? (ปุ่มหมดอายุใน {config.get('orderai_button_ttl_minutes', 15)} นาที)")
    return "\n".join(l for l in lines if l is not None)


def _prop_key(pid):
    return f"orderai_prop_{pid}"


def _tracked_key(symbol):
    return f"orderai_tracked_{symbol}"


def save_proposal(config, prop):
    return kv_set(config["kvdb_bucket"], _prop_key(prop["id"]), json.dumps(prop, ensure_ascii=False))


def load_proposal(config, pid):
    raw = kv_get(config["kvdb_bucket"], _prop_key(pid))
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def build_keyboard(pid):
    return {"inline_keyboard": [[
        {"text": "✅ วางแล้ว", "callback_data": f"oai:p:{pid}"},
        {"text": "❌ ไม่วาง", "callback_data": f"oai:s:{pid}"},
    ]]}


def analyze(config, ctx, results, master_trend, master_source, symbol_label, sizing_fn=None):
    """ทำทั้งกระบวนการ คืน dict: text, keyboard (หรือ None), proposal (dict สำหรับเก็บ/บันทึก Sheets)"""
    price = float(ctx["df_ind"]["close"].iloc[-1])
    cands = collect_candidates(results, price, config)
    inactive = [r for r in results if not r.get("active")]
    context_text = build_context_text(ctx, cands, inactive, master_trend, master_source, config)

    ai_error, usage, decision = None, {}, None
    parsed, ai_error, usage = call_ai(context_text, config)
    if parsed is not None:
        try:
            decision = sanitize_ai(parsed, cands)
        except Exception as e:
            ai_error = f"คำตอบ AI ไม่ตรงรูปแบบ: {e}"
    if decision is None:
        decision = rules_only(cands, master_trend)

    pick = next((c for c in cands if c["num"] == decision["pick"]), None) if decision["pick"] else None
    pid = uuid.uuid4().hex[:8]
    text = format_message(symbol_label, price, decision, cands, ai_error, config, config["kvdb_bucket"], sizing_fn)
    prop = {
        "id": pid, "symbol": ctx["symbol"], "created_ts": time.time(), "created_iso": _now().isoformat(),
        "status": "open" if pick else "info", "price": price, "verdict": decision["verdict"],
        "confidence": decision.get("confidence"), "reasons": decision["reasons"], "pick": pick,
        "ai_model": config.get("ai_model"), "ai_error": ai_error,
        "tokens": (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
        "bias_4h": (ctx.get("bias_4h") or {}).get("trend"), "trend_15m": (ctx.get("structure") or {}).get("trend"),
        "n_candidates": len(cands),
    }
    return {"text": text, "keyboard": build_keyboard(pid) if pick else None, "proposal": prop}


# ══════════════════════════════════════════════════════
# 4) Telegram helpers
# ══════════════════════════════════════════════════════
def _tg(token, method, **payload):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
        data = r.json()
        if not data.get("ok"):
            print(f"[orderai] Telegram {method} ล้มเหลว: {data.get('description')}")
        return data
    except Exception as e:
        print(f"[orderai] Telegram {method} error: {e}")
        return {"ok": False}


def send_message(token, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = keyboard
    data = _tg(token, "sendMessage", **payload)
    if not data.get("ok"):  # HTML พัง -> ส่งแบบ plain ไม่ให้ข้อความหาย
        payload.pop("parse_mode", None)
        data = _tg(token, "sendMessage", **payload)
    return data


def _sheet_log(row):
    try:
        import sheets_log
        sheets_log.log_order_ai(row)
    except Exception as e:
        print(f"[orderai] บันทึก Sheets ไม่สำเร็จ (ไม่กระทบการทำงาน): {e}")


def log_proposal_row(prop, decision_label=None, **extra):
    pick = prop.get("pick") or {}
    row = {
        "Proposal_ID": prop["id"], "Created_Time": prop["created_iso"], "Symbol": prop["symbol"],
        "AI_Verdict": prop["verdict"], "AI_Confidence": prop.get("confidence"),
        "Plan_ID": f"P{pick['num']}" if pick else None,
        "Direction": ("LONG" if pick.get("direction") == "bullish" else "SHORT") if pick else None,
        "Entry": pick.get("entry"), "SL": pick.get("sl"), "TP": pick.get("tp"), "RR": pick.get("rr"),
        "Plan_Score": pick.get("score"), "Price_At_Proposal": prop.get("price"),
        "AI_Model": prop.get("ai_model"), "AI_Reason": " | ".join(prop.get("reasons") or []),
        "AI_Error": prop.get("ai_error"), "Tokens": prop.get("tokens"),
        "Decision": decision_label, "Tag": "ai_manual",
    }
    row.update(extra)
    _sheet_log(row)


# ══════════════════════════════════════════════════════
# 5) ปุ่ม ✅ / ❌ (ทำงานบน Render ผ่าน polling loop)
# ══════════════════════════════════════════════════════
def handle_callback(config, token, cq, price_fn):
    """price_fn(symbol) -> ราคาล่าสุด (float) — ส่งเข้ามาจาก telegram_bot เพื่อไม่ import วนกัน"""
    cqid = cq.get("id")
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    from_id = str((cq.get("from") or {}).get("id", ""))

    def answer(text, alert=False):
        _tg(token, "answerCallbackQuery", callback_query_id=cqid, text=text[:190], show_alert=alert)

    def strip_buttons():
        if chat_id and message_id:
            _tg(token, "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                reply_markup={"inline_keyboard": []})

    if str(config.get("telegram_owner_id")) != from_id:
        answer("ปุ่มนี้ใช้ได้เฉพาะเจ้าของบอทเท่านั้น", alert=True)
        return
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "oai":
        return
    action, pid = parts[1], parts[2]
    prop = load_proposal(config, pid)
    if not prop:
        answer("ไม่พบรายการนี้แล้ว (หมดอายุ) พิมพ์ /orderai ใหม่ได้เลย", alert=True)
        strip_buttons()
        return
    if prop.get("status") != "open":
        answer("รายการนี้ดำเนินการไปแล้ว", alert=False)
        strip_buttons()
        return

    ttl = config.get("orderai_button_ttl_minutes", 15) * 60
    if time.time() - prop["created_ts"] > ttl:
        prop["status"] = "expired"
        save_proposal(config, prop)
        strip_buttons()
        answer("ปุ่มหมดอายุแล้ว พิมพ์ /orderai ใหม่เพื่อดูสถานการณ์ล่าสุด", alert=True)
        log_proposal_row(prop, "EXPIRED")
        return

    if action == "s":
        prop["status"] = "skipped"
        save_proposal(config, prop)
        strip_buttons()
        answer("รับทราบ ไม่วาง — ปล่อยผ่าน")
        log_proposal_row(prop, "NOT_PLACED", Decision_Time=_now().isoformat())
        return

    if action != "p":
        return

    pick = prop["pick"]
    try:
        price = float(price_fn(prop["symbol"]))
    except Exception as e:
        answer(f"ดึงราคาล่าสุดไม่ได้ ({e}) ลองกดใหม่อีกครั้ง", alert=True)
        return
    v = validate_trade(pick["direction"], pick["entry"], pick["sl"], pick["tp"], price, config)
    if not v["ok"]:
        prop["status"] = "refused"
        prop["refused_reason"] = v["reason"]
        save_proposal(config, prop)
        strip_buttons()
        answer("ไม่แนะนำวางแล้ว: " + v["reason"], alert=True)
        send_message(token, chat_id, f"⚠️ <b>ไม่ควรวางออเดอร์นี้แล้ว</b>\nเหตุผล: {_esc(v['reason'])}\n"
                                      f"ราคาตอนนี้ {price:.2f} (ตอนเสนอ {prop['price']:.2f})\nบอทไม่บันทึกออเดอร์นี้ "
                                      f"พิมพ์ /orderai ใหม่เพื่อดูสถานการณ์ล่าสุด")
        log_proposal_row(prop, "REFUSED_STALE", Decision_Time=_now().isoformat(), Cancel_Reason=v["reason"])
        return

    now = _now()
    item = {
        "id": pid, "symbol": prop["symbol"], "plan": pick["plan_key"], "plan_num": pick["num"],
        "direction": pick["direction"], "entry": pick["entry"], "sl": pick["sl"], "tp": pick["tp"], "rr": v["rr"],
        "entry_side": v["entry_side"], "status": "waiting", "placed_ts": now.timestamp(), "placed_iso": now.isoformat(),
        "expires_ts": (now + timedelta(hours=config.get("orderai_track_expiry_hours", 6))).timestamp(),
        "bias_at": prop.get("bias_4h"), "trend15_at": prop.get("trend_15m"),
        "flags": {}, "checked_once": False, "price_at_place": price,
    }
    key = _tracked_key(prop["symbol"])
    try:
        items = json.loads(kv_get(config["kvdb_bucket"], key) or "[]")
    except Exception:
        items = []
    items.append(item)
    if not kv_set(config["kvdb_bucket"], key, json.dumps(items)):
        answer("บันทึกไม่สำเร็จ (ฐานข้อมูลมีปัญหา) ลองกดใหม่อีกครั้ง", alert=True)
        return
    prop["status"] = "placed"
    save_proposal(config, prop)
    strip_buttons()
    answer("✅ บันทึกแล้ว — จะแจ้งเตือนเมื่อออเดอร์เข้า/ปิด/ควรยกเลิก")
    kind = "Limit" if v["entry_side"] == "below" else "Stop"
    send_message(token, chat_id,
                 f"📌 <b>บันทึกออเดอร์แล้ว</b> — แผน {pick['num']} · {_dir_th(pick['direction'])}\n"
                 f"{kind} @ {pick['entry']:.2f} | SL {pick['sl']:.2f} | TP {pick['tp']:.2f} (RR {v['rr']})\n"
                 f"บอทจะเช็คทุก ~5 นาที แจ้งเมื่อ: ออเดอร์เข้า ✅ / ปิด TP-SL 🎯❌ / ควรยกเลิก ⚠️\n"
                 f"(ติดตามนานสุด {config.get('orderai_track_expiry_hours', 6)} ชม. ถ้ายังไม่เข้าจะแจ้งหมดอายุ)")
    log_proposal_row(prop, "PLACED", Decision_Time=now.isoformat(), Order_Status="WAITING_ENTRY")


# ══════════════════════════════════════════════════════
# 6) ติดตามออเดอร์ (เรียกจาก main.py ทุกรอบ cron — กฎล้วน ไม่เรียก AI)
# ══════════════════════════════════════════════════════
def _alert(config, text):
    from notify import send_telegram_alert
    chat = config.get("telegram_owner_id") or config.get("telegram_chat_id")
    if chat:
        send_telegram_alert(config["telegram_token"], chat, text)


def _hhmm(ts):
    return datetime.fromtimestamp(ts, THAI_TZ).strftime("%H:%M")


def track_orders(config, symbol, df, current_price, bias_trend=None, trend_15m=None):
    """เช็คออเดอร์ที่ผู้ใช้กด "วางแล้ว" ทุกตัว ส่งแจ้งเตือนเสมอ (ไม่ขึ้นกับ push_notifications_enabled
    เพราะผู้ใช้เลือกติดตามเอง) คืนจำนวนข้อความที่ส่ง"""
    bucket = config["kvdb_bucket"]
    key = _tracked_key(symbol)
    raw = kv_get(bucket, key)
    if not raw:
        return 0
    try:
        items = json.loads(raw)
    except Exception:
        return 0
    if not items:
        return 0

    price = float(current_price)
    try:
        bar_hi, bar_lo = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
    except Exception:
        bar_hi, bar_lo = price, price
    now = _now()
    spread = config.get("spread_buffer", 0.0)
    missed_at = config.get("orderai_missed_progress", 0.5)
    sent, keep, changed = 0, [], False

    try:
        from news_scheduler import is_in_news_blackout
        in_news, news_ev = is_in_news_blackout(bucket, symbol)
    except Exception:
        in_news, news_ev = False, None

    for it in items:
        bullish = it["direction"] == "bullish"
        entry, sl, tp = it["entry"], it["sl"], it["tp"]
        # รอบแรกหลังวาง ใช้ราคาปัจจุบันอย่างเดียว (แท่งปัจจุบันอาจมี high/low ก่อนเวลาที่วาง)
        hi = max(bar_hi, price) if it.get("checked_once") else price
        lo = min(bar_lo, price) if it.get("checked_once") else price
        it["checked_once"] = True
        flags = it.setdefault("flags", {})
        name = f"แผน {it['plan_num']} · {_dir_th(it['direction'])} @ {entry:.2f}"
        closed = False

        if it["status"] == "waiting":
            filled = (it["entry_side"] == "below" and lo <= entry - spread) or \
                     (it["entry_side"] == "above" and hi >= entry + spread)
            sl_hit = (lo <= sl) if bullish else (hi >= sl)
            progress = (price - entry) / (tp - entry)
            if filled and not sl_hit:
                it["status"], it["filled_ts"] = "running", now.timestamp()
                changed = True
                _alert(config, f"✅ <b>ออเดอร์เข้าแล้ว</b>\n{name}\nSL {sl:.2f} | TP {tp:.2f} (RR {it['rr']})\n"
                               f"ราคาตอนนี้ {price:.2f} — เช็คในโบรกได้เลย")
                sent += 1
                log_row = {"Proposal_ID": it["id"], "Order_Status": "RUNNING", "Fill_Time": now.isoformat()}
                _sheet_log(log_row)
            elif sl_hit:
                _alert(config, f"⚠️ <b>ยกเลิกออเดอร์นี้ได้เลย</b>\n{name}\nราคาแตะ SL ({sl:.2f}) ไปแล้วก่อน/ระหว่างเข้า "
                               f"แผนพังแล้ว (ราคาตอนนี้ {price:.2f})")
                sent += 1
                _sheet_log({"Proposal_ID": it["id"], "Order_Status": "CANCELLED", "Result": "CANCELLED",
                            "Cancel_Reason": "ราคาแตะ SL ก่อนเข้า", "Close_Time": now.isoformat()})
                closed = True
            elif progress >= missed_at:
                _alert(config, f"⚠️ <b>ควรยกเลิกออเดอร์ (ตกรถแล้ว)</b>\n{name}\nราคาวิ่งไปทาง TP แล้ว {progress * 100:.0f}% "
                               f"โดยออเดอร์ยังไม่เข้า (ราคาตอนนี้ {price:.2f}) ถ้าเข้าตอนนี้ RR จะแย่ลงมาก")
                sent += 1
                _sheet_log({"Proposal_ID": it["id"], "Order_Status": "CANCELLED", "Result": "MISSED",
                            "Cancel_Reason": f"ราคาวิ่งไป {progress * 100:.0f}% ของระยะ TP โดยไม่เข้า",
                            "Close_Time": now.isoformat()})
                closed = True
            elif now.timestamp() >= it["expires_ts"]:
                _alert(config, f"⌛ <b>ออเดอร์หมดอายุ — ยกเลิกได้เลย</b>\n{name}\nรอเกิน "
                               f"{config.get('orderai_track_expiry_hours', 6)} ชม. ราคายังไม่มาถึง Entry")
                sent += 1
                _sheet_log({"Proposal_ID": it["id"], "Order_Status": "CANCELLED", "Result": "EXPIRED",
                            "Cancel_Reason": "หมดเวลารอ", "Close_Time": now.isoformat()})
                closed = True
            else:
                opp = "bearish" if bullish else "bullish"
                if bias_trend == opp and it.get("bias_at") != opp and not flags.get("flip4h"):
                    flags["flip4h"] = True
                    changed = True
                    _alert(config, f"⚠️ <b>ควรพิจารณายกเลิก</b>\n{name}\nเทรนด์ 4H พลิกสวนทิศออเดอร์แล้ว (ออเดอร์ยังไม่เข้า)")
                    sent += 1
                if trend_15m == opp and it.get("trend15_at") != opp and not flags.get("flip15m"):
                    flags["flip15m"] = True
                    changed = True
                    _alert(config, f"⚠️ <b>ควรพิจารณายกเลิก</b>\n{name}\nโครงสร้าง 15M พลิกสวนทิศออเดอร์แล้ว (ออเดอร์ยังไม่เข้า)")
                    sent += 1
                if in_news and not flags.get("news"):
                    flags["news"] = True
                    changed = True
                    title = (news_ev or {}).get("title", "ข่าวสำคัญ")
                    _alert(config, f"⚠️ <b>ระวัง/พิจารณายกเลิก</b>\n{name}\nอยู่ในช่วง ±60 นาทีของข่าวแรง: {_esc(title)} "
                                   f"ออเดอร์ยังไม่เข้า ราคาอาจกระชากทั้งสองทาง")
                    sent += 1
        else:  # running
            sl_hit = (lo <= sl) if bullish else (hi >= sl)
            tp_hit = (hi >= tp) if bullish else (lo <= tp)
            if sl_hit or tp_hit:
                # ถ้าแท่งเดียวแตะทั้งคู่ ถือว่าโดน SL ก่อน (เข้มงวดไว้ก่อน)
                win = tp_hit and not sl_hit
                r = it["rr"] if win else -1.0
                dur_min = int((now.timestamp() - it.get("filled_ts", it["placed_ts"])) / 60)
                note = "\n(แท่งเดียวแตะทั้ง TP และ SL — นับเป็น SL ไว้ก่อน เช็คผลจริงในโบรก)" if (sl_hit and tp_hit) else ""
                if win:
                    _alert(config, f"🎯 <b>ถึง TP แล้ว +{r}R</b>\n{name}\nTP {tp:.2f} · ถือ {dur_min} นาที")
                else:
                    _alert(config, f"❌ <b>โดน SL -1R</b>\n{name}\nSL {sl:.2f} · ถือ {dur_min} นาที{note}")
                sent += 1
                _sheet_log({"Proposal_ID": it["id"], "Order_Status": "CLOSED", "Result": "WIN" if win else "LOSS",
                            "R_Multiple": r, "Close_Time": now.isoformat()})
                closed = True

        if closed:
            changed = True
        else:
            keep.append(it)

    if changed or True:  # checked_once ต้องบันทึกเสมอ
        kv_set(bucket, key, json.dumps(keep))
    return sent
