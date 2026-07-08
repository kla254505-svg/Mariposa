"""
chart.py
สร้างกราฟแท่งเทียนพร้อม mark Order Block / FVG / Structure Zone / Swing point
จากข้อมูลราคาที่ pipeline ดึงมาอยู่แล้ว (ไม่เรียก API ภายนอกเพิ่ม)

หมายเหตุ: fetch_twelvedata() คืนแค่คอลัมน์ OHLCV ไม่มี timestamp จริงติดมาด้วย
(ตัดทิ้งไปตั้งแต่ fetch_data.py เพื่อความง่ายของ pipeline เดิม) แกน X ของกราฟนี้
เลยนับเป็น "ลำดับแท่งเทียน" ไม่ใช่เวลานาฬิกาจริง — เพียงพอสำหรับดูรูปทรง/บริบทราคาล่าสุด
"""

import matplotlib
matplotlib.use("Agg")  # ไม่มีหน้าจอบน GitHub Actions ต้องสั่ง backend นี้ก่อน import pyplot
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


def build_entry_chart(df, entry_signal=None, structure=None, lookback=80, out_path="/tmp/mariposa_chart.png"):
    """
    วาดกราฟแท่งเทียน N แท่งล่าสุด + โซนที่ entry_signal เจอ (OB/FVG/Structure zone)
    + จุด swing high/low ล่าสุดจาก structure คืนค่า path ไฟล์ .png ที่เซฟไว้
    """
    plot_df = df.tail(lookback)
    index_positions = {orig_idx: pos for pos, orig_idx in enumerate(plot_df.index)}
    n = len(plot_df)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=140)

    # --- แท่งเทียน ---
    for pos, (_, row) in enumerate(plot_df.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax.plot([pos, pos], [row["low"], row["high"]], color=color, linewidth=1)
        body_bottom = min(row["open"], row["close"])
        body_height = abs(row["close"] - row["open"]) or (row["high"] - row["low"]) * 0.01
        ax.add_patch(Rectangle((pos - 0.3, body_bottom), 0.6, body_height, color=color))

    # --- Order Block / FVG / Structure Zone ที่ entry_signal เจอ ---
    if entry_signal:
        zone_specs = [
            (entry_signal.get("ob"), "#2962ff", "OB"),
            (entry_signal.get("fvg"), "#ff9800", "FVG"),
            (entry_signal.get("structure_zone"), "#ab47bc", "Structure"),
        ]
        for zone, color, label in zone_specs:
            if zone:
                ax.axhspan(zone["bottom"], zone["top"], color=color, alpha=0.15)
                ax.axhline(zone["top"], color=color, linewidth=0.8, linestyle="--")
                ax.axhline(zone["bottom"], color=color, linewidth=0.8, linestyle="--")
                ax.text(n - 1, zone["top"], f" {label}", color=color, fontsize=8, va="bottom")

    # --- Swing point ล่าสุด (จุดฐานของ BOS/CHoCH) ---
    if structure:
        for p in structure.get("last_swings", []):
            pos = index_positions.get(p["index"])
            if pos is not None:
                marker = "v" if p["type"] == "high" else "^"
                ax.scatter(pos, p["price"], marker=marker, color="black", s=35, zorder=5)

    # --- เส้นราคาปัจจุบัน ---
    last_price = plot_df["close"].iloc[-1]
    ax.axhline(last_price, color="gray", linewidth=0.8, linestyle=":")
    ax.text(n - 1, last_price, f" {last_price:.2f}", color="gray", fontsize=8, va="center")

    ax.set_xlim(-1, n + 6)  # เผื่อที่ด้านขวาให้ label โซน/ราคาไม่ล้นขอบ
    ax.set_xticks([])
    # หมายเหตุ: ใช้ label เป็นภาษาอังกฤษล้วนบนรูปภาพ เพราะ font เริ่มต้นของ matplotlib (DejaVu Sans)
    # บน GitHub Actions ไม่รองรับตัวอักษรไทย จะเรอเป็นกล่องว่างถ้าใส่ข้อความไทยลงในรูป
    # ส่วนข้อความไทยเต็มๆ อยู่ใน caption ที่ส่งคู่กับรูปใน Telegram อยู่แล้ว (ไม่ใช่ในรูป)
    ax.set_title("Mariposa - Recent Price Structure (X axis = candle order, not clock time)", fontsize=9)
    ax.set_ylabel("Price")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path
