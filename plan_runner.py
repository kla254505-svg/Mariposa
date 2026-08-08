"""
plan_runner.py — เช็คเงื่อนไข trigger ของ Plan 2/3/4 (Breakout / สวนเทรนด์ / Daily Continuation)
ทุกรอบที่บอทรัน (ย้ายออกมาจาก if __name__ block เดิมใน main.py ตามแผน) ส่ง Telegram/บันทึก order
ผ่าน alert_dispatcher.py ทั้งหมด ไม่ทำเอง (กันโค้ดซ้ำ)

หมายเหตุขอบเขต: Plan 1 (run_pipeline) ยังอยู่ใน main.py เหมือนเดิม ไม่ได้ย้ายมาที่นี่ เพราะ Plan 1
มี logic คำนวณ Score/5M Trigger/SL tightening/Pending-order notice ที่ผูกกับลำดับการคำนวณแน่นมาก
(SL ที่ tighten แล้วต้องคำนวณ TP/RR/position size ใหม่ต่อ ก่อนจะรู้ว่าจะส่ง Alert ไหม) การแยก
detection ออกจาก dispatch ตรงนั้นเสี่ยงเกินไปที่จะทำในรอบเดียวกับ Plan 2-4 ที่โครงสร้างง่ายกว่ามาก
(detect -> คำนวณ order เดียว -> ส่ง/บันทึก ไม่มีการคำนวณซ้อนกันหลายชั้นแบบ Plan 1)
"""
from kvstore import kv_get, kv_set
from news_scheduler import is_in_news_blackout
from scenario import (
    detect_breakout_trigger, detect_counter_trend_trigger,
    calc_breakout_order, calc_counter_trend_order,
    get_daily_bias_and_range, detect_plan4_signal, calc_plan4_order,
)
from alert_dispatcher import send_alert_to_targets, save_plan_order
from orders import load_orders, add_pending_order


