"""
sheets_log.py — เชื่อม Bot เข้ากับ Google Sheets (Trading_Bot_Database) เป็น "คลังข้อมูลถาวร"
แยกจาก kvdb (Runtime Memory) โดยเด็ดขาด ตามหลักการที่ตกลงกันไว้:
    KVDB    = Last Signal / Current State / Signal Hash / AI State / Cooldown (real-time, บอทอ่าน
              ทุก cron)
    Sheets  = ประวัติทั้งหมด / Signal / Context / AI Log (persistent archive, บอท "เขียน" อย่างเดียว
              ไม่เคยอ่านกลับมาใช้ตัดสินใจ — ตามหลักการ "Google Sheets ไม่ใช่ Runtime Memory")

กติกาสำคัญที่ต้องคงไว้เสมอ (เหมือนหลักการของ ai_layer.py ทุกประการ):
  - ทำงานแบบ "non-blocking" เสมอ — เขียนไม่สำเร็จ (credential ผิด, network, rate limit, ยังไม่ตั้งค่า
    environment variable ฯลฯ) ต้องไม่ทำให้ Strategy/AI/Telegram หยุดทำงานเด็ดขาด ทุกฟังก์ชัน public
    ในไฟล์นี้ห่อด้วย try/except ครบ ไม่โยน exception ออกไปเลย คืนค่า True/False บอกผลแทน
  - Signal_Log: ใช้ Signal_ID (= order["id"] เดิมจาก orders.py ตรงๆ ไม่สร้าง ID คู่ขนานใหม่) เป็น
    Primary Key — เจอแล้ว UPDATE, ไม่เจอ INSERT (append แถวใหม่) ห้ามสร้างซ้ำ
  - Signal_Context / AI_Log: APPEND อย่างเดียวเสมอ ห้าม UPDATE (เป็นข้อมูลประวัติศาสตร์ ณ เวลานั้น)
  - ไม่เชื่อมต่อ Google Sheets ใหม่ทุกครั้งที่เรียก (ช้า/เปลือง quota) — cache client ไว้ในหน่วยความจำ
    ของ process เดียว ถ้าเชื่อมพังจะมี cooldown ก่อน retry ครั้งถัดไป ไม่ยิงรัวๆ ทุก signal ที่พลาด
  - ไม่ทำให้ Strategy Logic เปลี่ยนแปลงแม้แต่นิดเดียว — ไฟล์นี้แค่ "อ่าน" ค่าจาก order dict ที่
    Strategy สร้างไว้แล้วส่งไปเขียน Sheets เท่านั้น ไม่เคยคำนวณ Entry/SL/TP ใหม่

*** แก้ไขล่าสุด: เพิ่มคอลัมน์ "Score" ใน SIGNAL_LOG_HEADERS + log_signal() ***
ของเดิม order["score"] (คำนวณไว้แล้วใน score.py) ไม่เคยถูกเขียนลง Signal_Log เลย ทำให้ไม่มีทาง
วิเคราะห์ย้อนหลังได้ว่า "คะแนนที่ระบบให้ สัมพันธ์กับผลจริงไหม" (ดู score_outcome_analysis.py ที่เพิ่ม
เข้ามาพร้อมกัน) เพิ่มคอลัมน์นี้เพื่อให้วิเคราะห์ได้ ไม่กระทบพฤติกรรมอื่นของไฟล์นี้เลย — สัญญาณเก่าที่
เคย log ไปแล้วก่อนแก้จะไม่มีค่า Score (ว่างเปล่า ไม่ error)
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone

_client = None
_spreadsheet = None
_last_conn_attempt = 0.0
_CONN_RETRY_COOLDOWN_SECONDS = 60  # เชื่อมพังรอบนึงแล้ว อย่า retry รัวๆ ทุก signal ที่เข้ามาถัดไป

SIGNAL_LOG_HEADERS = [
    "Signal_ID", "Created_Date", "Created_Time", "Timestamp", "Symbol", "Plan_ID", "Direction",
    "Timeframe", "Entry", "SL", "TP", "RR", "Score", "Signal_Status", "Entry_Status", "Entry_Time",
    "Entry_Price", "Exit_Time", "Exit_Price", "Result", "R_Multiple", "Duration", "Cancel_Reason",
    "Telegram_Message_ID", "Created_By", "Last_Updated",
]
SIGNAL_CONTEXT_HEADERS = [
    "Signal_ID", "Snapshot_Time", "Symbol",
    "HTF_Bias",
    "Trend_4H", "Trend_1H", "Trend_30M", "Trend_15M", "Trend_5M",
    "EMA50_4H", "EMA200_4H", "EMA50_1H", "EMA200_1H", "EMA50_30M", "EMA200_30M",
    "EMA50_15M", "EMA200_15M", "EMA50_5M", "EMA200_5M",
    "BOS", "CHoCH", "Liquidity_Sweep",
    "Supply_Zone", "Demand_Zone", "Order_Block",
    "Momentum", "Volume_Context",
    "Current_Price",
    "Signal_Reason", "Trigger_Condition",
]
AI_LOG_HEADERS = [
    "AI_ID", "Signal_ID", "Analysis_Time",
    "AI_Event",
    "Overall_Bias", "Signal_Assessment", "Confidence", "Risk_Level",
    "Conflict", "Reason", "Key_Observation", "Next_Event_To_Watch",
    "AI_Model", "Prompt_Version",
    "Input_Token", "Output_Token", "Total_Token",
    "AI_Status", "Error_Message",
]

STATUS_TO_SIGNAL_STATUS = {
    "pending": "WAITING_ENTRY", "running": "OPEN", "win": "TP_HIT",
    "loss": "SL_HIT", "expired": "EXPIRED",
}
STATUS_TO_RESULT = {"win": "WIN", "loss": "LOSS", "expired": "EXPIRED"}


def _now_bangkok():
    return datetime.now(timezone(timedelta(hours=7)))


def _get_spreadsheet():
    """คืน gspread Spreadsheet object (cache ไว้ใช้ซ้ำในหน่วยความจำของ process) หรือ None ถ้ายังไม่ได้
    ตั้งค่า environment variable หรือเชื่อมต่อไม่สำเร็จ — ไม่โยน exception ออกไปเลย"""
    global _client, _spreadsheet, _last_conn_attempt

    if _spreadsheet is not None:
        return _spreadsheet

    now = time.time()
    if now - _last_conn_attempt < _CONN_RETRY_COOLDOWN_SECONDS:
        return None
    _last_conn_attempt = now

    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not creds_json or not sheet_id:
        return None  # ยังไม่ได้ตั้งค่า — ปิดฟีเจอร์นี้เงียบๆ ไม่ error (เหมือน ANTHROPIC_API_KEY ว่าง)

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(sheet_id)
        _client = client
        _spreadsheet = spreadsheet
        print("[Sheets Log] เชื่อมต่อ Google Sheets สำเร็จ")
        return _spreadsheet
    except Exception as e:
        print(f"[Sheets Log] เชื่อมต่อ Google Sheets ไม่สำเร็จ: {e}")
        return None


def _plan_id_short(plan_key):
    """แปลง plan key ภายในของ orders.py (เช่น 'plan3_counter_trend') เป็น 'P3' ตามฟอร์แมตที่ Sheets ใช้
    เผื่อ plan1_pullback_early ก็ยังได้ 'P1' (ตัด suffix ตัวอักษรออก เอาแค่ตัวเลขนำหน้า)"""
    try:
        from orders import PLAN_SHORT
        short = PLAN_SHORT.get(plan_key)
        if short:
            digits = "".join(ch for ch in short if ch.isdigit())
            return f"P{digits}" if digits else plan_key
    except Exception:
        pass
    return plan_key or ""


def _direction_label(direction):
    return "LONG" if direction == "bullish" else ("SHORT" if direction == "bearish" else direction)


def log_signal(order, symbol, timeframe="15m"):
    """UPSERT ข้อมูล Signal 1 ตัวลง Signal_Log ตามสถานะปัจจุบันของ order dict — ใช้ order["id"] เป็น
    Signal_ID (Primary Key) เจอแล้ว UPDATE ทั้งแถว (Signal_Log = Current State เสมอ) ไม่เจอ INSERT
    แถวใหม่ เรียกซ้ำได้ทุกครั้งที่ order เปลี่ยนสถานะ (pending->running->win/loss ฯลฯ) อย่างปลอดภัย
    (idempotent) ไม่โยน exception ออกไปเลย คืนค่า True/False"""
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("Signal_Log")
        signal_id = order.get("id")
        if not signal_id:
            return False
        now_bkk = _now_bangkok()

        take_profits = order.get("take_profits") or {}
        tp = take_profits.get("TP1") or (next(iter(take_profits.values())) if take_profits else None)
        status = order.get("status")
        entered = status in ("running", "win", "loss")

        # R_Multiple: ใช้ convention เดียวกับ calc_stats() ใน orders.py (win = +rr_tp1, loss = -1R)
        r_multiple = None
        if status == "win":
            r_multiple = order.get("rr_tp1")
        elif status == "loss":
            r_multiple = -1.0

        row = {
            "Signal_ID": signal_id,
            "Created_Date": now_bkk.strftime("%Y-%m-%d"),
            "Created_Time": order.get("opened_at", now_bkk.strftime("%H:%M")),
            "Timestamp": order.get("created_at_iso") or now_bkk.isoformat(),
            "Symbol": symbol,
            "Plan_ID": _plan_id_short(order.get("plan")),
            "Direction": _direction_label(order.get("direction")),
            "Timeframe": timeframe,
            "Entry": order.get("entry_price"),
            "SL": order.get("stop_loss"),
            "TP": tp,
            "RR": order.get("rr_tp1"),
            # เพิ่มเข้ามาใหม่: คะแนน Confidence Score ตอนเปิดสัญญาณ (order["score"] มีอยู่แล้วตั้งแต่
            # orders.py แต่ของเดิมไม่เคยเขียนคอลัมน์นี้ลง Sheets เลย — ไม่มีอะไรให้วิเคราะห์ย้อนหลังว่า
            # คะแนนสัมพันธ์กับผลจริงไหมถ้าไม่มีคอลัมน์นี้ ดู score_outcome_analysis.py)
            "Score": order.get("score"),
            "Signal_Status": STATUS_TO_SIGNAL_STATUS.get(status, status),
            "Entry_Status": "HIT" if entered else "NOT_HIT",
            "Entry_Time": order.get("filled_at") if entered else None,
            "Entry_Price": order.get("entry_price") if entered else None,
            # หมายเหตุ: orders.py ยังไม่เก็บเวลา/ราคาที่ TP/SL ถูก trigger แบบละเอียด (เช็คจาก
            # current_price ทุก 5 นาที ไม่ tick-by-tick) — ใช้เวลาที่เพิ่ง log นี้ + ระดับ TP/SL ตามแผน
            # เป็นค่าประมาณ ไม่ใช่ราคา fill จริงเป๊ะ (ตามข้อจำกัดของระบบเช็คราคาแบบ polling ทุก 5 นาที)
            "Exit_Time": now_bkk.isoformat() if status in ("win", "loss") else None,
            "Exit_Price": (tp if status == "win" else order.get("stop_loss")) if status in ("win", "loss") else None,
            "Result": STATUS_TO_RESULT.get(status),
            "R_Multiple": r_multiple,
            "Duration": None,  # ยังไม่ทำ — orders.py ไม่เก็บ timestamp ละเอียดพอจะคำนวณตอนนี้
            "Cancel_Reason": None,
            "Telegram_Message_ID": None,  # ยังไม่ได้เชื่อม — notify.py ยังไม่ capture message_id กลับมา
            "Created_By": "BOT",
            "Last_Updated": now_bkk.isoformat(),
        }
        row_values = [row.get(h) for h in SIGNAL_LOG_HEADERS]

        cell = ws.find(str(signal_id), in_column=1)
        if cell:
            # ช่วง cell คำนวณจากจำนวนคอลัมน์จริงใน SIGNAL_LOG_HEADERS แทนที่จะ hardcode "A...Y" ไว้ตรงๆ
            # (ของเดิม hardcode Y ซึ่งพอดีกับ 25 คอลัมน์เดิม — พอเพิ่ม Score เป็น 26 คอลัมน์ ถ้ายัง
            # hardcode Y ไว้ คอลัมน์สุดท้าย (Last_Updated) จะเขียนไม่ถึง กลายเป็นข้อมูลเก่าค้างอยู่)
            last_col_letter = _col_letter(len(SIGNAL_LOG_HEADERS))
            ws.update(f"A{cell.row}:{last_col_letter}{cell.row}", [row_values], value_input_option="USER_ENTERED")
        else:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets Log] บันทึก Signal_Log ไม่สำเร็จ ({order.get('id')}): {e}")
        return False


def _col_letter(n):
    """แปลงลำดับคอลัมน์ (1-indexed) เป็นตัวอักษรคอลัมน์แบบ A1 notation ของ Google Sheets เช่น
    1->A, 26->Z, 27->AA — เขียนแทนการ hardcode ตัวอักษรตรงๆ กันพังซ้ำถ้าจำนวนคอลัมน์เปลี่ยนอีกในอนาคต"""
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def log_signal_context(signal_id, symbol, market_context):
    """APPEND Market Snapshot ณ ตอนที่ Signal ถูกสร้าง — ไม่เคย UPDATE (ประวัติศาสตร์ ห้ามเขียนทับ)
    market_context: dict รูปแบบเดียวกับที่ ai_layer.py ใช้ (main.py สร้างไว้ชุดเดียว ใช้ร่วมกันทั้ง AI
    Layer และ Sheets Log ไม่คำนวณซ้ำ) — 30M/5M ไม่มีข้อมูลจริงในระบบตอนนี้ ปล่อยว่างตามกฎ "ห้ามสร้าง
    ข้อมูลปลอม" ไม่โยน exception ออกไปเลย คืนค่า True/False"""
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("Signal_Context")
        now_bkk = _now_bangkok()
        momentum = None
        if market_context.get("rsi") is not None:
            momentum = f"RSI {market_context.get('rsi')} / MACD hist {market_context.get('macd_hist')}"

        row = {
            "Signal_ID": signal_id,
            "Snapshot_Time": now_bkk.isoformat(),
            "Symbol": symbol,
            "HTF_Bias": (market_context.get("htf_bias") or "").upper() or None,
            "Trend_4H": market_context.get("htf_bias"),
            "Trend_1H": market_context.get("trend_1h"),
            "Trend_30M": None,
            "Trend_15M": market_context.get("trend_15m"),
            "Trend_5M": None,
            "EMA50_4H": market_context.get("ema50_4h"),
            "EMA200_4H": market_context.get("ema200_4h"),
            "EMA50_1H": market_context.get("ema50_1h"),
            "EMA200_1H": market_context.get("ema200_1h"),
            "EMA50_30M": None,
            "EMA200_30M": None,
            "EMA50_15M": market_context.get("ema50_15m"),
            "EMA200_15M": market_context.get("ema200_15m"),
            "EMA50_5M": None,
            "EMA200_5M": None,
            "BOS": "yes" if market_context.get("structure_event") == "BOS" else None,
            "CHoCH": "yes" if market_context.get("structure_event") == "CHoCH" else None,
            "Liquidity_Sweep": None,
            "Supply_Zone": None,
            "Demand_Zone": None,
            "Order_Block": market_context.get("ob_fvg_note"),
            "Momentum": momentum,
            "Volume_Context": None,
            "Current_Price": market_context.get("current_price"),
            "Signal_Reason": None,
            "Trigger_Condition": None,
        }
        row_values = [row.get(h) for h in SIGNAL_CONTEXT_HEADERS]
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets Log] บันทึก Signal_Context ไม่สำเร็จ ({signal_id}): {e}")
        return False


