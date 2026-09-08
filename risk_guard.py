"""
risk_guard.py — เบรกความเสี่ยงระดับพอร์ต (Portfolio Risk Control) ต่างจาก score.py/zones.py ที่กรอง
สัญญาณ "ต่อไม้" — ตัวนี้ดูภาพรวม "วันนี้/ตอนนี้เป็นยังไง" ก่อนจะยอมส่ง Telegram Alert ของไม้ใหม่ออกไป
เลย เพราะสถิติ (Win rate/Expectancy) ต่อแผนดีแค่ไหนก็ไม่มีประโยชน์ ถ้าคืนนึงเจอ Losing Streak ต่อกัน
จนบัญชีเสียหายหนักไปแล้ว — โดยเฉพาะทุนเริ่มต้นเล็ก (เช่น $100 ตามที่ผู้ใช้ตั้งผ่าน /setbalance) ที่เสี่ยง
เจ๊งเร็วกว่าทุนใหญ่มากถ้าไม่มีเบรกตรงนี้

อ้างอิงแนวคิดจากสเปกที่เขียนไว้ล่วงหน้าใน claude/auto_execute_risk_guard_spec.md (ยังไม่เคยถูก build
จริง — สเปกนั้นออกแบบไว้สำหรับตอนมี Broker execution จริงผ่าน MetaApi.cloud) แต่ปรับจากดอลลาร์เป็น
"R" (1R = ความเสี่ยงต่อไม้ที่ตั้งไว้ตอนเข้า) แทน เพราะ:
  1. ระบบยังไม่มี Broker execution จริง (ดู auto_execute_risk_guard_spec.md) เลยไม่มีทางรู้ยอดเงินจริง
     ในบัญชีตลอดเวลา มีแต่ทุนตั้งต้นที่ผู้ใช้กรอกเอง (account.py) กับผล win/loss ที่ orders.py บันทึกไว้
  2. ไม่ต้องจำลอง compounding balance ทีละไม้ (ซับซ้อนเกินจำเป็นตอนนี้ และผิดพลาดง่ายถ้า position
     size จริงที่ผู้ใช้เปิดในโบรกไม่ตรงกับที่ระบบคำนวณเป๊ะๆ) — นับจาก orders.py ที่มีอยู่แล้วตรงๆ พอ

3 เกณฑ์ที่เช็ค (ทุกอันปรับ/ปิดได้ผ่าน config, ดู get_symbol_config/CONFIG ใน config.py):
  - Daily Loss Stop (risk_guard_max_daily_loss_r, default 3.0R): วันนี้ (เวลาไทย 00:00 เป็นต้นไป)
    ขาดทุนรวมกี่ R แล้ว (นับเฉพาะออเดอร์ status='loss' ที่ปิดวันนี้ — loss ถือว่าเสียเต็ม 1R เสมอ
    เพราะ SL คือจุดตัดขาดทุนที่กำหนดไว้แล้ว) ถ้าเกินเพดาน -> ห้ามส่ง Alert ใหม่จนกว่าจะข้ามวัน
  - Consecutive Loss Stop (risk_guard_max_consecutive_losses, default 4): ออเดอร์ที่ปิดจบล่าสุด
    (ทุกแผนรวมกัน ไม่แยกแผน เพราะเป็นเบรกระดับพอร์ตรวม ไม่ใช่ต่อแผน) กี่ไม้ติดกันเป็น loss รวด
    ถ้าเกินเพดาน -> ห้ามส่ง Alert ใหม่ จนกว่าจะมีไม้ไหน Win มาคั่น
  - Max Concurrent Positions (risk_guard_max_concurrent_positions, default 3): ไม้ที่ status='running'
    อยู่ตอนนี้ (ทุกแผนรวมกัน) เกินเพดานไหม -> ห้ามส่ง Alert ของไม้ "ใหม่" เพิ่ม (ไม้ที่ running อยู่แล้ว
    ไม่กระทบ เดินต่อตามปกติจนกว่าจะถึง TP/SL)

⚠️ ทั้งหมดนี้คือ "ห้ามส่ง Telegram Alert เพิ่ม" เท่านั้น (advisory/gate) — ไม่มีการยกเลิกออเดอร์ที่ running
อยู่แล้ว ไม่มีการปิดไม้แทนผู้ใช้ เพราะระบบยังไม่มีการเชื่อมต่อ Broker จริงเลย การบันทึกลง Order Dashboard
(add_order/add_pending_order) ยังทำงานตามปกติเสมอ ไม่ถูกกระทบ (เหมือน push_notifications_enabled)
เพื่อให้สถิติ win rate/expectancy ของกลยุทธ์เองยังวัดผลได้ต่อเนื่อง ไม่ขาดช่วงตอน Risk Guard active
"""

from datetime import datetime, timezone, timedelta

from orders import load_orders

THAI_TZ = timezone(timedelta(hours=7))


def _today_th_str():
    return datetime.now(THAI_TZ).strftime("%Y-%m-%d")


