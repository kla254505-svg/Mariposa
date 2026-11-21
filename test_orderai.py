"""ทดสอบ orderai.py (ไม่ยิง API/Telegram/Redis จริง — mock ทั้งหมด)"""
import json
import time

import pandas as pd
import pytest

import orderai
from config import CONFIG

CFG = dict(CONFIG, kvdb_bucket="t", telegram_token="x", telegram_owner_id="42", telegram_chat_id="42")


class FakeKV:
    def __init__(self):
        self.d = {}

    def get(self, b, k):
        return self.d.get(k)

    def set(self, b, k, v):
        self.d[k] = str(v)
        return True


@pytest.fixture
def kv(monkeypatch):
    f = FakeKV()
    monkeypatch.setattr(orderai, "kv_get", f.get)
    monkeypatch.setattr(orderai, "kv_set", f.set)
    sent = []
    monkeypatch.setattr(orderai, "_alert", lambda c, t: sent.append(t))
    monkeypatch.setattr(orderai, "_sheet_log", lambda row: sent.append(("sheet", row)))
    monkeypatch.setattr(orderai, "_tg", lambda token, method, **p: {"ok": True})
    monkeypatch.setattr(orderai, "send_message", lambda *a, **k: sent.append(("msg", a[2])))
    f.sent = sent
    return f


def test_validate_ok_long():
    v = orderai.validate_trade("bullish", 4000, 3985, 4045, 4005, CFG)
    assert v["ok"] and v["rr"] == 3.0 and v["entry_side"] == "below"


def test_validate_wrong_side_sl():
    assert not orderai.validate_trade("bullish", 4000, 4010, 4045, 4005, CFG)["ok"]
    assert not orderai.validate_trade("bearish", 4000, 3990, 3950, 3995, CFG)["ok"]


def test_validate_sl_too_tight_and_low_rr():
    assert "แคบ" in orderai.validate_trade("bullish", 4000, 3995, 4045, 4005, CFG)["reason"]
    assert "RR" in orderai.validate_trade("bullish", 4000, 3985, 4010, 4005, CFG)["reason"]


def test_validate_missed_and_sl_touched():
    # ราคาวิ่งไป 60% ของระยะ TP แล้ว
    assert "ตกรถ" in orderai.validate_trade("bullish", 4000, 3985, 4045, 4027, CFG)["reason"]
    assert "SL" in orderai.validate_trade("bullish", 4000, 3985, 4045, 3984, CFG)["reason"]
    assert orderai.validate_trade("bearish", 4000, 4015, 3955, 4016, CFG)["ok"] is False


def test_stop_entry_side_above():
    v = orderai.validate_trade("bullish", 4010, 3995, 4055, 4000, CFG)
    assert v["ok"] and v["entry_side"] == "above"


def _results():
    return [
        {"num": 2, "active": True, "label": "P2", "direction": "bullish", "score": 80,
         "trade": {"entry": 4000, "sl": 3985, "tp": 4045}},          # ปิดใช้งาน -> ต้องถูกตัดทิ้ง
        {"num": 5, "active": True, "label": "P5", "direction": "bullish", "score": 70,
         "trade": {"entry": 4000, "sl": 3985, "tp": 4045}},
        {"num": 7, "active": True, "label": "P7", "direction": "bearish", "score": 60,
         "trade": {"entry": 4000, "sl": 3995, "tp": 3950}},          # SL แคบ -> invalid
        {"num": 8, "active": False, "label": "P8", "note": "รอ"},
    ]


def test_collect_candidates_drops_disabled_and_flags_invalid():
    c = orderai.collect_candidates(_results(), 4005, CFG)
    nums = {x["num"]: x for x in c}
    assert 2 not in nums and nums[5]["valid"] and not nums[7]["valid"]


