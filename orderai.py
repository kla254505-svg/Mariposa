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


def source_label(source, plan_num=None):
    if source == "ai_free":
        return "AI คิดเอง"
    if source == "ai_zone":
        return "โซนของคุณ"
    return f"แผน {plan_num}"


def order_type_name(direction, entry_side, with_hint=True):
    """ชื่อชนิดออเดอร์ตามที่โบรกใช้ (ขึ้นกับทิศ + Entry อยู่เหนือ/ใต้ราคา)
    entry_side 'below' = Entry ต่ำกว่าราคาตอนนั้น: LONG=Buy Limit, SHORT=Sell Stop
    entry_side 'above' = Entry สูงกว่าราคา: LONG=Buy Stop, SHORT=Sell Limit"""
    bull = direction == "bullish"
    if entry_side == "below":
        name, hint = ("Buy Limit", "รอราคาย่อลงมา") if bull else ("Sell Stop", "รอราคาทะลุลง")
    else:
        name, hint = ("Buy Stop", "รอราคาทะลุขึ้น") if bull else ("Sell Limit", "รอราคาเด้งขึ้นมา")
    return f"{name} ({hint})" if with_hint else name


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
งานของคุณมี 2 ส่วนแยกจากกัน:

ส่วน ① ตัดสิน "ผู้สมัครออเดอร์จากแผนของบอท": ควรเข้าไหม
  - ห้ามแก้ราคา เลือกได้เฉพาะ "หมายเลขแผน" ที่ valid=true ในรายการผู้สมัครเท่านั้น
  - verdict: ENTER (ควรเข้า — ต้องมี pick เป็นเลขแผน), WAIT (ยังไม่ควรเข้าแต่ยังมีลุ้น บอกว่ารออะไร), NO (ไม่ควรเข้า)
  - ไม่มีผู้สมัคร valid เลย ต้องตอบ WAIT หรือ NO และ pick = null

ส่วน ② "ความเห็นของคุณเอง" แยกจากแผนบอทโดยสิ้นเชิง: ถ้าคุณเห็นจุดเข้าที่ดีในตอนนี้ ให้เสนอ Entry/SL/TP เอง
  - ใช้ระดับราคาจากหัวข้อ KEY LEVELS (swing, โซน OB/FVG, equilibrium 4H) เป็นหลักอ้างอิง ห้ามแต่งระดับที่ไม่มีเหตุผลรองรับ
  - Entry ควรอยู่ที่โซน/ระดับสำคัญ (limit รอย่อ/รีบาวด์ หรือ stop รอทะลุ) SL อยู่หลังโครงสร้างที่ทำให้ไอเดียผิด TP อยู่ที่ระดับเป้าหมายจริง
  - SL ต้องห่างจาก Entry อย่างน้อย min_sl_distance และ RR อย่างน้อย min_rr (ค่าอยู่ในข้อมูล) ไม่งั้นโค้ดจะตีตก
  - จะเสนอทิศเดียวกับแผนบอท หรือคนละทิศก็ได้ ตามที่เห็นจริง
  - ถ้าตอนนี้ไม่มีจุดเข้าที่ดีจริงๆ ให้ตอบ direction = "NONE" พร้อมสาเหตุสั้นๆ ห้ามฝืนเสนอเพื่อให้มีออเดอร์ การบอกว่า "ไม่มี" คือคำตอบที่ถูกเมื่อเป็นความจริง

กติกาทั่วไป: ตัดสินจากหลักฐาน (เทรนด์ 4H/1H/15M, สถิติจริงของแต่ละแผน, ข่าว, session, premium/discount) แผนที่สถิติแย่ให้ระวัง
ห้ามแต่งข้อมูลที่ไม่มีให้ ค่าไหนเป็น not_available ให้บอกว่าไม่มีข้อมูล

ตอบเป็น JSON ก้อนเดียว ไม่มี markdown ไม่มีข้อความอื่น:
{"verdict":"ENTER|WAIT|NO","pick":<เลขแผนหรือ null>,"confidence":<0-100>,
 "reasons":["เหตุผลสั้นๆ 1","เหตุผลสั้นๆ 2","เหตุผลสั้นๆ 3 (ไม่บังคับ)"],
 "risk_note":"คำเตือนสั้นๆ 1 บรรทัด หรือสตริงว่าง",
 "no_entry_reason":"ถ้าส่วน ① ไม่มีจุดเข้า: สาเหตุสั้นๆ 1-2 ประโยค ไม่งั้นสตริงว่าง",
 "ai_idea":{"direction":"LONG|SHORT|NONE","entry":<ตัวเลขหรือ null>,"sl":<ตัวเลขหรือ null>,"tp":<ตัวเลขหรือ null>,
           "basis":"อ้างอิงระดับไหน/ทำไม 1 ประโยค (ห้ามเขียนชื่อชนิดออเดอร์ Limit/Stop โค้ดจะแสดงเอง)","confidence":<0-100 หรือ null>,
           "none_reason":"ถ้า NONE: สาเหตุสั้นๆ 1-2 ประโยค ไม่งั้นสตริงว่าง"}}
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


