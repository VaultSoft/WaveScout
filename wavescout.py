#!/usr/bin/env python3
"""WaveScout - Free Portable Windows WiFi Analyser (VaultSoft).

Scans nearby WiFi networks via the built-in Windows ``netsh wlan`` commands
(no third-party drivers, no admin rights for basic scanning). Shows a live
network table, channel congestion view, a rolling signal graph and your
current connection details. Minimises to the system tray.

Note: Windows 10/11 require *Location services* to be ON for any app to read
WLAN scan results. WaveScout detects when that is off and shows a clean,
actionable message rather than failing silently.
"""
from __future__ import annotations
import sys
import os
import re
import platform
import socket
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, List, Dict, Deque, Tuple

from app_metadata import APP_NAME, APP_VERSION, PUBLISHER
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QSystemTrayIcon, QMenu,
    QSizePolicy, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QTabWidget, QStyledItemDelegate, QStyleOptionViewItem, QAbstractItemView,
)
from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, QSize
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QIcon, QPixmap, QAction,
    QPainterPath, QRadialGradient,
)
import math as _math

REFRESH_MS  = 5_000          # rescan every 5 seconds
GRAPH_SECS  = 60             # rolling signal history window

_IS_WIN = platform.system() == "Windows"
# Hide the console window that subprocess would otherwise flash in a GUI app.
_NO_WINDOW = 0x08000000 if _IS_WIN else 0

# ══════════════════════════════════════════════════════════════════
# THEME  (matches PulseMonitor / BatteryVault — the VaultSoft brand)
# ══════════════════════════════════════════════════════════════════
C = {
    "bg0":   "#060A10",
    "bg1":   "#0D1117",
    "bg2":   "#131A23",
    "bg3":   "#1B2535",
    "b0":    "#1C2B3A",
    "b1":    "#263848",
    "acc":   "#00D4AA",   # teal accent
    "acc2":  "#007A60",
    "t1":    "#E2EAF4",
    "t2":    "#a0aec0",
    "t3":    "#617080",
    "title": "#ffffff",
    "grn":   "#2ECC71",
    "amb":   "#F0A500",
    "red":   "#E74C3C",
}


# ══════════════════════════════════════════════════════════════════
# DATA MODEL
# ══════════════════════════════════════════════════════════════════
@dataclass
class Network:
    ssid:      str
    bssid:     str            = ""
    signal_pct: int           = 0
    channel:   Optional[int]  = None
    security:  str            = "Open"
    radio:     str            = ""          # e.g. "802.11ax"

    @property
    def dbm(self) -> int:
        """Estimate RSSI in dBm from Windows signal quality (0-100%).
        Microsoft maps 0% -> -100 dBm and 100% -> -50 dBm linearly."""
        return int(self.signal_pct / 2) - 100

    @property
    def band(self) -> str:
        ch = self.channel
        if ch is None:
            return "—"
        if 1 <= ch <= 14:
            return "2.4 GHz"
        if 32 <= ch <= 177:
            return "5 GHz"
        if ch >= 178:
            return "6 GHz"
        return "—"

    @property
    def is_24(self) -> bool:
        return self.channel is not None and 1 <= self.channel <= 14


@dataclass
class Connection:
    connected: bool          = False
    ssid:      str           = ""
    bssid:     str           = ""
    signal_pct: int          = 0
    channel:   Optional[int] = None
    radio:     str           = ""
    security:  str           = ""
    ip:        str           = "N/A"
    gateway:   str           = "N/A"
    dns:       str           = "N/A"

    @property
    def band(self) -> str:
        ch = self.channel
        if ch is None:
            return "—"
        if 1 <= ch <= 14:
            return "2.4 GHz"
        if 32 <= ch <= 177:
            return "5 GHz"
        if ch >= 178:
            return "6 GHz"
        return "—"

    @property
    def dbm(self) -> int:
        return int(self.signal_pct / 2) - 100


class ScanError:
    """Lightweight enum of why a scan produced no data."""
    NONE       = "none"
    NO_ADAPTER = "no_adapter"     # no WLAN interface present
    LOCATION   = "location"       # Location services disabled (Win 10/11)
    NETSH      = "netsh"          # netsh missing / failed unexpectedly
    EMPTY      = "empty"          # adapter present but nothing found


def signal_band(pct: int) -> Tuple[str, str]:
    """Return (label, colour) for a signal percentage."""
    if pct >= 75: return ("Excellent", C["acc"])
    if pct >= 50: return ("Good", C["grn"])
    if pct >= 30: return ("Fair", C["amb"])
    return ("Poor", C["red"])


def signal_color(pct: int) -> str:
    return signal_band(pct)[1]


def sec_short(auth: str) -> str:
    """Normalise a netsh Authentication string to a short label."""
    a = (auth or "").lower()
    if not a or "open" in a:
        return "Open"
    if "wpa3" in a:
        return "WPA3"
    if "wpa2" in a:
        return "WPA2"
    if "wpa" in a:
        return "WPA"
    if "wep" in a:
        return "WEP"
    if "802.1x" in a or "enterprise" in a:
        return "Enterprise"
    return auth.strip()


