from datetime import datetime, timezone


def _hour_in_range(hour, start, end):
    return start <= hour < end


def get_session_info(config, now=None):
    """เช็คว่าเวลาปัจจุบัน (UTC) อยู่ใน trading session / kill zone ไหม

    ถ้า config["session_filter_enabled"] เป็น False (เช่น คู่เงินที่เทรด 24/7 อย่างคริปโต ไม่มี
    แนวคิด "session" กระจุกสภาพคล่องแบบ forex London/NY) ถือว่า "อยู่ใน session" เสมอ ไม่งั้น /status
    จะโชว์ "นอก Session ⛔" ให้สับสนทั้งที่ตลาดเปิดเทรดได้ปกติตลอดเวลาอยู่แล้ว"""
    now = now or datetime.now(timezone.utc)
    hour = now.hour

    if not config.get("session_filter_enabled", True):
        return {"utc_hour": hour, "in_session": True, "in_killzone": False}

    in_session = any(
        _hour_in_range(hour, start, end)
        for start, end in config.get("trading_sessions_utc", [])
    )
    in_killzone = any(
        _hour_in_range(hour, start, end)
        for start, end in config.get("killzones_utc", [])
    )

    return {
        "utc_hour": hour,
        "in_session": in_session,
        "in_killzone": in_killzone,
    }
