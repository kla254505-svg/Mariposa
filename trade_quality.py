"""
claude/trade_quality.py — Final Trade Score + Grade (A+/A/B/C/D/F) layer

ที่มา: ข้อเสนอของผู้ใช้ (8 ก.ย. 2026) ข้อ 2-4 — แยก "Strategy Score" (คุณภาพของสัญญาณตามกฎของ
แผนนั้นๆ ล้วนๆ — มาจาก score.py ของแผนที่ 1 หรือ plan_score.py ของแผนที่ 2-8) ออกจาก
"Final Trade Score" (คุณภาพของ "การเข้าไม้ ณ ตอนนี้" — เพิ่มบริบทรอบตัวสัญญาณเข้าไปด้วย:
Session/Killzone, ความเสี่ยงข่าว, Zone ตรงข้ามใกล้ๆ) แล้วแปลงเป็นเกรด A+/A/B/C/D/F ให้อ่านง่ายกว่า
ตัวเลขดิบ พร้อมทั้งใช้เกรดกำหนด Risk % ต่อไม้แทนการ scale เชิงเส้นแบบเดิม

หลักการสำคัญที่ต้องรักษาไว้เสมอ (อย่าแก้โดยไม่อ่านตรงนี้ก่อน): โมดูลนี้เป็น "ชั้นแสดงผล + ให้เกรด"
เท่านั้น ไม่ใช่ Hard Gate ใหม่ — ไม่ block การส่ง Alert หรือการบันทึกออเดอร์ของ Plan ใดๆ ทั้งสิ้น
Plan 2/4 ที่มี Hard Block ของ Opposing Zone อยู่แล้วใน plan_runner.py (skip ไปเลย ไม่ส่ง Alert) ยัง
ทำงานเหมือนเดิมทุกประการ ไม่ได้ถูกแทนที่หรือปิดทับด้วยโมดูลนี้ — Final Score/Grade ที่นี่มีไว้ "แสดง"
ให้ผู้ใช้เห็นคุณภาพเพิ่มเติมประกอบการตัดสินใจเอง และใช้กำหนด Risk % ต่อไม้เท่านั้น
"""

from session import get_session_info

GRADE_EMOJI = {
    "A+": "🟢",
    "A": "🟢",
    "B": "🟢",
    "C": "🟡",
    "D": "🔴",
    "F": "⛔",
}

# เกณฑ์แบ่งเกรดตามสัดส่วนคะแนน (ratio 0-1) เทียบระหว่าง floor (min_score_to_alert — คะแนนต่ำสุดที่
# ผ่านเกณฑ์แจ้งเตือนได้ ถือเป็น "จุดเริ่มมั่นใจน้อยที่สุด" เพราะต่ำกว่านี้ไม่มีทางถูกแจ้งเตือนออกมาอยู่
# แล้ว) กับ ceiling (คะแนนเต็มจริงของสูตรนั้นๆ) — ใช้ตรรกะเดียวกับ risk.calc_scaled_risk_pct() เดิม
# (เชิงเส้น) เพียงแต่แบ่งเป็นช่วง (bucket) แทนที่จะ scale ต่อเนื่อง ตัวเลขเลือกจากการแบ่งช่วงคร่าวๆ
# ปรับได้ภายหลังตามข้อมูลจริงที่สะสม (ดู claude/score_outcome_analysis.py — เมื่อมีข้อมูลปิดออเดอร์
# มากพอ ควรกลับมาปรับเกณฑ์นี้ให้สอดคล้องกับ win rate/expectancy จริงที่สังเกตเห็นในแต่ละช่วง)
GRADE_RATIO_THRESHOLDS = [
    ("A+", 0.85),
    ("A", 0.70),
    ("B", 0.55),
    ("C", 0.40),
]
DEFAULT_GRADE_BELOW_C = "D"

# ตารางสำรอง (fallback) ถ้า config.py ยังไม่มี "risk_pct_by_grade" — กันโค้ดพังถ้าใครลืมอัปเดต config
DEFAULT_RISK_PCT_BY_GRADE = {
    "A+": 1.0,
    "A": 0.75,
    "B": 0.5,
    "C": 0.25,
    "D": 0.0,
    "F": 0.0,
}