# ══════════════════════════════════════════════════════════════════
# WIFI SCANNER  (netsh wlan — fully error-tolerant, no admin needed)
# ══════════════════════════════════════════════════════════════════
class WiFiScanner:
    """Runs ``netsh wlan`` and parses its (locale-labelled) output.

    Every command is guarded; failures degrade to a ScanError state and an
    empty list rather than raising. Label parsing is tolerant: we match on the
    English labels Windows uses, falling back to value-shape heuristics
    (e.g. a ``xx:xx:..`` token is a BSSID) so minor locale drift still works.
    """

    _BSSID_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.I)

    def _run(self, args: List[str]) -> Tuple[str, int]:
        try:
            p = subprocess.run(
                ["netsh"] + args,
                capture_output=True, text=True, timeout=15,
                creationflags=_NO_WINDOW,
                encoding="utf-8", errors="replace",
            )
            return (p.stdout or "") + (p.stderr or ""), p.returncode
        except FileNotFoundError:
            return "", -1          # netsh not on PATH
        except Exception as exc:
            _write_runtime_note_once("netsh-run", f"netsh command failed: {exc}")
            return "", -2

    # ── helpers ───────────────────────────────────────────────────
    @staticmethod
    def _val(line: str) -> str:
        """Right-hand side of a 'Label : value' line."""
        return line.split(":", 1)[1].strip() if ":" in line else ""

    @staticmethod
    def _location_blocked(text: str) -> bool:
        t = text.lower()
        return ("location permission" in t or "location services" in t
                or "ms-settings:privacy-location" in t)

    # ── adapter / connection state ────────────────────────────────
    def read_connection(self) -> Tuple[Connection, str]:
        out, rc = self._run(["wlan", "show", "interfaces"])
        if rc == -1:
            return Connection(), ScanError.NETSH
        if self._location_blocked(out):
            return Connection(), ScanError.LOCATION
        low = out.lower()
        if "there is no wireless interface" in low or "no wireless interface" in low:
            return Connection(), ScanError.NO_ADAPTER
        if rc != 0:
            _write_runtime_note_once("netsh-interfaces", out.strip() or f"netsh returned {rc}")
            return Connection(), ScanError.NETSH
        if "1 interface" not in low and "interface on the system" not in low \
                and "name" not in low:
            # netsh ran but produced nothing useful
            return Connection(), ScanError.NO_ADAPTER

        c = Connection()
        state = ""
        for raw in out.splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            label = line.split(":", 1)[0].strip().lower()
            val = self._val(line)
            if label == "state":
                state = val.lower()
            elif label == "ssid":            # comes before BSSID
                if c.ssid == "":
                    c.ssid = val
            elif label == "bssid":
                m = self._BSSID_RE.search(val)
                c.bssid = m.group(1).upper() if m else val
            elif label == "signal":
                c.signal_pct = _pct(val)
            elif label == "channel":
                c.channel = _int(val)
            elif label in ("radio type", "radio"):
                c.radio = val
            elif label == "authentication":
                c.security = sec_short(val)
        c.connected = state == "connected" and bool(c.ssid)
        if c.connected:
            self._fill_ip(c)
        return c, ScanError.NONE

    def _fill_ip(self, c: Connection):
        # Local IP via a dummy UDP socket (no traffic actually sent).
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            c.ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        # Gateway + DNS via ipconfig /all — parse the active adapter block
        # tolerantly (IPv4 dotted quads only, English labels).
        try:
            p = subprocess.run(
                ["ipconfig", "/all"], capture_output=True, text=True,
                timeout=10, creationflags=_NO_WINDOW,
                encoding="utf-8", errors="replace")
            out = p.stdout or ""
            gw = _first_ipv4_after(out, "Default Gateway")
            dns = _first_ipv4_after(out, "DNS Servers")
            if gw:
                c.gateway = gw
            if dns:
                c.dns = dns
        except Exception:
            pass

    # ── full network list ─────────────────────────────────────────
    def scan(self) -> Tuple[List[Network], str]:
        out, rc = self._run(["wlan", "show", "networks", "mode=bssid"])
        if rc == -1:
            return [], ScanError.NETSH
        if self._location_blocked(out):
            return [], ScanError.LOCATION
        low = out.lower()
        if "there is no wireless interface" in low or "no wireless interface" in low:
            return [], ScanError.NO_ADAPTER
        if rc != 0:
            _write_runtime_note_once("netsh-networks", out.strip() or f"netsh returned {rc}")
            return [], ScanError.NETSH
        nets = self._parse_networks(out)
        if not nets:
            return [], ScanError.EMPTY
        return nets, ScanError.NONE

    def _parse_networks(self, out: str) -> List[Network]:
        """Parse 'netsh wlan show networks mode=bssid'.

        The block is hierarchical: each 'SSID N : name' starts a network, with
        one or more 'BSSID N : mac' sub-blocks each having Signal/Channel/Radio.
        Each BSSID becomes its own row (an AP can broadcast on several)."""
        nets: List[Network] = []
        cur_ssid = None
        cur_auth = "Open"
        cur: Optional[Network] = None

        for raw in out.splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            label = line.split(":", 1)[0].strip().lower()
            val = WiFiScanner._val(line)

            if label.startswith("ssid") and not label.startswith("bssid"):
                cur_ssid = val if val else "(hidden)"
                cur_auth = "Open"          # reset; auth lines follow per-SSID
                cur = None
            elif label == "authentication":
                cur_auth = sec_short(val)
                if cur is not None:
                    cur.security = cur_auth
            elif label.startswith("bssid"):
                m = self._BSSID_RE.search(val)
                bssid = m.group(1).upper() if m else val
                cur = Network(ssid=cur_ssid or "(hidden)", bssid=bssid,
                              security=cur_auth)
                nets.append(cur)
            elif label == "signal" and cur is not None:
                cur.signal_pct = _pct(val)
            elif label == "channel" and cur is not None:
                cur.channel = _int(val)
            elif label in ("radio type", "radio") and cur is not None:
                cur.radio = val
        return nets


# ══════════════════════════════════════════════════════════════════
# PARSE HELPERS
# ══════════════════════════════════════════════════════════════════
def _int(v) -> Optional[int]:
    try:
        m = re.search(r"-?\d+", str(v))
        return int(m.group()) if m else None
    except Exception:
        return None


def _pct(v) -> int:
    n = _int(v)
    return max(0, min(100, n)) if n is not None else 0


