#!/usr/bin/env python3
"""Windows system-tray entry and state bridge for Clawdmeter — APP-01.

Provides:
  TrayState   — thread-safe scalar bridge (daemon loop writes, tray reads)
  header_text — pure helper producing the D-05 status-header string
  main()      — tray entry: builds per-state icons, runs the daemon loop in a
                bg thread, and runs pystray.Icon on the main thread

The daemon loop (claude_usage_daemon_windows.main) is UNCHANGED in logic;
this module injects only additive state-setter calls at existing branch points.

Usage::

    python tray_windows.py

Run: python -m pytest daemon/tests/test_windows_tray.py -x -q
"""

import os
import sys
import threading
import time

# Repo root = the directory that CONTAINS the `daemon` package (this file is
# <repo>/daemon/tray_windows.py). Resolve it from __file__ so the package
# imports below and the brand-logo asset load work no matter what the current
# working directory is — critical for logon autostart, where the HKCU\Run entry
# starts with cwd = System32, not the repo (APP-01 / SC#1).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Autostart launches us with the BASE interpreter's pythonw.exe, not the venv's
# (see autostart_windows._command — the venv pythonw redirector pops a console
# window). The base interpreter does NOT see the venv's site-packages, so add
# them here to resolve pystray/bleak/PIL. os.path.isdir guards the no-venv and
# already-inside-venv cases; site.addsitedir is a no-op on a missing dir anyway.
_VENV_SITE = os.path.join(_REPO_ROOT, ".venv", "Lib", "site-packages")
if os.path.isdir(_VENV_SITE):
    import site
    # addsitedir APPENDS, so a stale copy in the base interpreter's
    # site-packages (e.g. an old typing_extensions) would shadow the venv's
    # and crash anyio on first use. Move the venv entries to the front.
    _base_path = list(sys.path)
    site.addsitedir(_VENV_SITE)
    sys.path[:] = [p for p in sys.path if p not in _base_path] + _base_path

# ---------------------------------------------------------------------------
# TrayState — thread-safe scalar bridge (loop -> tray)
# ---------------------------------------------------------------------------

class TrayState:
    """Shared state object bridging the daemon asyncio loop to the tray.

    The daemon loop writes state via the set_* methods; the tray reads the
    scalar attributes.  No lock is needed — writes are atomic attribute
    assignments of simple Python scalars, and the tray only ever reads them.

    The loop populates `loop` and `stop_event` at startup (inside
    daemon_main()) so the tray's Quit handler can route through
    loop.call_soon_threadsafe (RESEARCH Pitfall 2 / Anti-Pattern).
    """

    def __init__(self) -> None:
        self.state: str = "scanning"       # "connected" | "scanning" | "error"
        self.reason: str = ""              # error reason string (D-04)
        self.last_sync: float | None = None  # time.time() of last successful write

        # Populated by daemon main() at startup:
        self.loop = None        # asyncio running loop (for call_soon_threadsafe)
        self.stop_event = None  # asyncio.Event (the existing clean-shutdown hook)

    def set_connected(self, ts: float) -> None:
        """Called after write_payload returns True.  ts = time.time()."""
        self.state = "connected"
        self.reason = ""
        self.last_sync = ts

    def set_scanning(self) -> None:
        """Called in scan/reconnect branches.  BLE churn stays Scanning (D-01)."""
        self.state = "scanning"
        self.reason = ""

    def set_error(self, why: str) -> None:
        """Called on token-expired / API auth failure (D-01 Error = actionable only)."""
        self.state = "error"
        self.reason = why


# ---------------------------------------------------------------------------
# header_text — pure D-05 status header string
# ---------------------------------------------------------------------------

def header_text(ts: TrayState) -> str:
    """Return the D-05 menu status-header string for the current TrayState.

    Shapes:
      "Connected · last update HH:MM"  (ts.last_sync is a float)
      "Connected · last update never"  (ts.last_sync is None)
      "Scanning…"
      "Error: {reason}"
    """
    if ts.state == "connected":
        if ts.last_sync is not None:
            when = time.strftime("%H:%M", time.localtime(ts.last_sync))
        else:
            when = "never"
        return f"Connected · last update {when}"
    if ts.state == "scanning":
        return "Scanning…"   # "Scanning…"
    return f"Error: {ts.reason}"


# ---------------------------------------------------------------------------
# single-instance guard (named kernel mutex — no stale-lock problem)
# ---------------------------------------------------------------------------

