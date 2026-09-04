"""
dxy_filter.py — ตัวกรอง/คะแนนเสริมจากความแข็งแกร่งของเงินดอลลาร์ (DXY proxy) สำหรับผสมเข้ากับคะแนน
ของ 8 แผนเดิม (score.py / plan_score.py)

*** สถานะตอนนี้: เขียนไว้ก่อน ยังไม่เปิดใช้งานจริง ***
ไฟล์นี้ยังไม่ถูกเรียกจาก main.py / plan_runner.py / telegram_bot.py เลยแม้แต่จุดเดียว เป็นแค่โมดูล
standalone ที่ทดสอบเองได้ (ดู test_dxy_filter.py) รอจนกว่าจะวิเคราะห์ผล 8 แผนเดิมเสร็จก่อนถึงจะตัดสินใจ
ว่าจะเปิดใช้งานจริงไหม

score.py (calc_confidence_score) และ plan_score.py (generic_plan_score) ถูกแก้เพิ่ม parameter
dxy_context=None ไว้รอแล้ว — ค่า default None ทำให้พฤติกรรมเดิมไม่เปลี่ยนแม้แต่นิดเดียวถ้ายังไม่มีใคร
ส่งค่าเข้าไป (main.py/plan_runner.py ตอนนี้เรียกทั้งสองฟังก์ชันโดยไม่ส่ง dxy_context เลย) การเปิดใช้งาน
จริงในอนาคตต้องแก้ main.py/plan_runner.py ให้เรียก fetch_dxy_context() แล้วส่งผลลัพธ์เข้าไปเป็น
dxy_context ตอนเรียก calc_confidence_score()/generic_plan_score()

*** หมายเหตุสำคัญเรื่องแหล่งข้อมูล (อ่านก่อนเปิดใช้งานจริง) ***
เช็คแล้วว่า TwelveData ไม่มี symbol ดัชนี DXY ที่ยืนยันได้แน่ชัดว่าใช้ได้จริงบน plan ฟรี — แม้แต่ตัวอย่าง
การใช้งานจริงของคนอื่นที่ต่อ TwelveData ก็ยังเลือกคำนวณ DXY เองจากคู่เงิน 6 ตัวตามสูตร ICE Futures แทน
ที่จะดึง symbol ดัชนีสำเร็จรูปตรงๆ เพื่อไม่ให้ทายชื่อ symbol ผิดแล้วพังตอนใช้งานจริง ไฟล์นี้เลยใช้
EUR/USD เป็น "ตัวแทนความแข็งแกร่งของดอลลาร์" แทน (EUR คือสกุลเงินที่มีน้ำหนักสูงสุดใน DXY ราว 57.6%
ตามสูตร ICE เป็นตัวแทนที่สมเหตุสมผลและมีข้อมูลแน่นอนบน TwelveData อยู่แล้ว — ไม่ใช่ DXY เป๊ะๆ 100% แต่
เป็นค่าประมาณที่ยิง API เพิ่มแค่ 1 ครั้งต่อรอบ (เท่ากับที่จะเสียถ้าดึง DXY ตรงๆ ได้จริง) ไม่ใช่ 6 ครั้งแบบ
คำนวณเต็มสูตร ซึ่งจะกระทบโควตา TwelveData free tier ที่ระบบระวังอยู่แล้ว (ดู telegram_bot.py —
_CONTEXT_CACHE มีไว้เพราะเคยเจอปัญหาจริงที่ยิง API ถี่จนชน rate limit 8 requests/นาที)

ถ้าวันหลังยืนยันได้ว่า TwelveData มี symbol ดัชนี DXY ที่ใช้ได้จริงบน plan ที่ใช้อยู่ ก็แค่เปลี่ยนค่า
DXY_PROXY_SYMBOL ด้านล่างเป็น symbol นั้นตรงๆ แล้วตั้ง DXY_PROXY_INVERTED = False (เพราะ DXY จริงไม่ต้อง
กลับทิศทางเหมือน EUR/USD) ไม่ต้องแก้ logic ส่วนอื่นเลย
"""

DXY_PROXY_SYMBOL = "EUR/USD"
# EUR/USD ขึ้น = ดอลลาร์อ่อนลง (DXY ลง), EUR/USD ลง = ดอลลาร์แข็งขึ้น (DXY ขึ้น) -> ทิศทางกลับด้านกับ
# DXY จริงเสมอ เพราะ EUR อยู่ในตัวหาร ไม่ใช่ตัวตั้งของสูตร DXY
DXY_PROXY_INVERTED = True

# คะแนนเสริมสูงสุดถ้า DXY เห็นด้วยกับทิศทางที่จะเข้า — ตั้งไว้ใกล้เคียงระดับเดียวกับ bias4h_alignment/
# htf_1h_alignment ที่มีอยู่แล้วใน score.py (8/6 คะแนน) ไม่ให้มีน้ำหนักมากเกินไปจนกลบปัจจัยเดิม
DXY_ALIGNMENT_BONUS = 6


