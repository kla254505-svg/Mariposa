def print_report(symbol, timeframe, structure, entry_signal, stop_loss,
                  take_profits, position, rr, confidence, config):

    print("=" * 60)
    print(f"  TRADE SETUP: {symbol} | TF: {timeframe}")
    print("=" * 60)

    if not entry_signal["valid"]:
        print("สถานะ: ยังไม่มีจังหวะเข้าไม้ที่น่าสนใจ")
        print("-" * 60)
        for r in entry_signal["reasons"]:
            print(f" • {r}")
        print("=" * 60)
        return

    direction_th = "LONG (ซื้อ)" if entry_signal["direction"] == "bullish" else "SHORT (ขาย)"
    score = confidence["score"]
    passed = score >= config["min_score_console_watchlist"]

    print(f"ทิศทาง: {direction_th}")
    print(f"เทรนด์/Event: {structure['trend']} ({structure.get('trend_strength', '-')}) | {structure['event']}")
    print(f"คะแนนความมั่นใจ: {score}/100  -> {'ผ่านเกณฑ์' if passed else 'ต่ำกว่าเกณฑ์ แนะนำรอดูก่อน'}")
    print("-" * 60)

    print(f"Entry ที่แนะนำ : {entry_signal['entry_price']:.4f}")
    print(f"Stop Loss      : {stop_loss:.4f}")
    for name, price in take_profits.items():
        print(f"{name:<15}: {price:.4f}  (RR {rr[name]})")
    print("-" * 60)

    print(f"ขนาดโพซิชั่นแนะนำ : {position['position_size']}")
    print(f"เงินที่เสี่ยง       : {position['risk_amount']}")
    print("-" * 60)

    print("เหตุผลประกอบการวิเคราะห์:")
    for r in entry_signal["reasons"]:
        print(f" • {r}")

    print("รายละเอียดคะแนน:")
    for factor, pts in confidence["breakdown"].items():
        print(f" • {factor}: +{pts}")

    print("=" * 60)
