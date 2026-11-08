"""Narrow Windows desktop adapter. No network, credentials, screenshots or loops."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import PureWindowsPath

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

ERRORS = {
    "platform": "التفاعل المكتبي متاح على Windows فقط.",
    "dpi": "تعذر تأكيد إحداثيات الشاشة الفعلية؛ لم تُنفذ أي نقرة. أعد تشغيل التطبيق مع إعدادات العرض الحالية.",
    "target": "أظهر نافذة متصفح Snapchat Web في المقدمة، ثم أعد المعايرة.",
    "changed": "تغيرت نافذة الهدف أو عنوانها أو حجمها أو مقياس العرض؛ أعد المعايرة والمعاينة.",
    "point": "الموضع خارج نافذة الهدف أو تغطيه نافذة أخرى؛ لم تُنفذ النقرة.",
    "input": "تعذر إكمال إدخال النقرة في Windows؛ أُوقفت الجلسة دون إعادة محاولة.",
    "held": "حرّر أزرار الفأرة ومفاتيح Ctrl وAlt وShift وWindows قبل تنفيذ النقرة.",
    "hotkey": "تعذر حجز مفتاح الإيقاف F9؛ أغلق البرنامج الذي يستخدمه ثم أعد المحاولة.",
}


class DesktopError(Exception):
    def __init__(self, code):
        super().__init__(ERRORS[code])


@dataclass(frozen=True)
class Target:
    handle: int
    pid: int
    title: str
    browser: str
    rect: tuple[int, int, int, int]
    dpi: int
    desktop: tuple[int, int, int, int]

    def contains(self, point):
        x, y = point
        left, top, right, bottom = self.rect
        return left <= x < right and top <= y < bottom


class StopFilter(QAbstractNativeEventFilter):
    def __init__(self, adapter, callback):
        super().__init__()
        self.adapter, self.callback = adapter, callback

    def nativeEventFilter(self, kind, message):
        if bytes(kind) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = ctypes.cast(int(message), ctypes.POINTER(self.adapter.types.MSG)).contents
            if msg.message == 0x0312 and msg.wParam == self.adapter.HOTKEY:
                self.callback()
                return True, 0
        return False, 0


class WindowsDesktop:
    HOTKEY = 0x5343

    def __init__(self):
        self.filter = None
        self.available = os.name == "nt"
        if not self.available:
            return
        from ctypes import wintypes as w

        self.types = w
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "GetForegroundWindow": ([], w.HWND),
            "GetPhysicalCursorPos": ([ctypes.POINTER(w.POINT)], w.BOOL),
            "GetClientRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "ClientToScreen": ([w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            "GetWindowTextW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "GetDpiForWindow": ([w.HWND], w.UINT),
            "GetThreadDpiAwarenessContext": ([], w.HANDLE),
            "GetAwarenessFromDpiAwarenessContext": ([w.HANDLE], ctypes.c_int),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
            "GetAncestor": ([w.HWND, w.UINT], w.HWND),
            "WindowFromPoint": ([w.POINT], w.HWND),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "RegisterHotKey": ([w.HWND, ctypes.c_int, w.UINT, w.UINT], w.BOOL),
            "UnregisterHotKey": ([w.HWND, ctypes.c_int], w.BOOL),
            "SendInput": ([w.UINT, ctypes.c_void_p, ctypes.c_int], w.UINT),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            ctypes.POINTER(w.DWORD),
        ]
        self.kernel.QueryFullProcessImageNameW.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", w.LONG),
                ("dy", w.LONG),
                ("mouseData", w.DWORD),
                ("flags", w.DWORD),
                ("time", w.DWORD),
                ("extra", ctypes.c_size_t),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mouse", MouseInput)]  # MOUSEINPUT is the largest Windows INPUT variant.

        class Input(ctypes.Structure):
            _fields_ = [("kind", w.DWORD), ("value", InputUnion)]

        self.MouseInput, self.Input = MouseInput, Input

    def _check_platform(self):
        if not self.available:
            raise DesktopError("platform")
        context = self.user.GetThreadDpiAwarenessContext()
        if self.user.GetAwarenessFromDpiAwarenessContext(context) != 2:
            raise DesktopError("dpi")

    def snapshot(self):
        self._check_platform()
        w, user = self.types, self.user
        handle = user.GetForegroundWindow()
        title = ctypes.create_unicode_buffer(1025)
        user.GetWindowTextW(handle, title, len(title))
        if not handle or "snapchat" not in title.value.casefold():
            raise DesktopError("target")
        pid = w.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        process = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            raise DesktopError("target")
        try:
            path, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                raise DesktopError("target")
            browser = PureWindowsPath(path.value).name.lower()
        finally:
            self.kernel.CloseHandle(process)
        if browser not in ("chrome.exe", "msedge.exe", "firefox.exe"):
            raise DesktopError("target")
        rect, origin = w.RECT(), w.POINT()
        if not user.GetClientRect(handle, ctypes.byref(rect)) or not user.ClientToScreen(
            handle, ctypes.byref(origin)
        ):
            raise DesktopError("target")
        bounds = (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)
        desktop = tuple(user.GetSystemMetrics(index) for index in (76, 77, 78, 79))
        dpi = user.GetDpiForWindow(handle)
        if not dpi or rect.right <= 0 or rect.bottom <= 0 or min(desktop[2:]) <= 1:
            raise DesktopError("dpi")
        return Target(int(handle), pid.value, title.value, browser, bounds, dpi, desktop)

    def capture(self):
        target = self.snapshot()
        point = self.types.POINT()
        if not self.user.GetPhysicalCursorPos(ctypes.byref(point)):
            raise DesktopError("point")
        position = (point.x, point.y)
        self.validate(target, position)
        return target, position

    def validate(self, target, point):
        if self.snapshot() != target:
            raise DesktopError("changed")
        if not target.contains(point):
            raise DesktopError("point")
        root = self.user.GetAncestor(self.user.WindowFromPoint(self.types.POINT(*point)), 2)
        if root != target.handle:
            raise DesktopError("point")

    def arm_stop(self, callback):
        self._check_platform()
        if self.filter:
            raise DesktopError("hotkey")
        if not self.user.RegisterHotKey(None, self.HOTKEY, 0x4000, 0x78):  # MOD_NOREPEAT, F9
            raise DesktopError("hotkey")
        self.filter = StopFilter(self, callback)
        QCoreApplication.instance().installNativeEventFilter(self.filter)

    def disarm_stop(self):
        if self.filter:
            QCoreApplication.instance().removeNativeEventFilter(self.filter)
            self.filter = None
            if not self.user.UnregisterHotKey(None, self.HOTKEY):
                self.available = False
                raise DesktopError("hotkey")

    def click(self, target, point):
        self.validate(target, point)
        if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92)):
            raise DesktopError("held")
        left, top, width, height = target.desktop
        x = round((point[0] - left) * 65535 / (width - 1))
        y = round((point[1] - top) * 65535 / (height - 1))
        if not (0 <= x <= 65535 and 0 <= y <= 65535):
            raise DesktopError("point")
        events = (self.Input * 3)()
        for event, flags in zip(events, (0xC001, 0x0002, 0x0004), strict=True):
            event.kind = 0
            event.value.mouse = self.MouseInput(x, y, 0, flags, 0, 0)
        sent = self.user.SendInput(3, events, ctypes.sizeof(self.Input))
        if sent != 3:
            if sent == 2:
                # Release only a button this batch actually pressed; never leave it held.
                release = self.Input()
                release.value.mouse = self.MouseInput(0, 0, 0, 0x0004, 0, 0)
                self.user.SendInput(1, ctypes.byref(release), ctypes.sizeof(self.Input))
            raise DesktopError("input")
