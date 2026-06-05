#!/usr/bin/env python3
"""Generate WaveScout's brand icon — a bold teal radar sweep on a dark
squircle tile (VaultSoft house style). Renders at high resolution with Qt,
then packs a multi-resolution icon.ico via Pillow and refreshes the web
logo / favicons under docs/.
"""
import os, math, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath,
                         QRadialGradient)
from PyQt6.QtCore import Qt, QRectF, QPointF

ACC = QColor("#00D4AA")
BG = QColor("#0D1117")
WHITE = QColor("#EAFBF6")

HERE = os.path.dirname(os.path.abspath(__file__))


def _pen(w, color=ACC, cap=Qt.PenCapStyle.RoundCap):
    return QPen(color, w, Qt.PenStyle.SolidLine, cap, Qt.PenJoinStyle.RoundJoin)


def draw_radar(s: float) -> QPixmap:
    """The WaveScout mark: one bold ring + a solid scanning sweep + blip."""
    pm = QPixmap(int(s), int(s)); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    # dark squircle tile
    p.setBrush(BG); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, s, s), s*0.22, s*0.22)

    cx, cy = s/2, s/2
    R = s*0.36
    # bold outer ring
    p.setPen(_pen(s*0.030, QColor(0, 212, 170, 210)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), R, R)
    # solid sweep wedge with radial fade (bright at centre)
    p.save()
    clip = QPainterPath(); clip.addEllipse(QPointF(cx, cy), R, R); p.setClipPath(clip)
    a0, a1 = 48, 108
    path = QPainterPath(QPointF(cx, cy))
    path.arcTo(QRectF(cx-R, cy-R, 2*R, 2*R), a0, a1-a0)
    path.closeSubpath()
    rg = QRadialGradient(cx, cy, R)
    rg.setColorAt(0.0, QColor(0, 212, 170, 255))
    rg.setColorAt(1.0, QColor(0, 212, 170, 95))
    p.setBrush(QBrush(rg)); p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path)
    p.restore()
    # bright leading edge of the sweep
    a = math.radians(48)
    p.setPen(_pen(s*0.028))
    p.drawLine(QPointF(cx, cy), QPointF(cx+R*math.cos(a), cy-R*math.sin(a)))
    # white blip (a detected signal)
    p.setBrush(WHITE); p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(cx+R*0.6*math.cos(math.radians(80)),
                          cy-R*0.6*math.sin(math.radians(80))), s*0.034, s*0.034)
    # centre dot
    p.setBrush(ACC); p.drawEllipse(QPointF(cx, cy), s*0.042, s*0.042)
    p.end()
    return pm


def main():
    app = QApplication(sys.argv)
    # supersample render
    base = draw_radar(1024)
    tmp = os.path.join(tempfile.gettempdir(), "wavescout_icon_base.png")
    base.save(tmp, "PNG")

    from PIL import Image
    img = Image.open(tmp).convert("RGBA")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48),
                 (64, 64), (128, 128), (256, 256)]
    # icon.ico for the app / exe
    ico_path = os.path.join(HERE, "icon.ico")
    img.save(ico_path, format="ICO", sizes=ico_sizes)
    print("wrote", ico_path, os.path.getsize(ico_path), "bytes")

    # web assets under docs/
    docs = os.path.join(HERE, "docs")
    if os.path.isdir(docs):
        img.resize((256, 256), Image.LANCZOS).save(os.path.join(docs, "logo.png"))
        img.resize((32, 32), Image.LANCZOS).save(os.path.join(docs, "favicon-32.png"))
        img.save(os.path.join(docs, "favicon.ico"), format="ICO", sizes=ico_sizes)
        print("refreshed docs/logo.png, docs/favicon-32.png, docs/favicon.ico")


if __name__ == "__main__":
    main()
