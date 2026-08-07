import json
from datetime import datetime, timedelta, timezone

from kvstore import kv_get, kv_set
from tp import calc_risk_reward

ORDERS_KEY_PREFIX = "open_orders"
# pending: Set & Forget วาง limit ไว้ล่วงหน้า ยังไม่ fill จริง (แผน 5-8) — ไม่นับ win/loss จนกว่าจะ
# เปลี่ยนเป็น running ก่อน (ราคามาถึง entry จริง) กัน /stats เพี้ยนจากออเดอร์ที่ไม่เคยเข้าไม้จริง
# expired: pending ที่ราคาไม่มาถึง entry ภายในเวลาที่กำหนด (พลาดโอกาส) ก็ไม่นับ win/loss เหมือนกัน
STATUS_EMOJI = {"pending": "⏳", "running": "💸", "win": "✅", "loss": "❌", "expired": "⌛"}
PLAN_LABEL = {
    "plan1_pullback": "แผนที่ 1 (Pullback ยืนยันแล้ว)",
    "plan1_pullback_early": "แผนที่ 1 (เข้าก่อนยืนยัน)",
    "plan2_breakout": "แผนที่ 2 (Breakout)",
    "plan3_counter_trend": "แผนที่ 3 (สวนเทรนด์)",
    "plan4_daily_continuation": "แผนที่ 4 (Daily Continuation)",
    "plan5_zone_single": "แผนที่ 5 (SMC Zone Entry — Set & Forget)",
    "plan6_sweep_general": "แผนที่ 6 (Liquidity Sweep + Displacement — Set & Forget)",
}
PLAN_SHORT = {
    "plan1_pullback": "1",
    "plan1_pullback_early": "1e",
    "plan2_breakout": "2",
    "plan3_counter_trend": "3",
    "plan4_daily_continuation": "4",
    "plan5_zone_single": "5",
    "plan6_sweep_general": "6",
}