def _order_close_date_th(order):
    """*** แก้ไขล่าสุด (8 ก.ย. 2026): ใช้วันที่ 'ปิดไม้จริง' แทนวันที่ 'สร้างไม้' + เปลี่ยนพฤติกรรม
    fallback เมื่อไม่รู้วันที่แน่ชัด ***

    บั๊กที่เจอ (ผู้ใช้รายงานผ่าน /status เห็น "ขาดทุนวันนี้ 27R" ทั้งที่เพดานคือ 3R): เดิมฟังก์ชันนี้ใช้
    created_at_iso (วันที่ *สร้าง* ออเดอร์) มาเทียบว่าเป็น "วันนี้" ไหม แล้วถ้าไม่มีฟิลด์นี้เลย (ออเดอร์
    เก่าที่ปิดไปนานแล้ว สร้างก่อนรอบอัปเดตที่เพิ่งเพิ่ม created_at_iso ให้ครบทุกแผน) จะถือว่า "ไม่รู้วันที่
    แน่ชัด นับรวมไปก่อน" — ผลคือไม้ขาดทุนเก่าที่ไม่มีวันที่ถูกนับเป็น "ขาดทุนวันนี้" ตลอดไปไม่มีวันหมด
    อายุ สะสมเพิ่มขึ้นเรื่อยๆ (ไม่มีทางลดลง) จนเพดาน 3R ถูกทะลุค้างอยู่ตลอดกาล ถ้าเปิด push notifications
    กลับมาเมื่อไหร่ Risk Guard จะบล็อกทุกสัญญาณใหม่ทันทีโดยไม่มีทางกลับมาปกติเองเลย

    ตอนนี้แก้ 2 จุด:
      1. ใช้ closed_at_iso (เวลาที่ปิดไม้จริง — orders.py เพิ่งเพิ่มให้ set ทุกครั้งที่ปิดออเดอร์ win/loss
         แล้ว) เป็นหลักก่อนเสมอ ถูกต้องกว่า created_at_iso เพราะ Daily Loss Stop ควรนับ "ขาดทุนที่เกิด
         วันนี้" ไม่ใช่ "ไม้ที่เปิดวันนี้" (ไม้เปิดเมื่อวาน ปิดขาดทุนวันนี้ ต้องนับเป็นของวันนี้)
         ออเดอร์ที่ปิดตั้งแต่รอบอัปเดตนี้เป็นต้นไปจะมีฟิลด์นี้ครบทุกไม้เสมอ ไม่ต้อง fallback อีกต่อไป
      2. ถ้าไม่มีทั้ง closed_at_iso และ created_at_iso เลย (ออเดอร์เก่าก่อนรอบอัปเดตนี้) คืน None แล้ว
         ผู้เรียกจะ "ไม่นับ" แทนที่จะนับรวมไปก่อนแบบเดิม — เพราะออเดอร์เหล่านี้ปิดไปนานแล้วแน่นอน (ไม่ใช่
         ของวันนี้จริงๆ) การไม่นับจึงถูกต้องกว่าการนับค้างตลอดไปแบบเดิม"""
    iso = order.get("closed_at_iso") or order.get("created_at_iso")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone(THAI_TZ).strftime("%Y-%m-%d")
    except Exception:
        return None


def get_daily_stats(bucket, symbol):
    """สรุปสถิติ 'วันนี้' (เวลาไทย) จากออเดอร์ทั้งหมดของ symbol นี้
    คืน dict {loss_r_today, running_count, consecutive_losses}"""
    orders = load_orders(bucket, symbol)
    today = _today_th_str()

    loss_r_today = 0.0
    for o in orders:
        if o.get("status") != "loss":
            continue
        # *** แก้ไขล่าสุด (8 ก.ย. 2026): ไม่รู้วันที่แน่ชัด (ออเดอร์เก่าไม่มี closed_at_iso/
        # created_at_iso เลย) -> ไม่นับ แทนที่จะนับรวมไปก่อนแบบเดิม (ดู _order_close_date_th
        # ด้านบนสำหรับเหตุผลเต็ม — ของเดิมทำให้ขาดทุนเก่าสะสมค้างเป็น "วันนี้" ตลอดกาล)
        order_date = _order_close_date_th(o)
        if order_date != today:
            continue
        loss_r_today += 1.0  # loss ถือว่าเสียเต็ม 1R เสมอ (SL คือจุดตัดขาดทุนที่กำหนดไว้แล้ว)

    running_count = sum(1 for o in orders if o.get("status") == "running")

    # ไล่จากออเดอร์ที่ปิดจบล่าสุดย้อนหลัง (win/loss เท่านั้น) — orders.py append ต่อท้ายลิสต์เสมอ
    # ลำดับในลิสต์ = ลำดับเวลาที่ปิดจบจริง เลยไล่ reversed() ได้ตรงๆ ไม่ต้อง sort เพิ่ม
    closed = [o for o in orders if o.get("status") in ("win", "loss")]
    consecutive_losses = 0
    for o in reversed(closed):
        if o["status"] == "loss":
            consecutive_losses += 1
        else:
            break

    return {
        "loss_r_today": loss_r_today,
        "running_count": running_count,
        "consecutive_losses": consecutive_losses,
    }