def log_ai_analysis(signal_ids, events, ai_result, ai_model, ai_status="SUCCESS", error_message=None):
    """APPEND ผลวิเคราะห์ของ Central AI Layer 1 ครั้ง — ผูกกับหลาย Signal_ID พร้อมกันได้ (เพราะ AI เห็น
    หลายแผน active พร้อมกันในการเรียกครั้งเดียว ตาม Central AI Layer) เขียน 1 แถวต่อ 1 Signal_ID ที่
    เกี่ยวข้อง (AI_ID ไม่ซ้ำ เป็นตัวผูกว่ามาจากการเรียก AI ครั้งเดียวกัน, Signal_ID ซ้ำกันได้ใน Sheet นี้)
    ไม่โยน exception ออกไปเลย คืนค่า True/False"""
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("AI_Log")
        now_bkk = _now_bangkok()
        ai_result = ai_result or {}
        ids = signal_ids or [None]
        ai_id_base = f"AI-{now_bkk.strftime('%Y%m%d-%H%M%S')}"

        rows = []
        for idx, sid in enumerate(ids):
            ai_id = f"{ai_id_base}-{idx}" if len(ids) > 1 else ai_id_base
            row = {
                "AI_ID": ai_id,
                "Signal_ID": sid,
                "Analysis_Time": now_bkk.isoformat(),
                "AI_Event": ", ".join(sorted(events)) if events else None,
                "Overall_Bias": ai_result.get("overall_bias"),
                "Signal_Assessment": ai_result.get("signal_assessment"),
                "Confidence": ai_result.get("confidence"),
                "Risk_Level": ai_result.get("risk_level"),
                "Conflict": ai_result.get("conflict"),
                "Reason": ai_result.get("reason"),
                "Key_Observation": ai_result.get("key_observation"),
                "Next_Event_To_Watch": ai_result.get("next_event_to_watch"),
                "AI_Model": ai_model,
                "Prompt_Version": "AI_PROMPT_V1",
                "Input_Token": None,
                "Output_Token": None,
                "Total_Token": None,
                "AI_Status": ai_status,
                "Error_Message": error_message,
            }
            rows.append([row.get(h) for h in AI_LOG_HEADERS])

        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[Sheets Log] บันทึก AI_Log ไม่สำเร็จ: {e}")
        return False


