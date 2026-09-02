import json
from datetime import datetime, timedelta, timezone

from kvstore import kv_get, kv_set
from tp import calc_risk_reward

ORDERS_KEY_PREFIX = "open_orders"


def _log_to_sheets(order, symbol):
    """เรียก sheets_log.py แบบกันเหนียวสุดขีด — ทั้ง import และเรียกฟังก์ชันห่อด้วย try/except ที่นี่
    อีกชั้น แม้ sheets_log.py เองจะไม่โยน exception ออกมาอยู่แล้วก็ตาม (defense in depth) เพราะจุดนี้
    คือ Signal Lifecycle หลักของ Strategy (add_order/add_pending_order/update_*) ต้องไม่มีทางถูกทำให้
    พังได้จากฟีเจอร์เสริมอย่าง Google Sheets Logging เด็ดขาด ไม่ว่าจะกรณีไหนก็ตาม (เช่น ยังไม่ได้ติดตั้ง
    gspread บนเครื่องที่รัน, ไฟล์ sheets_log.py หาย ฯลฯ)"""
    try:
        import sheets_log
        sheets_log.log_signal(order, symbol)
    except Exception as e:
        print(f"[Order Tracking] เรียก Sheets Log ไม่สำเร็จ (ไม่กระทบการทำงานหลัก): {e}")


