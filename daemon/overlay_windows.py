"""Click-through desktop overlay for the machine meter (Windows only).

One borderless layered window docked bottom-right above the taskbar:

    CPU 37%          RAM 14.2 GB free
    [=====-------]   [=========---]
    inv1 up · g1 partial · 7 down

WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST |
WS_EX_NOACTIVATE make it click-through, never focused and absent from
Alt-Tab. It is painted with UpdateLayeredWindow from a premultiplied 32-bit
DIB, so there is no WM_PAINT.

Hover: a click-through window gets no mouse messages, so a coalescable
250 ms timer compares the cursor with the panel. Resting on the RAM half
opens a card above the panel with the processes using the most memory (one
NtQuerySystemInformation read, repeated every 3 s while it stays open);
resting on the rig line opens one with every rig's ports.

Threading: the window must belong to the thread that pumps messages, which
is the tray's main thread (pystray runs GetMessage(NULL) there and so
dispatches every window of that thread). The tray creates the window before
icon.run() or from a menu callback, both on that thread. The sampler thread
only PostMessage()s WM_REDRAW when a rounded value changed.

Pure ctypes GDI: no tkinter, no extra thread, no extra message loop.
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from daemon import machine_stats

OPACITY = 178            # 70% of 255
BASE_W, BASE_H = 280, 50  # at 96 dpi
MARGIN = 8
TOP_ROW_H = 27           # CPU / RAM labels and bars; the rig line sits below
CARD_GAP, ROW_H = 6, 16
HOVER_MS = 250
HOVER_TICKS = 2          # rest this many ticks before a card opens
RAM_REFRESH_TICKS = 12   # 3 s
MAX_CARD_RIGS = 10

# theme.h colors as COLORREF (0x00BBGGRR)
def _rgb(hex_rgb: int) -> int:
    return ((hex_rgb & 0xFF) << 16) | (hex_rgb & 0xFF00) | (hex_rgb >> 16)


COL_PANEL = _rgb(0x1F1F1E)
COL_TEXT = _rgb(0xFAF9F5)
COL_DIM = _rgb(0xB0AEA5)
COL_TRACK = _rgb(0x2A2A28)
COL_GREEN = _rgb(0x788C5D)
COL_AMBER = _rgb(0xD97757)
COL_RED = _rgb(0xC0392B)

_STATE_WORD = {1: "up", 2: "partial"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested off Windows)
# ---------------------------------------------------------------------------

def pct_color(pct: int) -> int:
    if pct >= 80:
        return COL_RED
    if pct >= 50:
        return COL_AMBER
    return COL_GREEN


def rig_line(rigs) -> str:
    """'inv1 up · g1 partial · 7 down' or 'No rigs found'."""
    if not rigs:
        return "No rigs found"
    parts = [f"{r[0]} {_STATE_WORD[r[1]]}" for r in rigs if r[1] in _STATE_WORD]
    down = sum(1 for r in rigs if r[1] not in _STATE_WORD)
    if down:
        parts.append(f"{down} down")
    return " · ".join(parts)


def size_text(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n // 2**20} MB"


def rig_card(rigs) -> tuple:
    """Card rows: ("title", text), ("rig", name, state, ((label, port, open), ...)), ("more", text)."""
    if not rigs:
        return (("title", "No stack.env rigs found"),)
    rows = [("title", "Rigs · ports on 127.0.0.1")]
    rows += [("rig", r[0], r[1], r[2] if len(r) > 2 else ()) for r in rigs[:MAX_CARD_RIGS]]
    if len(rigs) > MAX_CARD_RIGS:
        rows.append(("more", f"+{len(rigs) - MAX_CARD_RIGS} more"))
    return tuple(rows)


def ram_card(detail) -> tuple:
    """Card rows: ("title", text), ("proc", name, size text)."""
    if detail is None:
        return (("title", "Memory detail unavailable"),)
    used, total, top = detail
    rows = [("title", f"In use {used:.1f} of {total:.1f} GB · largest apps")]
    rows += [("proc", name if count == 1 else f"{name} ({count})", size_text(size))
             for name, size, count in top]
    return tuple(rows)


def content(snap, card: tuple = ()) -> tuple | None:
    """What the overlay draws for a snapshot and open card; equal tuples mean no redraw."""
    if snap is None:
        return None
    return (f"CPU {snap.cpu}%", snap.cpu,
            f"RAM {snap.ram_free_gb:.1f} GB free", snap.ram_used_pct,
            rig_line(snap.rigs), card)


def hover_region(pt, rect, size, base_h: int, top_row_h: int) -> str | None:
    """Which part of the window the cursor is on.

    rect is the window rect in the coordinates of pt; size is the bitmap
    (w, h) and the panel is its bottom base_h pixels. Returns None outside,
    "card" above the panel, else "cpu", "ram" or "rigs".
    """
    left, top, right, bottom = rect
    x, y = pt
    if not (left <= x < right and top <= y < bottom) or right <= left or bottom <= top:
        return None
    bx = (x - left) * size[0] / (right - left)
    by = (y - top) * size[1] / (bottom - top) - (size[1] - base_h)
    if by < 0:
        return "card"
    if by < top_row_h:
        return "cpu" if bx < size[0] / 2 else "ram"
    return "rigs"


def read_setting(path: Path) -> bool:
    """`overlay = on` in the daemon config; off when absent."""
    try:
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            key, sep, val = line.partition("=")
            if sep and key.strip().lower() == "overlay":
                return val.strip().lower() == "on"
    except OSError:
        pass
    return False


def write_setting(path: Path, on: bool) -> None:
    """Set `overlay = on|off`, keeping every other line as it was."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    new = f"overlay = {'on' if on else 'off'}"
    for i, line in enumerate(lines):
        key, sep, _ = line.split("#", 1)[0].partition("=")
        if sep and key.strip().lower() == "overlay":
            lines[i] = new
            break
    else:
        lines.append(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Win32 bindings
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    def _sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
    _sig(user32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
    _sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
         wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
    _sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    _sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC,
         ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
         ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION),
         wintypes.DWORD)
    _sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
    _sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
    _sig(user32.SystemParametersInfoW, wintypes.BOOL, wintypes.UINT, wintypes.UINT,
         wintypes.LPVOID, wintypes.UINT)
    _sig(user32.FillRect, ctypes.c_int, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH)
    _sig(user32.DrawTextW, ctypes.c_int, wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
         ctypes.POINTER(wintypes.RECT), wintypes.UINT)
    _sig(user32.GetDpiForWindow, wintypes.UINT, wintypes.HWND)
    _sig(user32.SetThreadDpiAwarenessContext, wintypes.HANDLE, wintypes.HANDLE)
    _sig(user32.SetCoalescableTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t,
         wintypes.UINT, ctypes.c_void_p, wintypes.ULONG)
    _sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
    _sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    _sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
    _sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
    _sig(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p,
         wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
    _sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
    _sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
    _sig(gdi32.CreateSolidBrush, wintypes.HBRUSH, wintypes.COLORREF)
    _sig(gdi32.CreateFontW, wintypes.HFONT, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR)
    _sig(gdi32.SetBkMode, ctypes.c_int, wintypes.HDC, ctypes.c_int)
    _sig(gdi32.SetTextColor, wintypes.COLORREF, wintypes.HDC, wintypes.COLORREF)
    _sig(gdi32.RoundRect, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    _sig(gdi32.Ellipse, wintypes.BOOL, wintypes.HDC, ctypes.c_int, ctypes.c_int,
         ctypes.c_int, ctypes.c_int)
    _sig(gdi32.GetStockObject, wintypes.HGDIOBJ, ctypes.c_int)
    _sig(gdi32.GdiFlush, wintypes.BOOL)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
WM_DISPLAYCHANGE = 0x007E
WM_SETTINGCHANGE = 0x001A
WM_DPICHANGED = 0x02E0
WM_MOUSEACTIVATE = 0x0021
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
WM_REDRAW = 0x8000 + 0x51   # WM_APP + 0x51
HOVER_TIMER_ID = 1
HOVER_TOLERANCE_MS = 100
MA_NOACTIVATE = 3
HTTRANSPARENT = -1
SPI_GETWORKAREA = 0x0030
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
TRANSPARENT_BK = 1
NULL_PEN = 8
DT_LEFT_SINGLE = 0x0000 | 0x0020 | 0x0800 | 0x8000   # DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS
DT_RIGHT_SINGLE = DT_LEFT_SINGLE | 0x0002
COL_STATE = {1: COL_GREEN, 2: COL_AMBER}
ANTIALIASED_QUALITY = 4
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_CLASS = "ClawdmeterMachineOverlay"


class MachineOverlay:
    """The overlay window. Create, show and hide it on the tray's main thread."""

    def __init__(self, sampler) -> None:
        self._sampler = sampler
        self._hwnd = None
        self._wndproc = None   # keep the ctypes callback alive as long as the class
        self._atom = None
        self._gdi = None       # (memdc, dib, bits, font, old_bmp, old_font, w, h, alpha)
        self._drawn = None
        self._dpi = 0
        self._rows = 0         # card rows the bitmap was sized for
        self._base_h = 0       # panel height in pixels; a card sits above it
        self._hover = None     # None, "ram" or "rigs": the open card
        self._pending = None   # card the cursor is resting on, not yet open
        self._pending_n = 0
        self._ram = None       # machine_stats.memory_detail() while the RAM card is open
        self._ram_age = 0
        sampler.add_listener(self._on_sample)

    @property
    def enabled(self) -> bool:
        return self._hwnd is not None

    # --- sampler thread ---
    def _on_sample(self, _snap) -> None:
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_REDRAW, 0, 0)

    # --- main thread ---
    def show(self) -> None:
        if self._hwnd:
            return
        hinst = kernel32.GetModuleHandleW(None)
        if self._atom is None:
            self._wndproc = WNDPROC(self._proc)
            wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc,
                             hInstance=hinst, lpszClassName=_CLASS)
            self._atom = user32.RegisterClassExW(ctypes.byref(wc))
        # Per-monitor DPI awareness for this window only, so the text is drawn at
        # the panel's real resolution instead of being bitmap-stretched; the
        # tray's own windows keep the process default.
        old = user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        try:
            self._hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                _CLASS, "Clawdmeter", WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        finally:
            if old:
                user32.SetThreadDpiAwarenessContext(old)
        if not self._hwnd:
            return
        self._drawn = None
        self._paint()
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        user32.SetCoalescableTimer(self._hwnd, HOVER_TIMER_ID, HOVER_MS, None, HOVER_TOLERANCE_MS)

    def hide(self) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd:
            user32.DestroyWindow(hwnd)   # also kills the hover timer
        self._free_gdi()
        self._hover = self._pending = self._ram = None

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_REDRAW:
            self._paint()
            return 0
        if msg == WM_TIMER and wparam == HOVER_TIMER_ID:
            self._on_hover_tick()
            return 0
        if msg in (WM_DISPLAYCHANGE, WM_SETTINGCHANGE, WM_DPICHANGED):
            self._drawn = None   # work area or scale moved: lay out again
            self._paint()
            return 0
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_NCHITTEST:
            return HTTRANSPARENT
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _card(self) -> tuple:
        if self._hover == "rigs" and self._sampler.snapshot is not None:
            return rig_card(self._sampler.snapshot.rigs)
        if self._hover == "ram":
            return ram_card(self._ram)
        return ()

    def _read_ram(self) -> None:
        try:
            self._ram = machine_stats.memory_detail()
        except Exception:
            self._ram = None
        self._ram_age = 0

    def _on_hover_tick(self) -> None:
        if not self._hwnd or not self._gdi:
            return
        pt, r = wintypes.POINT(), wintypes.RECT()
        if not user32.GetCursorPos(ctypes.byref(pt)) or not user32.GetWindowRect(self._hwnd, ctypes.byref(r)):
            return
        s = self._dpi / 96
        region = hover_region((pt.x, pt.y), (r.left, r.top, r.right, r.bottom),
                              (self._gdi[6], self._gdi[7]), self._base_h, round(TOP_ROW_H * s))
        target = self._hover if region == "card" else region if region in ("ram", "rigs") else None
        if target == self._hover:
            self._pending = None
            if target == "ram":
                self._ram_age += 1
                if self._ram_age >= RAM_REFRESH_TICKS:
                    self._read_ram()
                    self._paint()
            return
        if target is not None:
            # Open only after the cursor rests, so passing over the panel does nothing.
            self._pending_n = self._pending_n + 1 if target == self._pending else 1
            self._pending = target
            if self._pending_n < HOVER_TICKS:
                return
        self._pending = None
        self._hover = target
        if target == "ram":
            self._read_ram()
        else:
            self._ram = None
        self._paint()

    def _free_gdi(self) -> None:
        if self._gdi:
            memdc, dib, _bits, font, old_bmp, old_font, *_ = self._gdi
            gdi32.SelectObject(memdc, old_font)
            gdi32.SelectObject(memdc, old_bmp)
            gdi32.DeleteObject(font)
            gdi32.DeleteObject(dib)
            gdi32.DeleteDC(memdc)
            self._gdi = None

    def _ensure_gdi(self, rows: int) -> bool:
        dpi = user32.GetDpiForWindow(self._hwnd) or 96
        if self._gdi and dpi == self._dpi and rows == self._rows:
            return True
        self._free_gdi()
        self._dpi, self._rows = dpi, rows
        self._base_h = BASE_H * dpi // 96
        w, h = BASE_W * dpi // 96, self._base_h + self._card_h(rows, dpi / 96)
        memdc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w,
                               biHeight=-h, biPlanes=1, biBitCount=32, biCompression=0)
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not dib:
            gdi32.DeleteDC(memdc)
            return False
        old_bmp = gdi32.SelectObject(memdc, dib)
        font = gdi32.CreateFontW(-12 * dpi // 96, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0,
                                 ANTIALIASED_QUALITY, 0, "Segoe UI")
        old_font = gdi32.SelectObject(memdc, font)
        gdi32.SetBkMode(memdc, TRANSPARENT_BK)
        # Alpha plane, built once per size: the rounded panel at 70%, 0 outside it.
        self._gdi = (memdc, dib, bits.value, font, old_bmp, old_font, w, h, b"")
        self._fill_panel(COL_TEXT)
        gdi32.GdiFlush()
        mask = ctypes.string_at(bits.value, w * h * 4)[0::4]
        alpha = mask.translate(bytes(v * OPACITY // 255 for v in range(256)))
        self._gdi = (memdc, dib, bits.value, font, old_bmp, old_font, w, h, alpha)
        return True

    @staticmethod
    def _card_h(rows: int, s: float) -> int:
        """Card height plus the gap below it; 0 with no card."""
        return round((ROW_H * rows + 12 + CARD_GAP) * s) if rows else 0

    def _fill_panel(self, color: int) -> None:
        memdc, *_rest = self._gdi
        w, h = self._gdi[6], self._gdi[7]
        black = gdi32.GetStockObject(4)   # BLACK_BRUSH
        user32.FillRect(memdc, ctypes.byref(wintypes.RECT(0, 0, w, h)), black)
        brush = gdi32.CreateSolidBrush(color)
        old_brush = gdi32.SelectObject(memdc, brush)
        old_pen = gdi32.SelectObject(memdc, gdi32.GetStockObject(NULL_PEN))
        r = 10 * self._dpi // 96
        gdi32.RoundRect(memdc, 0, h - self._base_h, w + 1, h + 1, r, r)
        if self._rows:
            card_h = h - self._base_h - round(CARD_GAP * self._dpi / 96)
            gdi32.RoundRect(memdc, 0, 0, w + 1, card_h + 1, r, r)
        gdi32.SelectObject(memdc, old_pen)
        gdi32.SelectObject(memdc, old_brush)
        gdi32.DeleteObject(brush)

    def _dot(self, x: int, y: int, d: int, state: int) -> None:
        """Filled dot for up and partial, a ring for down (as on the device)."""
        memdc = self._gdi[0]
        old_pen = gdi32.SelectObject(memdc, gdi32.GetStockObject(NULL_PEN))
        brush = gdi32.CreateSolidBrush(COL_STATE.get(state, COL_DIM))
        old_brush = gdi32.SelectObject(memdc, brush)
        gdi32.Ellipse(memdc, x, y, x + d + 1, y + d + 1)
        if state not in COL_STATE:
            inner = gdi32.CreateSolidBrush(COL_PANEL)
            gdi32.SelectObject(memdc, inner)
            k = max(1, d // 4)
            gdi32.Ellipse(memdc, x + k, y + k, x + d + 1 - k, y + d + 1 - k)
            gdi32.SelectObject(memdc, brush)
            gdi32.DeleteObject(inner)
        gdi32.SelectObject(memdc, old_brush)
        gdi32.SelectObject(memdc, old_pen)
        gdi32.DeleteObject(brush)

    def _bar(self, x: int, y: int, w: int, h: int, pct: int) -> None:
        memdc = self._gdi[0]
        track = gdi32.CreateSolidBrush(COL_TRACK)
        user32.FillRect(memdc, ctypes.byref(wintypes.RECT(x, y, x + w, y + h)), track)
        gdi32.DeleteObject(track)
        fill_w = w * max(0, min(100, pct)) // 100
        if fill_w:
            fill = gdi32.CreateSolidBrush(pct_color(pct))
            user32.FillRect(memdc, ctypes.byref(wintypes.RECT(x, y, x + fill_w, y + h)), fill)
            gdi32.DeleteObject(fill)

    def _text(self, text: str, x: int, y: int, w: int, h: int, color: int,
              flags: int = DT_LEFT_SINGLE) -> None:
        memdc = self._gdi[0]
        gdi32.SetTextColor(memdc, color)
        user32.DrawTextW(memdc, text, -1, ctypes.byref(wintypes.RECT(x, y, x + w, y + h)), flags)

    def _draw_card(self, card: tuple, pad: int, s: float, w: int) -> None:
        line_h = round(ROW_H * s)
        size_w, name_w, port_w = round(64 * s), round(60 * s), round(62 * s)
        y = round(6 * s)
        for row in card:
            kind = row[0]
            if kind == "proc":
                self._text(row[1], pad, y, w - 2 * pad - size_w, line_h, COL_TEXT)
                self._text(row[2], w - pad - size_w, y, size_w, line_h, COL_TEXT, DT_RIGHT_SINGLE)
            elif kind == "rig":
                _, name, state, ports = row
                d = round(8 * s)
                self._dot(pad, y + (line_h - d) // 2, d, state)
                self._text(name, pad + round(14 * s), y, name_w, line_h,
                           COL_TEXT if state in COL_STATE else COL_DIM)
                x = pad + round(76 * s)
                for label, port, is_open in ports:
                    self._text(f"{label} {port}", x, y, port_w, line_h,
                               COL_GREEN if is_open else COL_DIM)
                    x += port_w
            else:   # title, more
                self._text(row[1], pad, y, w - 2 * pad, line_h, COL_DIM)
            y += line_h

    def _paint(self) -> None:
        if not self._hwnd:
            return
        card = self._card()
        c = content(self._sampler.snapshot, card)
        if c is None or c == self._drawn:
            return
        if not self._ensure_gdi(len(card)):
            return
        memdc, _dib, bits, _f, _ob, _of, w, h, alpha = self._gdi
        s = self._dpi / 96
        pad, col_w = round(8 * s), (w - round(24 * s)) // 2
        line_h, bar_h = round(16 * s), max(2, round(3 * s))
        cpu_text, cpu, ram_text, ram_used, rigs_text, _card = c
        y0 = h - self._base_h

        self._fill_panel(COL_PANEL)
        self._text(cpu_text, pad, y0 + round(4 * s), col_w, line_h, COL_TEXT)
        self._text(ram_text, w - pad - col_w, y0 + round(4 * s), col_w, line_h, COL_TEXT)
        self._bar(pad, y0 + round(21 * s), col_w, bar_h, cpu)
        self._bar(w - pad - col_w, y0 + round(21 * s), col_w, bar_h, ram_used)
        self._text(rigs_text, pad, y0 + round(29 * s), w - 2 * pad, line_h, COL_DIM)
        if card:
            self._draw_card(card, pad, s, w)
        gdi32.GdiFlush()

        # Premultiply: every color byte scaled to 70%, then the alpha plane.
        n = w * h * 4
        px = bytearray(ctypes.string_at(bits, n).translate(_PREMUL))
        px[3::4] = alpha
        ctypes.memmove(bits, bytes(px), n)

        work = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        m = round(MARGIN * s)
        dst = wintypes.POINT(work.right - w - m, work.bottom - h - m)
        size = wintypes.SIZE(w, h)
        src = wintypes.POINT(0, 0)
        blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
        screen = user32.GetDC(None)
        try:
            user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(dst), ctypes.byref(size),
                                       memdc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        finally:
            user32.ReleaseDC(None, screen)
        self._drawn = c


_PREMUL = bytes(v * OPACITY // 255 for v in range(256))
