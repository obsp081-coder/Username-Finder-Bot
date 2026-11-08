"""One scoped Windows Generic Credential; no plaintext password persistence."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

TARGET = "UsernameFinderBot/Osintgram/Instagram"


class CredentialError(RuntimeError):
    def __init__(self):
        super().__init__("تعذر الوصول إلى Windows Credential Manager؛ لم تُحفظ كلمة المرور في ملف بديل.")


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentials:
    def __init__(self, api=None):
        self._api = api

    @property
    def api(self):
        if self._api is None:
            if os.name != "nt":
                raise CredentialError()
            api = ctypes.WinDLL("advapi32", use_last_error=True)
            api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
            api.CredWriteW.restype = wintypes.BOOL
            api.CredReadW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.POINTER(Credential)),
            ]
            api.CredReadW.restype = wintypes.BOOL
            api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            api.CredDeleteW.restype = wintypes.BOOL
            api.CredFree.argtypes = [ctypes.c_void_p]
            self._api = api
        return self._api

    def save(self, username, password):
        try:
            encoded = password.encode("utf-16-le")
        except UnicodeError:
            raise CredentialError() from None
        if "\x00" in password:
            raise CredentialError()
        if not username or not encoded or len(encoded) > 2560:
            raise CredentialError()
        blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        value = Credential(
            Type=1,
            TargetName=TARGET,
            UserName=username,
            CredentialBlobSize=len(encoded),
            CredentialBlob=blob,
            Persist=2,
        )
        try:
            if not self.api.CredWriteW(ctypes.byref(value), 0):
                raise CredentialError()
        finally:
            ctypes.memset(blob, 0, len(blob))

    def load(self):
        pointer = ctypes.POINTER(Credential)()
        if not self.api.CredReadW(TARGET, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise CredentialError()
        try:
            item = pointer.contents
            if (
                not item.UserName
                or not item.CredentialBlob
                or not 0 < item.CredentialBlobSize <= 2560
                or item.CredentialBlobSize % 2
            ):
                raise CredentialError()
            try:
                password = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-16-le")
            except UnicodeError:
                raise CredentialError() from None
            return item.UserName, password
        finally:
            self.api.CredFree(pointer)

    def delete(self):
        if not self.api.CredDeleteW(TARGET, 1, 0) and ctypes.get_last_error() != 1168:
            raise CredentialError()
