from datetime import datetime, timezone


def _hour_in_range(hour, start, end):
    return start <= hour < end


def get_session_info(config, now=None):
    """เช็คว่าเวลาปัจจุบัน (UTC) อยู่ใน trading session / kill zone ไหม"""
    now = now or datetime.now(timezone.utc)
    hour = now.hour

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
