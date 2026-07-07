import json
from datetime import datetime, timezone

from kvstore import kv_get, kv_set

ORDERS_KEY_PREFIX = "open_orders"
STATUS_EMOJI = {"running": "💸", "win": "✅", "loss": "❌"}


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


def add_order(bucket, symbol, direction, entry_price, stop_loss, take_profits, score):
    """บันทึกออเดอร์ใหม่ตอนที่ Alert ถูกส่งจริง"""
    orders = load_orders(bucket, symbol)
    order = {
        "id": f"{symbol}_{int(datetime.now(timezone.utc).timestamp())}",
        "symbol": symbol,
        "direction": direction,  # "bullish" หรือ "bearish"
        "entry_price": round(float(entry_price), 3),
        "stop_loss": round(float(stop_loss), 3),
        "take_profits": {k: round(float(v), 3) for k, v in take_profits.items()},
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
        lines.append(
            f"{o['opened_at']} order {o['symbol']} {o['entry_price']} {dir_th} "
            f"run{emoji} {o['status']}"
        )

    running = sum(1 for o in orders if o["status"] == "running")
    wins = sum(1 for o in orders if o["status"] == "win")
    losses = sum(1 for o in orders if o["status"] == "loss")

    lines.append("")
    lines.append(f"กำลังรัน: {running} | Win ✅: {wins} | Loss ❌: {losses}")

    return "\n".join(lines)