def test_sanitize_enter_requires_valid_pick():
    c = orderai.collect_candidates(_results(), 4005, CFG)
    ok = orderai.sanitize_ai({"verdict": "ENTER", "pick": 5, "reasons": ["a"], "confidence": 70}, c)
    assert ok["verdict"] == "ENTER" and ok["pick"] == 5
    bad = orderai.sanitize_ai({"verdict": "ENTER", "pick": 7, "reasons": ["a"]}, c)       # invalid plan
    assert bad["verdict"] == "NO" and bad["pick"] is None and bad["downgraded"]
    bad2 = orderai.sanitize_ai({"verdict": "ENTER", "pick": 2, "reasons": []}, c)         # ไม่มีในรายการ
    assert bad2["verdict"] == "NO"
    with pytest.raises(ValueError):
        orderai.sanitize_ai({"verdict": "MAYBE"}, c)


def test_extract_json_handles_fence_and_noise():
    assert orderai._extract_json('```json\n{"a":1}\n```')["a"] == 1
    assert orderai._extract_json('ok {"a":2} bye')["a"] == 2
    with pytest.raises(Exception):
        orderai._extract_json('{"a": "unterminated')


def test_rules_only_picks_aligned_valid():
    c = orderai.collect_candidates(_results(), 4005, CFG)
    d = orderai.rules_only(c, "bullish")
    assert d["verdict"] == "RULES" and d["pick"] == 5
    assert orderai.rules_only(c, "bearish")["pick"] is None


def _ctx():
    df = pd.DataFrame({"close": [4004.0, 4005.0], "high": [4006, 4007], "low": [4001, 4002], "rsi": [50, 51]})
    return {"symbol": "XAUUSD", "df_ind": df, "bias_4h": {"trend": "bullish"}, "structure": {"trend": "bullish"},
            "higher_tf_trend": "bullish", "session_info": {"in_session": True}, "news_blackout": (False, None),
            "config": CFG}


def test_analyze_with_ai_enter_and_fallback(monkeypatch, kv):
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (
        {"verdict": "ENTER", "pick": 5, "confidence": 72, "reasons": ["ตามเทรนด์ 4H"], "risk_note": "", "no_entry_reason": ""},
        None, {"input_tokens": 100, "output_tokens": 50}))
    out = orderai.analyze(CFG, _ctx(), _results(), "bullish", "4H", "GOLD")
    assert out["keyboard"] and "ควรเข้า" in out["text"] and "4000.00" in out["text"]
    assert out["proposal"]["pick"]["num"] == 5 and out["proposal"]["tokens"] == 150

    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (None, "AI ตอบ HTTP 400", {}))
    out = orderai.analyze(CFG, _ctx(), _results(), "bullish", "4H", "GOLD")
    assert "สรุปตามกฎ" in out["text"] and "HTTP 400" in out["text"]


def test_analyze_no_entry_has_no_buttons(monkeypatch, kv):
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (
        {"verdict": "NO", "pick": None, "reasons": ["สวนเทรนด์"], "no_entry_reason": "ยังไม่มีโซนที่ชัด"}, None, {}))
    out = orderai.analyze(CFG, _ctx(), [_results()[3]], "bullish", "4H", "GOLD")
    assert out["keyboard"] is None and "จุดเข้า: ไม่มี" in out["text"] and "ยังไม่มีโซนที่ชัด" in out["text"]


def _prop(pick=None):
    pick = pick or {"num": 5, "plan_key": "plan5_zone_single", "direction": "bullish", "entry": 4000.0,
                    "sl": 3985.0, "tp": 4045.0, "rr": 3.0, "score": 70}
    return {"id": "abc12345", "symbol": "XAUUSD", "created_ts": time.time(), "created_iso": "x", "status": "open",
            "price": 4005.0, "verdict": "ENTER", "pick": pick, "bias_4h": "bullish", "trend_15m": "bullish",
            "reasons": [], "ai_model": "m"}


def _cq(data, uid=42):
    return {"id": "1", "data": data, "from": {"id": uid}, "message": {"chat": {"id": 42}, "message_id": 9}}