def load_orders(bucket, symbol):
    """โหลดลิสต์ออเดอร์ทั้งหมดของ symbol นี้จาก kvdb.io"""
    raw = kv_get(bucket, f"{ORDERS_KEY_PREFIX}_{symbol}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_orders(bucket, symbol, orders):
    """
    บันทึกลิสต์ออเดอร์ลง kvdb.io คืนค่า True/False ตามผลจริง (ไม่ใช่แค่ยิง request ไปเฉยๆ)

    บั๊กเดิม: ฟังก์ชันนี้เรียก kv_set() แต่ไม่เคยเช็ค/คืนค่าผลลัพธ์เลย ทำให้ add_order() ด้านล่าง
    รายงานว่า "บันทึกสำเร็จ" เสมอแม้ kv_set จะเขียนไม่ผ่านจริง (เช่นโดน rate limit ตอน kvdb.io
    ถูกเรียกถี่จากการทดสอบหนัก) ผู้ใช้เห็นข้อความ "บันทึกลง Order Dashboard แล้ว" ทั้งที่ /summary
    และ /stats ว่างเปล่า เพราะไม่มีอะไรถูกเขียนลง kvdb จริงๆ

    ตอนนี้ลอง retry 1 ครั้งกันเคส rate limit ชั่วคราว (เว้น 1 วิ) ก่อนจะยอมรับว่าล้มเหลวจริง
    """
    import time
    key = f"{ORDERS_KEY_PREFIX}_{symbol}"
    payload = json.dumps(orders)
    if kv_set(bucket, key, payload):
        return True
    time.sleep(1)
    return kv_set(bucket, key, payload)


def add_order(bucket, symbol, direction, entry_price, stop_loss, take_profits, score, plan="plan1_pullback"):
    """
    บันทึกออเดอร์ใหม่ตอนที่ Alert ถูกส่งจริง (ไม่ว่าจะเป็นแผนที่ 1/2/3)
    คืนค่า order dict ถ้าบันทึกสำเร็จจริง หรือ None ถ้าบันทึกไม่สำเร็จ (kvdb เขียนพลาดแม้ retry แล้ว)
    — ผู้เรียก (telegram_bot.py/main.py) ต้องเช็คค่าที่คืนมาก่อนบอกผู้ใช้ว่า "บันทึกสำเร็จ"
    ห้ามสมมติว่าสำเร็จเสมอเหมือนเดิม

    plan: "plan1_pullback" | "plan2_breakout" | "plan3_counter_trend" — ใช้แยกคำนวณสถิติ
    (win rate/expectancy) รายแผนใน calc_stats() ด้านล่าง ค่า default เป็น plan1_pullback
    เพื่อไม่ให้กระทบโค้ดเดิมที่เรียก add_order() อยู่แล้วโดยไม่ได้ระบุ plan (ของเดิมมีแค่ Plan 1)

    บันทึก rr_tp1 (Risk:Reward ของ TP1 ณ ตอนเปิดออเดอร์) ไว้ด้วย เพื่อใช้คำนวณ expectancy —
    หมายเหตุ: เป็นค่า "ตามแผน" ไม่ใช่ RR ที่ได้จริงตอนปิดออเดอร์ (ระบบยังไม่ track ราคาปิดจริงแบบละเอียด
    แค่ win/loss แบบ binary ว่าถึง TP1 หรือ SL ก่อนกัน) ถือเป็นค่าประมาณสำหรับวัดผลเบื้องต้น
    """
    orders = load_orders(bucket, symbol)
    tp1 = take_profits.get("TP1")
    try:
        rr_tp1 = calc_risk_reward(entry_price, stop_loss, tp1) if tp1 is not None else None
    except Exception:
        rr_tp1 = None

    order = {
        # ใช้ timestamp ระดับไมโครวินาที (ไม่ใช่แค่วินาที) กัน id ชนกันตอนมีออเดอร์หลายอันถูกสร้าง
        # ในวินาทีเดียวกัน (int(timestamp()) ปัดเหลือวินาทีเดียว ชนกันได้ง่ายขึ้นเรื่อยๆ ตอนนี้มีหลาย
        # แผนเช็คพร้อมกันในรอบเดียว) — เดิมใช้แค่ int(timestamp()) เสี่ยง id ซ้ำกันได้จริง
        "id": f"{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "symbol": symbol,
        "plan": plan,
        "direction": direction,  # "bullish" หรือ "bearish"
        "entry_price": round(float(entry_price), 3),
        "stop_loss": round(float(stop_loss), 3),
        "take_profits": {k: round(float(v), 3) for k, v in take_profits.items()},
        "rr_tp1": rr_tp1,
        "score": score,
        "opened_at": datetime.now(timezone.utc).strftime("%H:%M"),
        "status": "running",
    }
    orders.append(order)
    success = save_orders(bucket, symbol, orders)
    if not success:
        print(f"[Order Tracking Error] บันทึกออเดอร์ (symbol={symbol}, plan={plan}) ลง kvdb ไม่สำเร็จ "
              f"แม้ retry แล้ว — ออเดอร์นี้จะไม่ปรากฏใน /summary หรือ /stats")
        return None
    return order


def add_pending_order(bucket, symbol, direction, entry_price, stop_loss, take_profits, score,
                       plan, expires_in_hours=8):
    """
    บันทึกออเดอร์แบบ 'pending' (Set & Forget — แผน 5-8) — วาง Limit Order ไว้ล่วงหน้าตอนเจอ zone/pattern
    ทันที ก่อนที่ราคาจะเดินทางมาถึงจริง ต่างจาก add_order() (แผน 1-4 เดิม) ที่บันทึกเป็น 'running'
    ทันทีเพราะรอราคาแตะ + มี reaction ยืนยันมาก่อนแล้วถึงแจ้งเตือน (ถือว่าเข้าไม้จริงตั้งแต่แจ้ง)

    วงจรสถานะของออเดอร์แบบนี้: pending -> running (พอราคามาถึง entry จริง ผ่าน update_pending_orders())
    -> win/loss (เหมือนเดิม ผ่าน update_orders_status()) หรือ pending -> expired (ราคาไม่มาถึงภายใน
    expires_in_hours ชม. — ถือว่าพลาดโอกาส ไม่นับ win/loss เพราะไม่เคยเข้าไม้จริง)

    expires_in_hours: ปรับได้ตาม timeframe ของแต่ละแผนย่อยที่มาเรียกใช้ (เช่น zone จาก 4H บริบทSo
    ควรอยู่ได้นานกว่า pattern จาก 15M) ค่า default 8 ชม.
    """
    orders = load_orders(bucket, symbol)
    tp1 = take_profits.get("TP1") if take_profits else None
    if tp1 is None and take_profits:
        tp1 = next(iter(take_profits.values()))
    try:
        rr_tp1 = calc_risk_reward(entry_price, stop_loss, tp1) if tp1 is not None else None
    except Exception:
        rr_tp1 = None

    now = datetime.now(timezone.utc)
    order = {
        "id": f"{symbol}_{now.strftime('%Y%m%d%H%M%S%f')}",
        "symbol": symbol,
        "plan": plan,
        "direction": direction,  # "bullish" หรือ "bearish"
        "entry_price": round(float(entry_price), 3),
        "stop_loss": round(float(stop_loss), 3),
        "take_profits": {k: round(float(v), 3) for k, v in take_profits.items()},
        "rr_tp1": rr_tp1,
        "score": score,
        "opened_at": now.strftime("%H:%M"),
        "created_at_iso": now.isoformat(),
        "expires_at_iso": (now + timedelta(hours=expires_in_hours)).isoformat(),
        "status": "pending",
    }
    orders.append(order)
    success = save_orders(bucket, symbol, orders)
    if not success:
        print(f"[Order Tracking Error] บันทึก pending order (symbol={symbol}, plan={plan}) ลง kvdb "
              f"ไม่สำเร็จ แม้ retry แล้ว — ออเดอร์นี้จะไม่ปรากฏใน /summary หรือ /stats")
        return None
    return order


def update_pending_orders(bucket, symbol, current_price, spread_buffer=0.0):
    """
    เช็คทุกออเดอร์ที่ยัง 'pending' (Set & Forget ที่ยังไม่ fill จริง) ทุกรอบที่บอทรัน:
    - ราคาเดินทางมาถึง entry_price (เผื่อ spread_buffer แล้ว) -> เปลี่ยนเป็น 'running' (เริ่มนับสถิติ
      win/loss จากจุดนี้ ผ่าน update_orders_status() ในรอบถัดไป)
    - หมดเวลาที่กำหนดไว้ (expires_at_iso) แล้วยังไม่ fill -> เปลี่ยนเป็น 'expired' (พลาดโอกาส
      ไม่นับ win/loss เพราะไม่เคยเข้าไม้จริง)
    เช็ค expiry ก่อนเช็ค fill เสมอ — ถ้าหมดอายุแล้วไม่ต้องเสียเวลาเช็คว่า fill หรือยัง
    บันทึกกลับ kvdb เฉพาะตอนมีการเปลี่ยนสถานะจริง เหมือน update_orders_status()

    spread_buffer: ราคาที่บอทเช็คมาจาก TwelveData (ราคากลาง) ไม่ใช่ bid/ask ของโบรกที่คุณเทรดจริง
    ซึ่งมี spread คั่นอยู่ — ต้องให้ราคาเลยจุด Entry ไปอีก spread_buffer ก่อนถึงจะถือว่า fill จริง
    กันเคสระบบบอกว่า "เข้าแล้ว" ทั้งที่โบรกจริงยังไม่ทันได้ fill ให้ (ตามที่ผู้ใช้ฟีดแบ็คมา)
    ใช้เฉพาะจุดนี้จุดเดียว — ไม่กระทบการเช็ค TP/SL ใน update_orders_status() ซึ่งยังใช้ราคาตรงเป๊ะเหมือนเดิม
    ค่า default 0.0 (ไม่มีผล) กันโค้ดเก่าที่เรียกไม่ครบ 4 อาร์กิวเมนต์พัง
    """
    orders = load_orders(bucket, symbol)
    changed = False
    now = datetime.now(timezone.utc)

    for o in orders:
        if o.get("status") != "pending":
            continue

        expires_at_iso = o.get("expires_at_iso")
        if expires_at_iso:
            try:
                expires_at = datetime.fromisoformat(expires_at_iso)
                if now >= expires_at:
                    o["status"] = "expired"
                    changed = True
                    continue
            except Exception:
                pass  # parse ไม่ได้ (ข้อมูลเก่า/เพี้ยน) ถือว่ายังไม่หมดอายุ ปล่อยให้เช็ค fill ต่อไป

        entry_price = o["entry_price"]
        direction = o["direction"]
        filled = (
            (direction == "bullish" and current_price <= entry_price - spread_buffer) or
            (direction == "bearish" and current_price >= entry_price + spread_buffer)
        )
        if filled:
            o["status"] = "running"
            o["filled_at"] = now.strftime("%H:%M")
            changed = True

    if changed:
        if not save_orders(bucket, symbol, orders):
            print(f"[Order Tracking Error] บันทึกสถานะ pending->running/expired (symbol={symbol}) "
                  f"ลง kvdb ไม่สำเร็จ — ผลลัพธ์ที่เพิ่งเปลี่ยนอาจหายไปตอน process นี้ปิดตัว")

    return orders


def update_orders_status(bucket, symbol, current_price):
    """
    เช็คราคาปัจจุบันเทียบ SL / TP1 ของทุกออเดอร์ที่ยัง 'running'
    - ถึง TP1 ก่อน SL -> win
    - ถึง SL ก่อน TP1 -> loss
    บันทึกกลับ kvdb.io เฉพาะตอนมีการเปลี่ยนสถานะ
    """
    orders = load_orders(bucket, symbol)
    changed = False

    for o in orders:
        if o["status"] != "running":
            continue

        tp1 = o["take_profits"].get("TP1")
        sl = o["stop_loss"]
        direction = o["direction"]

        if direction == "bullish":
            if tp1 is not None and current_price >= tp1:
                o["status"] = "win"
                changed = True
            elif current_price <= sl:
                o["status"] = "loss"
                changed = True
        else:  # bearish
            if tp1 is not None and current_price <= tp1:
                o["status"] = "win"
                changed = True
            elif current_price >= sl:
                o["status"] = "loss"
                changed = True

    if changed:
        if not save_orders(bucket, symbol, orders):
            print(f"[Order Tracking Error] บันทึกสถานะ win/loss ที่เปลี่ยนไป (symbol={symbol}) ลง kvdb "
                  f"ไม่สำเร็จ — ผลลัพธ์ที่เพิ่งเปลี่ยนอาจหายไปตอน process นี้ปิดตัว")

    return orders


def calc_stats(orders):
    """
    คำนวณ win rate / expectancy แยกตามแผน (plan1/2/3) จากออเดอร์ที่ปิดแล้วเท่านั้น (win/loss)
    ออเดอร์ที่ยัง 'running' ไม่นับในสถิติ (ผลยังไม่ออก)

    Expectancy คำนวณแบบง่าย (ต่อ 1R เสี่ยง): win_rate × avg_RR_ของฝั่ง win − loss_rate × 1
    (loss ถือว่าเสีย 1R เต็มเสมอ เพราะ SL คือจุดตัดขาดทุนที่กำหนดไว้แล้ว)
    ค่า RR ที่ใช้เป็น "RR ตามแผนตอนเปิดออเดอร์" (rr_tp1) ไม่ใช่ RR ที่ได้จริงเป๊ะๆ เพราะระบบยัง
    ไม่ track ราคาปิดละเอียด — ใช้เป็นตัวชี้วัดเบื้องต้นว่าแผนไหนน่าจะมี edge มากกว่ากัน ไม่ใช่ตัวเลขแม่นยำ 100%

    คืน dict: {plan_key: {"total_closed","wins","losses","win_rate","avg_rr_win","expectancy"}, ...}
    บวกกับ key พิเศษ "overall" ที่รวมทุกแผนเข้าด้วยกัน
    """
    by_plan = {}
    for o in orders:
        if o["status"] not in ("win", "loss"):
            continue
        plan = o.get("plan", "plan1_pullback")
        by_plan.setdefault(plan, []).append(o)

    def _summarize(closed_orders):
        total = len(closed_orders)
        if total == 0:
            return None
        wins = [o for o in closed_orders if o["status"] == "win"]
        losses = [o for o in closed_orders if o["status"] == "loss"]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total

        win_rrs = [o["rr_tp1"] for o in wins if o.get("rr_tp1") is not None]
        avg_rr_win = (sum(win_rrs) / len(win_rrs)) if win_rrs else None

        expectancy = None
        if avg_rr_win is not None:
            loss_rate = loss_count / total
            expectancy = round(win_rate * avg_rr_win - loss_rate * 1, 2)

        return {
            "total_closed": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_rate * 100, 1),
            "avg_rr_win": round(avg_rr_win, 2) if avg_rr_win is not None else None,
            "expectancy": expectancy,
        }

    stats = {}
    all_closed = []
    for plan, closed_orders in by_plan.items():
        summary = _summarize(closed_orders)
        if summary:
            stats[plan] = summary
        all_closed.extend(closed_orders)

    overall = _summarize(all_closed)
    if overall:
        stats["overall"] = overall

    return stats


