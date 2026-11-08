"""Check the Windows pythonw entrypoint with a separate database and close only its window."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path


def child_process_ids(parent_id):
    class ProcessEntry(ctypes.Structure):
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
            ("executable", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    assert handle != wintypes.HANDLE(-1).value
    parents = {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            parents[entry.pid] = entry.parent
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    result = {parent_id}
    while children := {pid for pid, parent in parents.items() if parent in result} - result:
        result.update(children)
    return result


def main():
    if os.name != "nt":
        raise SystemExit("This smoke check requires Windows.")
    root = Path(__file__).resolve().parents[1]
    directory = root / "runtime" / ("launcher-quality-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    directory.mkdir(parents=True)
    database = directory / "isolated.db"
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    environment = os.environ.copy()
    environment.pop("QT_QPA_PLATFORM", None)
    log = (directory / "launcher.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(root / ".venv/Scripts/pythonw.exe"), "-m", "username_finder", "--database", str(database)],
        cwd=root,
        env=environment,
        stdout=log,
        stderr=log,
    )
    windows = []
    observed = set()
    process_ids = {process.pid}

    @callback_type
    def collect(handle, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value in process_ids and user32.IsWindowVisible(handle):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, title, len(title))
            observed.add(title.value)
            if title.value == "Username Finder Bot — Snapchat":
                windows.append(handle)
        return True

    report = {}
    try:
        deadline = time.monotonic() + 5
        while not windows and time.monotonic() < deadline and process.poll() is None:
            # Windows virtual environments use a redirector and a child pythonw process.
            process_ids = child_process_ids(process.pid)
            user32.EnumWindows(collect, 0)
            time.sleep(0.02)
        assert windows, f"No application window: exit={process.poll()}, titles={observed}. See launcher.log."
        time.sleep(0.15)
        report["responding"] = not bool(user32.IsHungAppWindow(windows[0]))
        assert report["responding"]
        assert user32.PostMessageW(windows[0], 0x0010, 0, 0)  # WM_CLOSE for this child only.
        report["exit_code"] = process.wait(timeout=5)
        assert report["exit_code"] == 0
        report["test_window_closed"] = True
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        log.close()
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("REPORT=" + str(directory / "report.json"))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