def test_callback_place_then_double_click(kv):
    orderai.save_proposal(CFG, _prop())
    orderai.handle_callback(CFG, "x", _cq("oai:p:abc12345"), lambda s: 4006.0)
    items = json.loads(kv.d["orderai_tracked_XAUUSD"])
    assert len(items) == 1 and items[0]["status"] == "waiting" and items[0]["entry_side"] == "below"
    orderai.handle_callback(CFG, "x", _cq("oai:p:abc12345"), lambda s: 4006.0)   # กดซ้ำ ต้องไม่เพิ่ม
    assert len(json.loads(kv.d["orderai_tracked_XAUUSD"])) == 1


def test_callback_rejects_stale_and_other_user(kv):
    orderai.save_proposal(CFG, _prop())
    orderai.handle_callback(CFG, "x", _cq("oai:p:abc12345", uid=7), lambda s: 4006.0)
    assert "orderai_tracked_XAUUSD" not in kv.d
    orderai.handle_callback(CFG, "x", _cq("oai:p:abc12345"), lambda s: 4030.0)   # ราคาหนีไป 67% ของ TP
    assert "orderai_tracked_XAUUSD" not in kv.d
    assert json.loads(kv.d["orderai_prop_abc12345"])["status"] == "refused"


def test_callback_expired_and_skip(kv):
    p = _prop(); p["created_ts"] = time.time() - 3600
    orderai.save_proposal(CFG, p)
    orderai.handle_callback(CFG, "x", _cq("oai:p:abc12345"), lambda s: 4006.0)
    assert json.loads(kv.d["orderai_prop_abc12345"])["status"] == "expired"
    orderai.save_proposal(CFG, _prop())
    orderai.handle_callback(CFG, "x", _cq("oai:s:abc12345"), lambda s: 4006.0)
    assert json.loads(kv.d["orderai_prop_abc12345"])["status"] == "skipped"
    assert "orderai_tracked_XAUUSD" not in kv.d


def _track(kv, item_over=None, **kw):
    it = {"id": "abc12345", "symbol": "XAUUSD", "plan": "plan5_zone_single", "plan_num": 5, "direction": "bullish",
          "entry": 4000.0, "sl": 3985.0, "tp": 4045.0, "rr": 3.0, "entry_side": "below", "status": "waiting",
          "placed_ts": time.time(), "expires_ts": time.time() + 3600, "bias_at": "bullish", "trend15_at": "bullish",
          "flags": {}, "checked_once": True}
    it.update(item_over or {})
    kv.d["orderai_tracked_XAUUSD"] = json.dumps([it])
    df = pd.DataFrame({"high": [kw.get("hi", 4010)], "low": [kw.get("lo", 4004)], "close": [kw.get("price", 4006)]})
    n = orderai.track_orders(CFG, "XAUUSD", df, kw.get("price", 4006), bias_trend=kw.get("bias"), trend_15m=kw.get("t15"))
    items = json.loads(kv.d["orderai_tracked_XAUUSD"])
    return n, items, [s for s in kv.sent if isinstance(s, str)]


def test_track_waiting_stays_quiet(kv):
    n, items, msgs = _track(kv, bias="bullish", t15="bullish")
    assert n == 0 and items[0]["status"] == "waiting" and not msgs


def test_track_fill_then_tp(kv):
    n, items, msgs = _track(kv, lo=3999.0, price=4001.0)
    assert items[0]["status"] == "running" and "เข้าแล้ว" in msgs[0]
    n, items, msgs = _track(kv, item_over={"status": "running"}, hi=4046.0, lo=4020.0, price=4040.0)
    assert items == [] and "TP" in msgs[-1] and "+3.0R" in msgs[-1]


def test_track_running_sl_and_same_bar_conflict(kv):
    n, items, msgs = _track(kv, item_over={"status": "running"}, hi=4010, lo=3984, price=3990)
    assert items == [] and "SL" in msgs[-1]
    n, items, msgs = _track(kv, item_over={"status": "running"}, hi=4046, lo=3984, price=4000)
    assert "SL" in msgs[-1] and "แท่งเดียวแตะทั้ง" in msgs[-1]