def build_stats_message(symbol, stats):
    """สร้างข้อความสถิติ win rate/expectancy แยกตามแผน สำหรับคำสั่ง /stats"""
    if not stats:
        return f"📊 <b>สถิติผลลัพธ์: {symbol}</b>\n\nยังไม่มีออเดอร์ที่ปิดจบ (win/loss) ให้วัดผลเลยครับ"

    lines = [f"📊 <b>สถิติผลลัพธ์: {symbol}</b>", ""]

    plan_order = ["plan1_pullback", "plan1_pullback_early", "plan2_breakout",
                  "plan3_counter_trend", "plan4_daily_continuation"]
    for plan in plan_order:
        s = stats.get(plan)
        if not s:
            continue
        lines.append(f"<b>{PLAN_LABEL.get(plan, plan)}</b>")
        lines.append(f"  ปิดแล้ว: {s['total_closed']} ไม้ (Win {s['wins']} / Loss {s['losses']})")
        lines.append(f"  Win rate: {s['win_rate']}%")
        if s["avg_rr_win"] is not None:
            lines.append(f"  RR เฉลี่ยตอน Win: {s['avg_rr_win']}")
        if s["expectancy"] is not None:
            sign = "✅ เป็นบวก" if s["expectancy"] > 0 else "⚠️ ติดลบ"
            lines.append(f"  Expectancy: {s['expectancy']}R ({sign})")
        lines.append("")

    overall = stats.get("overall")
    if overall:
        lines.append("<b>รวมทุกแผน</b>")
        lines.append(f"  ปิดแล้ว: {overall['total_closed']} ไม้ (Win {overall['wins']} / Loss {overall['losses']})")
        lines.append(f"  Win rate: {overall['win_rate']}%")
        if overall["expectancy"] is not None:
            sign = "✅ เป็นบวก" if overall["expectancy"] > 0 else "⚠️ ติดลบ"
            lines.append(f"  Expectancy: {overall['expectancy']}R ({sign})")

    lines.append("")
    lines.append(
        "หมายเหตุ: Expectancy คำนวณจาก RR ตามแผนตอนเปิดออเดอร์ ไม่ใช่ราคาปิดจริงเป๊ะๆ "
        "ใช้เป็นตัวชี้วัดเบื้องต้นว่าแผนไหนน่าจะมี edge มากกว่ากัน "
        "เทียบ \"แผนที่ 1 (เข้าก่อนยืนยัน)\" กับ \"แผนที่ 1 (Pullback ยืนยันแล้ว)\" ได้ว่าการรอ "
        "5M Trigger ก่อนเข้าจริงช่วยเพิ่มความแม่นยำหรือไม่"
    )

    return "\n".join(lines)