# Per-session mutex name. "Local\\" scopes it to the interactive logon, which is
# exactly the granularity we want: one tray per signed-in user. Both the headless
# autostart (HKCU\Run pythonw) and an ARSO-restored console instance live in the
# same session, so this name catches the duplicate-launch collision that produced
# the "mystery console window fighting the headless tray over BLE" field bug.
_SINGLETON_MUTEX_NAME = "Local\\Clawdmeter-tray-singleton"
_ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance():
    """Acquire the process-wide single-instance lock.

    Returns a truthy handle to keep alive for the process lifetime if this is
    the first/only tray, or None if another Clawdmeter tray already owns the
    lock (the caller must then exit immediately, before touching BLE).

    Uses a named kernel mutex: Windows releases it automatically when the owning
    process dies, so there is no stale-lock cleanup (unlike a pidfile). The
    handle lives until process exit, except on Restart, which closes it with
    _release_single_instance() so the relaunched tray can take the lock.

    Off-Windows (Linux dev box / unit tests) this is a no-op that always
    succeeds — the tray only ever runs on Windows, and the dev box must stay
    importable for the pure-helper tests.
    """
    if sys.platform != "win32":
        return object()  # no-op sentinel; never blocks off-Windows

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, True, _SINGLETON_MUTEX_NAME)
    if not handle:
        # Couldn't create the mutex at all — fail OPEN so a kernel quirk never
        # stops the tray from starting; single-instance is best-effort hardening.
        return object()
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return None  # another instance already holds it
    return handle


def _release_single_instance(handle) -> None:
    """Close the singleton mutex handle so a relaunched tray can acquire it.

    The fail-open sentinel (object()) and the off-Windows no-op have nothing to close.
    """
    if sys.platform != "win32" or not isinstance(handle, int):
        return
    import ctypes
    ctypes.WinDLL("kernel32").CloseHandle(handle)


# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the relaunched tray owns no
# console and does not share this process's console group.
_RELAUNCH_FLAGS = 0x00000008 | 0x00000200


def _relaunch_tray(instance_lock) -> None:
    """Release the singleton lock, then start a fresh tray with the autostart command.

    Called once the daemon thread has disconnected, so the new instance never
    overlaps this one on the lock or the BLE link. It is
    started by this process, so it inherits this tray's environment and stays
    outside any app container the original launch avoided.
    """
    import subprocess
    import daemon.autostart_windows as autostart

    _release_single_instance(instance_lock)
    subprocess.Popen(
        autostart._command(os.path.abspath(__file__)),
        cwd=_REPO_ROOT,
        creationflags=_RELAUNCH_FLAGS,
        close_fds=True,
    )


# Seconds a stopped tray may take to leave pystray's message loop before it is
# forced out. stop() alone waits up to SETUP_THREAD_TIMEOUT (5 s) on the setup
# thread, so this leaves room for a normal exit.
_STOP_GRACE_S = 15.0


def _force_exit(icon_ref) -> None:
    """Remove the tray icon and end the process without waiting on pystray."""
    try:
        icon_ref.visible = False
    except Exception:
        pass
    os._exit(0)


def _arm_exit_watchdog(icon_ref) -> threading.Timer:
    """End the process if the icon's message loop is still running after the grace period.

    A Restart click on 2026-10-19 stopped the daemon and the setup thread, yet the
    tray sat in pystray's GetMessage loop with its icon still showing, so nothing
    after icon.run() ever ran. The timer is a daemon thread: a normal exit ends
    the process before it fires.
    """
    timer = threading.Timer(_STOP_GRACE_S, _force_exit, args=(icon_ref,))
    timer.daemon = True
    timer.start()
    return timer


# ---------------------------------------------------------------------------
# main() — tray entry (pystray on main thread, daemon loop in bg thread)
# ---------------------------------------------------------------------------