def can_open_new_trade(bucket, symbol, config):
    """เช็คว่าตอนนี้ยังเปิดไม้ใหม่ได้ไหม (เรียกก่อนส่ง Telegram Alert ทุกครั้ง)
    คืน (allowed: bool, reason: str|None) — reason เป็น None ถ้า allowed=True
    ปิด Risk Guard ทั้งหมดได้ด้วย config['risk_guard_enabled']=False (default True — เปิดไว้เป็น
    ค่าเริ่มต้น เพราะทุนเริ่มต้นเล็กตามที่ผู้ใช้ตั้ง ($100) ยิ่งต้องระวัง)"""
    if not config.get("risk_guard_enabled", True):
        return True, None

    try:
        stats = get_daily_stats(bucket, symbol)
    except Exception as e:
        print(f"[Risk Guard Error] เช็คสถิติไม่สำเร็จ ({symbol}) — ปล่อยผ่านไปก่อน กันไม่ให้ error "
              f"ที่นี่บล็อกทั้งระบบ: {e}")
        return True, None

    max_daily_loss_r = config.get("risk_guard_max_daily_loss_r", 3.0)
    if stats["loss_r_today"] >= max_daily_loss_r:
        return False, (
            f"🛑 <b>Daily Loss Stop — {symbol}</b>\n"
            f"วันนี้ขาดทุนรวมแล้ว {stats['loss_r_today']:.0f}R (เพดาน {max_daily_loss_r:.0f}R) — "
            f"ระงับ Alert ไม้ใหม่ชั่วคราว จนกว่าจะข้ามวัน (เที่ยงคืนเวลาไทย)"
        )

    max_consecutive = config.get("risk_guard_max_consecutive_losses", 4)
    if stats["consecutive_losses"] >= max_consecutive:
        return False, (
            f"🛑 <b>Consecutive Loss Stop — {symbol}</b>\n"
            f"เสีย {stats['consecutive_losses']} ไม้ติดกันแล้ว (เพดาน {max_consecutive} ไม้) — "
            f"ระงับ Alert ไม้ใหม่ พักมือทบทวนก่อน (จะกลับมาแจ้งเตือนปกติทันทีที่มีไม้ไหน Win)"
        )

    max_concurrent = config.get("risk_guard_max_concurrent_positions", 3)
    if stats["running_count"] >= max_concurrent:
        return False, (
            f"🛑 <b>Max Concurrent Positions — {symbol}</b>\n"
            f"มีไม้ที่กำลังรันอยู่ {stats['running_count']} ไม้แล้ว (เพดาน {max_concurrent} ไม้) — "
            f"ระงับ Alert ไม้ใหม่ จนกว่าจะมีไม้ไหนปิดจบก่อน"
        )

    return True, None


def check_and_notify(config, symbol):
    """เรียกครั้งเดียวต่อรอบ cron (ก่อนเช็คแผนทั้งหมด) — แจ้งเตือนผู้ใช้ผ่าน Telegram ครั้งเดียวตอน
    Risk Guard เพิ่ง "เปลี่ยนสถานะ" เท่านั้น (ปกติ -> ถูกระงับ หรือ ถูกระงับ -> กลับมาปกติ) ไม่แจ้งซ้ำ
    ทุกรอบขณะสถานะเดิมยังคงอยู่ (กันสแปม Telegram ทุก 5 นาที) ใช้ state เก็บใน kvdb แบบเดียวกับ
    dedup pattern ใน plan_runner.py

    คืน (allowed, reason) เหมือน can_open_new_trade() — ผู้เรียก (main.py) เอาไปกำหนดว่าจะยอมส่ง
    Alert ของ Plan 1 (ซึ่งไม่ได้ผ่าน alert_dispatcher.py) รอบนี้ไหม — ส่วน Plan 2-8 เช็คกันเองอีกชั้น
    ที่ alert_dispatcher.send_alert_to_targets() (ดูไฟล์นั้น) อยู่แล้ว ไม่ต้องพึ่งค่าที่คืนจากตรงนี้
    """
    from alert_dispatcher import send_alert_to_targets
    from kvstore import kv_get, kv_set

    bucket = config["kvdb_bucket"]
    state_key = f"risk_guard_state_{symbol}"
    allowed, reason = can_open_new_trade(bucket, symbol, config)
    prev_state = kv_get(bucket, state_key)

    if not allowed:
        if prev_state != "blocked":
            send_alert_to_targets(
                config,
                f"{reason}\n\n"
                "(หมายเหตุ: ไม้ที่กำลังรันอยู่แล้วไม่กระทบ ยังเดินต่อตามปกติจนถึง TP/SL — "
                "นี่แค่ระงับ Alert ของไม้ใหม่เท่านั้น)"
            )
            kv_set(bucket, state_key, "blocked")
    else:
        if prev_state == "blocked":
            send_alert_to_targets(
                config, f"✅ <b>Risk Guard กลับมาปกติแล้ว — {symbol}</b>\nเริ่มแจ้งเตือนไม้ใหม่ตามปกติครับ"
            )
            kv_set(bucket, state_key, "")

    return allowed, reason