def build_orders_dashboard(symbol, orders, current_price):
    """สร้างข้อความ Order Dashboard แยกจาก Dashboard หลัก"""
    if not orders:
        return f"📋 <b>Order Dashboard: {symbol}</b>\n\nยังไม่มีออเดอร์ที่ถูกส่ง"

    lines = [
        f"📋 <b>Order Dashboard: {symbol}</b>",
        f"ราคาปัจจุบัน: {current_price:.3f}",
        "",
    ]

    # โชว์ 10 รายการล่าสุด เรียงใหม่สุดขึ้นก่อน กันข้อความยาวเกิน
    for o in orders[-10:][::-1]:
        dir_th = "LONG" if o["direction"] == "bullish" else "SHORT"
        emoji = STATUS_EMOJI.get(o["status"], "❔")
        plan_tag = PLAN_SHORT.get(o.get("plan", "plan1_pullback"), "?")
        lines.append(
            f"{o['opened_at']} [P{plan_tag}] {o['symbol']} {o['entry_price']} {dir_th} "
            f"{emoji} {o['status']}"
        )

    running = sum(1 for o in orders if o["status"] == "running")
    wins = sum(1 for o in orders if o["status"] == "win")
    losses = sum(1 for o in orders if o["status"] == "loss")
    pending = sum(1 for o in orders if o["status"] == "pending")
    expired = sum(1 for o in orders if o["status"] == "expired")

    lines.append("")
    summary_parts = []
    if pending:
        summary_parts.append(f"รอราคาถึง ⏳: {pending}")
    summary_parts.append(f"กำลังรัน: {running}")
    summary_parts.append(f"Win ✅: {wins}")
    summary_parts.append(f"Loss ❌: {losses}")
    if expired:
        summary_parts.append(f"หมดอายุ ⌛: {expired}")
    lines.append(" | ".join(summary_parts))
    lines.append("พิมพ์ /stats เพื่อดู win rate/expectancy แยกตามแผน")

    return "\n".join(lines)