def test_track_missed_expired_and_flip(kv):
    n, items, msgs = _track(kv, hi=4030, lo=4025, price=4028)         # ไม่เคยลงมาถึง 4000 แต่วิ่งไป 62%
    assert items == [] and "ตกรถ" in msgs[-1]
    n, items, msgs = _track(kv, item_over={"expires_ts": time.time() - 1})
    assert items == [] and "หมดอายุ" in msgs[-1]
    n, items, msgs = _track(kv, bias="bearish", t15="bullish")
    assert len(items) == 1 and items[0]["flags"]["flip4h"] and "4H" in msgs[-1]
    n, items, msgs = _track(kv, item_over={"flags": {"flip4h": True}}, bias="bearish", t15="bullish")
    assert n == 0                                                       # เตือนครั้งเดียว ไม่สแปม


def test_track_first_check_uses_price_only(kv):
    # รอบแรกหลังวาง: low ของแท่ง (3990) อาจเกิดก่อนวาง ต้องไม่ถือว่าเข้า ถ้าราคาปัจจุบันยัง 4006
    n, items, msgs = _track(kv, item_over={"checked_once": False}, lo=3990, hi=4010, price=4006)
    assert items[0]["status"] == "waiting" and not msgs


# ───────── ส่วน ② ความเห็น AI เอง ─────────
def test_sanitize_idea_valid_none_and_invalid():
    ok = orderai.sanitize_idea({"direction": "LONG", "entry": 4000, "sl": 3985, "tp": 4045, "basis": "OB", "confidence": 60}, 4005, CFG)
    assert ok["valid"] and ok["direction"] == "bullish" and ok["rr"] == 3.0
    none = orderai.sanitize_idea({"direction": "NONE", "none_reason": "ราคากลางกรอบ"}, 4005, CFG)
    assert none["direction"] is None and none["none_reason"] == "ราคากลางกรอบ" and not none["valid"]
    wrong = orderai.sanitize_idea({"direction": "LONG", "entry": 4000, "sl": 4010, "tp": 4045}, 4005, CFG)
    assert not wrong["valid"] and wrong["invalid_reason"]
    tight = orderai.sanitize_idea({"direction": "SHORT", "entry": 4010, "sl": 4013, "tp": 3950}, 4005, CFG)
    assert not tight["valid"] and "แคบ" in tight["invalid_reason"]
    incomplete = orderai.sanitize_idea({"direction": "LONG", "entry": 4000}, 4005, CFG)
    assert not incomplete["valid"] and "ไม่ครบ" in incomplete["invalid_reason"]
    assert orderai.sanitize_idea(None, 4005, CFG)["direction"] is None


def test_analyze_two_sections_and_ai_free_flow(monkeypatch, kv):
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (
        {"verdict": "WAIT", "pick": None, "reasons": ["รอก่อน"], "no_entry_reason": "",
         "ai_idea": {"direction": "LONG", "entry": 3990, "sl": 3975, "tp": 4035, "basis": "OB 4H", "confidence": 55}},
        None, {}))
    out = orderai.analyze(CFG, _ctx(), [_results()[3]], "bullish", "4H", "GOLD")
    assert out["keyboard"] is None                     # ส่วน ① ไม่มีจุดเข้า
    assert out["idea_keyboard"] and "ความเห็นของ AI" in out["idea_text"] and "3990.00" in out["idea_text"]
    assert "Risk" not in out["idea_text"] and "ยังไม่มีสถิติ" in out["idea_text"]
    ip = out["idea_proposal"]
    assert ip["id"] == out["proposal"]["id"] + "a" and ip["pick"]["source"] == "ai_free"

    # กด วางแล้ว -> เข้า tracking พร้อมแท็ก ai_free
    ip["price"] = 4005.0
    orderai.save_proposal(CFG, ip)
    orderai.handle_callback(CFG, "x", _cq(f"oai:p:{ip['id']}"), lambda s: 4005.0)
    items = json.loads(kv.d["orderai_tracked_XAUUSD"])
    assert items[0]["source"] == "ai_free" and "AI คิดเอง" not in json.dumps(items[0]["flags"])
    rows = [r[1] for r in kv.sent if isinstance(r, tuple) and r[0] == "sheet"]
    assert any(r.get("Tag") == "ai_free" and r.get("Plan_ID") == "AI" and r.get("Decision") == "PLACED" for r in rows)
    # tracking: เข้าแล้วข้อความต้องเรียกว่า "AI คิดเอง"
    orderai.track_orders(CFG, "XAUUSD", pd.DataFrame({"high": [4000], "low": [3989]}), 3995.0)
    orderai.track_orders(CFG, "XAUUSD", pd.DataFrame({"high": [4000], "low": [3989]}), 3995.0)
    assert any(isinstance(m, str) and "AI คิดเอง" in m and "ออเดอร์เข้าแล้ว" in m for m in kv.sent)


