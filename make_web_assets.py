#!/usr/bin/env python3
"""Generate web assets for the WaveScout landing page (docs/):
  logo.png (256), favicon-32.png (32), favicon.ico (256).
Reuses the teal WiFi signal-bars glyph on a rounded dark VaultSoft tile."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPixmap, QPainter, QColor
from PyQt6.QtCore import Qt, QRectF

ACC = "#00D4AA"; BG = "#0D1117"


def draw(size: int) -> QPixmap:
    s = size
    pm = QPixmap(s, s); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(BG)); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.18, s * 0.18)
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
    docs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    os.makedirs(docs, exist_ok=True)
    out = {
        "logo.png": draw(256),
        "favicon-32.png": draw(32),
    }
    for name, pm in out.items():
        ok = pm.save(os.path.join(docs, name))
        print("wrote", name, ok)
    ico_ok = draw(256).save(os.path.join(docs, "favicon.ico"), "ICO")
    print("wrote favicon.ico", ico_ok)


if __name__ == "__main__":
    main()
