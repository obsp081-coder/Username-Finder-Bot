import ctypes
import os
from types import SimpleNamespace

import pytest
from snapscore_fixture import POINTS, TARGET

from username_finder.snapscore_desktop import DesktopError, WindowsDesktop

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows INPUT ABI and native event filter")


@pytest.fixture
def adapter(qapp, monkeypatch):
    desktop = WindowsDesktop()
    calls = []

    def send(count, data, size):
        values = ctypes.cast(data, ctypes.POINTER(desktop.Input))
        calls.append(
            (
                count,
                size,
                [
                    (values[i].value.mouse.flags, values[i].value.mouse.dx, values[i].value.mouse.dy)
                    for i in range(count)
                ],
            )
        )
        return count

    # Replace the entire user32 boundary before any adapter action. No OS input is sent.
    desktop.user = SimpleNamespace(
        SendInput=send,
        GetAsyncKeyState=lambda _: 0,
        RegisterHotKey=lambda *args: True,
        UnregisterHotKey=lambda *args: True,
        WindowFromPoint=lambda _: TARGET.handle,
        GetAncestor=lambda *args: TARGET.handle,
    )
    monkeypatch.setattr(desktop, "_check_platform", lambda: None)
    monkeypatch.setattr(desktop, "snapshot", lambda: TARGET)
    yield desktop, calls
    desktop.disarm_stop()


def test_windows_input_layout_and_single_balanced_click(adapter):
    desktop, calls = adapter
    assert ctypes.sizeof(desktop.Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    desktop.click(TARGET, POINTS[0])
    assert len(calls) == 1 and calls[0][0] == 3
    assert [event[0] for event in calls[0][2]] == [0xC001, 2, 4]
    assert all(0 <= value <= 65535 for value in calls[0][2][0][1:])


@pytest.mark.parametrize("sent", [0, 1, 2])
def test_partial_os_input_fails_and_only_owned_press_is_released(adapter, sent):
    desktop, _ = adapter
    counts = []

    def partial(count, data, size):
        counts.append(count)
        return sent if len(counts) == 1 else count

    desktop.user.SendInput = partial
    with pytest.raises(DesktopError):
        desktop.click(TARGET, POINTS[0])
    assert counts == ([3, 1] if sent == 2 else [3])


@pytest.mark.parametrize("case", ["focus", "outside", "occluded", "held"])
def test_desktop_guards_block_before_os_input(adapter, monkeypatch, case):
    desktop, calls = adapter
    point = POINTS[0]
    if case == "focus":
        monkeypatch.setattr(desktop, "snapshot", lambda: None)
    elif case == "outside":
        point = (-1, -1)
    elif case == "occluded":
        desktop.user.GetAncestor = lambda *args: 999
    else:
        desktop.user.GetAsyncKeyState = lambda _: 0x8000
    with pytest.raises(DesktopError):
        desktop.click(TARGET, point)
    assert not calls


def test_global_stop_filter_and_registration_failure(adapter):
    desktop, calls = adapter
    stopped = []
    desktop.arm_stop(lambda: stopped.append(True))
    msg = desktop.types.MSG()
    msg.message, msg.wParam = 0x0312, desktop.HOTKEY
    assert desktop.filter.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(msg)) == (True, 0)
    assert stopped == [True] and not calls
    desktop.disarm_stop()
    assert desktop.filter is None
    desktop.user.RegisterHotKey = lambda *args: False
    with pytest.raises(DesktopError):
        desktop.arm_stop(lambda: None)
    assert desktop.filter is None and not calls


def test_stop_cleanup_failure_disables_further_desktop_use(adapter):
    desktop, calls = adapter
    desktop.arm_stop(lambda: None)
    desktop.user.UnregisterHotKey = lambda *args: False
    with pytest.raises(DesktopError):
        desktop.disarm_stop()
    assert desktop.filter is None and not desktop.available and not calls