def test_sheets_connection():
    """ทดสอบว่าตั้งค่า Google Sheets ถูกต้องและเขียนได้จริงไหม (ใช้โดยคำสั่ง /sheetscheck) — ทำจริง
    ไม่ใช่แค่เช็คว่า credential parse ได้ ต้องลองเปิด Spreadsheet + เห็น worksheet ครบ 3 อันจริงๆ
    คืนค่า (ok: bool, message: str) ไม่โยน exception เด็ดขาด

    บอกสาเหตุที่พบบ่อยแยกเป็นข้อความต่างกันชัดเจน (env var หาย / JSON parse ไม่ได้ / share สิทธิ์ไม่ครบ /
    Sheet ID ผิด / worksheet name ไม่ตรง) แทนที่จะโยน error ดิบๆ ให้อ่านยาก"""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()

    if not creds_json:
        return False, "ยังไม่ได้ตั้งค่า GOOGLE_SHEETS_CREDENTIALS_JSON บน Render (Environment Variables)"
    if not sheet_id:
        return False, "ยังไม่ได้ตั้งค่า GOOGLE_SHEET_ID บน Render (Environment Variables)"

    try:
        creds_dict = json.loads(creds_json)
    except json.JSONDecodeError as e:
        return False, f"GOOGLE_SHEETS_CREDENTIALS_JSON ไม่ใช่ JSON ที่ถูกต้อง (วางไฟล์มาไม่ครบ/เพี้ยน?): {e}"

    client_email = creds_dict.get("client_email", "(ไม่พบ client_email ใน JSON)")

    # บังคับลองเชื่อมใหม่ (ไม่ใช้ cache) เพื่อให้ /sheetscheck สะท้อนสถานะจริง ณ ตอนนี้เสมอ
    global _spreadsheet, _client, _last_conn_attempt
    _spreadsheet = None
    _client = None
    _last_conn_attempt = 0.0

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
    except Exception as e:
        return False, f"สร้าง credential ไม่สำเร็จ (ตรวจ private_key ในไฟล์ JSON ว่าคัดลอกมาครบไหม): {e}"

    try:
        spreadsheet = client.open_by_key(sheet_id)
    except Exception as e:
        return False, (
            f"เปิด Google Sheet ไม่สำเร็จ (Sheet ID ผิด หรือยังไม่ได้ Share สิทธิ์ Editor ให้ "
            f"{client_email} — เช็คได้ที่ปุ่ม Share บน Google Sheet): {e}"
        )

    try:
        sheet_names = [ws.title for ws in spreadsheet.worksheets()]
    except Exception as e:
        return False, f"เปิด Sheet ได้ แต่อ่านรายชื่อ worksheet ไม่สำเร็จ: {e}"

    required = {"Signal_Log", "Signal_Context", "AI_Log"}
    missing = required - set(sheet_names)
    if missing:
        return False, (
            f"เชื่อมต่อสำเร็จ แต่หา worksheet ไม่เจอ: {', '.join(missing)} "
            f"(เจอจริง: {', '.join(sheet_names)}) — ชื่อ Sheet ต้องตรงเป๊ะ ตัวพิมพ์เล็ก-ใหญ่มีผล"
        )

    _spreadsheet = spreadsheet
    _client = client
    return True, f"เชื่อมต่อสำเร็จ ({client_email}) — เจอ worksheet ครบ: {', '.join(sheet_names)}"