def build_key_levels(ctx, cands_price):
    """ระดับราคาอ้างอิงให้ AI ใช้ตอนเสนอจุดเข้าเอง (ส่วน ②) — ข้อมูลจริงจากกราฟ 15M + โซน 4H ไม่ใช่ตัวเลขที่ AI ต้องเดา
    คืน list ของข้อความ (ว่างถ้าคำนวณไม่ได้)"""
    out = []
    try:
        df = ctx["df_ind"]
        price = float(df["close"].iloc[-1])
        n24 = min(len(df), 96)
        out.append(f"High/Low 24 ชม. (15M x{n24} แท่ง): {df['high'].iloc[-n24:].max():.2f} / {df['low'].iloc[-n24:].min():.2f}")
        n6 = min(len(df), 24)
        out.append(f"High/Low 6 ชม.: {df['high'].iloc[-n6:].max():.2f} / {df['low'].iloc[-n6:].min():.2f}")
        try:
            from pattern import find_swings
            sw = find_swings(df.iloc[-200:], lookback=3)
            highs = sw.loc[sw["swing_high"], "high"].tolist()[-4:]
            lows = sw.loc[sw["swing_low"], "low"].tolist()[-4:]
            if highs:
                out.append("Swing high 15M ล่าสุด (เก่า->ใหม่): " + ", ".join(f"{x:.2f}" for x in highs))
            if lows:
                out.append("Swing low 15M ล่าสุด (เก่า->ใหม่): " + ", ".join(f"{x:.2f}" for x in lows))
        except Exception:
            pass
        cfg = ctx.get("config") or {}
        try:
            from orderblock import find_order_blocks, get_nearest_unmitigated_ob
            obs = find_order_blocks(df, cfg)
            for d, lab in (("bullish", "OB ขาขึ้น (แนวรับ) ใกล้สุดใต้ราคา"), ("bearish", "OB ขาลง (แนวต้าน) ใกล้สุดเหนือราคา")):
                ob = get_nearest_unmitigated_ob(obs, d, price)
                if ob:
                    out.append(f"{lab}: {ob['bottom']:.2f} - {ob['top']:.2f}")
        except Exception:
            pass
        try:
            from fvg import find_fvgs, get_nearest_unfilled_fvg
            fv = find_fvgs(df, cfg)
            for d, lab in (("bullish", "FVG ขาขึ้น ใกล้สุดใต้ราคา"), ("bearish", "FVG ขาลง ใกล้สุดเหนือราคา")):
                f = get_nearest_unfilled_fvg(fv, d, price)
                if f:
                    out.append(f"{lab}: {f['bottom']:.2f} - {f['top']:.2f}")
        except Exception:
            pass
        b4 = ctx.get("bias_4h") or {}
        parts = [f"{k}={b4[k]:.2f}" for k in ("swing_high", "swing_low", "equilibrium_price") if isinstance(b4.get(k), (int, float))]
        if parts:
            out.append("กรอบ 4H: " + ", ".join(parts))
    except Exception:
        pass
    return out


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
        f"เกณฑ์ที่โค้ดใช้ตรวจ: min_sl_distance={config.get('min_sl_distance')} min_rr={config.get('min_rr', 1.2)}",
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
    lines.append("== KEY LEVELS (ใช้อ้างอิงส่วน ② ความเห็นของคุณเอง) ==")
    kl = build_key_levels(ctx, price)
    lines.extend(kl if kl else ["not_available"])
    lines.append("")
    lines.append("== ผู้สมัครออเดอร์จากแผนบอท (ส่วน ① ตัวเลขมาจากบอท ห้ามแก้) ==")
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


def _post_ai(payload, config):
    """ยิง Anthropic API คืน (parsed|None, error|None, usage)"""
    key = config.get("anthropic_api_key")
    if not key:
        return None, "ยังไม่ได้ตั้งค่า ANTHROPIC_API_KEY", {}
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


def _call_ai_payload(payload, config):
    parsed, err, _usage = _post_ai(payload, config)
    return parsed, err


def call_ai(context_text, config):
    """คืน (parsed|None, error|None, usage dict) — ไม่โยน exception"""
    payload = {
        "model": config.get("ai_model", "claude-sonnet-5-5"),
        "max_tokens": config.get("orderai_max_tokens", 3000),
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": context_text}],
    }
    return _post_ai(payload, config)


def sanitize_idea(raw, price, config):
    """ตรวจ "ความเห็น AI" (ส่วน ②) ด้วยกฎในโค้ดชุดเดียวกับแผนบอท คืน dict:
    direction (bullish/bearish/None), entry/sl/tp/rr, entry_side, basis, confidence, valid, invalid_reason, none_reason"""
    out = {"direction": None, "entry": None, "sl": None, "tp": None, "rr": None, "entry_side": None,
           "basis": "", "confidence": None, "valid": False, "invalid_reason": None, "none_reason": ""}
    if not isinstance(raw, dict):
        out["none_reason"] = "AI ไม่ได้ให้ความเห็นส่วนนี้"
        return out
    d = str(raw.get("direction", "NONE")).upper().strip()
    out["none_reason"] = str(raw.get("none_reason") or "").strip()[:300]
    out["basis"] = str(raw.get("basis") or "").strip()[:240]
    try:
        out["confidence"] = max(0, min(100, int(raw.get("confidence"))))
    except (TypeError, ValueError):
        pass
    if d not in ("LONG", "SHORT"):
        return out
    direction = "bullish" if d == "LONG" else "bearish"
    try:
        entry, sl, tp = float(raw["entry"]), float(raw["sl"]), float(raw["tp"])
    except (KeyError, TypeError, ValueError):
        out["direction"] = direction
        out["invalid_reason"] = "AI เสนอทิศแต่ตัวเลข Entry/SL/TP ไม่ครบ"
        return out
    out.update({"direction": direction, "entry": round(entry, 3), "sl": round(sl, 3), "tp": round(tp, 3)})
    v = validate_trade(direction, entry, sl, tp, price, config)
    out["rr"], out["entry_side"] = v["rr"], v["entry_side"]
    out["valid"], out["invalid_reason"] = v["ok"], v["reason"]
    return out


def sanitize_ai(data, cands, price=None, config=None):
    """ตรวจคำตอบ AI: verdict ต้องถูกต้อง ENTER ต้องเลือกแผน valid จริง ไม่งั้นลดระดับเป็น NO
    และตรวจ ai_idea (ความเห็นส่วน ②) ด้วยกฎในโค้ด"""
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
    idea = sanitize_idea(data.get("ai_idea"), price, config or {}) if price is not None else None
    return {
        "verdict": verdict, "pick": pick, "confidence": conf, "reasons": reasons,
        "risk_note": str(data.get("risk_note") or "").strip()[:200],
        "no_entry_reason": str(data.get("no_entry_reason") or "").strip()[:300],
        "downgraded": downgraded, "idea": idea,
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
            "risk_note": "ยังไม่มี AI ช่วยตรวจบริบท/ข่าว ตัดสินใจเองอีกชั้น", "no_entry_reason": "", "downgraded": None,
            "idea": None}


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
        kind = order_type_name(pick["direction"], pick["entry_side"])
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