def main() -> None:
    """Tray entry point: build icons, start daemon bg thread, run pystray.

    `import pystray` is intentionally INSIDE this function (not at module top)
    so the module can be imported on a GTK-less Linux dev box for unit tests
    of the pure helpers (TrayState, header_text) without pystray failing.
    """
    # Single-instance guard FIRST — before icons, the daemon thread, or any BLE
    # work. If another tray already owns the session mutex (e.g. ARSO restored a
    # console instance and the headless autostart also fired), exit silently.
    # Under pythonw there is no console to print to, so this is a quiet return.
    _instance_lock = _acquire_single_instance()
    if _instance_lock is None:
        return

    import asyncio as _asyncio
    import pystray
    from pystray import Menu, MenuItem

    import daemon.autostart_windows as autostart
    from daemon.claude_usage_daemon_windows import main as daemon_main, log as daemon_log
    from daemon.icon_assets import load_logo_rgba, build_state_icons

    # Build per-state icons once at startup; swap icon.icon per tick (never recomposite).
    base = load_logo_rgba(os.path.join(_REPO_ROOT, "firmware", "src", "logo.h"))
    images = build_state_icons(base)

    ts = TrayState()
    icon = pystray.Icon("Clawdmeter", images["scanning"], "Clawdmeter")

    # Set by the Quit handler so the supervisor below knows a clean stop was
    # requested and must NOT resurrect the loop.
    _quit_requested = threading.Event()

    # --- background thread: asyncio loop (supervised, auto-restart on crash) ---
    def _run_daemon() -> None:
        # A crash used to END this daemon=True thread for good: polling stopped
        # silently and the tray froze on its last state until the user manually
        # relaunched (the field "frozen tray" / "sticky error" mode — one transient
        # bleak assert turned into an outage). Now we SUPERVISE it: log the
        # traceback, flip the tray to an actionable error, then restart the loop
        # with capped backoff. A CLEAN return means Quit was requested (main()'s
        # loop exited on stop_event), so we stay down and do NOT restart.
        backoff = 2
        while not _quit_requested.is_set():
            try:
                _asyncio.run(daemon_main(tray_state=ts))
                return  # clean exit == Quit requested; stay down
            except Exception as e:  # last-resort thread guard
                import traceback
                daemon_log(f"Daemon thread crashed: {e!r}")
                daemon_log(traceback.format_exc())
                ts.set_error(f"daemon crashed: {type(e).__name__}")
            if _quit_requested.is_set():
                return
            daemon_log(f"Restarting daemon loop in {backoff}s after crash")
            # Interruptible sleep: a Quit during backoff wakes us immediately.
            if _quit_requested.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 30)

    daemon_thread = threading.Thread(target=_run_daemon, daemon=True)
    daemon_thread.start()

    # --- menu ---
    def _on_quit(icon_ref, _item) -> None:
        # NEVER call ts.stop_event.set() directly from the tray thread;
        # asyncio.Event is NOT thread-safe (RESEARCH Pitfall 2).
        #
        # After signalling, WAIT for the daemon thread to finish its graceful
        # shutdown (the loop's finally: client.disconnect()) BEFORE we stop the
        # icon and let the process exit. Without this join the daemon=True thread
        # is killed mid-flight, the peer never gets a clean GATT disconnect, and
        # the device sits frozen on stale data instead of returning to its waiting
        # screen (SC#3 field report). The timeout caps the block so Quit can never
        # hang if a WinRT disconnect wedges (rare) — we exit anyway as a fallback.
        #
        # Set _quit_requested FIRST so the supervisor loop in _run_daemon never
        # resurrects the daemon after we signal stop (Quit must be final).
        _stop_daemon()
        _arm_exit_watchdog(icon_ref)
        icon_ref.stop()

    def _stop_daemon() -> None:
        _quit_requested.set()
        if ts.loop is not None and ts.stop_event is not None:
            ts.loop.call_soon_threadsafe(ts.stop_event.set)
            daemon_thread.join(timeout=6.0)

    def _on_restart(icon_ref, _item) -> None:
        # Relaunch from inside the click handler, before stopping the icon: code
        # after icon.run() never ran when pystray's loop failed to exit.
        _stop_daemon()
        daemon_log("Restart requested from tray menu; relaunching")
        _relaunch_tray(_instance_lock)
        _arm_exit_watchdog(icon_ref)
        icon_ref.stop()

    def _on_toggle(_icon_ref, _item) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            # Pass THIS file explicitly — without it enable() defaults the Run
            # value to autostart_windows.py (which has no entry point and starts
            # nothing), silently breaking menu-enabled autostart.
            autostart.enable(tray_script=os.path.abspath(__file__))
        icon.update_menu()

    icon.menu = Menu(
        # Non-clickable status header; text updates via update_menu() on state change.
        MenuItem(lambda _item: header_text(ts), None, enabled=False),
        # Start-at-login toggle: checked= is a CALLABLE for live query (Pitfall 6).
        MenuItem("Start at login", _on_toggle, checked=lambda _item: autostart.is_enabled()),
        MenuItem("Restart", _on_restart),
        MenuItem("Quit", _on_quit),
    )

    # --- setup callback (runs in pystray's setup thread, 1s poll) ---
    prev_state: dict = {"state": None, "last_sync": None}

    def _refresh(_icon: pystray.Icon) -> None:
        _icon.visible = True
        while _icon._running:  # type: ignore[attr-defined]
            current = ts.state
            last_sync = ts.last_sync
            state_changed = current != prev_state["state"]
            # Refresh the tooltip/menu when last_sync advances too — not only on
            # state change. A healthy "connected" daemon polling a flat usage
            # value never changes state, so a transition-only refresh froze the
            # "last update HH:MM" tooltip and read as a dead daemon (SC#2 field
            # report: device + tooltip both looked stuck while polling was fine).
            if state_changed or last_sync != prev_state["last_sync"]:
                if state_changed:
                    _icon.icon = images[current]  # icon image depends on state only
                _icon.title = header_text(ts)
                # D-04: toast ONLY on transition INTO error, not on every error tick.
                if current == "error" and prev_state["state"] != "error":
                    _icon.notify(ts.reason or "Clawdmeter error", "Clawdmeter")
                prev_state["state"] = current
                prev_state["last_sync"] = last_sync
                _icon.update_menu()
            time.sleep(1.0)

    # Blocks the main thread until icon.stop() is called from _on_quit.
    icon.run(setup=_refresh)


if __name__ == "__main__":
    main()
