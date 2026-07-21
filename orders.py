import json
from datetime import datetime, timezone

from kvstore import kv_get, kv_set
from tp import calc_risk_reward

ORDERS_KEY_PREFIX = "open_orders"
STATUS_EMOJI = {"running": "💸", "win": "✅", "loss": "❌"}
PLAN_LABEL = {
    "plan1_pullback": "แผนที่ 1 (Pullback)",
    "plan2_breakout": "แผนที่ 2 (Breakout)",
    "plan3_counter_trend": "แผนที่ 3 (สวนเทรนด์)",
}
PLAN_SHORT = {
    "plan1_pullback": "1",
    "plan2_breakout": "2",
    "plan3_counter_trend": "3",
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
    kv_set(bucket, f"{ORDERS_KEY_PREFIX}_{symbol}", json.dumps(orders))


def add_order(bucket, symbol, direction, entry_price, stop_loss, take_profits, score, plan="plan1_pullback"):
    """
    บันทึกออเดอร์ใหม่ตอนที่ Alert ถูกส่งจริง (ไม่ว่าจะเป็นแผนที่ 1/2/3)

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
        "id": f"{symbol}_{int(datetime.now(timezone.utc).timestamp())}",
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
    save_orders(bucket, symbol, orders)
    return order


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
        save_orders(bucket, symbol, orders)

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

    plan_order = ["plan1_pullback", "plan2_breakout", "plan3_counter_trend"]
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
        "ใช้เป็นตัวชี้วัดเบื้องต้นว่าแผนไหนน่าจะมี edge มากกว่ากัน"
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
            f"run{emoji} {o['status']}"
        )

    running = sum(1 for o in orders if o["status"] == "running")
    wins = sum(1 for o in orders if o["status"] == "win")
    losses = sum(1 for o in orders if o["status"] == "loss")

    lines.append("")
    lines.append(f"กำลังรัน: {running} | Win ✅: {wins} | Loss ❌: {losses}")
    lines.append("พิมพ์ /stats เพื่อดู win rate/expectancy แยกตามแผน")

    return "\n".join(lines)