def fetch_dxy_context(config, outputsize=100):
    """ดึงข้อมูลราคา + วิเคราะห์เทรนด์ของตัวแทน DXY (ดูหมายเหตุหัวไฟล์เรื่องแหล่งข้อมูล) คืน dict
    {trend, trend_strength, proxy_symbol} หรือ None ถ้าดึง/วิเคราะห์ไม่สำเร็จ — ห่อด้วย try/except
    กันเหนียวเหมือนโมดูลเสริมอื่นๆ ในโปรเจกต์ (เช่น sheets_log.py) ฟีเจอร์เสริมต้องไม่มีทางทำให้ pipeline
    หลักพังได้ไม่ว่ากรณีไหน"""
    try:
        from fetch_data import fetch_twelvedata
        from indicator import add_indicators
        from trend import analyze_structure

        df = fetch_twelvedata(
            symbol=DXY_PROXY_SYMBOL, interval="1h", outputsize=outputsize,
            api_key=config["twelvedata_api_key"],
        )
        df_ind = add_indicators(df, config)
        structure = analyze_structure(df_ind, config)

        proxy_trend = structure.get("trend")
        dxy_trend = proxy_trend
        if DXY_PROXY_INVERTED and proxy_trend in ("bullish", "bearish"):
            dxy_trend = "bearish" if proxy_trend == "bullish" else "bullish"

        return {
            "trend": dxy_trend,
            "trend_strength": structure.get("trend_strength"),
            "proxy_symbol": DXY_PROXY_SYMBOL,
        }
    except Exception as e:
        print(f"[DXY Filter] ดึง/วิเคราะห์ข้อมูล DXY proxy ไม่สำเร็จ (ไม่กระทบการทำงานหลัก): {e}")
        return None


def calc_dxy_alignment_score(direction, dxy_context):
    """คืน (bonus_points, note)

    direction: ทิศทางของสัญญาณทอง ("bullish"/"bearish")
    dxy_context: ผลลัพธ์จาก fetch_dxy_context() — เป็น trend ของ DXY จริงแล้ว (กลับทิศทางจาก proxy
    ให้เรียบร้อยตั้งแต่ fetch_dxy_context() แล้ว ฟังก์ชันนี้ไม่ต้องรู้เรื่อง proxy/inversion อีกเลย)

    ทองกับดอลลาร์ (DXY) มีความสัมพันธ์ผกผันกันในทางปฏิบัติ — ดอลลาร์แข็ง (DXY bullish) มักกดดันราคาทอง
    (bearish gold), ดอลลาร์อ่อน (DXY bearish) มักหนุนราคาทอง (bullish gold) ดังนั้น "สอดคล้องกัน" ในที่นี้
    หมายถึงทิศทางตรงข้ามกันระหว่าง direction (ทอง) กับ dxy_context["trend"] (ดอลลาร์) ไม่ใช่ทิศทางเดียวกัน

    กติกา: DXY สวนทางกับสัญญาณ (เช่นจะ Buy ทอง แต่ DXY กำลังแข็งค่าขึ้นด้วย) = ไม่ได้คะแนนเสริม + มี
    หมายเหตุเตือนกลับมา (ไม่ veto — แค่ไม่ได้บวกเสริม เหมือนกติกาของ bias4h_alignment/htf_1h_alignment
    เดิมในไฟล์ score.py) ไม่เคยหักคะแนนติดลบ เพื่อไม่ให้กระทบพฤติกรรมเดิมรุนแรงเกินไปตอนเปิดใช้งานจริง"""
    if not dxy_context:
        return 0, None

    dxy_trend = dxy_context.get("trend")
    if dxy_trend not in ("bullish", "bearish"):
        return 0, "DXY proxy ยัง sideway ไม่มีทิศทางชัดเจน"

    dxy_supports_gold_direction = (
        (direction == "bullish" and dxy_trend == "bearish") or
        (direction == "bearish" and dxy_trend == "bullish")
    )

    proxy_symbol = dxy_context.get("proxy_symbol", "DXY")

    if dxy_supports_gold_direction:
        strength_mult = 1.0 if dxy_context.get("trend_strength") == "strong" else 0.6
        bonus = round(DXY_ALIGNMENT_BONUS * strength_mult, 1)
        return bonus, f"DXY ({proxy_symbol}) หนุนทิศทางนี้ (ดอลลาร์{'อ่อนลง' if dxy_trend == 'bearish' else 'แข็งขึ้น'})"

    return 0, f"DXY ({proxy_symbol}) สวนทางกับสัญญาณนี้ — ระวังเพิ่ม"
