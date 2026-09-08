# WaveScout

**Free portable Windows WiFi analyser** — by VaultSoft.

WaveScout scans the WiFi networks around you and shows signal strength, channel
congestion, security and a live signal graph in a clean, dark, minimal UI. No
installer, no admin rights for basic scanning, single portable `.exe`.

![brand](icon.ico)

🆕 **New:** install, update and launch every VaultSoft app from one place — get **[VaultSoft Hub](https://github.com/VaultSoft/vaultsoft-hub/releases/latest)**.

## Features

- **Network table** — every detected access point with SSID, estimated signal
  (dBm), a colour-coded signal-% bar, channel, band (2.4 / 5 / 6 GHz), security
  (Open / WEP / WPA / WPA2 / WPA3 / Enterprise) and BSSID. Click a row for an
  expanded details panel. Auto-rescans every 5 seconds.
- **Channel congestion view** — bar charts of how many networks sit on each
  2.4 GHz and 5 GHz channel, with the least-congested channel highlighted in
  teal and a *"Best channel for you"* recommendation.
- **Live signal graph** — rolling 60-second history for the selected network
  (or your current connection), teal line on dark.
- **Your connection panel** — current SSID, signal, channel, band, security,
  IP address, gateway, DNS and the AP's BSSID, pinned at the top.
- **System tray** — icon shows live signal bars, tooltip shows SSID + signal %,
  right-click to Open / Exit. Closing the window minimises to the tray.

## Running from source

```bat
pip install -r requirements.txt
python wavescout.py
```

Requirements: Windows 10/11 64-bit, Python 3.10+, PyQt6.

## Building the portable EXE

```bat
build_installer.bat
```

This regenerates the icon, runs PyInstaller against `WaveScout.spec`, and
produces `WaveScout_v1.0.0_Portable.zip` (containing `WaveScout.exe`,
`_internal\` and `README.txt`). No UPX is used, to avoid antivirus false
positives.

## How it scans

WaveScout uses the built-in Windows `netsh wlan` commands (via `subprocess`) —
no third-party drivers and no extra runtime dependencies. Signal is reported by
Windows as a 0–100% quality value; the dBm figure is estimated with Microsoft's
standard mapping (0% ≈ −100 dBm, 100% ≈ −50 dBm).

## ⚠ Location services requirement

Windows 10/11 require **Location services** to be turned **on** for *any*
application (including `netsh` and WiFi-analyser tools) to read WLAN scan
results. If it's off, WaveScout shows a clear banner explaining this. Enable it
under **Settings → Privacy & security → Location**, and the network list and
channel views populate automatically. Your current-connection details still work
regardless.

---

## Also by VaultSoft

**[VaultSoft Hub](https://github.com/VaultSoft/vaultsoft-hub)** — install, update and launch every VaultSoft app from one window.

**[PulseMonitor](https://vaultsoft.github.io/PulseMonitor)** — Lightweight real-time PC health monitor for Windows. Track CPU, GPU, RAM, network, and disk stats with a clean desktop overlay and always-on-top gaming widget.

**[SweptPC](https://vaultsoft.github.io/SweptPC)** — Free portable Windows PC cleaner. Remove junk files, browser caches, and system clutter in seconds.

---

© VaultSoft · WaveScout v1.0.0