# pending: Set & Forget วาง limit ไว้ล่วงหน้า ยังไม่ fill จริง (แผน 5-8) — ไม่นับ win/loss จนกว่าจะ
# เปลี่ยนเป็น running ก่อน (ราคามาถึง entry จริง) กันสถิติเพี้ยนจากออเดอร์ที่ไม่เคยเข้าไม้จริง
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
    "plan7_qm_pattern": "แผนที่ 7 (Quasimodo Pattern — Set & Forget)",
    "plan8_flag_pattern": "แผนที่ 8 (Flag Pattern — Set & Forget)",
}
PLAN_SHORT = {
    "plan1_pullback": "1",
    "plan1_pullback_early": "1e",
    "plan2_breakout": "2",
    "plan3_counter_trend": "3",
    "plan4_daily_continuation": "4",
    "plan5_zone_single": "5",
    "plan6_sweep_general": "6",
    "plan7_qm_pattern": "7",
    "plan8_flag_pattern": "8",
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
    บันทึกลิสต์ออเดอร์ลง Upstash Redis คืนค่า True/False ตามผลจริง (ไม่ใช่แค่ยิง request ไปเฉยๆ)

    ไม่ retry เองที่นี่แล้ว — kv_set() ใน kvstore.py มี retry-with-backoff ในตัวอยู่แล้ว (3 ครั้ง,
    เว้น 1 วิ แล้ว 2 วิ) retry ซ้ำสองชั้นแบบเดิม (sleep(1) แล้วเรียก kv_set ซ้ำ ซึ่งข้างในมี retry
    ของตัวเองอยู่แล้ว) ทำให้แต่ละคำสั่ง Set & Forget (เช่น /order5, /order6) ที่มีการเรียก
    save_orders หลายครั้งต่อคำสั่ง (dedup check + บันทึกจริง) ช้าสะสมได้ถึงเกือบ 1-2 นาที
    ตามที่ผู้ใช้แจ้งมา — จุดนี้คือสาเหตุหลัก
    """
    key = f"{ORDERS_KEY_PREFIX}_{symbol}"
    payload = json.dumps(orders)
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
              f"แม้ retry แล้ว — ออเดอร์นี้จะไม่ถูกนับในสถิติที่บันทึกไว้")
        return None
    _log_to_sheets(order, symbol)
    return order


def add_pending_order(bucket, symbol, direction, entry_price, stop_loss, take_profits, score,
                       plan, current_price, expires_in_hours=8, existing_orders=None):
    """
    บันทึกออเดอร์แบบ 'pending' (Set & Forget — แผน 5-8) — วาง Limit/Stop Order ไว้ล่วงหน้าตอนเจอ
    zone/pattern ทันที ก่อนที่ราคาจะเดินทางมาถึงจริง ต่างจาก add_order() (แผน 1-4 เดิม) ที่บันทึกเป็น
    'running' ทันทีเพราะรอราคาแตะ + มี reaction ยืนยันมาก่อนแล้วถึงแจ้งเตือน (ถือว่าเข้าไม้จริงตั้งแต่แจ้ง)

    วงจรสถานะของออเดอร์แบบนี้: pending -> running (พอราคามาถึง entry จริง ผ่าน update_pending_orders())
    -> win/loss (เหมือนเดิม ผ่าน update_orders_status()) หรือ pending -> expired (ราคาไม่มาถึงภายใน
    expires_in_hours ชม. — ถือว่าพลาดโอกาส ไม่นับ win/loss เพราะไม่เคยเข้าไม้จริง)

    current_price: ราคาตอนที่สร้างออเดอร์นี้ — ใช้คำนวณ 'entry_side' (entry อยู่ต่ำ/สูงกว่าราคาตอนนี้)
    เก็บไว้ตัดสินทิศทางการเช็ค fill ใน update_pending_orders() แทนการอนุมานจาก direction (bullish/
    bearish) เพราะ Limit กับ Stop มีทิศทางการ fill ตรงข้ามกันแม้ direction เดียวกัน:
      - Buy Limit (bullish, entry ต่ำกว่าราคาปัจจุบัน — รอย่อลงมาเข้า แบบกลุ่ม A/C/D) fill เมื่อราคาลง
      - Buy Stop (bullish, entry สูงกว่าราคาปัจจุบัน — รอทะลุขึ้นไปเข้า แบบ breakout กลุ่ม B) fill เมื่อราคาขึ้น
    ถ้าใช้ direction เป็นตัวตัดสินอย่างเดียว (โค้ดเดิมก่อนแก้) จะเช็ค fill ผิดทิศทางสำหรับ Stop order
    ทันที (กลุ่ม A/C/D ไม่มีปัญหานี้เพราะเป็น Limit ล้วนบังเอิญ entry_side ตรงกับ direction เป๊ะ)

    expires_in_hours: ปรับได้ตาม timeframe ของแต่ละแผนย่อยที่มาเรียกใช้ (เช่น zone จาก 4H บริบทSo
    ควรอยู่ได้นานกว่า pattern จาก 15M) ค่า default 8 ชม.

    existing_orders: ถ้าผู้เรียกโหลด orders list มาแล้ว (เช่น เพิ่งเช็ค dedup ผ่าน load_orders() ไป
    ก่อนหน้า) ส่งเข้ามาตรงนี้เพื่อไม่ต้องยิง kv_get ซ้ำอีกรอบ — กันการโหลดซ้ำที่ทำให้แต่ละคำสั่ง
    Set & Forget ช้าสะสม (dedup check + save เดิมโหลด orders 2 รอบแยกกัน ตอนนี้เหลือรอบเดียว)
    """
    orders = existing_orders if existing_orders is not None else load_orders(bucket, symbol)
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
        "entry_side": "below" if entry_price <= current_price else "above",
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
              f"ไม่สำเร็จ แม้ retry แล้ว — ออเดอร์นี้จะไม่ถูกนับในสถิติที่บันทึกไว้")
        return None
    _log_to_sheets(order, symbol)
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

    เช็ค fill จาก 'entry_side' (entry อยู่ต่ำ/สูงกว่าราคาตอนสร้างออเดอร์) ไม่ใช่ direction — กัน
    เช็คผิดทิศทางสำหรับ Stop order (เช่น breakout pattern กลุ่ม B ที่ entry อยู่สูงกว่าราคาปัจจุบัน
    รอราคาขึ้นไปทะลุ ต่างจาก Limit ของกลุ่ม A/C/D ที่ entry อยู่ต่ำกว่า รอราคาย่อลงมา)
    ออเดอร์เก่าที่ไม่มี entry_side (สร้างก่อนแก้จุดนี้) จะ fallback ไปใช้ direction แบบเดิม (สมมติเป็น
    Limit เสมอ ตรงกับพฤติกรรมเดิมของกลุ่ม A/C/D ที่ผ่านมา ไม่กระทบออเดอร์ที่มีอยู่แล้ว)

    spread_buffer: ราคาที่บอทเช็คมาจาก TwelveData (ราคากลาง) ไม่ใช่ bid/ask ของโบรกที่คุณเทรดจริง
    ซึ่งมี spread คั่นอยู่ — ต้องให้ราคาเลยจุด Entry ไปอีก spread_buffer ก่อนถึงจะถือว่า fill จริง
    กันเคสระบบบอกว่า "เข้าแล้ว" ทั้งที่โบรกจริงยังไม่ทันได้ fill ให้ (ตามที่ผู้ใช้ฟีดแบ็คมา)
    ใช้เฉพาะจุดนี้จุดเดียว — ไม่กระทบการเช็ค TP/SL ใน update_orders_status() ซึ่งยังใช้ราคาตรงเป๊ะเหมือนเดิม
    ค่า default 0.0 (ไม่มีผล) กันโค้ดเก่าที่เรียกไม่ครบ 4 อาร์กิวเมนต์พัง
    """
    orders = load_orders(bucket, symbol)
    changed = False
    changed_orders = []
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
                    changed_orders.append(o)
                    continue
            except Exception:
                pass  # parse ไม่ได้ (ข้อมูลเก่า/เพี้ยน) ถือว่ายังไม่หมดอายุ ปล่อยให้เช็ค fill ต่อไป

        entry_price = o["entry_price"]
        entry_side = o.get("entry_side")
        if entry_side is None:
            # ออเดอร์เก่าก่อนแก้จุดนี้ — fallback ตาม direction แบบเดิม (Limit เสมอ)
            entry_side = "below" if o["direction"] == "bullish" else "above"

        filled = (
            (entry_side == "below" and current_price <= entry_price - spread_buffer) or
            (entry_side == "above" and current_price >= entry_price + spread_buffer)
        )
        if filled:
            o["status"] = "running"
            o["filled_at"] = now.strftime("%H:%M")
            changed = True
            changed_orders.append(o)

    if changed:
        if not save_orders(bucket, symbol, orders):
            print(f"[Order Tracking Error] บันทึกสถานะ pending->running/expired (symbol={symbol}) "
                  f"ลง kvdb ไม่สำเร็จ — ผลลัพธ์ที่เพิ่งเปลี่ยนอาจหายไปตอน process นี้ปิดตัว")
        for o in changed_orders:
            _log_to_sheets(o, symbol)

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
    changed_orders = []

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
                changed_orders.append(o)
            elif current_price <= sl:
                o["status"] = "loss"
                changed = True
                changed_orders.append(o)
        else:  # bearish
            if tp1 is not None and current_price <= tp1:
                o["status"] = "win"
                changed = True
                changed_orders.append(o)
            elif current_price >= sl:
                o["status"] = "loss"
                changed = True
                changed_orders.append(o)

    if changed:
        if not save_orders(bucket, symbol, orders):
            print(f"[Order Tracking Error] บันทึกสถานะ win/loss ที่เปลี่ยนไป (symbol={symbol}) ลง kvdb "
                  f"ไม่สำเร็จ — ผลลัพธ์ที่เพิ่งเปลี่ยนอาจหายไปตอน process นี้ปิดตัว")
        for o in changed_orders:
            _log_to_sheets(o, symbol)

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
    """สร้างข้อความสถิติ win rate/expectancy แยกตามแผน — หมายเหตุ: ไม่มีคำสั่ง Telegram เรียกใช้
    ฟังก์ชันนี้แล้วตอนนี้ (/stats ถูกถอดออกแล้ว) เก็บไว้เผื่อ Data Layer ในอนาคตเรียกใช้ซ้ำได้"""
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

    return "\n".join(lines)
