from datetime import datetime, timezone

from kvstore import kv_get, kv_set


def is_in_cooldown(bucket, symbol, direction, cooldown_minutes):
    """
    เช็คว่าทิศทางนี้ (bullish/bearish) ของ symbol นี้ เพิ่งส่ง Alert ไปเมื่อไม่นานมานี้รึเปล่า
    ใช้เวลาจริง (นาที) แทน 'จำนวนแท่งเทียน' เพราะ GitHub Actions รันแบบ stateless
    ไม่มี concept ของ 'bar_index' ข้ามรันเหมือน Pine Script
    """
    key = f"last_alert_{symbol}_{direction}"
    raw = kv_get(bucket, key)
    if not raw:
        return False
    try:
        last_time = datetime.fromisoformat(raw)
    except Exception:
        return False

    elapsed_minutes = (datetime.now(timezone.utc) - last_time).total_seconds() / 60.0
    return elapsed_minutes < cooldown_minutes


def mark_alert_sent(bucket, symbol, direction):
    """บันทึกเวลาที่เพิ่งส่ง Alert ไป เพื่อใช้เช็ค cooldown ในรอบถัดไป"""
    key = f"last_alert_{symbol}_{direction}"
    kv_set(bucket, key, datetime.now(timezone.utc).isoformat())
