"""Thin, defensive wrappers around the Win32 APIs used by the app."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable, Optional

import psutil
import win32api
import win32con
import win32gui
import win32process

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOW = 5

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11

ERROR_ALREADY_EXISTS = 183
SPI_GETWORKAREA = 0x0030
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_MUTEX = None
_SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "DV2ControlHost",
    "MsgrIMEWindowClass",
    "SysShadow",
    "Button",
    "tooltips_class32",
    "TaskListThumbnailWnd",
}


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def as_dict(self) -> dict:
        return {"left": int(self.left), "top": int(self.top),
                "width": int(self.width), "height": int(self.height)}


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def enable_dpi_awareness() -> None:
    """Must run before Tk/mss so coordinates match physical pixels."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def dpi_for_window(hwnd: int) -> float:
    try:
        dpi = user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    try:
        dc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, dc)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def workarea() -> tuple[int, int, int, int]:
    rect = RECT()
    try:
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.width, rect.height
    except Exception:
        pass
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen() -> tuple[int, int, int, int]:
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    return left, top, max(width, 1), max(height, 1)


@dataclass
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool
    name: str
    device: str

    @property
    def rect(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    @property
    def label(self) -> str:
        suffix = " · основной" if self.primary else ""
        return f"Экран {self.index} · {self.width}×{self.height}{suffix}"


def monitors() -> list[Monitor]:
    result: list[Monitor] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(RECT), wintypes.LPARAM)

    def callback(hmon, hdc, lprc, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            result.append((hmon, info))
        return True

    try:
        user32.EnumDisplayMonitors(0, 0, proc(callback), 0)
    except Exception:
        pass

    out: list[Monitor] = []
    for idx, (_hmon, info) in enumerate(result, 1):
        rect = info.rcMonitor
        out.append(Monitor(
            index=idx,
            left=rect.left, top=rect.top, width=rect.width, height=rect.height,
            primary=bool(info.dwFlags & 1),
            name=info.szDevice.replace("\\\\.\\", ""),
            device=info.szDevice,
        ))
    if not out:
        out.append(Monitor(1, 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), True,
                           "DISPLAY1", "\\\\.\\DISPLAY1"))
    out.sort(key=lambda m: (not m.primary, m.left, m.top))
    for i, m in enumerate(out, 1):
        m.index = i
    return out


def find_monitor(name: str = "", rect: Optional[dict] = None) -> Optional[Monitor]:
    mons = monitors()
    if name:
        for m in mons:
            if m.device == name or m.name == name:
                return m
    if rect:
        for m in mons:
            if (m.left == rect.get("left") and m.top == rect.get("top")
                    and m.width == rect.get("width") and m.height == rect.get("height")):
                return m
    return mons[0] if mons else None


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    rect: dict
    process: str
    class_name: str

    @property
    def label(self) -> str:
        title = self.title.strip() or self.process or "Окно"
        if len(title) > 60:
            title = title[:57] + "…"
        return title


def window_frame(hwnd: int) -> Optional[dict]:
    rect = RECT()
    try:
        res = dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                           ctypes.byref(rect), ctypes.sizeof(rect))
        if res == 0 and rect.width > 0 and rect.height > 0:
            return rect.as_dict()
    except Exception:
        pass
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if r > l and b > t:
            return {"left": l, "top": t, "width": r - l, "height": b - t}
    except Exception:
        pass
    return None


def _is_cloaked(hwnd: int) -> bool:
    value = wintypes.DWORD(0)
    try:
        res = dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                           ctypes.byref(value), ctypes.sizeof(value))
        return res == 0 and value.value != 0
    except Exception:
        return False


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().replace(".exe", "")
    except Exception:
        return ""


def list_windows(exclude_pid: Optional[int] = None, include_minimized: bool = False) -> list[WindowInfo]:
    exclude_pid = exclude_pid if exclude_pid is not None else os.getpid()
    items: list[WindowInfo] = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not include_minimized and win32gui.IsIconic(hwnd):
                return True
            if _is_cloaked(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd)
            if cls in _SKIP_CLASSES:
                return True
            exstyle = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
            if exstyle & WS_EX_TOOLWINDOW:
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == exclude_pid:
                return True
            rect = window_frame(hwnd)
            if not rect or rect["width"] < 160 or rect["height"] < 100:
                return True
            items.append(WindowInfo(hwnd, title, rect, _process_name(pid), cls))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass
    return items


