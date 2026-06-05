#!/usr/bin/env python3
"""Generate icon.ico for WaveScout — a teal WiFi signal-bars glyph on a
rounded dark VaultSoft tile. Saves a 256px ICO (Windows downscales fine)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap, QPainter, QColor
from PyQt6.QtCore import Qt, QRectF

ACC = "#00D4AA"; BG = "#0D1117"; B0 = "#1C2B3A"


def draw(size: int) -> QPixmap:
    s = size
    pm = QPixmap(s, s); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # rounded dark tile background
    p.setBrush(QColor(BG)); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.18, s * 0.18)
    # four signal bars, ascending — all teal (full-strength brand glyph)
    bw = s * 0.13
    gap = s * 0.07
    base_y = s * 0.74
    heights = [0.16, 0.27, 0.40, 0.54]
    total_w = 4 * bw + 3 * gap
    x = (s - total_w) / 2
    for hfrac in heights:
        bh = s * hfrac
        p.setBrush(QColor(ACC)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(x, base_y - bh, bw, bh), s * 0.03, s * 0.03)
        x += bw + gap
    p.end()
    return pm


def main():
    app = QApplication(sys.argv)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    big = draw(256)
    ok = big.save(out, "ICO")
    print("wrote", out, ok, "exists:", os.path.exists(out),
          os.path.getsize(out) if os.path.exists(out) else 0, "bytes")


if __name__ == "__main__":
    main()
