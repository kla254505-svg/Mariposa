import os

CONFIG = {
    "swing_lookback": 7,
    "structure_lookback": 50,
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_trend": 200,
    "rsi_period": 14,
    "atr_period": 14,
    "adx_period": 14,
    "adx_min_trend": 20,
    "session_filter_enabled": True,
    "trading_sessions_utc": [(7, 21)],   # London start ถึง NY end รวมกนเป็นช่วงเดียว
    "killzones_utc": [(7, 10), (12, 15)],  # London Open, NY Open
    "ob_lookback": 30,
    "fvg_min_gap_atr": 0.15,
    "fvg_lookback": 60,
    "liquidity_lookback": 40,
    "liquidity_sweep_lookback": 10,
    "equal_level_tolerance_atr": 0.1,
    "atr_contraction_filter_enabled": True,
    "atr_contraction_lookback": 50,
    "atr_contraction_ratio": 0.7,
    # --- 4H Bias (เทรนด์ใหญ่สุด + Premium/Discount) ---
    "bias4h_filter_enabled": True,
    "premium_discount_filter_enabled": True,
    # --- 5M Trigger (รอ reaction กลับตัวจริงก่อนยิง entry) ---
    "trigger5m_filter_enabled": True,
    "trigger5m_lookback": 6,
    "trigger5m_choch_lookback": 20,
    "risk_per_trade_pct": 1.0,
    "structure_entry_atr_mult": 0.5,
    "sl_buffer_atr": 0.25,
    "min_rr": 1.2,
    "tp1_rr": 1.5,
    "tp2_rr": 2.5,
    "tp3_rr": 4.0,
    # หมายเหตุ: ตัวนี้ใช้แค่ตอน print console report (report.py) ว่า setup "น่าสนใจ" พอจะจับตาดูไหม
    # ไม่ใช่ตัวกำหนดว่าจะยิง Telegram Alert หรือไม่ — ตัวที่คุมการยิง Alert จริงคือ min_score_to_alert (ด้านล่าง)
    # ตั้งไว้ต่ำกว่า min_score_to_alert เสมอ (เป็น "เฝ้าดูก่อน" ที่บาร์ต่ำกว่า "พร้อมแจ้งเตือนจริง")
    "min_score_console_watchlist": 30,
    # --- SL: กันเคส zone แคบ/ATR หดตัวชั่วคราวจนได้ SL แคบผิดปกติ ---
    # --- Spread buffer: กันเคส pending order (Set & Forget) รายงานว่า "fill แล้ว" ทั้งที่จริงราคา
    # ของโบรก (ซึ่งมี spread ระหว่าง bid/ask) อาจยังไปไม่ถึงจุด Entry จริง — ราคาที่บอทใช้เช็คมาจาก
    # TwelveData (ราคากลาง ไม่ใช่ราคาบิด/แอสก์ของโบรกที่คุณเทรดจริง) ต้องเผื่อระยะไว้หน่อย
    # ใช้เฉพาะตอนเช็ค Entry fill เท่านั้น (ตาม feedback ผู้ใช้) ไม่กระทบ TP/SL ซึ่งยังเช็คราคาตรงเป๊ะเหมือนเดิม
    "spread_buffer": 0.25,   # กลางช่วง spread จริงของโบรกผู้ใช้ (0.2-0.3)
    "min_sl_distance": 10.0,   # ระยะ SL ขั้นต่ำเป็นราคาจริง (เช่น เข้า 4124 SL ห่างอย่างน้อย 10.0 = 4114)
    "sl_atr_avg_period": 20,   # ใช้ ATR เฉลี่ยย้อนหลังกี่แท่งสำหรับคำนวณ buffer แทน ATR แท่งล่าสุดเป๊ะๆ
    "fib_levels": [0.382, 0.5, 0.618, 0.705, 0.79],
    # --- Plan 2/3 (Breakout / สวนเทรนด์) จาก Hourly Briefing: เกณฑ์ยืนยันก่อนยิง Alert จริง ---
    "breakout_confirm_atr_mult": 0.3,   # ราคาต้องปิดเลยระดับ swing high/low ไปเกิน 0.3*ATR ถึงจะนับว่า "ทะลุแรงๆ" จริง
    # --- Secrets: อ่านจาก GitHub Actions Secrets (Environment Variables) ---
    "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    # กลุ่ม Telegram แยกต่างหาก (optional) — ใช้เฉพาะ "สัญญาณเข้าเทรด" กับ "เตือนข่าวล่วงหน้า 1 ชม."
    # ถ้าไม่ตั้งค่า env ตัวนี้ไว้ ระบบจะไม่ส่งเข้ากลุ่ม (ส่งแค่ telegram_chat_id เดิมตามปกติ)
    "telegram_group_chat_id": os.environ.get("TELEGRAM_GROUP_CHAT_ID", ""),
    # ID ผู้ใช้ Telegram ของเจ้าของบอท (ตัวเลข ไม่ใช่ username) — คำสั่ง /order /trend /news /status /aicheck
    # จะตอบเฉพาะคนนี้เท่านั้น คนอื่นในกลุ่มพิมพ์คำสั่งจะถูกเมินเงียบๆ หาได้จาก @userinfobot บน Telegram
    # ถ้าไม่ตั้งค่านี้ไว้ ระบบจะไม่ประมวลผลคำสั่งใดๆ เลย (ปลอดภัยไว้ก่อน)
    "telegram_owner_id": os.environ.get("TELEGRAM_OWNER_ID", ""),
    # ปิด/เปิดการแจ้งเตือนอัตโนมัติ (Push) ทั้งหมด — ถ้า False บอทจะเงียบสนิท ไม่ส่งอะไรเองเลย
    # ต้องพิมพ์คำสั่ง /order /trend /news /status /aicheck เอาเองถึงจะได้คำตอบ (Pull-only mode)
    # ตั้งเป็น True เมื่อไหร่ก็ได้ถ้าอยากได้ Push กลับมาเหมือนเดิม ไม่ต้องแก้โค้ดที่อื่นเลย
    # เจอสาเหตุจริงว่าทำไม Plan 2-8 เอง + plan_summary.py (แผนที่แนะนำ/อัปเดตสถานะ/สรุปผล) ไม่เคยส่ง
    # เข้า Telegram เลยสักครั้ง (4 ก.ย. 69) — flag นี้เคยถูกปิดไว้ (False) ซึ่งไปตัดการส่งของ
    # send_alert_to_targets() ทั้งหมดเงียบๆ ไม่มี error โผล่ที่ไหนเลย (คืนแค่ [] เฉยๆ)
    # AI Second Opinion ยังทำงานได้ปกติตลอดมาเพราะส่งผ่าน send_telegram_alert() ตรงๆ คนละทาง
    # ไม่ผ่าน flag นี้เลย ทำให้ดูเหมือนระบบส่งข้อความได้ปกติทั้งที่จริงๆ อีกครึ่งระบบเงียบสนิทมาตลอด
    "push_notifications_enabled": True,
    # ตัวนี้คุมว่าจะยิง Telegram Alert จริงหรือไม่ (ต่างจาก min_score_console_watchlist ด้านบนที่แค่ print console)
    # ปรับจาก 45 -> 55 (3 ก.ย. 69) ทดสอบว่าคะแนนสูงขึ้นช่วยลดอัตราโดน SL ไหม (ของเดิม 45/120 ≈ 37.5%
    # ผ่านง่ายไป โดน SL บ่อยตามที่สังเกตจริง) ถ้าลองแล้วยังไม่ดีขึ้นค่อยปรับใหม่ได้ ไม่ตายตัว
    "min_score_to_alert": 55,
    "twelvedata_api_key": os.environ.get("TWELVEDATA_API_KEY", ""),
    "healthchecks_url": os.environ.get("HEALTHCHECKS_URL", ""),
    "kvdb_bucket": os.environ.get("KVDB_BUCKET", ""),

    # --- Central AI Second Opinion Layer (Choice B) — ดู ai_layer.py ---
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
    "ai_model": os.environ.get("AI_MODEL", "claude-sonnet-5").strip(),
    "ai_analysis_enabled": os.environ.get("AI_ANALYSIS_ENABLED", "true").strip().lower() != "false",
    # เวลาที่อนุญาตให้เรียก AI เท่านั้น (จ-ศ 10:00-22:00 เวลาไทย) — คุมเฉพาะ AI Layer ไม่เกี่ยวกับ
    # Strategy (Plan 1-8) ที่ยังทำงาน 24/7 เหมือนเดิมทุกประการ ห้ามเอาไปใช้ gate Strategy เด็ดขาด
    "ai_time_filter_days": {0, 1, 2, 3, 4},  # Mon=0 ... Sun=6 (ตาม datetime.weekday())
    "ai_time_filter_hours": (0, 24),  # เปิดเต็ม 24 ชม. (เดิม 10:00-22:00) — ไม่กระทบ session filter
    # ที่คุมการยิง Order Alert (ยังเป็น 14:00-04:00 ไทยเหมือนเดิม) นี่แค่ขยายช่วงที่ Central AI Layer
    # (Second Opinion) พร้อมทำงานให้กว้างขึ้นเฉยๆ ตามที่ตกลงกันไว้ — AI จะยังไม่ได้เห็นสัญญาณนอก
    # session อยู่ดี เพราะ session filter ตัดสัญญาณทิ้งไปก่อนถึงขั้น AI แล้ว (ดูรายละเอียดที่คุยกัน)
    # กันเรียก AI ถี่เกินไปแม้ state จะเปลี่ยนบ่อยผิดปกติ (เช่น เผื่อ cron รันซ้อนกัน) — ตั้งไว้ "สั้น
    # กว่า" ความถี่ cron จริง (5 นาที) เสมอ ไม่งั้นจะไปบล็อกสัญญาณใหม่ที่เกิดขึ้นจริงในรอบถัดไปโดยไม่
    # ตั้งใจ (เจอบั๊กนี้จริงตอนเทส: ตั้งไว้ 10 นาทีแล้ว Plan ใหม่ที่เกิดขึ้นในรอบถัดไป — ห่างจากครั้งก่อน
    # แค่ 5 นาที — ถูกกันไม่ให้ AI วิเคราะห์ไปด้วย ทั้งที่ state เปลี่ยนจริงและควรแจ้งเตือน)
    "ai_cooldown_minutes": 2,
    # ราคาปัจจุบันเข้าใกล้ entry ของออเดอร์ที่ยัง pending ภายในกี่เท่าของ ATR ถึงจะถือว่าเป็น event
    # "PRICE_APPROACH_ENTRY" (ให้ Central AI Layer วิเคราะห์เพิ่มได้ แม้ยังไม่ถึง entry จริงก็ตาม)
    "ai_price_approach_atr_mult": 0.5,


}