def check_plan2_plan3_triggers(df, config, symbol):
    """
    Plan 2/3 จาก Hourly Briefing (Breakout / สวนเทรนด์): เช็คทุกรอบว่า "เข้าออเดอร์จริง" หรือยัง
    ไม่ใช่แค่ข้อความในบรีฟฟิ่งเฉยๆ แล้ว ถ้าทริกเกอร์จริงจะยิง Telegram (แชทเดิม + กลุ่ม) พร้อมหมายเหตุ
    และบันทึกลง Order Dashboard ด้วย เพื่อให้ /stats วัด win rate/expectancy แยกรายแผนได้ครบ ใช้
    calc_breakout_order/calc_counter_trend_order จาก scenario.py (จุดเดียวกับที่ /order ใน
    telegram_bot.py ใช้แสดงผล กันตรรกะคำนวณ SL/TP ซ้ำซ้อนสองที่)

    กันสแปมแบบ "state-based" แทน cooldown ตามเวลา: เดิมใช้ is_in_cooldown/mark_alert_sent (ตามนาที)
    แต่พบว่าพอเวลาผ่านไปเกิน cooldown มันเตือนซ้ำระดับ Breakout เดิมที่ยังไม่มีสวิงใหม่เกิดขึ้นจริง
    (โมเมนตัมจบไปแล้ว แค่ยังไม่มีสวิงไฮ/โลว์ใหม่ให้ระบบอ้างอิง) ตอนนี้เปลี่ยนมา dedup ตาม "เงื่อนไขจริง"
    แทนเวลา: Plan 2 จะแจ้งซ้ำก็ต่อเมื่อสวิงไฮ/โลว์ที่ทะลุเปลี่ยนเป็นระดับใหม่เท่านั้น, Plan 3 จะเงียบไปจนกว่า
    checklist จะหลุด (ไม่ครบ 3/3) แล้วกลับมาครบใหม่อีกครั้ง (rising-edge) ไม่ใช่แจ้งซ้ำทุกช่วงเวลาที่ตั้งไว้
    """
    from indicator import add_indicators
    from trend import analyze_structure

    bucket = config["kvdb_bucket"]
    try:
        df_ind_plan = add_indicators(df, config)
        structure_plan = analyze_structure(df_ind_plan, config)

        plan_triggers = [
            ("plan2_breakout", "Breakout (แผนที่ 2)",
             detect_breakout_trigger(df_ind_plan, structure_plan, config),
             "ราคาทะลุระดับ {level:.4f} แรงๆ ที่ราคา {price:.4f}"),
            ("plan3_counter_trend", "สวนเทรนด์ (แผนที่ 3)",
             detect_counter_trend_trigger(df_ind_plan, structure_plan),
             "Checklist สวนเทรนด์ผ่านครบ 3/3 ข้อแล้ว"),
        ]

        # เช็คครั้งเดียวก่อนเข้าลูป ใช้ร่วมกันทั้ง Plan 2/3 (ข่าวเดียวกัน ไม่ต้องเช็คซ้ำต่อแผน)
        plan_blackout, plan_blackout_event = is_in_news_blackout(bucket, symbol)

        for plan_key, plan_label, trigger, detail_template in plan_triggers:
            state_key = f"plan_state_{symbol}_{plan_key}"

            if not trigger:
                # เงื่อนไขไม่ตรงแล้วในรอบนี้ (breakout ยังไม่มีสวิงใหม่ / checklist หลุดจาก 3/3)
                # เคลียร์ state ทิ้ง รอบหน้าถ้ากลับมาเป็นจริงใหม่จะได้แจ้งเตือนสดอีกครั้ง (ไม่ใช่ของค้าง)
                kv_set(bucket, state_key, "")
                continue

            if plan_blackout:
                # อยู่ในช่วงห้ามเทรดรอบข่าว -> ข้ามไปเงียบๆ ไม่ mark state (กันไม่ให้พอข่าวผ่านไปแล้ว
                # เงื่อนไขเดิมยังจริงอยู่ แต่ถูก dedup ทิ้งเพราะเข้าใจผิดว่าเคยแจ้งไปแล้วตอนที่จริงแค่ถูกระงับ)
                continue

            if plan_key == "plan2_breakout":
                # dedup ตาม "ระดับที่ทะลุ" ไม่ใช่เวลา — แจ้งซ้ำก็ต่อเมื่อมีสวิงไฮ/โลว์ใหม่จริงๆ เท่านั้น
                dedup_value = f"{trigger['direction']}:{trigger['level']:.4f}"
            else:
                # plan3: ตราบใด trigger ไม่ None แปลว่า checklist ครบ 3/3 อยู่แล้วเสมอ (เงื่อนไขตายตัว)
                # dedup แค่ทิศทาง เพื่อกันไม่ให้แจ้งซ้ำขณะเงื่อนไขยังเป็นจริงต่อเนื่องรอบต่อรอบ
                dedup_value = trigger["direction"]

            prev_value = kv_get(bucket, state_key)
            if prev_value == dedup_value:
                continue  # เงื่อนไขเดิมที่เคยแจ้งไปแล้ว ไม่แจ้งซ้ำ

            direction_th = "LONG (ซื้อ)" if trigger["direction"] == "bullish" else "SHORT (ขาย)"
            detail = detail_template.format(**trigger) if "{" in detail_template else detail_template
            plan_msg = (
                f"🚨 <b>ออเดอร์เข้า — {plan_label}</b>\n"
                f"Symbol: {symbol} | ทิศทาง: {direction_th}\n"
                f"{detail}\n\n"
                "หมายเหตุ: สัญญาณนี้มาจาก Plan เสริมใน Hourly Briefing ไม่ใช่ระบบ Scoring หลัก "
                "ไม่ได้ผ่านฟิลเตอร์ 4H Bias/1H Trend/Session เหมือนสัญญาณเข้าเทรดปกติ (Plan 1) "
                "ควรพิจารณาความเสี่ยงเพิ่มเติมเอง หรือลดขนาดไม้ก่อนเข้า"
            )

            # --- คำนวณ SL/TP ของแผนนี้แล้วบันทึกลง Order Dashboard (ให้ /stats วัดผลได้) ---
            # ใช้ calc_breakout_order/calc_counter_trend_order จาก scenario.py จุดเดียวกับที่
            # telegram_bot.py ใช้แสดงผลใน /order — ถ้าคำนวณไม่สำเร็จ (หา swing/ATR ไม่ได้) จะข้าม
            # การบันทึกออเดอร์ไปเงียบๆ แต่ยังคงส่ง Telegram alert ตามปกติ (ไม่ให้ alert หายเพราะ
            # แค่บันทึกสถิติพลาด)
            try:
                if plan_key == "plan2_breakout":
                    calc_order = calc_breakout_order(trigger, structure_plan, df_ind_plan, config)
                else:
                    calc_order = calc_counter_trend_order(trigger, df_ind_plan, config)

                if calc_order:
                    save_plan_order(config, symbol, calc_order["direction"], calc_order["entry_price"],
                                     calc_order["stop_loss"], {"TP1": calc_order["take_profit"]},
                                     score=None, plan_key=plan_key)
                    plan_msg += (
                        f"\n\nEntry: {calc_order['entry_price']:.4f} | SL: {calc_order['stop_loss']:.4f} | "
                        f"TP: {calc_order['take_profit']:.4f} (RR {calc_order['rr']})"
                    )
            except Exception as e:
                print(f"[Plan {plan_key} Order Tracking Error] {e}")

            send_alert_to_targets(config, plan_msg)

            kv_set(bucket, state_key, dedup_value)
    except Exception as e:
        print(f"[Plan 2/3 Trigger Error] {symbol}: {e}")