def format_idea_message(price, idea, config):
    """ส่วน ② ความเห็นของ AI (แยกข้อความจากส่วน ① ไม่แสดง Risk)"""
    lines = ["🧠 <b>ความเห็นของ AI เอง</b> (แยกจากแผนบอท)", "<i>AI คิดเองจากโครงสร้างราคา — ยังไม่มีสถิติย้อนหลังรองรับ</i>", ""]
    if not idea.get("direction"):
        lines.append("📍 <b>จุดเข้า: ไม่มี</b>")
        lines.append(_esc(idea.get("none_reason") or "AI มองว่าตอนนี้ยังไม่มีจุดเข้าที่คุ้มพอ"))
        return "\n".join(lines)
    if not idea.get("valid"):
        lines.append(f"📍 AI เสนอ{_dir_th(idea['direction'])} แต่โค้ดตรวจแล้วไม่ผ่าน จึงไม่แนะนำให้วาง")
        if idea.get("entry") is not None:
            lines.append(f"Entry {idea['entry']:.2f} | SL {idea['sl']:.2f} | TP {idea['tp']:.2f}")
        lines.append(f"❌ เหตุผลที่ไม่ผ่าน: {_esc(idea.get('invalid_reason'))}")
        if idea.get("basis"):
            lines.append(f"แนวคิด AI: {_esc(idea['basis'])}")
        return "\n".join(lines)
    kind = order_type_name(idea["direction"], idea["entry_side"])
    dist = abs(idea["entry"] - price)
    lines += [
        f"📍 <b>จุดเข้า — {_dir_th(idea['direction'])}</b>"
        + (f"  (AI มั่นใจ {idea['confidence']}%)" if idea.get("confidence") is not None else ""),
        f"Entry ({kind}): <b>{idea['entry']:.2f}</b>  (ห่างราคาปัจจุบัน {dist:.1f})",
        f"SL: <b>{idea['sl']:.2f}</b>  (ห่าง {abs(idea['entry'] - idea['sl']):.1f})",
        f"TP: <b>{idea['tp']:.2f}</b>  (RR {idea['rr']})",
    ]
    if idea.get("basis"):
        lines.append(f"💡 {_esc(idea['basis'])}")
    lines.append(f"\n👇 ถ้าวางออเดอร์นี้เอง กดปุ่มด้านล่าง (หมดอายุใน {config.get('orderai_button_ttl_minutes', 15)} นาที)")
    return "\n".join(lines)


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
            decision = sanitize_ai(parsed, cands, price, config)
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
    out = {"text": text, "keyboard": build_keyboard(pid) if pick else None, "proposal": prop,
           "idea_text": None, "idea_keyboard": None, "idea_proposal": None}

    idea = decision.get("idea")
    if idea is not None:  # AI ตอบได้ (ไม่ใช่โหมด fallback) -> แสดงส่วน ② เสมอ
        idea_pid = pid + "a"
        out["idea_text"] = format_idea_message(price, idea, config)
        if idea.get("valid"):
            ipick = {"num": "AI", "label": "AI คิดเอง", "plan_key": "ai_free", "source": "ai_free",
                     "direction": idea["direction"], "entry": idea["entry"], "sl": idea["sl"], "tp": idea["tp"],
                     "rr": idea["rr"], "entry_side": idea["entry_side"], "score": None, "note": idea.get("basis")}
            out["idea_keyboard"] = build_keyboard(idea_pid)
        else:
            ipick = None
        out["idea_proposal"] = dict(prop, id=idea_pid, pick=ipick, source="ai_free",
                                     status="open" if ipick else "info",
                                     verdict="IDEA" if idea.get("direction") else "NO_IDEA",
                                     confidence=idea.get("confidence"),
                                     reasons=[x for x in (idea.get("basis"), idea.get("none_reason"), idea.get("invalid_reason")) if x],
                                     ai_error=ai_error)
    return out


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
        "Plan_ID": ({"ai_free": "AI", "ai_zone": "ZONE"}.get(pick.get("source"), f"P{pick['num']}") if pick else None),
        "Direction": ("LONG" if pick.get("direction") == "bullish" else "SHORT") if pick else None,
        "Entry": pick.get("entry"), "SL": pick.get("sl"), "TP": pick.get("tp"), "RR": pick.get("rr"),
        "Plan_Score": pick.get("score"), "Price_At_Proposal": prop.get("price"),
        "AI_Model": prop.get("ai_model"), "AI_Reason": " | ".join(prop.get("reasons") or []),
        "AI_Error": prop.get("ai_error"), "Tokens": prop.get("tokens"),
        "Decision": decision_label,
        "Tag": prop.get("tag") or ("ai_free" if prop.get("source") == "ai_free" else "ai_manual"),
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

    if action == "n":  # /myorder: ไม่ขยับ -> ติดตามออเดอร์เดิมต่อ
        prop["status"] = "skipped"
        save_proposal(config, prop)
        strip_buttons()
        answer("รับทราบ ไม่ขยับ — ติดตามออเดอร์เดิมต่อ")
        return

    if action == "s":
        prop["status"] = "skipped"
        save_proposal(config, prop)
        strip_buttons()
        answer("รับทราบ ไม่วาง — ปล่อยผ่าน")
        log_proposal_row(prop, "NOT_PLACED", Decision_Time=_now().isoformat())
        return

    if action not in ("p", "r"):
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
        "source": pick.get("source", "plan"), "tag": prop.get("tag"),
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
    replaced_old = None
    if action == "r":  # /myorder: ยกเลิกออเดอร์เดิม + ติดตามค่าใหม่แทน
        old = next((x for x in items if x["id"] == prop.get("replaces")), None)
        if old is None or old.get("status") != "waiting":
            prop["status"] = "refused"
            save_proposal(config, prop)
            strip_buttons()
            answer("ออเดอร์เดิมเข้าแล้ว/ปิดแล้ว/ไม่อยู่ในรายการ จึงไม่ควรวางใหม่ ดู /myorder อีกครั้ง", alert=True)
            return
        replaced_old = old
        items = [x for x in items if x["id"] != old["id"]]
        item["replaces"] = old["id"]
    items.append(item)
    if not kv_set(config["kvdb_bucket"], key, json.dumps(items)):
        answer("บันทึกไม่สำเร็จ (ฐานข้อมูลมีปัญหา) ลองกดใหม่อีกครั้ง", alert=True)
        return
    prop["status"] = "placed"
    save_proposal(config, prop)
    strip_buttons()
    answer("✅ บันทึกแล้ว — จะแจ้งเตือนเมื่อออเดอร์เข้า/ปิด/ควรยกเลิก")
    if replaced_old:
        _sheet_log({"Proposal_ID": replaced_old["id"], "Order_Status": "CANCELLED", "Result": "REPLACED",
                    "Cancel_Reason": f"ขยับเป็นออเดอร์ใหม่ {pid}", "Close_Time": now.isoformat()})
    kind = order_type_name(pick["direction"], v["entry_side"], with_hint=False)
    send_message(token, chat_id,
                 f"📌 <b>{'ย้ายไปติดตามออเดอร์ใหม่แล้ว (เลิกติดตามอันเดิม — อย่าลืมยกเลิกอันเดิมในโบรก)' if replaced_old else 'บันทึกออเดอร์แล้ว'}</b> — {source_label(pick.get('source'), pick['num'])} · {_dir_th(pick['direction'])}\n"
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


def track_orders(config, symbol, df, current_price, bias_trend=None, trend_15m=None, df_5m=None):
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
        # รอบถัดไป: ถ้ามีแท่ง 5M ให้กวาดทุกแท่งตั้งแต่เช็คครั้งก่อน (กัน cron ดีเลย์แล้วพลาดการแตะ Entry/SL/TP
        # ที่ราคาแตะแล้วถอยกลับ — เดิมดูแค่แท่งสุดท้ายแท่งเดียว)
        ref_ts = it.get("last_check_ts") or it.get("placed_ts")  # ออเดอร์เก่าที่ยังไม่มี last_check_ts ใช้เวลาที่วางแทน
        if it.get("checked_once") and df_5m is not None and ref_ts:
            try:
                k = max(1, min(len(df_5m), 288, int((now.timestamp() - ref_ts) / 300) + 1))
                hi = max(hi, float(df_5m["high"].iloc[-k:].max()))
                lo = min(lo, float(df_5m["low"].iloc[-k:].min()))
            except Exception:
                pass
        it["checked_once"] = True
        it["last_check_ts"] = now.timestamp()
        flags = it.setdefault("flags", {})
        who = source_label(it.get("source"), it["plan_num"])
        name = f"{who} · {_dir_th(it['direction'])} @ {entry:.2f}"
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


# ══════════════════════════════════════════════════════
# 7) /myorder — สถานะออเดอร์ที่ติดตามอยู่ + AI แนะนำ รอต่อ/ยกเลิก/ขยับ
# ══════════════════════════════════════════════════════
MYORDER_PROMPT = """คุณเป็นผู้ช่วยดูแลออเดอร์ทอง (XAUUSD) ที่เจ้าของบอทวางไว้เองในโบรกและบอทกำลังติดตามอยู่ เขาอ่านภาษาไทยง่ายๆ
ให้ประเมินออเดอร์แต่ละตัวจากข้อมูลที่ให้ แล้วแนะนำ:
- ออเดอร์สถานะ waiting (ยังไม่เข้า): HOLD (รอต่อ) / CANCEL (ควรยกเลิก) / MOVE (ควรยกเลิกแล้ววางใหม่ที่ค่าใหม่)
- ออเดอร์สถานะ running (เข้าแล้ว): HOLD (ถือต่อ) หรือ CLOSE (ควรพิจารณาปิดก่อนถึง TP/SL) ห้ามเสนอ MOVE เพราะออเดอร์เข้าไปแล้ว
กติกา:
1. MOVE ต้องเป็นทิศเดิมเท่านั้น และให้ new_entry/new_sl/new_tp ที่อิงระดับราคาจริงใน KEY LEVELS ห่าง SL อย่างน้อย min_sl_distance และ RR อย่างน้อย min_rr (โค้ดจะตรวจซ้ำ ไม่ผ่านจะตีตก)
2. เสนอ MOVE เมื่อกรอบเดิมไม่ตรงโครงสร้างราคาตอนนี้แล้วแต่ทิศยังน่าเชื่อ ถ้าทิศเองก็พังแล้วให้ CANCEL ถ้าเดิมยังดีอยู่ให้ HOLD ไม่ต้องขยับพร่ำเพรื่อ
3. ตัดสินจากหลักฐาน (เทรนด์ 4H/1H/15M เทียบตอนวาง, ระยะราคาห่าง Entry, เวลาที่รอ, ข่าว) ห้ามแต่งข้อมูลที่ไม่มี
4. ห้ามเขียนชื่อชนิดออเดอร์ (Limit/Stop) ในเหตุผล โค้ดแสดงเอง
ตอบเป็น JSON ก้อนเดียว ไม่มี markdown:
{"orders":[{"id":"<id ที่ให้มา>","action":"HOLD|CANCEL|MOVE|CLOSE","reason":"เหตุผลสั้นๆ ไม่เกิน ~120 ตัวอักษร",
 "new_entry":<ตัวเลขหรือ null>,"new_sl":<ตัวเลขหรือ null>,"new_tp":<ตัวเลขหรือ null>}]}"""


def order_facts(it, price, bias_trend, trend_15m, now_ts=None):
    """ข้อเท็จจริงของออเดอร์ที่ติดตามอยู่ (คำนวณจากกฎ ไม่พึ่ง AI)"""
    now_ts = now_ts or time.time()
    bull = it["direction"] == "bullish"
    entry, sl, tp = it["entry"], it["sl"], it["tp"]
    f = {
        "id": it["id"], "status": it["status"], "waited_min": int((now_ts - it["placed_ts"]) / 60),
        "expires_in_min": int((it["expires_ts"] - now_ts) / 60) if it["status"] == "waiting" else None,
        "dist_to_entry": round(price - entry, 2),
        "progress": round((price - entry) / (tp - entry), 2),
        "opp_trend4h": bias_trend == ("bearish" if bull else "bullish"),
        "opp_trend15": trend_15m == ("bearish" if bull else "bullish"),
        "flags": sorted((it.get("flags") or {}).keys()),
    }
    if it["status"] == "running":
        risk = abs(entry - sl) or 1
        f["open_r"] = round(((price - entry) if bull else (entry - price)) / risk, 2)
    return f


def _item_name(it):
    who = source_label(it.get("source"), it["plan_num"])
    return f"{who} · {_dir_th(it['direction'])}"


def rules_review(it, facts):
    """คำแนะนำจากกฎล้วน (ใช้เมื่อ AI ใช้ไม่ได้)"""
    if it["status"] == "running":
        return {"action": "HOLD", "reason": "ออเดอร์เข้าแล้ว รอ TP/SL ตามแผน"}
    if facts["opp_trend4h"] or facts["opp_trend15"]:
        return {"action": "CANCEL", "reason": "เทรนด์พลิกสวนทิศออเดอร์แล้ว ออเดอร์ยังไม่เข้า"}
    if facts["progress"] >= 0.3:
        return {"action": "CANCEL", "reason": f"ราคาวิ่งไปทาง TP แล้ว {facts['progress'] * 100:.0f}% โดยยังไม่เข้า"}
    return {"action": "HOLD", "reason": "ยังไม่มีสัญญาณให้เปลี่ยนแผน รอต่อ"}


def sanitize_review(raw, items, facts_by_id, price, config):
    """ตรวจคำแนะนำ AI รายออเดอร์ -> dict id -> {action, reason, new(optional dict), note}"""
    by_id = {r.get("id"): r for r in (raw.get("orders") or []) if isinstance(r, dict)}
    out = {}
    for it in items:
        r = by_id.get(it["id"])
        if not r:
            continue
        action = str(r.get("action", "")).upper().strip()
        reason = str(r.get("reason") or "").strip()[:200]
        running = it["status"] == "running"
        if action not in (("HOLD", "CLOSE") if running else ("HOLD", "CANCEL", "MOVE")):
            continue
        rec = {"action": action, "reason": reason}
        if action == "MOVE":
            v = validate_trade(it["direction"], r.get("new_entry"), r.get("new_sl"), r.get("new_tp"), price, config)
            if v["ok"]:
                rec["new"] = {"entry": round(float(r["new_entry"]), 3), "sl": round(float(r["new_sl"]), 3),
                              "tp": round(float(r["new_tp"]), 3), "rr": v["rr"], "entry_side": v["entry_side"]}
            else:
                rec["action"] = "HOLD"
                rec["note"] = f"AI เสนอขยับแต่ค่าใหม่ไม่ผ่านการตรวจ ({v['reason']}) จึงให้รอต่อ/ตัดสินใจเอง"
        out[it["id"]] = rec
    return out


def review_orders(config, ctx, items, master_trend, symbol_label):
    """ทำทั้งกระบวนการของ /myorder คืน dict: text, keyboard|None, proposals(list ของข้อเสนอขยับ)"""
    df = ctx["df_ind"]
    price = float(df["close"].iloc[-1])
    bias = (ctx.get("bias_4h") or {}).get("trend")
    t15 = (ctx.get("structure") or {}).get("trend")
    facts = {it["id"]: order_facts(it, price, bias, t15) for it in items}

    ai_error, recs = None, {}
    if items:
        lines = [f"Symbol: {ctx['symbol']}", f"เวลา (ไทย): {datetime.now(THAI_TZ).strftime('%Y-%m-%d %H:%M')}",
                 f"ราคาปัจจุบัน: {price:.2f}",
                 f"เกณฑ์ที่โค้ดใช้ตรวจ: min_sl_distance={config.get('min_sl_distance')} min_rr={config.get('min_rr', 1.2)}",
                 f"เทรนด์ตอนนี้: 4H={_fmt(bias)} 1H={_fmt(ctx.get('higher_tf_trend'))} 15M={_fmt(t15)} (ทิศ 'bullish'=ขึ้น 'bearish'=ลง)"]
        news = _upcoming_news(config, ctx["symbol"])
        lines.append("ข่าวใกล้ๆ: " + ("; ".join(news) if news else "ไม่มีในช่วง 3 ชม. ข้างหน้า"))
        lines += ["", "== KEY LEVELS =="] + (build_key_levels(ctx, price) or ["not_available"]) + ["", "== ออเดอร์ที่ติดตามอยู่ =="]
        for it in items:
            f = facts[it["id"]]
            lines.append(
                f"id={it['id']} | {_item_name(it)} | ชนิด {order_type_name(it['direction'], it['entry_side'], False)} | "
                f"Entry {it['entry']} SL {it['sl']} TP {it['tp']} RR {it['rr']} | สถานะ {it['status']} | "
                f"วางมา {f['waited_min']} นาที" + (f" หมดอายุอีก {f['expires_in_min']} นาที" if f["expires_in_min"] is not None else "")
                + f" | ราคาห่าง Entry {f['dist_to_entry']:+.2f} | ความคืบหน้าไป TP {f['progress'] * 100:.0f}%"
                + (f" | กำไร/ขาดทุนตอนนี้ {f['open_r']:+.2f}R" if "open_r" in f else "")
                + f" | เทรนด์ตอนวาง 4H={_fmt(it.get('bias_at'))} 15M={_fmt(it.get('trend15_at'))}"
                + f" | เทรนด์พลิกสวนทิศแล้ว 4H={'ใช่' if f['opp_trend4h'] else 'ไม่'} 15M={'ใช่' if f['opp_trend15'] else 'ไม่'}")
        payload = {"model": config.get("ai_model", "claude-sonnet-5-5"), "max_tokens": config.get("orderai_max_tokens", 3000),
                   "system": MYORDER_PROMPT, "messages": [{"role": "user", "content": "\n".join(lines)}]}
        parsed, ai_error = _call_ai_payload(payload, config)
        if parsed is not None:
            try:
                recs = sanitize_review(parsed, items, facts, price, config)
            except Exception as e:
                ai_error = f"คำตอบ AI ไม่ตรงรูปแบบ: {e}"

    out_lines = [f"📋 <b>/myorder — {_esc(symbol_label)}</b>", f"ราคา {price:.2f} · {datetime.now(THAI_TZ).strftime('%H:%M')} น.", ""]
    proposals, kb_rows = [], []
    if not items:
        out_lines.append("ตอนนี้ไม่มีออเดอร์ที่ติดตามอยู่\nกด ✅ วางแล้ว จาก /orderai ก่อน บอทถึงจะเริ่มติดตามให้")
    for n, it in enumerate(items, 1):
        f = facts[it["id"]]
        rec = recs.get(it["id"]) or rules_review(it, f)
        run = it["status"] == "running"
        head = f"<b>#{n} {_esc(_item_name(it))}</b> · {order_type_name(it['direction'], it['entry_side'], False)} @ {it['entry']:.2f}"
        state = ("🏃 เข้าแล้ว กำลังวิ่ง" + (f" ({f['open_r']:+.2f}R)" if "open_r" in f else "")) if run else \
                f"⏳ รอเข้า · วางมา {f['waited_min']} นาที (หมดอายุอีก {max(f['expires_in_min'], 0)} นาที)"
        out_lines += [head, state,
                      f"SL {it['sl']:.2f} | TP {it['tp']:.2f} (RR {it['rr']}) · ราคาห่าง Entry {f['dist_to_entry']:+.1f}"
                      + ("" if run else f" · ไป TP แล้ว {max(f['progress'], 0) * 100:.0f}%")]
        act = rec["action"]
        if act == "HOLD":
            out_lines.append(f"✅ <b>{'ถือต่อ' if run else 'รอต่อ'}</b> — {_esc(rec['reason'])}")
        elif act == "CANCEL":
            out_lines.append(f"⚠️ <b>ควรยกเลิก</b> — {_esc(rec['reason'])}")
        elif act == "CLOSE":
            out_lines.append(f"⚠️ <b>ควรพิจารณาปิดก่อน</b> — {_esc(rec['reason'])}")
        elif act == "MOVE":
            nw = rec["new"]
            out_lines.append(f"🔄 <b>ควรขยับ</b> — {_esc(rec['reason'])}")
            out_lines.append(f"👉 ยกเลิกออเดอร์เดิมในโบรก แล้ววางใหม่: {order_type_name(it['direction'], nw['entry_side'], False)} "
                             f"<b>{nw['entry']:.2f}</b> | SL <b>{nw['sl']:.2f}</b> | TP <b>{nw['tp']:.2f}</b> (RR {nw['rr']})")
            npid = uuid.uuid4().hex[:8]
            now = _now()
            proposals.append({
                "id": npid, "symbol": it["symbol"], "created_ts": time.time(), "created_iso": now.isoformat(), "status": "open",
                "price": price, "verdict": "MOVE", "confidence": None, "reasons": [rec["reason"]],
                "pick": {"num": it["plan_num"], "label": _item_name(it), "plan_key": it["plan"], "source": it.get("source", "plan"),
                         "direction": it["direction"], "entry": nw["entry"], "sl": nw["sl"], "tp": nw["tp"], "rr": nw["rr"],
                         "entry_side": nw["entry_side"], "score": None, "note": None},
                "ai_model": config.get("ai_model"), "ai_error": None, "tokens": None, "bias_4h": bias, "trend_15m": t15,
                "replaces": it["id"], "tag": "ai_move", "source": it.get("source", "plan"),
            })
            kb_rows.append([{"text": f"✅ #{n} วางใหม่แล้ว", "callback_data": f"oai:r:{npid}"},
                            {"text": f"❌ #{n} ไม่ขยับ", "callback_data": f"oai:n:{npid}"}])
        if rec.get("note"):
            out_lines.append(f"ℹ️ {_esc(rec['note'])}")
        out_lines.append("")
    if ai_error:
        out_lines.append(f"🛠 AI: {_esc(ai_error)[:160]} (ใช้คำแนะนำจากกฎแทน)")
    if kb_rows:
        out_lines.append(f"👇 ถ้าขยับจริง กดวางใหม่ (ปุ่มหมดอายุใน {config.get('orderai_button_ttl_minutes', 15)} นาที) บอทจะเลิกติดตามอันเดิมแล้วติดตามอันใหม่")
    return {"text": "\n".join(out_lines).strip(), "keyboard": {"inline_keyboard": kb_rows} if kb_rows else None,
            "proposals": proposals}


# ══════════════════════════════════════════════════════
# 8) /orderai <โซน> <ทิศ> — ให้ AI ตรวจ "โซนที่ผู้ใช้เห็นเอง" แทนการหาโซนเอง
# ══════════════════════════════════════════════════════
ZONE_USAGE = ("พิมพ์แบบนี้ครับ:\n/orderai 3568-3580 short  (โซนขาย)\n/orderai 3500-3510 long  (โซนซื้อ)\n"
              "ต้องมี ราคาต่ำ-ราคาสูง และทิศ short/long (เว้นวรรคหรือไม่เว้นก็ได้)")
_NUM = r"(\d+(?:[.,]\d+)?)"
_ZONE_RE = re.compile(_NUM + r"\s*(?:-|–|—|~|ถึง|to)\s*" + _NUM)


def parse_zone_args(args):
    """แยกโซน+ทิศจากคำสั่ง /orderai คืน (zone|None, error|None)
    zone = {"lo","hi","direction":"bullish|bearish"}  ไม่พบตัวเลขโซนเลย -> (None, None) = ใช้โหมดปกติ
    มีตัวเลขแต่ไม่ครบ/ไม่มีทิศ -> (None, ข้อความบอกวิธีพิมพ์)"""
    text = " ".join(str(a) for a in args).lower().replace(",", "")
    if not re.search(r"\d", text):
        return None, None
    m = _ZONE_RE.search(text)
    if not m:
        return None, "อ่านโซนไม่ออกครับ\n" + ZONE_USAGE
    lo, hi = sorted((float(m.group(1)), float(m.group(2))))
    rest = text[:m.start()] + " " + text[m.end():]
    if re.search(r"short|sell|ขาย|เซล", rest):
        d = "bearish"
    elif re.search(r"long|buy|ซื้อ|บาย", rest):
        d = "bullish"
    else:
        return None, "ต้องใส่ทิศด้วยครับ (short หรือ long)\n" + ZONE_USAGE
    if hi - lo < 0.5:
        return None, "โซนแคบเกินไป (ต้องต่างกันอย่างน้อย 0.5)\n" + ZONE_USAGE
    return {"lo": lo, "hi": hi, "direction": d}, None


ZONE_PROMPT = """คุณเป็นผู้ช่วยตัดสินใจเทรดทอง (XAUUSD) เจ้าของบอทเทรดมือบนโบรกและเห็น "โซน" จากกราฟของเขาเอง (30m-1H) ให้คุณตรวจโซนนั้น ไม่ต้องหาโซนใหม่
เขาอ่านภาษาไทยง่ายๆ งานของคุณ: ตัดสินว่าตอนนี้ควรวางออเดอร์รอที่โซนนั้นไหม
- verdict: ENTER (ควรวางออเดอร์รอที่โซนนี้ ให้ entry/sl/tp), WAIT (โซนน่าสนใจแต่ยังไม่ควรวาง บอกใน wait_for ว่ารออะไร), NO (ไม่ควรเข้าโซนนี้ เช่น สวนเทรนด์/โซนอ่อน/ใกล้ข่าวแรง)
กติกาของ ENTER:
1. entry ต้องอยู่ในโซนที่ให้มา (หรือใกล้ขอบไม่เกิน 2) เลือกจุดที่ดีที่สุดในโซนเอง เช่น ขอบที่ราคามาถึงก่อน กลางโซน
2. sl ต้องอยู่นอกขอบไกลของโซน (SHORT: เหนือขอบบน LONG: ใต้ขอบล่าง) บวกส่วนเผื่อพอสมควร และอยู่หลังโครงสร้างที่ทำให้ไอเดียผิด
3. tp อยู่ที่ระดับเป้าหมายจริงตาม KEY LEVELS ห่าง SL อย่างน้อย min_sl_distance และ RR อย่างน้อย min_rr (โค้ดจะตรวจซ้ำ ไม่ผ่านจะตีตก)
4. ห้ามแต่งระดับที่ไม่มีเหตุผลรองรับ ห้ามเขียนชื่อชนิดออเดอร์ (Limit/Stop) โค้ดแสดงเอง
5. ตัดสินจากหลักฐาน: เทรนด์ 4H/1H/15M เทียบทิศที่ผู้ใช้ให้ ระยะราคาห่างโซน ข่าว session ถ้าข้อมูลไหน not_available ให้บอกว่าไม่มีข้อมูล
ถ้าทิศที่ผู้ใช้ให้สวนเทรนด์ใหญ่ชัดเจน ไม่ต้องเกรงใจ ตอบ NO หรือ WAIT พร้อมเหตุผลตรงๆ
ตอบเป็น JSON ก้อนเดียว ไม่มี markdown:
{"verdict":"ENTER|WAIT|NO","confidence":<0-100>,"reasons":["เหตุผลสั้นๆ 1","2","3 (ไม่บังคับ)"],
 "entry":<ตัวเลขหรือ null>,"sl":<ตัวเลขหรือ null>,"tp":<ตัวเลขหรือ null>,"basis":"อ้างอิงระดับไหน 1 ประโยค","wait_for":"ถ้า WAIT: รออะไร ไม่งั้นสตริงว่าง"}
reasons แต่ละข้อไม่เกิน ~110 ตัวอักษร"""


def sanitize_zone(data, zone, price, config):
    """ตรวจคำตอบ AI สำหรับโหมดโซน -> dict (valid=True เฉพาะ ENTER ที่ผ่านกฎโค้ดทั้งหมด)"""
    verdict = str(data.get("verdict", "")).upper().strip()
    if verdict not in ("ENTER", "WAIT", "NO"):
        raise ValueError("verdict ไม่ถูกต้อง")
    out = {"verdict": verdict, "reasons": [str(x).strip()[:160] for x in (data.get("reasons") or []) if str(x).strip()][:3],
           "basis": str(data.get("basis") or "").strip()[:240], "wait_for": str(data.get("wait_for") or "").strip()[:200],
           "confidence": None, "entry": None, "sl": None, "tp": None, "rr": None, "entry_side": None,
           "valid": False, "invalid_reason": None}
    try:
        out["confidence"] = max(0, min(100, int(data.get("confidence"))))
    except (TypeError, ValueError):
        pass
    if verdict != "ENTER":
        return out
    try:
        entry, sl, tp = float(data["entry"]), float(data["sl"]), float(data["tp"])
    except (KeyError, TypeError, ValueError):
        out["invalid_reason"] = "AI ตอบควรเข้าแต่ตัวเลข Entry/SL/TP ไม่ครบ"
        return out
    out.update({"entry": round(entry, 3), "sl": round(sl, 3), "tp": round(tp, 3)})
    lo, hi, d = zone["lo"], zone["hi"], zone["direction"]
    if not (lo - 2 <= entry <= hi + 2):
        out["invalid_reason"] = f"Entry {entry:.2f} อยู่นอกโซนที่คุณให้ ({lo:g}-{hi:g})"
        return out
    if (d == "bearish" and sl <= hi) or (d == "bullish" and sl >= lo):
        out["invalid_reason"] = "SL อยู่ในโซน/ไม่พ้นขอบโซน โซนนี้จึงไม่ได้ป้องกันไอเดียจริง"
        return out
    v = validate_trade(d, entry, sl, tp, price, config)
    out["rr"], out["entry_side"] = v["rr"], v["entry_side"]
    out["valid"], out["invalid_reason"] = v["ok"], v["reason"]
    return out


def format_zone_message(symbol_label, price, zone, res, ai_error, config):
    d_txt = _dir_th(zone["direction"])
    lo, hi = zone["lo"], zone["hi"]
    if lo <= price <= hi:
        pos = "ราคาอยู่ในโซนแล้ว"
    elif price < lo:
        pos = f"ราคาต่ำกว่าโซน {lo - price:.1f}"
    else:
        pos = f"ราคาสูงกว่าโซน {price - hi:.1f}"
    lines = [f"🎯 <b>/orderai โซนของคุณ — {_esc(symbol_label)}</b>", f"ราคา {price:.2f} · {datetime.now(THAI_TZ).strftime('%H:%M')} น.",
             f"โซนที่คุณให้: <b>{lo:g} – {hi:g}</b> · {d_txt} · {pos}", ""]
    if res is None:
        lines.append("🛠 ตรวจโซนไม่ได้ตอนนี้ (AI ใช้ไม่ได้)")
        if ai_error:
            lines.append(_esc(ai_error)[:200])
        lines.append("ลองพิมพ์ใหม่อีกครั้ง")
        return "\n".join(lines)
    lines.append(VERDICT_LABEL[res["verdict"]] + (f"  (มั่นใจ {res['confidence']}%)" if res.get("confidence") is not None else ""))
    if res["reasons"]:
        lines.append("")
        lines.append("<b>สาเหตุ</b>")
        lines += [f"• {_esc(r)}" for r in res["reasons"]]
    lines.append("")
    if res["verdict"] == "ENTER" and res["valid"]:
        kind = order_type_name(zone["direction"], res["entry_side"])
        lines += [f"📍 <b>จุดเข้า — {d_txt}</b>",
                  f"Entry ({kind}): <b>{res['entry']:.2f}</b>  (ห่างราคาปัจจุบัน {abs(res['entry'] - price):.1f})",
                  f"SL: <b>{res['sl']:.2f}</b>  (ห่าง {abs(res['entry'] - res['sl']):.1f})",
                  f"TP: <b>{res['tp']:.2f}</b>  (RR {res['rr']})"]
        if res.get("basis"):
            lines.append(f"💡 {_esc(res['basis'])}")
        lines.append(f"\n👇 ถ้าวางออเดอร์นี้เอง กดปุ่มด้านล่าง (หมดอายุใน {config.get('orderai_button_ttl_minutes', 15)} นาที)")
    elif res["verdict"] == "ENTER":
        lines.append("📍 AI เสนอให้เข้า แต่โค้ดตรวจแล้วไม่ผ่าน จึงไม่แนะนำให้วาง")
        if res.get("entry") is not None:
            lines.append(f"Entry {res['entry']:.2f} | SL {res['sl']:.2f} | TP {res['tp']:.2f}")
        lines.append(f"❌ เหตุผลที่ไม่ผ่าน: {_esc(res['invalid_reason'])}")
    else:
        lines.append("📍 <b>จุดเข้า: ไม่มี</b>")
        if res.get("wait_for"):
            lines.append(f"⏳ รอ: {_esc(res['wait_for'])}")
    if ai_error:
        lines.append(f"\n🛠 AI: {_esc(ai_error)[:160]}")
    return "\n".join(lines)


def analyze_zone(config, ctx, zone, symbol_label, master_trend=None):
    """ตรวจโซนของผู้ใช้ คืน dict: text, keyboard|None, proposal"""
    df = ctx["df_ind"]
    price = float(df["close"].iloc[-1])
    bias = (ctx.get("bias_4h") or {}).get("trend")
    t15 = (ctx.get("structure") or {}).get("trend")
    lines = [f"Symbol: {ctx['symbol']}", f"เวลา (ไทย): {datetime.now(THAI_TZ).strftime('%Y-%m-%d %H:%M')}",
             f"ราคาปัจจุบัน: {price:.2f}",
             f"เกณฑ์ที่โค้ดใช้ตรวจ: min_sl_distance={config.get('min_sl_distance')} min_rr={config.get('min_rr', 1.2)}",
             f"โซนที่ผู้ใช้ให้: {zone['lo']:g}-{zone['hi']:g} ทิศ {'SHORT (ขาย)' if zone['direction'] == 'bearish' else 'LONG (ซื้อ)'} "
             f"(ทิศ 'bullish'=ขึ้น 'bearish'=ลง)",
             f"เทรนด์ตอนนี้: 4H={_fmt(bias)} 1H={_fmt(ctx.get('higher_tf_trend'))} 15M={_fmt(t15)} เทรนด์หลักที่บอทสรุป={_fmt(master_trend)}",
             f"ตัวชี้วัด 15M: RSI={_fmt(round(float(df['rsi'].iloc[-1]), 1) if 'rsi' in df.columns else None)} "
             f"ADX={_fmt(round(float(df['adx'].iloc[-1]), 1) if 'adx' in df.columns else None)}",
             f"Session: {json.dumps(ctx.get('session_info'), default=str, ensure_ascii=False)}"]
    blackout = ctx.get("news_blackout", (False, None))
    lines.append(f"ช่วงห้ามเทรดรอบข่าว (±60 นาที): {'ใช่ — ' + str((blackout[1] or {}).get('title')) if blackout[0] else 'ไม่'}")
    news = _upcoming_news(config, ctx["symbol"])
    lines.append("ข่าวใกล้ๆ: " + ("; ".join(news) if news else "ไม่มีในช่วง 3 ชม. ข้างหน้า"))
    lines += ["", "== KEY LEVELS =="] + (build_key_levels(ctx, price) or ["not_available"])
    payload = {"model": config.get("ai_model", "claude-sonnet-5-5"), "max_tokens": config.get("orderai_max_tokens", 3000),
               "system": ZONE_PROMPT, "messages": [{"role": "user", "content": "\n".join(lines)}]}
    parsed, ai_error, usage = _post_ai(payload, config)
    res = None
    if parsed is not None:
        try:
            res = sanitize_zone(parsed, zone, price, config)
        except Exception as e:
            ai_error = f"คำตอบ AI ไม่ตรงรูปแบบ: {e}"
    pid = uuid.uuid4().hex[:8]
    pick = None
    if res and res["verdict"] == "ENTER" and res["valid"]:
        pick = {"num": "ZONE", "label": "โซนของคุณ", "plan_key": "ai_zone", "source": "ai_zone",
                "direction": zone["direction"], "entry": res["entry"], "sl": res["sl"], "tp": res["tp"], "rr": res["rr"],
                "entry_side": res["entry_side"], "score": None, "note": res.get("basis")}
    reasons = list((res or {}).get("reasons") or [])
    if res and res.get("invalid_reason"):
        reasons.append("ไม่ผ่านการตรวจ: " + res["invalid_reason"])
    zone_txt = f"โซนผู้ใช้ {zone['lo']:g}-{zone['hi']:g}"
    prop = {
        "id": pid, "symbol": ctx["symbol"], "created_ts": time.time(), "created_iso": _now().isoformat(),
        "status": "open" if pick else "info", "price": price, "verdict": (res or {}).get("verdict", "ERROR"),
        "confidence": (res or {}).get("confidence"), "reasons": [zone_txt] + reasons, "pick": pick,
        "ai_model": config.get("ai_model"), "ai_error": ai_error,
        "tokens": (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
        "bias_4h": bias, "trend_15m": t15, "tag": "ai_zone", "source": "ai_zone", "zone": zone,
    }
    return {"text": format_zone_message(symbol_label, price, zone, res, ai_error, config),
            "keyboard": build_keyboard(pid) if pick else None, "proposal": prop}