def compute_grade(score, config, score_ceiling=100.0, forced_grade=None):
    """คำนวณเกรด A+/A/B/C/D จากตำแหน่งของ score ระหว่าง min_score_to_alert (floor) ถึง
    score_ceiling (เต็ม) — ใช้ตรรกะ ratio เดียวกับ risk.calc_scaled_risk_pct() เพื่อให้เกรดกับ Risk %
    สอดคล้องทิศทางเดียวกันเสมอ (คะแนนสูง = เกรดดี = Risk % สูงตามเกรด)

    forced_grade: ถ้าผู้เรียกมีเหตุผลให้ Block ชัดเจนอยู่แล้ว (เช่น RR ต่ำกว่า min_rr, 4H/1H ขัดแย้ง
    กันเอง) ส่ง "F" เข้ามาตรงนี้เพื่อบังคับเกรดโดยไม่ต้องผ่านการคำนวณ ratio เลย — ในทางปฏิบัติเคสนี้
    ไม่ค่อยเกิดเพราะแผนส่วนใหญ่กรอง RR/conflict ไว้ตั้งแต่ก่อนสร้างสัญญาณอยู่แล้ว (สัญญาณที่หลุดมาถึง
    จุดคำนวณ Final Score จึงผ่านเกณฑ์พื้นฐานมาแล้วเสมอ) แต่เผื่อไว้ให้ผู้เรียกในอนาคตใช้ได้ตรงๆ
    """
    if forced_grade:
        return forced_grade
    if score is None:
        return DEFAULT_GRADE_BELOW_C

    floor_score = config.get("min_score_to_alert", 45)
    if score_ceiling <= floor_score:
        return DEFAULT_GRADE_BELOW_C

    ratio = (score - floor_score) / (score_ceiling - floor_score)
    ratio = max(0.0, min(1.0, ratio))

    for grade, threshold in GRADE_RATIO_THRESHOLDS:
        if ratio >= threshold:
            return grade
    return DEFAULT_GRADE_BELOW_C


def risk_pct_for_grade(grade, config):
    """คืน Risk % ต่อไม้ตามเกรด — อ่านจาก config['risk_pct_by_grade'] (ตั้งได้ใน config.py) fallback
    เป็นตารางเริ่มต้น (DEFAULT_RISK_PCT_BY_GRADE) ถ้าไม่มีใน config เลย (กันโค้ดพังถ้า config.py
    ยังไม่ได้อัปเดตให้มี key นี้)"""
    table = config.get("risk_pct_by_grade") or DEFAULT_RISK_PCT_BY_GRADE
    return table.get(grade, 0.0)


