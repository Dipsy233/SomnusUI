"""Guards on the synthetic double-click path.

A coordinate click is the one primitive that can affect a window *other* than
the target, so it must refuse to fire when it cannot prove the target is where
it says it is. These tests cover the two refusals found live:

* a degenerate rectangle (a zero-area element), which previously passed a
  ``x <= 0 and y <= 0`` check and clicked the desktop origin;
* a point owned by a different process (the window is not foreground), which
  would click whatever is actually on screen.
"""

from __future__ import annotations

from types import SimpleNamespace

from somnus.ir.action import ExecResult
from somnus.providers.windows import executor


def _record_click(called):
    def _click(x, y):
        called.append((x, y))
        return ExecResult(ok=True)

    return _click


class _FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeElement:
    def __init__(self, rect, pid=42, hwnd=0):
        self.CurrentBoundingRectangle = rect
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = hwnd

    def SetFocus(self):
        pass


def _fake_uia():
    return SimpleNamespace(
        UIA_ScrollItemPatternId=1,
        IUIAutomationScrollItemPattern=object,
        UIA_WindowPatternId=2,
        IUIAutomationWindowPattern=object,
    )


class _FakeWin32:
    """A minimal user32 stand-in whose members accept attribute assignment."""

    class _Fn:
        restype = None
        argtypes = None

        def __init__(self, fn):
            self._fn = fn

        def __call__(self, *a, **k):
            return self._fn(*a, **k)

    def __init__(self, hit_pid: int):
        self.WindowFromPoint = self._Fn(lambda pt: 1234)

        def _get_pid(hwnd, out):
            # The real call passes a byref(); follow it to the DWORD so the
            # stub behaves like the Win32 signature.
            target = getattr(out, "_obj", out)
            target.value = hit_pid

        self.GetWindowThreadProcessId = self._Fn(_get_pid)


def _patched_user32(hit_pid: int):
    return lambda *a, **k: _FakeWin32(hit_pid)


def test_zero_area_element_is_refused(monkeypatch):
    """A degenerate rect means there is nothing to click; the old
    ``x <= 0 and y <= 0`` test let a zero-size rect through whenever its centre
    was positive, so the click fired at a meaningless point."""
    called = []
    monkeypatch.setattr(executor, "double_click", _record_click(called))
    monkeypatch.setattr(
        executor, "_ensure_visible", lambda el, uia: executor.ExecResult(ok=True)
    )
    el = _FakeElement(_FakeRect(100, 100, 100, 100))
    result = executor._double_click_element(el, _fake_uia())
    assert not result.ok
    assert result.error == "ACTION_NOT_SUPPORTED"
    assert not called


def test_element_that_cannot_be_raised_is_refused(monkeypatch):
    """If the owning window cannot be restored/raised, do not click blindly."""
    called = []
    monkeypatch.setattr(executor, "double_click", _record_click(called))
    monkeypatch.setattr(
        executor,
        "_ensure_visible",
        lambda el, uia: executor.ExecResult(
            ok=False, error="TARGET_NOT_FOREGROUND", message="cannot raise"
        ),
    )
    el = _FakeElement(_FakeRect(0, 0, 200, 40))
    result = executor._double_click_element(el, _fake_uia())
    assert not result.ok
    assert result.error == "TARGET_NOT_FOREGROUND"
    assert not called


def test_click_is_refused_when_another_process_owns_the_point(monkeypatch):
    """The hit-test must catch "the window is not actually there" instead of
    clicking whatever occupies those pixels."""
    called = []
    monkeypatch.setattr(executor, "double_click", _record_click(called))
    monkeypatch.setattr(
        executor, "_ensure_visible", lambda el, uia: executor.ExecResult(ok=True)
    )
    monkeypatch.setattr(executor.ctypes, "WinDLL", _patched_user32(999))
    el = _FakeElement(_FakeRect(0, 0, 200, 40), pid=42)
    result = executor._double_click_element(el, _fake_uia())
    assert not result.ok
    assert result.error == "TARGET_NOT_FOREGROUND"
    assert not called


def test_click_proceeds_when_target_owns_the_point(monkeypatch):
    called = []
    monkeypatch.setattr(executor, "double_click", _record_click(called))
    monkeypatch.setattr(
        executor, "_ensure_visible", lambda el, uia: executor.ExecResult(ok=True)
    )
    monkeypatch.setattr(executor.ctypes, "WinDLL", _patched_user32(42))
    el = _FakeElement(_FakeRect(0, 0, 200, 40), pid=42)
    result = executor._double_click_element(el, _fake_uia())
    assert result.ok
    assert called == [(100, 20)]


def test_click_is_refused_when_the_hit_test_cannot_run(monkeypatch):
    """The guard fails closed: not knowing what is under the point is exactly
    the case it exists to catch, so it must refuse rather than click."""
    called = []
    monkeypatch.setattr(executor, "double_click", _record_click(called))
    monkeypatch.setattr(
        executor, "_ensure_visible", lambda el, uia: executor.ExecResult(ok=True)
    )

    def _boom(*a, **k):
        raise OSError("no user32")

    monkeypatch.setattr(executor.ctypes, "WinDLL", _boom)
    el = _FakeElement(_FakeRect(0, 0, 200, 40), pid=42)
    result = executor._double_click_element(el, _fake_uia())
    assert not result.ok
    assert result.error == "TARGET_NOT_FOREGROUND"
    assert not called