def test_analyze_idea_none_and_invalid_have_no_buttons(monkeypatch, kv):
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (
        {"verdict": "NO", "reasons": ["x"], "ai_idea": {"direction": "NONE", "none_reason": "ไม่มีโซนชัด"}}, None, {}))
    out = orderai.analyze(CFG, _ctx(), [], "bullish", "4H", "GOLD")
    assert out["idea_keyboard"] is None and "จุดเข้า: ไม่มี" in out["idea_text"] and "ไม่มีโซนชัด" in out["idea_text"]
    assert out["idea_proposal"]["status"] == "info"
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (
        {"verdict": "NO", "reasons": ["x"], "ai_idea": {"direction": "LONG", "entry": 4000, "sl": 4010, "tp": 4045}}, None, {}))
    out = orderai.analyze(CFG, _ctx(), [], "bullish", "4H", "GOLD")
    assert out["idea_keyboard"] is None and "ไม่ผ่าน" in out["idea_text"]


def test_rules_fallback_has_no_idea_section(monkeypatch, kv):
    monkeypatch.setattr(orderai, "call_ai", lambda t, c: (None, "AI ตอบ HTTP 400", {}))
    assert orderai.analyze(CFG, _ctx(), _results(), "bullish", "4H", "GOLD")["idea_text"] is None


def test_key_levels_from_ctx():
    n = 60
    df = pd.DataFrame({"open": [4000 + (i % 7) for i in range(n)], "high": [4005 + (i % 9) for i in range(n)],
                       "low": [3995 - (i % 5) for i in range(n)], "close": [4001 + (i % 6) for i in range(n)]})
    ctx = {"df_ind": df, "bias_4h": {"swing_high": 4100.0, "swing_low": 3900.0, "equilibrium_price": 4000.0}, "config": CFG}
    kl = orderai.build_key_levels(ctx, 4003)
    txt = "\n".join(kl)
    assert "High/Low 24 ชม." in txt and "กรอบ 4H" in txt and "4100.00" in txt


# ───────── /myorder + ชื่อชนิดออเดอร์ ─────────
def test_order_type_name_by_direction():
    assert orderai.order_type_name("bullish", "below", False) == "Buy Limit"
    assert orderai.order_type_name("bullish", "above", False) == "Buy Stop"
    assert orderai.order_type_name("bearish", "above", False) == "Sell Limit"   # กรณีในรูป: SHORT Entry เหนือราคา
    assert orderai.order_type_name("bearish", "below", False) == "Sell Stop"


def _tracked(kv, **over):
    it = {"id": "old11111", "symbol": "XAUUSD", "plan": "ai_free", "plan_num": "AI", "source": "ai_free", "direction": "bearish",
          "entry": 4010.0, "sl": 4025.0, "tp": 3965.0, "rr": 3.0, "entry_side": "above", "status": "waiting",
          "placed_ts": time.time() - 1800, "placed_iso": "x", "expires_ts": time.time() + 3600 * 5, "bias_at": "bearish",
          "trend15_at": "bearish", "flags": {}, "checked_once": True}
    it.update(over)
    kv.d["orderai_tracked_XAUUSD"] = json.dumps([it])
    return it


