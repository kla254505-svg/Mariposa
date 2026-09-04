"""
plan_score.py — ระบบให้คะแนนเปรียบเทียบทั้ง 8 แผน สำหรับคำสั่ง /order รวม (ดู telegram_bot.py:
_cmd_order_all) คนละชุดตรรกะกับ score.py (ซึ่งเป็นสูตร Confidence Score แบบละเอียดของแผนที่ 1
โดยเฉพาะ ผูกกับ Order Block/FVG quality/RSI/MACD ฯลฯ ที่มีแค่แผนที่ 1 เท่านั้นที่มีข้อมูลครบพอจะ
คำนวณละเอียดขนาดนั้น)

แผนที่ 2-8 ไม่มีสูตร Confidence Score เฉพาะของตัวเอง จึงให้คะแนนแบบทั่วไป (เต็ม 100) จาก 4 ปัจจัย
ที่ทุกแผนมีข้อมูลอยู่แล้วเสมอเมื่อ trigger จริง (ไม่ต้องเจาะไปดึงข้อมูลเพิ่มเฉพาะแผน กันคำสั่ง /order
ช้าลงจากการยิง API เพิ่ม):
  - พื้นฐาน (สัญญาณเข้าเงื่อนไขจริงตามกติกาของแผนนั้นแล้ว)         40 คะแนน
  - คุณภาพ RR เทียบ min_rr ที่ตั้งไว้ใน config                    สูงสุด 25 คะแนน (เต็มที่ RR >= 2 เท่าของ min_rr)
  - ทิศทางของสัญญาณตรงกับ 4H Bias                                 20 คะแนน
  - ทิศทางของสัญญาณตรงกับเทรนด์หลัก 15M (Structure)                15 คะแนน
รวมเต็ม = 100 (+ DXY bonus ถ้ามี — ดูหมายเหตุ dxy_context ด้านล่าง)

คะแนนของแผนที่ 1 มาจาก score.py (calc_confidence_score) ตามเดิมทุกประการ ไม่ได้แก้สูตร — น้ำหนัก
รวมเต็มๆ ของแผนที่ 1 จะสูงกว่า 100 เล็กน้อย (~120) เพราะเป็นสูตรเฉพาะที่ละเอียดกว่า (มีตัวแปรที่แผน
อื่นไม่มี) ใช้เทียบ "ลำดับความน่าสนใจสัมพัทธ์" ระหว่างแผนได้ตามปกติ (ยิ่งสูง ยิ่งน่าสนใจ) แต่ไม่ใช่ %
ความแม่นยำที่เทียบตรงตัวกันเป๊ะๆ ข้ามแผน — มีหมายเหตุกำกับไว้ในข้อความ /order ให้ผู้ใช้ทราบด้วย

*** แก้ไขล่าสุด: เพิ่ม parameter dxy_context=None (ยังไม่เปิดใช้งานจริง) ***
เพิ่มไว้รองรับ dxy_filter.py (ดูหมายเหตุหัวไฟล์นั้น) — ค่า default None ทำให้พฤติกรรม/คะแนนที่ได้
เหมือนเดิม 100% ถ้าไม่มีใครส่ง dxy_context เข้ามา (main.py/plan_runner.py ตอนนี้ยังไม่ส่ง) ต้องรอ
วิเคราะห์ผล 8 แผนเดิมเสร็จก่อนถึงจะตัดสินใจเปิดใช้งานจริง (แก้ main.py/plan_runner.py ให้ fetch แล้ว
ส่งเข้ามา)
"""

from dxy_filter import calc_dxy_alignment_score

GENERIC_MAX_SCORE = 100.0


def generic_plan_score(direction, rr, bias_4h, structure, config, dxy_context=None):
    """คำนวณคะแนนทั่วไป (เต็ม 100 + DXY bonus ถ้ามี) ให้แผนที่ 2-8 คืน (score, breakdown) — breakdown
    เป็น dict label ภาษาไทย -> คะแนนที่ได้ในหมวดนั้น ใช้โชว์เหตุผลประกอบคะแนนใน /order

    dxy_context: ผลลัพธ์จาก dxy_filter.fetch_dxy_context() หรือ None (ค่า default — ยังไม่เปิดใช้งาน
    จริงตอนนี้ ดูหมายเหตุหัวไฟล์)"""
    score = 40.0
    breakdown = {"สัญญาณเข้าเงื่อนไข": 40.0}

    min_rr = config.get("min_rr", 1.2) or 1.2
    if rr:
        try:
            ratio = min(float(rr) / float(min_rr), 2.0) / 2.0
            pts = round(25.0 * max(ratio, 0.0), 1)
            if pts:
                score += pts
                breakdown["คุณภาพ RR"] = pts
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    bias_trend = (bias_4h or {}).get("trend")
    if bias_trend and bias_trend == direction:
        score += 20.0
        breakdown["สอดคล้อง 4H Bias"] = 20.0

    main_trend = (structure or {}).get("trend")
    if main_trend and main_trend == direction:
        score += 15.0
        breakdown["สอดคล้องเทรนด์หลัก 15M"] = 15.0

    if dxy_context is not None:
        dxy_bonus, dxy_note = calc_dxy_alignment_score(direction, dxy_context)
        if dxy_bonus:
            score += dxy_bonus
            breakdown[dxy_note or "DXY"] = dxy_bonus

    return round(score, 1), breakdown


def determine_master_trend(bias_4h, structure):
    """ทิศทาง 'เทรนด์หลัก' ที่ใช้ไฮไลต์ผลลัพธ์ /order ให้น้ำหนัก 4H Bias ก่อน (ภาพใหญ่กว่า) ถ้า
    4H ยัง sideway/ไม่มีข้อมูล ค่อย fallback ไปใช้เทรนด์ 15M (Structure) แทน
    คืนค่า (trend, source_label) หรือ (None, None) ถ้าหาเทรนด์หลักไม่ได้เลยทั้งคู่ (sideway ทั้งคู่)"""
    bias_trend = (bias_4h or {}).get("trend")
    if bias_trend in ("bullish", "bearish"):
        return bias_trend, "4H Bias"
    structure_trend = (structure or {}).get("trend")
    if structure_trend in ("bullish", "bearish"):
        return structure_trend, "15M Structure"
    return None, None