_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _first_ipv4_after(text: str, label: str) -> Optional[str]:
    """Find the first IPv4 address on (or just after) a labelled line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if label.lower() in line.lower():
            m = _IPV4_RE.search(line)
            if m and m.group(1) != "0.0.0.0":
                return m.group(1)
            # value sometimes wraps onto the following indented line
            if i + 1 < len(lines):
                m2 = _IPV4_RE.search(lines[i + 1])
                if m2 and m2.group(1) != "0.0.0.0":
                    return m2.group(1)
    return None


def fmt_ch(ch: Optional[int]) -> str:
    return str(ch) if ch is not None else "—"


# ══════════════════════════════════════════════════════════════════
# SIGNAL-BAR TABLE DELEGATE  (paints the Signal % column as a bar)
# ══════════════════════════════════════════════════════════════════
class SignalBarDelegate(QStyledItemDelegate):
    """Renders an integer 0-100 stored in Qt.UserRole as a coloured bar."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        pct = index.data(Qt.ItemDataRole.UserRole)
        if pct is None:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(8, 9, -8, -9)
        # track
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C["b0"]))
        painter.drawRoundedRect(QRectF(r), 4, 4)
        # fill
        frac = max(0.0, min(1.0, pct / 100.0))
        if frac > 0:
            fr = QRectF(r.x(), r.y(), r.width() * frac, r.height())
            painter.setBrush(QColor(signal_color(int(pct))))
            painter.drawRoundedRect(fr, 4, 4)
        # label
        painter.setPen(QColor(C["t1"]))
        f = QFont("Segoe UI"); f.setPointSize(8); f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, f"{int(pct)}%")
        painter.restore()


# ══════════════════════════════════════════════════════════════════
# CHANNEL CONGESTION CHART  (custom-painted bar chart per band)
# ══════════════════════════════════════════════════════════════════
class ChannelChart(QWidget):
    """Bar chart of network count per channel for one band. Highlights the
    least-congested channel in teal."""

    def __init__(self, channels: List[int], title: str):
        super().__init__()
        self._channels = channels
        self._title = title
        self._counts: Dict[int, int] = {c: 0 for c in channels}
        self._best: Optional[int] = None
        self.setMinimumHeight(210)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_data(self, nets: List[Network]):
        self._counts = {c: 0 for c in self._channels}
        for n in nets:
            if n.channel in self._counts:
                self._counts[n.channel] += 1
        # best = lowest count (ties -> lowest channel number)
        self._best = min(self._channels, key=lambda c: (self._counts[c], c)) \
            if self._channels else None
        self.update()

    def best_channel(self) -> Optional[int]:
        return self._best

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 12, 12, 30, 26
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        n = len(self._channels)
        if n == 0 or plot_w <= 0:
            p.end(); return

        # title
        p.setPen(QColor(C["t2"]))
        f = QFont("Segoe UI"); f.setPointSize(9); f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(pad_l, 4, plot_w, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._title)

        max_count = max(1, max(self._counts.values()) if self._counts else 1)
        slot = plot_w / n
        bar_w = min(slot * 0.66, 46)
        base_y = pad_t + plot_h

        small = QFont("Segoe UI"); small.setPointSize(7)
        for i, ch in enumerate(self._channels):
            cx = pad_l + slot * (i + 0.5)
            cnt = self._counts[ch]
            bh = (cnt / max_count) * plot_h if cnt else 0
            x = cx - bar_w / 2
            is_best = (ch == self._best)
            # bar
            if cnt > 0:
                col = QColor(C["acc"]) if is_best else QColor(C["b1"])
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(col)
                p.drawRoundedRect(QRectF(x, base_y - bh, bar_w, bh), 3, 3)
                # count above bar
                p.setPen(QColor(C["t1"])); p.setFont(small)
                p.drawText(QRectF(cx - slot/2, base_y - bh - 16, slot, 14),
                           Qt.AlignmentFlag.AlignCenter, str(cnt))
            elif is_best:
                # empty + best: a thin teal marker so it's still highlighted
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(C["acc2"]))
                p.drawRoundedRect(QRectF(x, base_y - 4, bar_w, 4), 2, 2)
            # channel label
            p.setPen(QColor(C["acc"] if is_best else C["t3"]))
            p.setFont(small)
            p.drawText(QRectF(cx - slot/2, base_y + 4, slot, 16),
                       Qt.AlignmentFlag.AlignCenter, str(ch))
        # baseline
        p.setPen(QPen(QColor(C["b0"]), 1))
        p.drawLine(int(pad_l), int(base_y), int(pad_l + plot_w), int(base_y))
        p.end()