def test_myorder_empty_and_rules_fallback(monkeypatch, kv):
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: (None, "AI ตอบ HTTP 400", {}))
    out = orderai.review_orders(CFG, _ctx(), [], "bullish", "GOLD")
    assert "ไม่มีออเดอร์ที่ติดตาม" in out["text"] and out["keyboard"] is None
    it = _tracked(kv)
    out = orderai.review_orders(CFG, _ctx(), [it], "bullish", "GOLD")   # ctx เทรนด์ขึ้น = สวน SHORT -> กฎแนะนำยกเลิก
    assert "ควรยกเลิก" in out["text"] and "Sell Limit" in out["text"] and out["keyboard"] is None


def test_myorder_ai_move_valid_and_replace_flow(monkeypatch, kv):
    it = _tracked(kv)
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"orders": [
        {"id": "old11111", "action": "MOVE", "reason": "โซนเลื่อนลง", "new_entry": 4008, "new_sl": 4020, "new_tp": 3970}]}, None, {}))
    out = orderai.review_orders(CFG, _ctx(), [it], "bearish", "GOLD")
    assert out["keyboard"] and "ยกเลิกออเดอร์เดิมในโบรก" in out["text"] and "4008.00" in out["text"]
    p = out["proposals"][0]
    assert p["replaces"] == "old11111" and p["tag"] == "ai_move"
    orderai.save_proposal(CFG, p)
    orderai.handle_callback(CFG, "x", _cq(f"oai:r:{p['id']}"), lambda s: 4005.0)
    items = json.loads(kv.d["orderai_tracked_XAUUSD"])
    assert [x["id"] for x in items] == [p["id"]] and items[0]["entry"] == 4008.0 and items[0]["replaces"] == "old11111"
    rows = [r[1] for r in kv.sent if isinstance(r, tuple) and r[0] == "sheet"]
    assert any(r.get("Result") == "REPLACED" and r.get("Proposal_ID") == "old11111" for r in rows)
    assert any(r.get("Tag") == "ai_move" and r.get("Decision") == "PLACED" for r in rows)


def test_myorder_move_invalid_downgrades_and_running_cannot_move(monkeypatch, kv):
    it = _tracked(kv)
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"orders": [
        {"id": "old11111", "action": "MOVE", "reason": "x", "new_entry": 4008, "new_sl": 3990, "new_tp": 3970}]}, None, {}))
    out = orderai.review_orders(CFG, _ctx(), [it], "bearish", "GOLD")
    assert out["keyboard"] is None and "ไม่ผ่านการตรวจ" in out["text"]
    run = _tracked(kv, status="running")
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"orders": [
        {"id": "old11111", "action": "MOVE", "reason": "x", "new_entry": 4008, "new_sl": 4020, "new_tp": 3970}]}, None, {}))
    out = orderai.review_orders(CFG, _ctx(), [run], "bearish", "GOLD")   # MOVE ไม่ถูกยอมรับกับออเดอร์ที่วิ่งอยู่ -> ใช้กฎ
    assert out["keyboard"] is None and "ถือต่อ" in out["text"]


def test_replace_refused_if_old_already_filled_and_keep_old(monkeypatch, kv):
    it = _tracked(kv)
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"orders": [
        {"id": "old11111", "action": "MOVE", "reason": "x", "new_entry": 4008, "new_sl": 4020, "new_tp": 3970}]}, None, {}))
    p = orderai.review_orders(CFG, _ctx(), [it], "bearish", "GOLD")["proposals"][0]
    orderai.save_proposal(CFG, p)
    _tracked(kv, status="running")                      # ระหว่างนั้นออเดอร์เดิมเข้าแล้ว
    orderai.handle_callback(CFG, "x", _cq(f"oai:r:{p['id']}"), lambda s: 4005.0)
    items = json.loads(kv.d["orderai_tracked_XAUUSD"])
    assert [x["id"] for x in items] == ["old11111"]     # ไม่แตะของเดิม ไม่เพิ่มของใหม่
    p2 = orderai.review_orders(CFG, _ctx(), [it], "bearish", "GOLD")["proposals"][0]
    orderai.save_proposal(CFG, p2)
    orderai.handle_callback(CFG, "x", _cq(f"oai:n:{p2['id']}"), lambda s: 4005.0)
    assert json.loads(orderai.load_proposal.__globals__["kv_get"]("t", f"orderai_prop_{p2['id']}"))["status"] == "skipped"
    assert [x["id"] for x in json.loads(kv.d["orderai_tracked_XAUUSD"])] == ["old11111"]


