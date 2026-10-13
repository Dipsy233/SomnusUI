"""Windows window-enumeration logic (runs on Windows; skipped elsewhere).

These tests do not need a live desktop: they exercise the pure decision helpers
and the UWP process-resolution walk with lightweight stands-ins, which is what
makes UWP apps visible at all.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.windows


def _windows():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")


class _FakeElement:
    def __init__(
        self,
        *,
        name="",
        class_name="",
        pid=1,
        control_type=50032,
        hwnd=0,
        offscreen=False,
        children=(),
    ):
        self.CurrentName = name
        self.CurrentClassName = class_name
        self.CurrentProcessId = pid
        self.CurrentControlType = control_type
        self.CurrentNativeWindowHandle = hwnd
        self.CurrentIsOffscreen = offscreen
        self.CurrentHasKeyboardFocus = False
        self._children = list(children)


class _FakeWalker:
    def __init__(self, children):
        self._children = list(children)

    def GetFirstChildElement(self, _el):
        return self._children[0] if self._children else None

    def GetNextSiblingElement(self, el):
        i = self._children.index(el)
        return self._children[i + 1] if i + 1 < len(self._children) else None


class _FakeAutomation:
    def __init__(self, walker):
        self.ControlViewWalker = walker


def test_uwp_content_pid_walks_children_for_a_different_process():
    """ApplicationFrameHost frames host the real app in a child with its own pid."""
    _windows()
    from somnus.providers.windows.uia import UiaProvider

    frame = _FakeElement(name="设置", class_name="ApplicationFrameWindow", pid=100)
    # Two children owned by the host, then one owned by the real app.
    children = [
        _FakeElement(pid=100),
        _FakeElement(pid=100),
        _FakeElement(pid=4242),  # SystemSettings.exe
    ]
    provider = UiaProvider.__new__(UiaProvider)  # no COM init needed
    provider._auto = _FakeAutomation(_FakeWalker(children))

    assert provider._uwp_content_pid(frame) == 4242


def test_uwp_content_pid_returns_none_without_a_foreign_child():
    _windows()
    from somnus.providers.windows.uia import UiaProvider

    frame = _FakeElement(pid=100)
    children = [_FakeElement(pid=100), _FakeElement(pid=100)]
    provider = UiaProvider.__new__(UiaProvider)
    provider._auto = _FakeAutomation(_FakeWalker(children))
    assert provider._uwp_content_pid(frame) is None


def test_is_shell_chrome_drops_uwp_content_window():
    _windows()
    from somnus.providers.windows.uia import _is_shell_chrome

    assert _is_shell_chrome(_FakeElement(class_name="Windows.UI.Core.CoreWindow"))
    assert not _is_shell_chrome(_FakeElement(class_name="ApplicationFrameWindow"))
    assert not _is_shell_chrome(_FakeElement(class_name="CabinetWClass"))


def test_toplevel_control_types_cover_window_and_pane():
    _windows()
    from somnus.providers.windows.uia import _CT_PANE, _CT_WINDOW, _TOPLEVEL_CONTROL_TYPES

    assert _CT_WINDOW in _TOPLEVEL_CONTROL_TYPES
    assert _CT_PANE in _TOPLEVEL_CONTROL_TYPES


def test_enum_top_level_windows_returns_handles():
    _windows()
    from somnus.providers.windows.uia import _enum_top_level_windows

    windows = _enum_top_level_windows()
    # There is always at least one visible window in an interactive session.
    assert isinstance(windows, list)
    for hwnd, pid, title in windows:
        assert isinstance(hwnd, int) and hwnd
        assert isinstance(pid, int)
        assert isinstance(title, str) and title


def test_sendinput_structures_match_the_platform():
    """SendInput fails with ERROR_INVALID_PARAMETER if INPUT is the wrong size.

    INPUT is 40 bytes on x64 (DWORD type + 4 bytes padding + 32-byte union).
    A short union silently breaks every keyboard and mouse action.
    """
    _windows()
    import ctypes

    from somnus.providers.windows import keyboard, mouse

    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(keyboard._INPUT) == expected
    assert ctypes.sizeof(mouse._INPUT) == expected