def compute_final_score(strategy_score, config, session_info=None, news_blackout=False,
                         opposite_zone_note=None, score_ceiling=100.0):
    """
    รวม Strategy Score พื้นฐาน + ส่วนเสริมบริบท เป็น Final Trade Score (ตัวเลขเดียวกับที่ใช้คำนวณ
    เกรดใน compute_grade ด้านบน) ส่วนเสริมที่รวมเข้าไป (เป็นค่าบวก/ลบเล็กน้อยเทียบกับ Strategy Score
    เดิม ไม่ทำให้คะแนนหลุดกรอบมากเกินจริง — clamp ไว้ที่ 0 ถึง score_ceiling+10):

      - Session/Killzone: +3 ถ้าอยู่ใน Killzone (London Open/NY Open) ตอนนี้พอดี, +1 ถ้าอยู่ใน
        session ปกติแต่ไม่ใช่ Killzone, +0 ถ้านอก session (คู่เงินที่ session_filter_enabled=False
        เช่น ETHUSDT จะได้ in_session=True เสมออยู่แล้วจาก session.py — ไม่กระทบ)
      - News Risk: -5 ถ้าอยู่ในช่วง News Blackout — หมายเหตุ: Plan ที่มี Hard Block เรื่องข่าวอยู่แล้ว
        (Plan 1-8 เกือบทั้งหมด ดู news_scheduler.is_in_news_blackout) จะไม่มีทางเดินทางมาถึงจุดคำนวณ
        Final Score ตอน news_blackout=True อยู่แล้ว (ถูกกันไว้ตั้งแต่ก่อนสร้าง Alert) พารามิเตอร์นี้
        เผื่อไว้สำหรับ caller ในอนาคตที่อาจไม่มี Hard Block เรื่องข่าว
      - Opposite Zone: -5 ถ้ามีโน้ตเตือนโซนตรงข้ามใกล้ๆ ส่งเข้ามา (opposite_zone_note ไม่ None) —
        ใช้ได้กับ Plan ที่ไม่มี Hard Block เรื่องนี้อยู่แล้ว (Plan 2/4 มี Hard Block แยกต่างหากใน
        plan_runner.py ที่ skip การส่ง Alert ไปเลย ไม่ถึงจุดคำนวณ Final Score ด้วยซ้ำ — พารามิเตอร์นี้
        จึงมีผลจริงเฉพาะ Plan อื่นที่ยังไม่มี Hard Block เรื่อง Zone ตรงข้าม)

    คืน (final_score, breakdown) — breakdown เป็น list of (label, delta) เรียงตามลำดับที่บวก/ลบเข้าไป
    เพื่อใช้แสดงในข้อความ Telegram (ดู format_trade_quality_line ด้านล่าง)
    """
    breakdown = [("Strategy Score", round(float(strategy_score), 1))]
    final = float(strategy_score)

    session_info = session_info or get_session_info(config)
    if session_info.get("in_killzone"):
        final += 3
        breakdown.append(("Session (Killzone)", 3))
    elif session_info.get("in_session"):
        final += 1
        breakdown.append(("Session (ปกติ)", 1))
    else:
        breakdown.append(("Session (นอกช่วงหลัก)", 0))

    if news_blackout:
        final -= 5
        breakdown.append(("News Risk (ใกล้ข่าวแรง)", -5))
    else:
        breakdown.append(("News Risk", 0))

    if opposite_zone_note:
        final -= 5
        breakdown.append((f"Opposite Zone ({opposite_zone_note})", -5))

    final = max(0.0, min(final, score_ceiling + 10))
    return round(final, 1), breakdown


def format_trade_quality_line(final_score, score_ceiling, breakdown, grade, config):
    """สร้างข้อความ TRADE QUALITY สั้นๆ แปะท้าย Telegram Alert — โชว์เกรด + Final Score +
    breakdown แบบย่อ (เฉพาะรายการที่มีผลจริง) + Risk % ที่แนะนำตามเกรดนั้น

    หมายเหตุ: Risk % ที่โชว์ตรงนี้มีผลจริงกับ position sizing ก็ต่อเมื่อ
    config['risk_sizing_mode'] == 'grade' (ดู risk.py: calc_scaled_risk_pct()) ถ้ายังเป็นโหมดเดิม
    ('linear' หรือปิดไว้) ตัวเลขที่แสดงตรงนี้เป็นข้อมูลอ้างอิงอย่างเดียว ไม่ตรงกับที่ระบบใช้จริง —
    เพื่อลดความสับสนแนะนำตั้ง risk_sizing_mode='grade' คู่กับการเปิดใช้ฟีเจอร์นี้เสมอ
    """
    emoji = GRADE_EMOJI.get(grade, "⚪")
    risk_pct = risk_pct_for_grade(grade, config)

    lines = [f"{emoji} <b>TRADE QUALITY: {grade}</b>  (Final Score {final_score}/{score_ceiling:.0f})"]

    detail_parts = []
    for label, delta in breakdown:
        if label == "Strategy Score":
            detail_parts.append(f"{label} {delta}")
        elif delta:
            sign = "+" if delta > 0 else ""
            detail_parts.append(f"{label} {sign}{delta}")
    if detail_parts:
        lines.append(" | ".join(detail_parts))

    if grade in ("D", "F"):
        lines.append(f"⚠️ เกรดต่ำ — พิจารณาข้ามไม้นี้ หรือลด Risk ให้ต่ำที่สุด (แนะนำ: {risk_pct:.2f}%)")
    else:
        lines.append(f"แนะนำ Risk ต่อไม้นี้ตามเกรด: {risk_pct:.2f}%")

    return "\n".join(lines)