def test_track_sweeps_5m_bars_after_delayed_cron(kv):
    # cron ดีเลย์: ราคาแตะ Entry ไปแล้วแต่ตอนนี้ถอยกลับมาต่ำกว่า Entry — ต้องจับได้จากแท่ง 5M
    it = {"id": "d1", "symbol": "XAUUSD", "plan": "ai_free", "plan_num": "AI", "source": "ai_free", "direction": "bearish",
          "entry": 4010.0, "sl": 4025.0, "tp": 3965.0, "rr": 3.0, "entry_side": "above", "status": "waiting",
          "placed_ts": time.time() - 3000, "expires_ts": time.time() + 9999, "flags": {}, "checked_once": True,
          "last_check_ts": time.time() - 1500}
    kv.d["orderai_tracked_XAUUSD"] = json.dumps([it])
    df15 = pd.DataFrame({"high": [4002.0], "low": [3998.0]})
    df5 = pd.DataFrame({"high": [4001.0, 4011.0, 4003.0, 4002.0, 4001.0, 4000.0], "low": [3995.0] * 6})
    orderai.track_orders(CFG, "XAUUSD", df15, 4000.0, df_5m=df5)
    assert json.loads(kv.d["orderai_tracked_XAUUSD"])[0]["status"] == "running"
    assert any(isinstance(m, str) and "ออเดอร์เข้าแล้ว" in m for m in kv.sent)
    # ไม่ส่ง df_5m -> พฤติกรรมเดิม (ดูแค่แท่งสุดท้าย) ไม่พัง
    it["status"] = "waiting"; it["last_check_ts"] = time.time() - 1500
    kv.d["orderai_tracked_XAUUSD"] = json.dumps([it])
    orderai.track_orders(CFG, "XAUUSD", df15, 4000.0)
    assert json.loads(kv.d["orderai_tracked_XAUUSD"])[0]["status"] == "waiting"


# ───────── /orderai <โซน> <ทิศ> ─────────
@pytest.mark.parametrize("args,lo,hi,d", [
    (["3568-3580", "short"], 3568, 3580, "bearish"),
    (["3625.261-3658.251", "short"], 3625.261, 3658.251, "bearish"),
    (["3625–3658", "long"], 3625, 3658, "bullish"),          # en dash
    (["3580-3568SELL"], 3568, 3580, "bearish"),              # สลับสูง/ต่ำ + ติดกัน
    (["3500", "-", "3510", "buy"], 3500, 3510, "bullish"),
    (["3500", "ถึง", "3510", "ซื้อ"], 3500, 3510, "bullish"),
])
def test_parse_zone_args_ok(args, lo, hi, d):
    z, err = orderai.parse_zone_args(args)
    assert err is None and z == {"lo": lo, "hi": hi, "direction": d}


def test_parse_zone_args_modes_and_errors():
    assert orderai.parse_zone_args([]) == (None, None)
    assert orderai.parse_zone_args(["gold"]) == (None, None)             # โหมดเดิม (เลือกคู่เงิน)
    z, err = orderai.parse_zone_args(["3568-3580"])
    assert z is None and "ต้องใส่ทิศ" in err
    assert orderai.parse_zone_args(["3568", "short"])[0] is None          # โซนไม่ครบ
    assert orderai.parse_zone_args(["3568-3568.2", "short"])[1]           # แคบเกิน