# ══════════════════════════════════════════════════════
# SYMBOL_CONFIG_OVERRIDES — ค่าที่ต้องแยกต่อคู่เงิน เพราะผูกกับสเกลราคา/พฤติกรรมตลาดเฉพาะตัว
# (ต่างจากตัวคูณสัมพัทธ์อย่าง ATR ที่ปรับตามสเกลราคาของแต่ละคู่เงินเองอยู่แล้วโดยธรรมชาติ ไม่ต้อง
# แยก) key เป็น display symbol เดียวกับที่ telegram_bot.py ใช้ภายใน (SYMBOL_ALIASES resolve มาแล้ว
# เช่น "ETHUSDT" ไม่ใช่ "eth")
#
# ตอนนี้แยกให้ ETHUSDT เพราะต่างจาก XAUUSD (ทอง) ตรงที่:
#   - เทรด 24/7 ไม่มีวันหยุด ไม่มี "session" ที่สภาพคล่องกระจุกตัวชัดเจนแบบ forex London/NY —
#     session_filter_enabled จึงปิดไว้ (ดู session.py: ปิดแล้วถือว่า "อยู่ใน session" เสมอ)
#   - สเปรด/ระยะ SL ขั้นต่ำที่เหมาะสมคนละสเกลกับทองสิ้นเชิง (ราคา ผันผวน และเอ็กซ์เชนจ์ที่เทรดจริง
#     ต่างกัน) ตัวเลข spread_buffer/min_sl_distance ด้านล่างเป็นค่าเริ่มต้นคร่าวๆ เท่านั้น ควรเก็บ
#     ข้อมูลจริงจาก /order eth สักพัก (สเปรดจริงของเอ็กซ์เชนจ์ที่ใช้เทรด, ATR จริงที่สังเกตเห็น) แล้ว
#     ปรับตัวเลขนี้ให้ตรงกับที่สังเกตเห็นจริงอีกที ไม่ใช่ตัวเลขสูตรสำเร็จที่ยืนยันแล้วว่าถูกต้อง
# ══════════════════════════════════════════════════════
SYMBOL_CONFIG_OVERRIDES = {
    "ETHUSDT": {
        "session_filter_enabled": False,
        "spread_buffer": 1.0,
        "min_sl_distance": 15.0,
    },
}


def get_symbol_config(base_config, symbol):
    """คืน config ที่ merge SYMBOL_CONFIG_OVERRIDES ของ symbol นั้นเข้ากับ base_config แล้ว — ไม่แก้ไข
    base_config เดิม (คืน dict ใหม่เสมอ) คู่เงินที่ไม่มี override (เช่น XAUUSD) จะได้ base_config
    กลับไปตรงๆ ไม่มีอะไรเปลี่ยน (ไม่ breaking change)"""
    overrides = SYMBOL_CONFIG_OVERRIDES.get(symbol)
    if not overrides:
        return base_config
    merged = dict(base_config)
    merged.update(overrides)
    return merged
