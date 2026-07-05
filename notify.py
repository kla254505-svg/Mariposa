import requests


def send_telegram_alert(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, data=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return False


def format_alert_message(symbol, timeframe, structure, entry_signal,
                          stop_loss, take_profits, rr, confidence):
    direction_th = "LONG (ซื้อ)" if entry_signal["direction"] == "bullish" else "SHORT (ขาย)"
    lines = [
        f"🚨 <b>สัญญาณเทรด: {symbol} ({timeframe})</b>",
        f"ทิศทาง: {direction_th}",
        f"Trend/Event: {structure['trend']} | {structure['event']}",
        f"Score: {confidence['score']}/100",
        "",
        f"Entry: {entry_signal['entry_price']:.4f}",
        f"SL: {stop_loss:.4f}",
    ]
    for name, price in take_profits.items():
        lines.append(f"{name}: {price:.4f} (RR {rr[name]})")
    return "\n".join(lines)
