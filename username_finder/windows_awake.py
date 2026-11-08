"""Thread-scoped Windows system-idle request; never force the display or away mode."""

import ctypes
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def windows_api():
    if os.name != "nt":
        return None
    try:
        api = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
        api.argtypes = [ctypes.c_uint32]
        api.restype = ctypes.c_uint32
        return api
    except (AttributeError, OSError):
        return None


class AwakeRequest:
    """All calls belong to the same GUI thread; tests inject the native boundary."""

    def __init__(self, api=None):
        self.api = api if api is not None else windows_api()
        self.active = False
        self.error = ""
        self.closed = False

    def update(self, required):
        required = bool(required and not self.closed)
        if required == self.active:
            return
        if not self.api:
            self.error = "منع السكون غير مدعوم في هذه البيئة."
            return
        try:
            if not self.api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if required else ES_CONTINUOUS):
                raise OSError("Power request rejected")
        except (OSError, ValueError):
            self.error = "تعذر تأكيد طلب طاقة Windows؛ راجع السكون يدويًا."
            return
        self.active, self.error = required, ""

    def close(self):
        self.closed = True
        self.update(False)