def get_cached_daily_range(bucket, symbol):
    """
    แผนที่ 4 ใช้ Daily range ของ "วันก่อนหน้า" ซึ่งไม่เปลี่ยนตลอดทั้งวัน — cache ไว้ตามวันที่ปัจจุบัน
    (ต่างจาก HTF cache ที่รีเฟรชทุก 30 นาที เพราะ Daily range ไม่จำเป็นต้องสดขนาดนั้น รีเฟรชวันละครั้งพอ)
    คืนค่า dict {"bias","prev_high","prev_low","equilibrium"} หรือ None ถ้ายังไม่มี cache/ข้ามวันแล้ว
    """
    import json
    from datetime import datetime, timezone

    raw = kv_get(bucket, f"daily_range_{symbol}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("date") != today_key:
        return None
    return data.get("daily_range")


def set_cached_daily_range(bucket, symbol, daily_range):
    """บันทึก Daily range ที่เพิ่งคำนวณลง kvdb ให้รอบถัดไปในวันเดียวกันใช้ซ้ำได้ ไม่ต้องดึง Daily ใหม่ทุกรอบ"""
    import json
    from datetime import datetime, timezone

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = json.dumps({"date": today_key, "daily_range": daily_range}, default=float)
    kv_set(bucket, f"daily_range_{symbol}", payload)


def check_plan4_trigger(df_5m, config, symbol, td_symbol):
    """
    Plan 4 (Daily Continuation): ต่างจากแผน 1-3 ตรงที่อ้างอิง Daily range ไม่ใช่ 15M/5M
    ใช้ df_5m ที่ดึงมาแล้วตอนต้นรอบนี้ (ตัวเดียวกับที่ Plan 1 ใช้หา 5M Trigger) ไม่ต้องดึงซ้ำ
    แต่ต้องดึง Daily เพิ่ม 1 ครั้ง — cache ไว้ทั้งวัน (get/set_cached_daily_range) ประหยัด quota
    dedup แบบเดียวกับ Plan 2/3: บันทึก state ตาม entry/direction กันแจ้งซ้ำขณะเงื่อนไขเดิมยังจริงอยู่
    """
    from fetch_data import fetch_twelvedata

    bucket = config["kvdb_bucket"]
    try:
        daily_range = get_cached_daily_range(bucket, symbol)
        if daily_range is None:
            daily_df = fetch_twelvedata(
                symbol=td_symbol, interval="1day", outputsize=3,
                api_key=config["twelvedata_api_key"]
            )
            daily_range = get_daily_bias_and_range(daily_df)
            if daily_range:
                set_cached_daily_range(bucket, symbol, daily_range)

        if daily_range:
            plan4_signal = detect_plan4_signal(df_5m)
            if plan4_signal and plan4_signal["direction"] == daily_range["bias"]:
                plan4_order = calc_plan4_order(plan4_signal, daily_range)
                if plan4_order:
                    state_key = f"plan_state_{symbol}_plan4_daily_continuation"
                    dedup_value = f"{plan4_order['direction']}:{plan4_order['entry_price']:.4f}"
                    prev_value = kv_get(bucket, state_key)

                    if prev_value != dedup_value:
                        if not is_in_news_blackout(bucket, symbol)[0]:
                            direction_th = "LONG (ซื้อ)" if plan4_order["direction"] == "bullish" else "SHORT (ขาย)"
                            plan4_msg = (
                                f"🚨 <b>ออเดอร์เข้า — Daily Continuation (แผนที่ 4)</b>\n"
                                f"Symbol: {symbol} | ทิศทาง: {direction_th}\n"
                                f"Entry: {plan4_order['entry_price']:.4f} | "
                                f"SL: {plan4_order['stop_loss']:.4f} | "
                                f"TP: {plan4_order['take_profit']:.4f} (RR {plan4_order['rr']})\n\n"
                                "หมายเหตุ: แผนนี้ถือยาวเป็นชั่วโมง-วัน อ้างอิง Daily range ไม่ใช่ "
                                "day-trade แบบแผน 1-3 ไม่มี partial TP ปล่อยไหลจนถึงเป้าเดียวนี้เท่านั้น "
                                "ยังไม่เคยผ่านการ backtest มาก่อน ควรพิจารณาความเสี่ยงเพิ่มเติมเอง"
                            )

                            send_alert_to_targets(config, plan4_msg)

                            save_plan_order(config, symbol, plan4_order["direction"],
                                             plan4_order["entry_price"], plan4_order["stop_loss"],
                                             {"TP1": plan4_order["take_profit"]}, score=None,
                                             plan_key="plan4_daily_continuation")

                            kv_set(bucket, state_key, dedup_value)
    except Exception as e:
        print(f"[Plan 4 Trigger Error] {symbol}: {e}")


def check_zone_entry_trigger(df, bias_4h, config, symbol):
    """
    กลุ่ม A (Set & Forget SMC Zone Entry — /order5) — เช็คทุกรอบว่ามี zone/confluence ใหม่ให้แจ้ง
    เตือนอัตโนมัติไหม โดยไม่ต้องรอให้ผู้ใช้พิมพ์ /order5 เอง (เหมือน Plan 2/3/4)

    ต่างจาก Plan 2/3/4 ตรงที่บันทึกเป็นสถานะ 'pending' (ผ่าน add_pending_order) ไม่ใช่ 'running' ทันที
    เพราะเป็น Set & Forget — แจ้งก่อนราคาจะเดินทางมาถึง Entry จริง (ดู zone_entry.py/orders.py
    สำหรับรายละเอียด logic การหา zone และวงจรสถานะ pending -> running -> win/loss/expired)

    ใช้ bias_4h ที่ main.py cache ไว้แล้ว (ยืมจาก cache 30 นาทีที่มีอยู่แล้ว) ไม่ต้องคำนวณ 4H ใหม่
    dedup แบบเดียวกับ telegram_bot.py's _has_similar_pending_or_running_order (เช็คทั้ง pending+running
    กันแจ้งซ้ำ zone เดิมที่ยังไม่ fill/ยังไม่หมดอายุ)
    """
    from indicator import add_indicators
    from zone_entry import find_zone_entry, calc_zone_entry_order

    bucket = config["kvdb_bucket"]
    try:
        df_ind_plan = add_indicators(df, config)
        result = find_zone_entry(bias_4h, df_ind_plan, config)
        if not result["valid"]:
            return

        order = calc_zone_entry_order(result, df_ind_plan, config)
        if not order:
            return

        plan_blackout, _ = is_in_news_blackout(bucket, symbol)
        if plan_blackout:
            return

        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = (
            df_ind_plan["atr"].tail(atr_period).mean()
            if "atr" in df_ind_plan.columns and len(df_ind_plan)
            else 0
        )
        threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

        existing_orders = load_orders(bucket, symbol)  # โหลดครั้งเดียว ใช้ทั้ง dedup check และ save
        for o in existing_orders:
            if (o["status"] in ("pending", "running") and o.get("plan") == "plan5_zone_single"
                    and o["direction"] == order["direction"]
                    and abs(o["entry_price"] - order["entry_price"]) < threshold):
                return  # มี zone ลักษณะเดียวกันแจ้งไปแล้ว (ยัง pending หรือ fill ไปแล้ว) ไม่แจ้งซ้ำ

        direction_th = "LONG (ซื้อ)" if order["direction"] == "bullish" else "SHORT (ขาย)"
        msg = (
            f"🚨 <b>เจอ Zone ใหม่ — แผนที่ 5 (SMC Zone Entry, Set & Forget)</b>\n"
            f"Symbol: {symbol} | ทิศทาง: {direction_th}\n"
            + "\n".join(result["reasons"]) + "\n\n"
            f"Entry (Limit): {order['entry_price']:.4f}\n"
            f"SL: {order['stop_loss']:.4f}\n"
            f"TP: {order['take_profit']:.4f} (RR {order['rr']})\n\n"
            "หมายเหตุ: แจ้งทันทีที่เจอ zone (Set \u0026 Forget) — วาง Limit Order ไว้รอได้เลย "
            "ยังไม่นับเป็นออเดอร์จริงจนกว่าราคาจะเดินทางมาถึง Entry (เช็คสถานะที่ /summary)"
        )
        send_alert_to_targets(config, msg)

        saved = add_pending_order(
            bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
            {"TP1": order["take_profit"]}, score=None, plan="plan5_zone_single",
            expires_in_hours=config.get("zone_entry_expires_hours", 8), existing_orders=existing_orders,
        )
        if saved is None:
            print(f"[Order Tracking Error] บันทึก pending order plan5_zone_single ({symbol}) "
                  f"ลง kvdb ไม่สำเร็จ")
    except Exception as e:
        print(f"[Plan 5 Zone Entry Trigger Error] {symbol}: {e}")


def check_sweep_entry_trigger(df, bias_4h, config, symbol):
    """
    กลุ่ม C (Liquidity Sweep + Displacement — /order6) — เช็คทุกรอบว่ามีการกวาด Liquidity แล้ว
    กลับตัว + ยืนยันด้วย FVG ให้แจ้งเตือนอัตโนมัติไหม โดยไม่ต้องรอผู้ใช้พิมพ์ /order6 เอง
    (เหมือน check_zone_entry_trigger ของกลุ่ม A แต่ engine คนละตัว — ดู liquidity_sweep_entry.py)

    บันทึกเป็นสถานะ 'pending' เหมือนกลุ่ม A (Set & Forget) dedup แบบเดียวกัน (เช็ค pending+running)
    """
    from indicator import add_indicators
    from liquidity_sweep_entry import find_sweep_entry, calc_sweep_entry_order

    bucket = config["kvdb_bucket"]
    try:
        df_ind_plan = add_indicators(df, config)
        result = find_sweep_entry(df_ind_plan, bias_4h, config)
        if not result["valid"]:
            return

        order = calc_sweep_entry_order(result, df_ind_plan, config)
        if not order:
            return

        plan_blackout, _ = is_in_news_blackout(bucket, symbol)
        if plan_blackout:
            return

        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = (
            df_ind_plan["atr"].tail(atr_period).mean()
            if "atr" in df_ind_plan.columns and len(df_ind_plan)
            else 0
        )
        threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

        existing_orders = load_orders(bucket, symbol)  # โหลดครั้งเดียว ใช้ทั้ง dedup check และ save
        for o in existing_orders:
            if (o["status"] in ("pending", "running") and o.get("plan") == "plan6_sweep_general"
                    and o["direction"] == order["direction"]
                    and abs(o["entry_price"] - order["entry_price"]) < threshold):
                return  # มีโอกาสลักษณะเดียวกันแจ้งไปแล้ว ไม่แจ้งซ้ำ

        direction_th = "LONG (ซื้อ)" if order["direction"] == "bullish" else "SHORT (ขาย)"
        msg = (
            f"🚨 <b>เจอโอกาสใหม่ — แผนที่ 6 (Liquidity Sweep + Displacement, Set \u0026 Forget)</b>\n"
            f"Symbol: {symbol} | ทิศทาง: {direction_th}\n"
            + "\n".join(result["reasons"]) + "\n\n"
            f"Entry (Limit): {order['entry_price']:.4f}\n"
            f"SL: {order['stop_loss']:.4f}\n"
            f"TP: {order['take_profit']:.4f} (RR {order['rr']})\n\n"
            "หมายเหตุ: แจ้งทันทีที่เจอโอกาส (Set \u0026 Forget) — วาง Limit Order ไว้รอได้เลย "
            "ยังไม่นับเป็นออเดอร์จริงจนกว่าราคาจะเดินทางมาถึง Entry (เช็คสถานะที่ /summary)"
        )
        send_alert_to_targets(config, msg)

        saved = add_pending_order(
            bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
            {"TP1": order["take_profit"]}, score=None, plan="plan6_sweep_general",
            expires_in_hours=config.get("sweep_entry_expires_hours", 6), existing_orders=existing_orders,
        )
        if saved is None:
            print(f"[Order Tracking Error] บันทึก pending order plan6_sweep_general ({symbol}) "
                  f"ลง kvdb ไม่สำเร็จ")
    except Exception as e:
        print(f"[Plan 6 Liquidity Sweep Trigger Error] {symbol}: {e}")


def check_qm_pattern_trigger(df, config, symbol):
    """
    กลุ่ม D (Quasimodo Pattern — /order7) — เช็คทุกรอบว่ามีโครงสร้าง QM (H-L-HH-LL หรือกระจก)
    ให้แจ้งเตือนอัตโนมัติไหม โดยไม่ต้องรอผู้ใช้พิมพ์ /order7 เอง (เหมือน check_zone_entry_trigger/
    check_sweep_entry_trigger ของกลุ่ม A/C แต่ engine คนละตัว — ดู qm_pattern_entry.py)

    บันทึกเป็นสถานะ 'pending' เหมือนกลุ่ม A/C (Set & Forget) dedup แบบเดียวกัน (เช็ค pending+running)
    ไม่ต้องใช้ bias_4h เพราะกลุ่ม D หาโครงสร้างจาก swing points ของ timeframe เข้าไม้เองล้วนๆ
    """
    from indicator import add_indicators
    from qm_pattern_entry import find_qm_pattern, calc_qm_entry_order

    bucket = config["kvdb_bucket"]
    try:
        df_ind_plan = add_indicators(df, config)
        result = find_qm_pattern(df_ind_plan, config)
        if not result["valid"]:
            return

        order = calc_qm_entry_order(result, config)
        if not order:
            return

        plan_blackout, _ = is_in_news_blackout(bucket, symbol)
        if plan_blackout:
            return

        atr_period = config.get("sl_atr_avg_period", 20)
        current_atr = (
            df_ind_plan["atr"].tail(atr_period).mean()
            if "atr" in df_ind_plan.columns and len(df_ind_plan)
            else 0
        )
        threshold = current_atr if current_atr else config.get("min_sl_distance", 10.0)

        existing_orders = load_orders(bucket, symbol)
        for o in existing_orders:
            if (o["status"] in ("pending", "running") and o.get("plan") == "plan7_qm_pattern"
                    and o["direction"] == order["direction"]
                    and abs(o["entry_price"] - order["entry_price"]) < threshold):
                return  # มีโอกาสลักษณะเดียวกันแจ้งไปแล้ว ไม่แจ้งซ้ำ

        direction_th = "LONG (ซื้อ)" if order["direction"] == "bullish" else "SHORT (ขาย)"
        msg = (
            f"🚨 <b>เจอโอกาสใหม่ — แผนที่ 7 (Quasimodo Pattern, Set \u0026 Forget)</b>\n"
            f"Symbol: {symbol} | ทิศทาง: {direction_th}\n"
            + "\n".join(result["reasons"]) + "\n\n"
            f"Entry (Limit): {order['entry_price']:.4f}\n"
            f"SL: {order['stop_loss']:.4f}\n"
            f"TP: {order['take_profit']:.4f} (RR {order['rr']})\n\n"
            "หมายเหตุ: แจ้งทันทีที่เจอโครงสร้าง (Set \u0026 Forget) — วาง Limit Order ไว้รอได้เลย "
            "ยังไม่นับเป็นออเดอร์จริงจนกว่าราคาจะเดินทางมาถึง Entry (เช็คสถานะที่ /summary)"
        )
        send_alert_to_targets(config, msg)

        saved = add_pending_order(
            bucket, symbol, order["direction"], order["entry_price"], order["stop_loss"],
            {"TP1": order["take_profit"]}, score=None, plan="plan7_qm_pattern",
            expires_in_hours=config.get("qm_entry_expires_hours", 8), existing_orders=existing_orders,
        )
        if saved is None:
            print(f"[Order Tracking Error] บันทึก pending order plan7_qm_pattern ({symbol}) "
                  f"ลง kvdb ไม่สำเร็จ")
    except Exception as e:
        print(f"[Plan 7 QM Pattern Trigger Error] {symbol}: {e}")