ZONE = {"lo": 4010.0, "hi": 4020.0, "direction": "bearish"}


def test_sanitize_zone_rules():
    ok = orderai.sanitize_zone({"verdict": "ENTER", "confidence": 60, "entry": 4015, "sl": 4030, "tp": 3970}, ZONE, 4000, CFG)
    assert ok["valid"] and ok["entry_side"] == "above"
    out_zone = orderai.sanitize_zone({"verdict": "ENTER", "entry": 4040, "sl": 4055, "tp": 3990}, ZONE, 4000, CFG)
    assert not out_zone["valid"] and "นอกโซน" in out_zone["invalid_reason"]
    sl_in = orderai.sanitize_zone({"verdict": "ENTER", "entry": 4015, "sl": 4018, "tp": 3970}, ZONE, 4000, CFG)
    assert not sl_in["valid"] and "SL" in sl_in["invalid_reason"]
    assert not orderai.sanitize_zone({"verdict": "ENTER", "entry": 4015}, ZONE, 4000, CFG)["valid"]
    wait = orderai.sanitize_zone({"verdict": "WAIT", "reasons": ["x"], "wait_for": "รอแท่งกลับตัว"}, ZONE, 4000, CFG)
    assert wait["verdict"] == "WAIT" and not wait["valid"]
    with pytest.raises(ValueError):
        orderai.sanitize_zone({"verdict": "??"}, ZONE, 4000, CFG)


def test_analyze_zone_enter_then_place_tracks_as_ai_zone(monkeypatch, kv):
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: (
        {"verdict": "ENTER", "confidence": 65, "reasons": ["เทรนด์ลงตรงทิศ"], "entry": 4015, "sl": 4030, "tp": 3970,
         "basis": "ขอบโซนขาลง 15M"}, None, {"input_tokens": 10, "output_tokens": 5}))
    out = orderai.analyze_zone(CFG, _ctx(), ZONE, "GOLD", "bearish")
    assert out["keyboard"] and "โซนที่คุณให้" in out["text"] and "Sell Limit" in out["text"] and "Risk" not in out["text"]
    p = out["proposal"]
    assert p["tag"] == "ai_zone" and p["pick"]["source"] == "ai_zone" and p["tokens"] == 15
    orderai.save_proposal(CFG, p)
    orderai.handle_callback(CFG, "x", _cq(f"oai:p:{p['id']}"), lambda s: 4005.0)
    assert json.loads(kv.d["orderai_tracked_XAUUSD"])[0]["source"] == "ai_zone"
    rows = [r[1] for r in kv.sent if isinstance(r, tuple) and r[0] == "sheet"]
    assert any(r.get("Tag") == "ai_zone" and r.get("Plan_ID") == "ZONE" and r.get("Decision") == "PLACED" for r in rows)
    assert "โซนของคุณ" in orderai._item_name(json.loads(kv.d["orderai_tracked_XAUUSD"])[0])


def test_analyze_zone_no_wait_invalid_and_ai_error(monkeypatch, kv):
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"verdict": "NO", "reasons": ["สวนเทรนด์"]}, None, {}))
    out = orderai.analyze_zone(CFG, _ctx(), ZONE, "GOLD")
    assert out["keyboard"] is None and "ไม่ควรเข้า" in out["text"] and out["proposal"]["status"] == "info"
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: ({"verdict": "ENTER", "entry": 4040, "sl": 4055, "tp": 3990}, None, {}))
    out = orderai.analyze_zone(CFG, _ctx(), ZONE, "GOLD")
    assert out["keyboard"] is None and "ไม่ผ่าน" in out["text"]
    monkeypatch.setattr(orderai, "_post_ai", lambda p, c: (None, "AI ตอบ HTTP 400", {}))
    out = orderai.analyze_zone(CFG, _ctx(), ZONE, "GOLD")
    assert out["keyboard"] is None and "ตรวจโซนไม่ได้" in out["text"]
