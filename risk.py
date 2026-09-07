def calc_stop_loss(entry_signal, current_atr, config):
    """
    current_atr: ควรส่งเป็น ATR เฉลี่ยย้อนหลัง (ไม่ใช่ ATR แท่งล่าสุดเป๊ะๆ) เพื่อกันเคสตลาดหดตัว
    ผิดปกติชั่วคราวแล้วได้ buffer แคบเกินจริง — ดู main.py/scenario.py ตรงจุดที่เรียกใช้ฟังก์ชันนี้
    """
    direction = entry_signal["direction"]
    entry_price = entry_signal["entry_price"]
    buffer = config["sl_buffer_atr"] * current_atr

    ob = entry_signal.get("ob")
    fvg = entry_signal.get("fvg")

    structure_zone = entry_signal.get("structure_zone")

    if ob:
        base = ob["bottom"] if direction == "bullish" else ob["top"]
    elif fvg:
        base = fvg["bottom"] if direction == "bullish" else fvg["top"]
    elif structure_zone:
        base = structure_zone["bottom"] if direction == "bullish" else structure_zone["top"]
    else:
        base = entry_price

    stop_loss = (base - buffer) if direction == "bullish" else (base + buffer)

    # --- SL ขั้นต่ำ: กันเคส zone แคบ/ATR ต่ำจนได้ SL แคบผิดปกติ เสี่ยงโดนสะบัดออกจาก noise ---
    # เช่นตั้ง min_sl_distance = 10.0 -> เข้า 4124 SL ต้องห่างอย่างน้อย 4114 (ฝั่ง Buy) เสมอ
    min_distance = config.get("min_sl_distance", 0)
    current_distance = abs(entry_price - stop_loss)
    if min_distance and current_distance < min_distance:
        stop_loss = (entry_price - min_distance) if direction == "bullish" else (entry_price + min_distance)

    return stop_loss


def calc_position_size(account_balance, entry_price, stop_loss, config, risk_pct_override=None):
    """
    risk_pct_override (ใหม่, optional): ถ้าใส่ไว้จะใช้ % นี้แทน config["risk_per_trade_pct"] คงที่เดิม
    — ใช้โดย calc_scaled_risk_pct() ด้านล่าง (สเกลความเสี่ยงตาม Confidence Score) ไม่ใส่ (None, ค่า
    default) พฤติกรรมเหมือนเดิมทุกประการ (ไม่ breaking change สำหรับผู้เรียกเดิมที่ยังไม่รู้จัก
    parameter ตัวนี้)
    """
    risk_pct = risk_pct_override if risk_pct_override is not None else config["risk_per_trade_pct"]
    risk_amount = account_balance * (risk_pct / 100)
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance == 0:
        return {"risk_amount": risk_amount, "sl_distance": 0, "position_size": 0, "risk_pct": risk_pct}
    position_size = risk_amount / sl_distance
    return {
        "risk_amount": round(risk_amount, 2),
        "sl_distance": round(sl_distance, 6),
        "position_size": round(position_size, 6),
        "risk_pct": round(risk_pct, 3),
    }


def calc_scaled_risk_pct(score, config, score_ceiling=100.0):
    """
    *** ใหม่ (7 ก.ย. 2026): Score-based Position Sizing ***
    เดิมทุกไม้ (ไม่ว่าคะแนนจะ 46 หรือ 118) ใช้ risk_per_trade_pct คงที่ตัวเดียวกันเป๊ะ ทั้งที่ความ
    มั่นใจต่างกันมาก — เทรดเดอร์มืออาชีพจริงมักเสี่ยงมากขึ้นในไม้ที่มั่นใจสูง เสี่ยงน้อยลงในไม้ที่
    เฉียดเกณฑ์ผ่านแบบบางๆ ฟังก์ชันนี้ scale risk_per_trade_pct เชิงเส้นตาม Score แทนค่าคงที่เดิม

    วิธีคิด: เทียบตำแหน่งของ score ระหว่าง min_score_to_alert (คะแนนต่ำสุดที่ผ่านเกณฑ์แจ้งเตือนได้ —
    ถือเป็น "จุดเริ่มมั่นใจน้อยที่สุด" เพราะคะแนนต่ำกว่านี้ไม่มีทางถูกแจ้งเตือนออกมาอยู่แล้ว) กับ
    score_ceiling (คะแนนเต็มตามจริงของสูตรนั้นๆ — แผนที่ 1 ใช้ score.PLAN1_SCORE_CEILING (~120),
    แผนที่ 2-8 ใช้ plan_score.GENERIC_MAX_SCORE (100)) เป็นสัดส่วน 0-1 แล้ว interpolate เชิงเส้น
    ระหว่าง risk_sizing_min_pct (ที่คะแนนเฉียดเกณฑ์) กับ risk_sizing_max_pct (ที่คะแนนเต็ม)

    ปิดได้ทั้งหมดด้วย config['risk_sizing_by_score_enabled']=False (fallback กลับไปใช้
    risk_per_trade_pct คงที่เดิมทันที) หรือ score=None (เผื่อผู้เรียกไม่มีคะแนนให้ใช้)
    """
    if not config.get("risk_sizing_by_score_enabled", True) or score is None:
        return config.get("risk_per_trade_pct", 1.0)

    floor_score = config.get("min_score_to_alert", 45)
    min_pct = config.get("risk_sizing_min_pct", 0.5)
    max_pct = config.get("risk_sizing_max_pct", 1.5)

    if score_ceiling <= floor_score:
        # ตั้งค่าผิดปกติ (เพดาน <= พื้น) — กันเหนียว fallback ไปใช้ค่าคงที่เดิมแทนที่จะหารด้วยศูนย์/ติดลบ
        return config.get("risk_per_trade_pct", 1.0)

    ratio = (score - floor_score) / (score_ceiling - floor_score)
    ratio = max(0.0, min(1.0, ratio))  # clamp 0-1 (เผื่อ score ต่ำกว่า floor หรือสูงกว่า ceiling ผิดปกติ)
    return round(min_pct + ratio * (max_pct - min_pct), 3)