# ══════════════════════════════════════════════════════════════════
# SIGNAL HISTORY GRAPH  (rolling 60 s line for the selected network)
# ══════════════════════════════════════════════════════════════════
class SignalGraph(QWidget):
    def __init__(self):
        super().__init__()
        self._hist: Deque[Optional[int]] = deque(maxlen=GRAPH_SECS)
        self._label = "No network selected"
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_label(self, text: str):
        self._label = text

    def push(self, pct: Optional[int]):
        self._hist.append(pct)
        self.update()

    def clear(self):
        self._hist.clear()
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 40, 14, 26, 22
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 0 or plot_h <= 0:
            p.end(); return

        # title
        p.setPen(QColor(C["t2"]))
        f = QFont("Segoe UI"); f.setPointSize(9); f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(pad_l, 2, plot_w, 18),
                   Qt.AlignmentFlag.AlignLeft, self._label)

        # gridlines + y labels (0/25/50/75/100 %)
        gf = QFont("Segoe UI"); gf.setPointSize(7); p.setFont(gf)
        for q in (0, 25, 50, 75, 100):
            y = pad_t + plot_h * (1 - q / 100.0)
            p.setPen(QPen(QColor(C["b0"]), 1, Qt.PenStyle.DotLine))
            p.drawLine(int(pad_l), int(y), int(pad_l + plot_w), int(y))
            p.setPen(QColor(C["t3"]))
            p.drawText(QRectF(0, y - 8, pad_l - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{q}")

        data = list(self._hist)
        # x position for sample i (right-aligned: newest at right edge)
        def xy(i, val):
            n = GRAPH_SECS
            x = pad_l + plot_w * (i / max(1, n - 1))
            y = pad_t + plot_h * (1 - max(0, min(100, val)) / 100.0)
            return x, y

        # build path over the trailing samples
        pts = []
        offset = GRAPH_SECS - len(data)
        for i, val in enumerate(data):
            if val is None:
                pts.append(None)
            else:
                pts.append(xy(offset + i, val))

        # area + line
        line = QPainterPath()
        started = False
        last_pt = None
        seg_start = None
        for pt in pts:
            if pt is None:
                started = False
                continue
            if not started:
                line.moveTo(*pt)
                started = True
                if seg_start is None:
                    seg_start = pt
            else:
                line.lineTo(*pt)
            last_pt = pt

        if last_pt is not None and seg_start is not None:
            # filled area under the curve
            area = QPainterPath(line)
            area.lineTo(last_pt[0], pad_t + plot_h)
            area.lineTo(seg_start[0], pad_t + plot_h)
            area.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 212, 170, 38))
            p.drawPath(area)
            # line
            p.setPen(QPen(QColor(C["acc"]), 2,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(line)
            # current value dot + readout
            p.setBrush(QColor(C["acc"])); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPoint(int(last_pt[0]), int(last_pt[1])), 3, 3)
            if data and data[-1] is not None:
                p.setPen(QColor(C["t1"]))
                vf = QFont("Segoe UI"); vf.setPointSize(9)
                vf.setWeight(QFont.Weight.Bold); p.setFont(vf)
                p.drawText(QRectF(pad_l, pad_t, plot_w, 16),
                           Qt.AlignmentFlag.AlignRight, f"{data[-1]}%")
        else:
            p.setPen(QColor(C["t3"]))
            p.setFont(f)
            p.drawText(QRectF(pad_l, pad_t, plot_w, plot_h),
                       Qt.AlignmentFlag.AlignCenter,
                       "Waiting for signal data…")
        # x-axis caption
        p.setPen(QColor(C["t3"])); p.setFont(gf)
        p.drawText(QRectF(pad_l, pad_t + plot_h + 4, plot_w, 16),
                   Qt.AlignmentFlag.AlignLeft, f"-{GRAPH_SECS}s")
        p.drawText(QRectF(pad_l, pad_t + plot_h + 4, plot_w, 16),
                   Qt.AlignmentFlag.AlignRight, "now")
        p.end()


# ══════════════════════════════════════════════════════════════════
# SMALL CARD  (label + value, used in connection panel)
# ══════════════════════════════════════════════════════════════════
class Stat(QFrame):
    def __init__(self, title: str, accent: str = None):
        super().__init__()
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(self); lay.setContentsMargins(13, 9, 13, 9); lay.setSpacing(3)
        self._title = QLabel(title.upper())
        self._title.setStyleSheet(
            f"color:{C['t2']}; font-size:9px; font-weight:700; letter-spacing:1.1px;"
            "background:transparent; border:none;")
        self._val = QLabel("N/A")
        self._accent = accent or C["t1"]
        self._val.setStyleSheet(
            f"color:{self._accent}; font-size:15px; font-weight:700;"
            "background:transparent; border:none;")
        lay.addWidget(self._title); lay.addWidget(self._val)

    def set_value(self, text: str, color: str = None):
        self._val.setText(text)
        self._val.setStyleSheet(
            f"color:{color or self._accent}; font-size:15px; font-weight:700;"
            "background:transparent; border:none;")


# ══════════════════════════════════════════════════════════════════
# TITLE BAR  (frameless, draggable, brand)
# ══════════════════════════════════════════════════════════════════
class TitleBar(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self._win = win
        self._drag: Optional[QPoint] = None
        self.setFixedHeight(46)
        self.setStyleSheet(f"background:{C['bg0']}; border-bottom:1px solid {C['b0']};")
        lay = QHBoxLayout(self); lay.setContentsMargins(16, 0, 8, 0); lay.setSpacing(8)

        dot = QLabel()
        dot.setPixmap(make_brand_pixmap(24, tile=False))
        dot.setFixedSize(24, 24)
        dot.setStyleSheet("background:transparent;")
        brand = QLabel("WAVE<span style='color:%s'>SCOUT</span>" % C["acc"])
        brand.setTextFormat(Qt.TextFormat.RichText)
        brand.setStyleSheet(
            f"color:{C['title']}; font-size:14px; font-weight:900;"
            "letter-spacing:2px; background:transparent;")
        ver = QLabel(f"v{APP_VERSION}")
        ver.setStyleSheet(f"color:{C['t3']}; font-size:11px; margin-left:4px; background:transparent;")
        lay.addWidget(dot); lay.addWidget(brand); lay.addWidget(ver)
        lay.addStretch(1)

        for txt, cb, hover in (("—", win.showMinimized, C["bg3"]),
                               ("✕", win.close, C["red"])):
            b = QPushButton(txt)
            b.setFixedSize(34, 30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(cb)
            b.setStyleSheet(
                f"QPushButton{{color:{C['t2']}; background:transparent; border:none;"
                f"font-size:13px; border-radius:6px;}}"
                f"QPushButton:hover{{background:{hover}; color:#fff;}}")
            lay.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


# ══════════════════════════════════════════════════════════════════
# CONNECTION PANEL  (your current connection, pinned at top)
# ══════════════════════════════════════════════════════════════════
class ConnectionPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        lay = QVBoxLayout(self); lay.setContentsMargins(16, 12, 16, 12); lay.setSpacing(10)

        head = QHBoxLayout(); head.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color:{C['t3']}; font-size:13px; background:transparent;")
        self._ssid = QLabel("Not connected")
        self._ssid.setStyleSheet(
            f"color:{C['title']}; font-size:16px; font-weight:800; background:transparent;")
        self._sub = QLabel("")
        self._sub.setStyleSheet(f"color:{C['t2']}; font-size:11px; background:transparent;")
        head.addWidget(self._dot); head.addWidget(self._ssid)
        head.addSpacing(6); head.addWidget(self._sub); head.addStretch(1)
        lay.addLayout(head)

        grid = QGridLayout(); grid.setHorizontalSpacing(10); grid.setVerticalSpacing(10)
        self._cards: Dict[str, Stat] = {
            "signal":  Stat("Signal", C["acc"]),
            "channel": Stat("Channel"),
            "band":    Stat("Band"),
            "security": Stat("Security"),
            "ip":      Stat("IP Address"),
            "gateway": Stat("Gateway"),
            "dns":     Stat("DNS"),
            "bssid":   Stat("BSSID (AP)"),
        }
        order = ["signal", "channel", "band", "security",
                 "ip", "gateway", "dns", "bssid"]
        for i, k in enumerate(order):
            grid.addWidget(self._cards[k], i // 4, i % 4)
        lay.addLayout(grid)

    def update_conn(self, c: Connection):
        if c.connected:
            self._dot.setStyleSheet(
                f"color:{C['acc']}; font-size:13px; background:transparent;")
            self._ssid.setText(c.ssid or "(unknown)")
            band, col = signal_band(c.signal_pct)
            self._sub.setText(f"{band} · {c.signal_pct}% · {c.dbm} dBm"
                              + (f" · {c.radio}" if c.radio else ""))
            self._cards["signal"].set_value(f"{c.signal_pct}%  ({c.dbm} dBm)", col)
            self._cards["channel"].set_value(fmt_ch(c.channel))
            self._cards["band"].set_value(c.band)
            self._cards["security"].set_value(c.security or "—")
            self._cards["ip"].set_value(c.ip)
            self._cards["gateway"].set_value(c.gateway)
            self._cards["dns"].set_value(c.dns)
            self._cards["bssid"].set_value(c.bssid or "—")
        else:
            self._dot.setStyleSheet(
                f"color:{C['t3']}; font-size:13px; background:transparent;")
            self._ssid.setText("Not connected")
            self._sub.setText("")
            for k in self._cards:
                self._cards[k].set_value("—", C["t2"])


# ══════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════
TWENTY_FOUR = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
FIVE_GHZ    = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
               116, 120, 124, 128, 132, 136, 140, 149, 153, 157, 161, 165]

COLS = ["SSID", "Signal", "Signal %", "Ch", "Band", "Security", "BSSID"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setMinimumSize(820, 660)
        self.resize(940, 760)
        self._scanner = WiFiScanner()
        self._allow_exit = False
        self._nets: List[Network] = []
        self._selected_key: Optional[str] = None   # bssid of selected row
        self._conn = Connection()

        central = QWidget(); self.setCentralWidget(central)
        central.setStyleSheet(f"background:{C['bg1']};")
        root = QVBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        root.addWidget(TitleBar(self))

        body = QWidget()
        self._bodylay = QVBoxLayout(body)
        self._bodylay.setContentsMargins(18, 14, 18, 12); self._bodylay.setSpacing(12)
        root.addWidget(body, 1)

        # connection panel (top)
        self._panel = ConnectionPanel()
        self._bodylay.addWidget(self._panel)

        # error banner (hidden unless a scan problem occurs)
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setOpenExternalLinks(False)
        self._banner.setTextFormat(Qt.TextFormat.RichText)
        self._banner.setStyleSheet(
            f"background:{C['bg3']}; color:{C['amb']}; border:1px solid {C['b1']};"
            "border-radius:8px; padding:10px 14px; font-size:12px;")
        self._banner.hide()
        self._bodylay.addWidget(self._banner)

        # tabs
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._bodylay.addWidget(self._tabs, 1)
        self._build_networks_tab()
        self._build_channels_tab()
        self._build_signal_tab()

        footer = QLabel(f"by {PUBLISHER}  ·  rescans every {REFRESH_MS//1000}s  ·  "
                        f"signal dBm is estimated from Windows signal quality")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet(f"color:{C['t3']}; font-size:10px; background:transparent;")
        self._bodylay.addWidget(footer)

        self._build_tray()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # ── tabs ──────────────────────────────────────────────────────
    def _build_networks_tab(self):
        wrap = QWidget(); lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 10, 0, 0); lay.setSpacing(10)

        self._table = QTableWidget(0, len(COLS))
        self._table.setHorizontalHeaderLabels(COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setItemDelegateForColumn(2, SignalBarDelegate(self._table))
        self._table.setAlternatingRowColors(True)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(COLS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(2, 90)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        lay.addWidget(self._table, 1)

        # expandable details panel
        self._details = QFrame(); self._details.setObjectName("panel")
        dl = QVBoxLayout(self._details); dl.setContentsMargins(14, 10, 14, 10); dl.setSpacing(4)
        self._details_title = QLabel("Select a network to see details")
        self._details_title.setStyleSheet(
            f"color:{C['t1']}; font-size:13px; font-weight:700; background:transparent;")
        self._details_body = QLabel("")
        self._details_body.setStyleSheet(
            f"color:{C['t2']}; font-size:12px; background:transparent;")
        self._details_body.setWordWrap(True)
        dl.addWidget(self._details_title); dl.addWidget(self._details_body)
        lay.addWidget(self._details)

        self._tabs.addTab(wrap, "Networks")

    def _build_channels_tab(self):
        wrap = QWidget(); lay = QVBoxLayout(wrap)
        lay.setContentsMargins(2, 12, 2, 2); lay.setSpacing(10)

        self._best_lbl = QLabel("Best channels — scanning…")
        self._best_lbl.setStyleSheet(
            f"color:{C['acc']}; font-size:14px; font-weight:800; background:transparent;")
        lay.addWidget(self._best_lbl)

        self._chart24 = ChannelChart(TWENTY_FOUR, "2.4 GHz  ·  networks per channel")
        self._chart5 = ChannelChart(FIVE_GHZ, "5 GHz  ·  networks per channel")
        for ch in (self._chart24, self._chart5):
            f = QFrame(); f.setObjectName("panel")
            fl = QVBoxLayout(f); fl.setContentsMargins(12, 10, 12, 8)
            fl.addWidget(ch)
            lay.addWidget(f, 1)

        self._tabs.addTab(wrap, "Channels")

    def _build_signal_tab(self):
        wrap = QWidget(); lay = QVBoxLayout(wrap)
        lay.setContentsMargins(2, 12, 2, 2); lay.setSpacing(8)
        hint = QLabel("Live signal for the selected network (or your connection "
                      "if none selected). Rolling 60-second history.")
        hint.setStyleSheet(f"color:{C['t2']}; font-size:11px; background:transparent;")
        lay.addWidget(hint)
        self._graph = SignalGraph()
        f = QFrame(); f.setObjectName("panel")
        fl = QVBoxLayout(f); fl.setContentsMargins(12, 10, 12, 10)
        fl.addWidget(self._graph)
        lay.addWidget(f, 1)
        self._tabs.addTab(wrap, "Signal Graph")

    # ── selection ─────────────────────────────────────────────────
    def _on_row_selected(self):
        items = self._table.selectedItems()
        if not items:
            return
        row = items[0].row()
        key_item = self._table.item(row, 0)
        if key_item is None:
            return
        bssid = key_item.data(Qt.ItemDataRole.UserRole)
        self._selected_key = bssid
        net = next((n for n in self._nets if n.bssid == bssid), None)
        if net:
            self._show_details(net)
            self._graph.set_label(f"{net.ssid}  ·  {net.bssid}")
            self._graph.clear()

    def _show_details(self, n: Network):
        band, col = signal_band(n.signal_pct)
        self._details_title.setText(
            f"{n.ssid or '(hidden)'}   —   {band}")
        self._details_body.setText(
            f"BSSID: <b>{n.bssid}</b><br>"
            f"Signal: <b>{n.signal_pct}%</b>  (~{n.dbm} dBm)<br>"
            f"Channel: <b>{fmt_ch(n.channel)}</b>  ·  Band: <b>{n.band}</b><br>"
            f"Security: <b>{n.security}</b>"
            + (f"  ·  Radio: <b>{n.radio}</b>" if n.radio else ""))

    # ── tray ──────────────────────────────────────────────────────
    def _build_tray(self):
        self._tray = QSystemTrayIcon(make_wifi_icon(0, C["t3"]), self)
        self._tray.setToolTip(f"{APP_NAME} — starting…")
        menu = QMenu()
        menu.setStyleSheet(
            f"QMenu{{background:{C['bg2']}; color:{C['t1']}; border:1px solid {C['b1']};"
            f"padding:6px;}}"
            f"QMenu::item{{padding:7px 22px; border-radius:5px;}}"
            f"QMenu::item:selected{{background:{C['bg3']};}}")
        open_act = QAction(f"Open {APP_NAME}", self)
        open_act.triggered.connect(self._show_from_tray)
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self._exit_app)
        menu.addAction(open_act); menu.addSeparator(); menu.addAction(exit_act)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _exit_app(self):
        self._allow_exit = True
        self._tray.hide()
        QApplication.quit()

    # ── refresh ───────────────────────────────────────────────────
    def refresh(self):
        try:
            conn, cerr = self._scanner.read_connection()
        except Exception:
            conn, cerr = Connection(), ScanError.NETSH
        try:
            nets, nerr = self._scanner.scan()
        except Exception:
            nets, nerr = [], ScanError.NETSH

        self._conn = conn
        self._panel.update_conn(conn)

        # decide overall error state (connection read is the better adapter probe)
        err = cerr if cerr in (ScanError.NO_ADAPTER, ScanError.LOCATION,
                               ScanError.NETSH) else nerr
        self._show_banner(err)

        # networks table
        self._nets = nets
        self._populate_table(nets)

        # channels
        self._chart24.set_data(nets)
        self._chart5.set_data(nets)
        b24 = self._chart24.best_channel()
        b5 = self._chart5.best_channel()
        if nets:
            self._best_lbl.setText(
                f"Best channel for you:  2.4 GHz → {b24}   ·   5 GHz → {b5}")
        else:
            self._best_lbl.setText("Best channels — no networks detected yet")

        # signal graph: selected network, else current connection
        sel = next((n for n in nets if n.bssid == self._selected_key), None)
        if sel is not None:
            self._graph.set_label(f"{sel.ssid}  ·  {sel.bssid}")
            self._graph.push(sel.signal_pct)
        elif conn.connected:
            self._graph.set_label(f"{conn.ssid}  (connected)")
            self._graph.push(conn.signal_pct)
        else:
            self._graph.set_label("No network selected")
            self._graph.push(None)

        self._update_tray(conn, err)

    def _show_banner(self, err: str):
        if err == ScanError.LOCATION:
            self._banner.setText(
                "⚠  <b>Location services are off.</b> Windows requires Location "
                "to be enabled for any app to read WiFi scan results. "
                "Turn it on under <b>Settings → Privacy &amp; security → Location</b>, "
                "then WaveScout will populate automatically.")
            self._banner.setStyleSheet(
                f"background:{C['bg3']}; color:{C['amb']}; border:1px solid {C['amb']};"
                "border-radius:8px; padding:10px 14px; font-size:12px;")
            self._banner.show()
        elif err == ScanError.NO_ADAPTER:
            self._banner.setText(
                "⚠  <b>No WiFi adapter detected.</b> WaveScout needs a wireless "
                "network adapter. If you're on a desktop with Ethernet only, "
                "there's nothing to scan.")
            self._banner.setStyleSheet(
                f"background:{C['bg3']}; color:{C['red']}; border:1px solid {C['red']};"
                "border-radius:8px; padding:10px 14px; font-size:12px;")
            self._banner.show()
        elif err == ScanError.NETSH:
            self._banner.setText(
                "⚠  <b>Could not run the Windows WLAN service.</b> "
                "'netsh wlan' is unavailable — make sure the WLAN AutoConfig "
                "service is running.")
            self._banner.setStyleSheet(
                f"background:{C['bg3']}; color:{C['red']}; border:1px solid {C['red']};"
                "border-radius:8px; padding:10px 14px; font-size:12px;")
            self._banner.show()
        else:
            self._banner.hide()

    def _populate_table(self, nets: List[Network]):
        nets = sorted(nets, key=lambda n: n.signal_pct, reverse=True)
        sel = self._selected_key
        self._table.setUpdatesEnabled(False)
        self._table.clearContents()
        self._table.setRowCount(len(nets))
        sel_row = -1
        for r, n in enumerate(nets):
            col = signal_color(n.signal_pct)

            ssid_item = QTableWidgetItem(n.ssid or "(hidden)")
            ssid_item.setData(Qt.ItemDataRole.UserRole, n.bssid)
            ssid_item.setForeground(QColor(C["t1"]))
            f = ssid_item.font(); f.setBold(True); ssid_item.setFont(f)

            dbm_item = QTableWidgetItem(f"{n.dbm} dBm")
            dbm_item.setForeground(QColor(col))
            dbm_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            bar_item = QTableWidgetItem()
            bar_item.setData(Qt.ItemDataRole.UserRole, n.signal_pct)

            ch_item = QTableWidgetItem(fmt_ch(n.channel))
            ch_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            ch_item.setForeground(QColor(C["t1"]))

            band_item = QTableWidgetItem(n.band)
            band_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            bcol = C["acc"] if n.band == "5 GHz" else (
                C["amb"] if n.band == "2.4 GHz" else C["t2"])
            band_item.setForeground(QColor(bcol))

            sec_item = QTableWidgetItem(n.security)
            sec_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            sec_item.setForeground(
                QColor(C["red"] if n.security == "Open" else C["t2"]))

            bssid_item = QTableWidgetItem(n.bssid)
            bssid_item.setForeground(QColor(C["t3"]))

            for c_idx, it in enumerate(
                    [ssid_item, dbm_item, bar_item, ch_item,
                     band_item, sec_item, bssid_item]):
                self._table.setItem(r, c_idx, it)

            if sel is not None and n.bssid == sel:
                sel_row = r

        self._table.setUpdatesEnabled(True)
        if sel_row >= 0:
            self._table.selectRow(sel_row)

    def _update_tray(self, conn: Connection, err: str):
        if err == ScanError.LOCATION:
            self._tray.setIcon(make_wifi_icon(0, C["amb"]))
            self._tray.setToolTip(f"{APP_NAME}\nLocation services off — can't scan")
            return
        if err in (ScanError.NO_ADAPTER, ScanError.NETSH):
            self._tray.setIcon(make_wifi_icon(0, C["red"]))
            self._tray.setToolTip(f"{APP_NAME}\nNo WiFi adapter")
            return
        if conn.connected:
            col = signal_color(conn.signal_pct)
            self._tray.setIcon(make_wifi_icon(conn.signal_pct, col))
            self._tray.setToolTip(
                f"{APP_NAME}\n{conn.ssid} — {conn.signal_pct}% ({conn.dbm} dBm)\n"
                f"Ch {fmt_ch(conn.channel)} · {conn.band}")
        else:
            self._tray.setIcon(make_wifi_icon(0, C["t3"]))
            self._tray.setToolTip(f"{APP_NAME}\nNot connected · {len(self._nets)} networks")

    # ── minimise to tray instead of closing ───────────────────────
    def closeEvent(self, e):
        if self._allow_exit:
            e.accept(); return
        e.ignore(); self.hide()
        if self._tray.supportsMessages():
            self._tray.showMessage(
                APP_NAME,
                "Still running in the tray. Double-click the icon to reopen.",
                QSystemTrayIcon.MessageIcon.Information, 2500)


# ══════════════════════════════════════════════════════════════════
# TRAY / APP ICON  (WiFi signal-bars glyph)
# ══════════════════════════════════════════════════════════════════
def make_wifi_icon(pct: float, color: str) -> QIcon:
    pm = QPixmap(32, 32); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bars = [(7, 22, 5), (13, 18, 9), (19, 13, 14), (25, 8, 19)]  # x, y, h
    frac = max(0.0, min(1.0, pct / 100.0))
    active = int(round(frac * 4))
    if pct > 0 and active == 0:
        active = 1
    for i, (x, y, h) in enumerate(bars):
        on = i < active
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color) if on else QColor(C["b0"]))
        p.drawRoundedRect(QRectF(x, y, 4, h), 1.5, 1.5)
    p.end()
    return QIcon(pm)


def _paint_radar(p: QPainter, s: float, tile: bool = True):
    """Paint the WaveScout radar mark onto painter ``p`` at size ``s``.
    When ``tile`` is True a dark squircle backdrop is drawn (for app/exe
    icons); when False only the glyph is drawn (for inline UI on dark bg)."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if tile:
        p.setBrush(QColor(C["bg1"])); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0, 0, s, s), s*0.22, s*0.22)
    cx, cy, R = s/2, s/2, s*0.36
    acc = QColor(C["acc"])
    # bold outer ring
    p.setPen(QPen(QColor(0, 212, 170, 210), s*0.030, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), R, R)
    # solid sweep wedge with radial fade
    p.save()
    clip = QPainterPath(); clip.addEllipse(QPointF(cx, cy), R, R); p.setClipPath(clip)
    path = QPainterPath(QPointF(cx, cy))
    path.arcTo(QRectF(cx-R, cy-R, 2*R, 2*R), 48, 60)
    path.closeSubpath()
    rg = QRadialGradient(cx, cy, R)
    rg.setColorAt(0.0, QColor(0, 212, 170, 255))
    rg.setColorAt(1.0, QColor(0, 212, 170, 95))
    p.setBrush(QBrush(rg)); p.setPen(Qt.PenStyle.NoPen); p.drawPath(path)
    p.restore()
    # bright leading edge
    a = _math.radians(48)
    p.setPen(QPen(acc, s*0.028, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawLine(QPointF(cx, cy), QPointF(cx+R*_math.cos(a), cy-R*_math.sin(a)))
    # white blip
    p.setBrush(QColor("#EAFBF6")); p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(cx+R*0.6*_math.cos(_math.radians(80)),
                          cy-R*0.6*_math.sin(_math.radians(80))), s*0.034, s*0.034)
    # centre dot
    p.setBrush(acc); p.drawEllipse(QPointF(cx, cy), s*0.042, s*0.042)


def make_brand_pixmap(size: int, tile: bool = True) -> QPixmap:
    pm = QPixmap(size, size); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    _paint_radar(p, float(size), tile)
    p.end()
    return pm


def make_brand_icon() -> QIcon:
    """The WaveScout brand mark — a bold teal radar sweep on a dark squircle
    tile. Used as the window / taskbar icon (matches icon.ico)."""
    return QIcon(make_brand_pixmap(64, tile=True))


# ══════════════════════════════════════════════════════════════════
# GLOBAL STYLE
# ══════════════════════════════════════════════════════════════════
def global_style() -> str:
    return f"""
QMainWindow, QWidget {{
    background-color:{C['bg1']}; color:{C['t1']};
    font-family:"Segoe UI","SF Pro Display",sans-serif; font-size:13px;
}}
QFrame#card {{
    background-color:{C['bg2']}; border:1px solid {C['b0']}; border-radius:9px;
}}
QFrame#panel {{
    background-color:{C['bg2']}; border:1px solid {C['b0']}; border-radius:11px;
}}
QTabWidget::pane {{
    border:1px solid {C['b0']}; border-radius:10px; background:{C['bg2']};
    top:-1px;
}}
QTabBar::tab {{
    background:transparent; color:{C['t2']}; padding:8px 20px;
    border:none; font-size:12px; font-weight:600; margin-right:2px;
}}
QTabBar::tab:selected {{
    color:{C['acc']}; border-bottom:2px solid {C['acc']};
}}
QTabBar::tab:hover {{ color:{C['t1']}; }}
QTableWidget {{
    background-color:{C['bg2']}; border:1px solid {C['b0']}; border-radius:10px;
    gridline-color:{C['b0']}; outline:none;
}}
QTableWidget::item {{ padding:6px 8px; border:none; }}
QTableWidget::item:selected {{ background:{C['bg3']}; color:{C['t1']}; }}
QTableWidget {{ alternate-background-color:{C['bg1']}; }}
QHeaderView::section {{
    background-color:{C['bg0']}; color:{C['t2']}; padding:8px;
    border:none; border-bottom:1px solid {C['b1']};
    font-size:10px; font-weight:700; letter-spacing:0.6px;
}}
QScrollBar:vertical {{
    background:{C['bg1']}; width:10px; margin:0; border:none;
}}
QScrollBar::handle:vertical {{
    background:{C['b1']}; border-radius:5px; min-height:30px;
}}
QScrollBar::handle:vertical:hover {{ background:{C['acc2']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
QToolTip {{
    background:{C['bg3']}; color:{C['t1']}; border:1px solid {C['b1']};
    padding:4px 8px; border-radius:4px;
}}
"""


# ══════════════════════════════════════════════════════════════════
# CRASH / STARTUP LOGGING
# ══════════════════════════════════════════════════════════════════
_APP_DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
os.makedirs(_APP_DATA_DIR, exist_ok=True)
_CRASH_LOG = os.path.join(_APP_DATA_DIR, "crash.log")
_RUNTIME_NOTES_LOGGED: set[str] = set()


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write_crash(exc_type, exc_value, exc_tb):
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 70 + "\n")
            f.write(f"CRASH  {_timestamp()}\n" + "=" * 70 + "\n")
            f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)) + "\n")
    except Exception:
        pass


def _write_runtime_note_once(key: str, message: str):
    if key in _RUNTIME_NOTES_LOGGED:
        return
    _RUNTIME_NOTES_LOGGED.add(key)
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\nNOTE   {_timestamp()}\n")
            f.write(message + "\n")
    except Exception:
        pass


def _qt_message_handler(mode, context, message):
    try:
        if "Critical" not in mode.name and "Fatal" not in mode.name:
            return
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\nQT {mode.name}  {_timestamp()}\n")
            f.write(f"  {context.file}:{context.line}  {message}\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════
def main():
    sys.excepthook = _write_crash
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(global_style())
    app.setWindowIcon(make_brand_icon())
    from PyQt6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(_qt_message_handler)

    def _slot_hook(et, ev, etb):
        _write_crash(et, ev, etb)
        sys.__excepthook__(et, ev, etb)
    sys.excepthook = _slot_hook

    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"\nSTARTUP  {_timestamp()}  v{APP_VERSION}\n")
    except Exception:
        pass

    win = MainWindow()
    win.show()
    geo = app.primaryScreen().availableGeometry()
    win.move(geo.center() - win.rect().center())
    win.raise_(); win.activateWindow()

    smoke_exit_ms = os.environ.get("WAVESCOUT_SMOKE_EXIT_MS")
    if smoke_exit_ms:
        try:
            QTimer.singleShot(max(0, int(smoke_exit_ms)), win._exit_app)
        except ValueError:
            pass

    if _IS_WIN:
        try:
            import ctypes
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(win.winId()), 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass

    try:
        sys.exit(app.exec())
    except SystemExit:
        raise
    except Exception:
        _write_crash(*sys.exc_info())
        raise


if __name__ == "__main__":
    main()