def find_window(hwnd: int = 0, title: str = "") -> Optional[WindowInfo]:
    if hwnd and win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd):
        try:
            rect = window_frame(hwnd)
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            if rect:
                return WindowInfo(hwnd, win32gui.GetWindowText(hwnd) or title, rect,
                                  _process_name(pid), win32gui.GetClassName(hwnd))
        except Exception:
            pass
    if title:
        for w in list_windows(include_minimized=False):
            if w.title == title:
                return w
        for w in list_windows(include_minimized=False):
            if title.lower() in w.title.lower():
                return w
    return None


def window_at_point(x: int, y: int, exclude_pid: Optional[int] = None) -> Optional[WindowInfo]:
    exclude_pid = exclude_pid if exclude_pid is not None else os.getpid()
    best: Optional[WindowInfo] = None
    for w in list_windows(exclude_pid=exclude_pid, include_minimized=False):
        r = w.rect
        if r["left"] <= x < r["left"] + r["width"] and r["top"] <= y < r["top"] + r["height"]:
            best = w  # EnumWindows is top-down, the last match is the top-most child
            break
    return best


def cursor_position() -> tuple[int, int]:
    try:
        return win32api.GetCursorPos()
    except Exception:
        return (0, 0)


def get_hwnd(widget) -> int:
    try:
        hwnd = user32.GetParent(widget.winfo_id())
        return hwnd or widget.winfo_id()
    except Exception:
        return 0


def make_taskbar_window(hwnd: int) -> None:
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)  # FRAMECHANGED|NOMOVE|NOSIZE|NOZORDER
    except Exception:
        pass


def set_round_region(hwnd: int, width: int, height: int, radius: int) -> None:
    try:
        rgn = gdi32.CreateRoundRectRgn(0, 0, int(width) + 1, int(height) + 1,
                                       int(radius) * 2, int(radius) * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def minimize(hwnd: int) -> None:
    try:
        user32.ShowWindow(hwnd, SW_MINIMIZE)
    except Exception:
        pass


def is_minimized(hwnd: int) -> bool:
    try:
        return bool(user32.IsIconic(hwnd))
    except Exception:
        return False


def set_no_activate(hwnd: int) -> None:
    """Keep a helper window out of Alt+Tab and stop it stealing focus."""
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x27)
    except Exception:
        pass


def restore(hwnd: int) -> None:
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def bring_to_front(hwnd: int) -> None:
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def set_capture_excluded(hwnd: int, excluded: bool) -> bool:
    try:
        ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if excluded else WDA_NONE)
        return bool(ok)
    except Exception:
        return False


def boost_thread_priority() -> None:
    try:
        h = kernel32.GetCurrentThread()
        kernel32.SetThreadPriority(h, 2)  # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass


def prevent_sleep(enabled: bool) -> None:
    try:
        flags = ES_CONTINUOUS
        if enabled:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def single_instance() -> bool:
    """False when another copy is already running."""
    global _MUTEX
    try:
        _MUTEX = kernel32.CreateMutexW(None, False, "Local\\SRPro_ScreenRecorder_v4")
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def activate_existing() -> None:
    # The window may be hidden in the tray, so FindWindow (which also sees
    # hidden windows) is used before the generic visible-window scan.
    try:
        import win32gui

        hwnd = win32gui.FindWindow(None, "SR Pro")
        if hwnd:
            bring_to_front(hwnd)
            return
    except Exception:
        pass
    for w in list_windows(exclude_pid=-1, include_minimized=True):
        if w.title.startswith("SR Pro") and "Recorder" not in w.title:
            bring_to_front(w.hwnd)
            return


def beep(freq: int = 880, duration_ms: int = 90) -> None:
    try:
        import winsound
        winsound.Beep(freq, duration_ms)
    except Exception:
        pass


def foreground_process_name() -> str:
    try:
        hwnd = win32gui.GetForegroundWindow()
        _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
        return _process_name(pid)
    except Exception:
        return ""