def format_position_sizing_line(bucket, entry_price, stop_loss, score, config, score_ceiling=100.0):
    """
    สร้างข้อความสรุป "เงินเสี่ยงต่อไม้นี้เป็นตัวเงินจริง" ตามทุนที่ผู้ใช้ตั้งไว้ผ่าน /setbalance
    (account.py) + สเกลตาม Score (calc_scaled_risk_pct ด้านบน) — ใช้แปะท้ายข้อความ Telegram Alert
    ของทุกแผน (Plan 1-8)

    ก่อนหน้านี้ main.py คำนวณ position size (calc_position_size) ไว้จริง แต่ไม่เคยส่งเข้า Telegram
    เลยสักครั้ง (ใช้แค่ print console บน GitHub Actions ที่ผู้ใช้ไม่ค่อยได้เปิดดู) ทำให้ทั้งที่คำนวณ
    ตัวเงินไว้แล้ว ผู้ใช้เห็นแค่ราคา Entry/SL/TP เฉยๆ ไม่รู้เลยว่า "เท่าไหร่" ตามที่ถามมา — ฟังก์ชันนี้
    แก้จุดนั้นโดยตรง

    คืน string พร้อมแปะต่อท้าย message ได้เลย (ขึ้นบรรทัดใหม่ในตัวเองแล้ว ไม่ต้องเติม \\n นำหน้าเอง)
    หรือ "" ถ้าคำนวณไม่ได้ (เช่น account.py import ไม่ได้/bucket ว่าง) — กันเหนียว ไม่ทำให้ alert
    หลักพังเพราะฟีเจอร์เสริมตัวนี้จุดเดียว

    หมายเหตุสำคัญที่บอกผู้ใช้ตรงๆ ในข้อความ: position_size ที่คำนวณได้ "ไม่ใช่" ขนาด Lot จริงในโบรก —
    เป็นแค่ risk_amount หารด้วยระยะ SL (หน่วยราคาดิบ) เพราะระบบไม่รู้ค่า $/pip ของสัญญาที่โบรกคุณใช้จริง
    (ไม่มีการเชื่อมต่อ Broker API ดู claude/auto_execute_risk_guard_spec.md) ผู้ใช้ต้องแปลงเป็น Lot Size
    เองอีกทีตามสเปกสัญญาที่เทรดจริง
    """
    try:
        from account import get_account_balance

        balance = get_account_balance(bucket)
        risk_pct = calc_scaled_risk_pct(score, config, score_ceiling=score_ceiling)
        position = calc_position_size(balance, entry_price, stop_loss, config, risk_pct_override=risk_pct)
        return (
            f"\n💰 ทุนที่ใช้คำนวณ: ${balance:,.2f} | เสี่ยงต่อไม้นี้: {risk_pct:.2f}% = "
            f"${position['risk_amount']:,.2f}\n"
            f"⚠️ ยังไม่ใช่ขนาด Lot จริง — แปลงเป็น Lot Size เองตามค่า $/pip ของโบรกคุณ (ตั้งทุนใหม่ผ่าน "
            f"/setbalance)"
        )
    except Exception as e:
        print(f"[Position Sizing Error] {e}")
        return ""
