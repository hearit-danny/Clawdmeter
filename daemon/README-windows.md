# Windows Setup and Run Guide

This guide covers running the Clawdmeter Windows daemon on native Windows hardware.
It includes the turnkey `install-windows.ps1` bootstrap (tray icon + login autostart),
the manual-run fallback, and how to manage or remove autostart.

---

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **Native Windows** | Must run on real Windows — not WSL. The script prints a warning and BLE will not work under WSL. |
| **Python 3.11+** | Download from [python.org](https://www.python.org/downloads/) if not already installed. Ensure "Add python.exe to PATH" is checked during install. |
| **Claude Code installed** | Install Claude Code and complete `claude login` so credentials exist on disk. |
| **Clawdmeter powered on** | The device must be powered on and in range before the daemon starts. |
| **Paired with Windows Bluetooth** | Pair the device once via **Settings → Bluetooth & devices → Add device** (see [Pair the device](#pair-the-device-one-time)). This is required — the device is a bonded BLE HID keyboard, so pairing enables its physical buttons and keeps a persistent connection that shows your last usage even when the daemon is stopped. |

### Where are my credentials?

`claude login` writes the OAuth token to (first match wins):

1. `%USERPROFILE%\.claude\.credentials.json` — primary path (confirmed by Claude Code docs)
2. `%LOCALAPPDATA%\Claude\.credentials.json` — fallback
3. `%APPDATA%\Claude\.credentials.json` — fallback

The daemon probes these paths in order. You can also set `CLAUDE_CREDENTIALS_PATH` to an
absolute path or `CLAUDE_CONFIG_DIR` to a directory to override the search entirely.

> **Security note:** The credentials file contains your OAuth token. Never share its contents
> or embed it in scripts. The daemon reads it from disk and uses it only as the API
> `Authorization` header — the token is never written to any log, tooltip, or notification.

---

## Pair the device (one time)

The Clawdmeter is a **bonded BLE HID keyboard** as well as a usage display — its firmware
enables bonding (`NimBLEDevice::setSecurityAuth`) and advertises the HID service so its
physical buttons act as a keyboard (Space / Shift+Tab). Pair it with Windows **once**,
before running the daemon:

1. Put the device on its Bluetooth waiting screen (powered on, not yet connected).
2. Open **Settings → Bluetooth & devices → Add device → Bluetooth**.
3. Select **Clawdmeter** and complete pairing.

**Why this is required:**

- **Keyboard buttons** — HID over BLE requires bonding on Windows. Without pairing, the
  device's buttons won't reach the PC.
- **Persistent point-in-time view** — once paired, Windows maintains the BLE link and
  auto-reconnects the device whenever it is in range. This is intentional: the device keeps
  showing your **last-synced** usage even after you Quit the daemon, as a glanceable
  point-in-time view. Quitting the daemon releases only its data connection — it does **not**
  drop the Windows pairing, so the device stays connected to Windows.

To undo, use **Settings → Bluetooth & devices → (device) → Remove device**. Removing the
pairing disables the keyboard buttons.

---

## Setup (one time)

Open a PowerShell terminal and `cd` to the repository root.

**1. Create a virtual environment**

```powershell
python -m venv .venv
```

**2. Activate it**

```powershell
.venv\Scripts\Activate.ps1
```

If you see a scripts-execution-policy error, run:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```
Then repeat the `Activate.ps1` step.

**3. Install dependencies**

```powershell
pip install -r daemon\requirements-windows.txt
```

This installs `bleak` (WinRT BLE) and `httpx` (async HTTP for the Anthropic API).

---

## Running the daemon

With the venv active and the Clawdmeter powered on:

```powershell
python daemon\claude_usage_daemon_windows.py
```

### Expected console output

```
[HH:MM:SS] === Claude Usage Tracker Daemon (BLE, Windows) ===
[HH:MM:SS] Poll interval: 60s
[HH:MM:SS] Scanning for 'Clawdmeter' (8.0s)...
[HH:MM:SS] Not advertising; connecting to bonded address XX:XX:XX:XX:XX:XX
[HH:MM:SS] Connecting to XX:XX:XX:XX:XX:XX...
[HH:MM:SS] Connected
[HH:MM:SS] Sending: {"s":42,"sr":180,"w":17,"wr":8820,"st":"active","ok":true}
```

- **The device must be paired with Windows first** (see [Pair the device](#pair-the-device-one-time)).
  The daemon then connects over that existing link via `BleakScanner` + `BleakClient`; it does
  not pop its own pairing dialog.
- **`Scanning…` → `Not advertising; connecting to bonded address …` is normal.** Once paired,
  Windows keeps the device connected, so it stops advertising and an advertisement scan can't
  see it. The daemon detects this and connects directly to the device's address (recovered from
  the Windows PnP table). The 8-second scan that precedes the fallback happens once per session.
  Set `CLAWDMETER_BLE_ADDRESS=AA:BB:CC:DD:EE:FF` to pin the address and skip PnP lookup.
- After `Connected`, the daemon polls the Anthropic API immediately and sends the first
  payload within a few seconds of connect (warm token path). With a valid, non-expired token
  the device should leave its waiting screen and show session + weekly percentages within
  about 10 seconds of launch.
- The daemon then re-polls every 60 seconds while connected. If the device fires a refresh
  request (e.g., after a button press), an immediate re-poll occurs without waiting for the
  60-second interval.
- If the device disconnects or goes out of range, the daemon logs `Device disconnected` and
  re-scans automatically with exponential backoff (starting at 1 second, capped at 60 seconds).

### Stopping

Press **Ctrl+C** in the terminal. The daemon logs `Daemon stopping` and exits cleanly.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Warning: running under Linux/WSL` | Running in WSL, not native Windows | Run from a native PowerShell or Command Prompt on Windows |
| `Scanning for 'Clawdmeter'… Device not found` | Clawdmeter is off, out of range, or not yet paired | Power on the device, pair it once (see [Pair the device](#pair-the-device-one-time)), and ensure it is in range |
| `No token; skipping poll` | No credentials file found at any candidate path | Confirm `claude login` ran on this machine; check `%USERPROFILE%\.claude\.credentials.json` exists |
| `API HTTP 401` | Token expired | Re-run `claude login` in a terminal to refresh the token, then restart the daemon |
| `Connection failed` | WinRT BLE initialisation issue | Ensure Windows Bluetooth is on; try toggling Bluetooth off/on in Windows Settings |

---

## Tray icon, login autostart, and turnkey install

### One-command install (recommended)

> **Copy the repo to a native Windows path first.** Clone or copy this repository
> to a Windows location such as `%USERPROFILE%\Clawdmeter` — **not** a WSL share
> (`\\wsl$\...` or `\\wsl.localhost\...`). Installing from the WSL share would point
> the virtual environment and the login-autostart entry at a path that disappears when
> WSL shuts down, defeating the whole point of the Windows daemon. The installer
> detects a WSL path and refuses to run, telling you how to relocate.
>
> ```powershell
> Copy-Item -Recurse '\\wsl.localhost\Ubuntu\home\<you>\repos\Clawdmeter' "$env:USERPROFILE\Clawdmeter"
> cd "$env:USERPROFILE\Clawdmeter"
> ```

Run this once from the repository root in PowerShell (a native Windows path):

```powershell
powershell -ExecutionPolicy Bypass -File install-windows.ps1
```

The script does four things in order and logs progress at each step:

1. Creates a Python virtual environment at `.venv`.
2. Installs dependencies from `daemon\requirements-windows.txt` (bleak, httpx, pystray, Pillow).
3. Registers the tray app to launch automatically at login via `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` — per-user, no admin required.
4. Launches the tray app immediately (headless — no console window).

The script downloads nothing from the internet. It only installs the packages listed in
the in-repo `daemon\requirements-windows.txt`.

### Tray icon and status

After install, the Clawdmeter icon appears in the Windows notification area:

| State | Icon bubble | Tooltip |
|-------|-------------|---------|
| Connected | green | `Connected · last update HH:MM` |
| Scanning | amber | `Scanning…` |
| Error | red | `Error: token expired — run claude login` |

Hover over the icon to see the current status tooltip. A notification fires once when the
daemon first enters the Error state (e.g. after a token expiry).

### Tray menu

Right-click the tray icon for the menu:

- **Status header** (non-clickable) — live status + last data sync time.
- **Start at login** (checkable toggle) — enables or disables autostart at runtime.
  Reflects the current registry state each time the menu opens.
- **Machine overlay** (checkable toggle) - shows or hides the desktop machine overlay
  (see [Machine meter](#machine-meter)). Saved as `overlay = on|off` in the daemon config.
- **Quit** — stops the daemon cleanly and exits with no lingering process. It releases the
  daemon's own data connection but does **not** drop the Windows Bluetooth pairing — the
  device stays connected to Windows and keeps showing your last-synced usage (point-in-time
  view).

### Disabling or removing autostart

Use the tray menu toggle, or remove the registry value manually:

```powershell
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Clawdmeter /f
```

### WSL independence

The daemon operates fully independently of WSL. The token is read from native Windows
credential paths (`%USERPROFILE%\.claude\.credentials.json` and fallbacks); BLE uses
the WinRT stack directly. Running `wsl --shutdown` does not affect the BLE link, and
the daemon starts correctly even in a fresh Windows session where WSL has never been
launched.

---

## Machine meter

The daemon also reports this PC's CPU load, free RAM and which MNIIS rigs are up.

- **On the device:** a third screen in the tap cycle (splash, usage, machine). CPU bar with
  percent, RAM bar (used) with free GB, then one row per rig: solid green dot when all of
  its ports listen, amber when some do, hollow ring when none do. Down rigs sort last, so
  when there are more rigs than rows the last row reads `+N more`. Firmware without the
  screen ignores the new keys; a daemon without them leaves the screen on "No Machine Data".
- **On the desktop:** tray menu **Machine overlay**. A small click-through panel docked
  bottom-right above the taskbar at 70% opacity: CPU and free RAM with two thin bars, and a
  line like `inv1 up · g1 partial · 7 down`. It never takes focus and is not in Alt-Tab.
  Rest the cursor on it for half a second to open a card above the panel:
  - **RAM half** - memory in use and the six apps using the most, by private working set
    (the Task Manager Memory column), grouped by name with the process count.
  - **Rig line** - every rig with its db, api and web ports, green when listening.

How it is sampled (`daemon/machine_stats.py`, stdlib and ctypes only):

- CPU from two `GetSystemTimes` reads 5 s apart; RAM from `GlobalMemoryStatusEx`.
- Rigs every 60 s: the `stack.env` files `rigs.mjs` scans (the primary checkout's
  worktrees via `.git/worktrees/*/gitdir`, plus `RiderProjects\*` and `C:\wt\*`), then one
  non-blocking TCP connect per port to 127.0.0.1, all waited on in a single 50 ms select.
  The primary checkout's own stack (5432/8080/5173) is not listed.
- No docker, wsl.exe, WMI, PowerShell or subprocess. dockerd runs in the Ubuntu WSL distro
  and any docker or wsl call boots the VM when it is stopped; a refused connect does not.
- Listeners run only when a rounded value changed. The device keys (`cpu`, `rf`, `rp`,
  `rg`) ride on the usage write the daemon already sends every 60 s, only when something
  changed and always on the first write of a connection; there is no extra BLE write. The
  rig list is trimmed so a payload stays within one 240-byte write. The overlay repaints
  on the same change signal.
- Hover: the overlay is click-through, so it gets no mouse messages. A coalescable
  250 ms timer compares the cursor position with the panel. The RAM card's process list
  is one `NtQuerySystemInformation` read when it opens and every 3 s while it stays
  open; nothing is read or kept while it is closed.

Measured cost on this PC (2026-10-29, Python 3.12, 3840x1600 at 100%):

| What | Number |
|------|--------|
| Sampler + overlay, isolated, 15 min | 0.054% of one core (0.064% minus a 0.010% idle-process control) |
| Extra memory, isolated, beside the daemon's imports | sampler only: +1.3 MB working set, +1.1 MB private; with the overlay: +3.1 MB working set, +1.4 MB private (the overlay's share is mostly shared GDI and font DLL pages) |
| Whole tray, 15 min idle at 30 min uptime, overlay on, vs the old code at the same uptime | 0.44% vs 0.45% of one core; 61.6 vs 63.4 MB working set; 43.7 vs 40.2 MB private |
| One overlay repaint | 0.27 ms |
| One rig probe of 30 ports | about 7 ms wall clock, bounded by the 50 ms timeout |
| Device firmware | +160 B RAM, +2.2 KB flash |

---

## What is NOT covered here

- PyInstaller / one-file `.exe` packaging — v2
- MAC-address cache / sleep-wake reconnect hardening — Phase 3
