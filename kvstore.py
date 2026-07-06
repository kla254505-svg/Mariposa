import requests

KVDB_BASE = "https://kvdb.io"


def kv_get(bucket, key):
    """อ่านค่าจาก kvdb.io ถ้าไม่มีหรือ error จะคืนค่า None"""
    try:
        resp = requests.get(f"{KVDB_BASE}/{bucket}/{key}", timeout=10)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception:
        return None


def kv_set(bucket, key, value):
    """เขียนค่าลง kvdb.io"""
    try:
        requests.post(f"{KVDB_BASE}/{bucket}/{key}", data=str(value), timeout=10)
        return True
    except Exception:
        return False
