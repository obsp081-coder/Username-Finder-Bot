"""Owned Qt subprocesses with Windows containment and allowlisted diagnostics."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


class WindowsJob:
    """Contain only the process we started and its venv redirector descendants."""

    def __init__(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        for name in ("CreateJobObjectW", "OpenProcess", "CreateToolhelp32Snapshot"):
            getattr(kernel, name).restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("job creation failed")

        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]

        info = Extended(basic=Limits(flags=0x2000))  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("job limits failed")
        try:
            self._assign(pid)

            class Entry(ctypes.Structure):
                _fields_ = [
                    ("size", wintypes.DWORD),
                    ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD),
                    ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD),
                    ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD),
                    ("exe", wintypes.WCHAR * 260),
                ]

            kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
            kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
            snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
            parents = {}
            try:
                item = Entry(size=ctypes.sizeof(Entry))
                more = kernel.Process32FirstW(snapshot, ctypes.byref(item))
                while more:
                    parents[item.pid] = item.parent
                    more = kernel.Process32NextW(snapshot, ctypes.byref(item))
            finally:
                kernel.CloseHandle(snapshot)
            owned = {pid}
            while children := {child for child, parent in parents.items() if parent in owned} - owned:
                for child in children:
                    self._assign(child, allow_exited=True)
                owned.update(children)
        except Exception:
            self.close()
            raise

    def _assign(self, pid, allow_exited=False):
        process = self.kernel.OpenProcess(0x0501, False, pid)  # QUERY_INFORMATION | SET_QUOTA | TERMINATE
        if not process:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: vanished PID
                if allow_exited:
                    return
                raise ProcessLookupError("owned interpreter already exited")
            raise OSError("cannot contain owned process")
        try:

            def exited():
                code = wintypes.DWORD()
                return (
                    self.kernel.GetExitCodeProcess(process, ctypes.byref(code))
                    and code.value != 259  # STILL_ACTIVE
                )

            if exited():
                if allow_exited:
                    return
                raise ProcessLookupError("owned interpreter already exited")
            contained = wintypes.BOOL()
            self.kernel.IsProcessInJob.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.BOOL),
            ]
            if self.kernel.IsProcessInJob(process, self.handle, ctypes.byref(contained)) and contained.value:
                return
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                if exited():
                    if allow_exited:
                        return
                    raise ProcessLookupError("owned interpreter already exited")
                raise OSError("cannot contain owned process")
        finally:
            self.kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def child_environment(account=None):
    source = QProcessEnvironment.systemEnvironment()
    result = QProcessEnvironment()
    for name in (
        "PATH",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PATHEXT",
    ):
        if source.contains(name):
            result.insert(name, source.value(name))
    result.insert("PYTHONIOENCODING", "utf-8")
    result.insert("PYTHONUNBUFFERED", "1")
    result.insert("INSTAGRAM_BACKEND", "instagrapi")
    if account:
        result.insert("INSTAGRAM_USERNAME", account[0])
        result.insert("INSTAGRAM_PASSWORD", account[1])
    return result


class OwnedProcess(QObject):
    started = Signal()
    ended = Signal(int)
    output = Signal(bytes)
    failed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.job = None
        self.diagnostic_code = ""
        self._stderr_tail = b""
        self.process.started.connect(self._started)
        self.process.finished.connect(self._ended)
        self.process.errorOccurred.connect(self._error)
        self.process.readyReadStandardOutput.connect(
            lambda: self.output.emit(bytes(self.process.readAllStandardOutput()))
        )
        self.process.readyReadStandardError.connect(self._read_diagnostics)

    @property
    def active(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self, python, arguments, directory, environment, capture=False):
        self.process.setWorkingDirectory(str(directory))
        self.process.setProcessEnvironment(environment)
        # Drain stderr promptly. Retain only a bounded parsing tail and fixed codes,
        # never forward subprocess text to the UI, settings or logs.
        if not capture:
            self.process.setStandardOutputFile(QProcess.nullDevice())
        self.process.start(str(python), arguments)

    def _read_diagnostics(self):
        chunk = bytes(self.process.readAllStandardError())
        for offset in range(0, len(chunk), 2048):
            text = (self._stderr_tail + chunk[offset : offset + 2048]).lower()
            patterns = (
                ("port_in_use", (b"winerror 10048", b"errno 10048", b"errno 98", b"address already in use")),
                (
                    "permission",
                    (b"permissionerror", b"permission denied", b"winerror 10013", b"access is denied"),
                ),
                ("python_dependency", (b"modulenotfounderror", b"importerror", b"no module named")),
                ("application_import", (b"error loading asgi app", b"could not import module")),
                ("application_startup", (b"application startup failed",)),
            )
            for code, markers in patterns:
                if any(marker in text for marker in markers):
                    self.diagnostic_code = code
                    break
            self._stderr_tail = text[-256:]

    def _started(self):
        self.process.setProcessEnvironment(QProcessEnvironment())
        try:
            if os.name == "nt":
                self.job = WindowsJob(self.process.processId())
        except ProcessLookupError:
            # Preserve the natural exit; Qt will deliver its finished signal.
            return
        except OSError:
            self.diagnostic_code = "windows_job"
            self._stderr_tail = b""
            self.process.kill()
            self.failed.emit()
            return
        self.started.emit()

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.diagnostic_code = (
                "working_directory" if not os.path.isdir(self.process.workingDirectory()) else "process_start"
            )
            self.process.setProcessEnvironment(QProcessEnvironment())
            self.failed.emit()

    def _ended(self, code, *_):
        self._read_diagnostics()
        self._stderr_tail = b""
        if self.job:
            self.job.close()
            self.job = None
        self.ended.emit(code)

    def write(self, value):
        self.process.write(value)

    def stop(self):
        if self.job:
            self.job.close()
            self.job = None
        if self.active:
            self.process.terminate()
            QTimer.singleShot(1500, self._kill_if_running)

    def _kill_if_running(self):
        if self.active:
            self.process.kill()
