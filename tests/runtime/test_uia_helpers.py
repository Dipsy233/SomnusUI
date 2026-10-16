"""Pure helpers from the Windows UIA provider.

The provider as a whole needs a live desktop, but these helpers are decision
logic with no COM in them - the cached-property readers, the value stringifier,
and the window-classifiers that decide what counts as a top-level application
window. They are where the subtle rules live (what a missing member means, what
`WS_EX_TOOLWINDOW` implies), so they are worth pinning without a window.

Windows-only: the module imports platform APIs at call time.

The shell popup menu (`_popup_menu_snapshot_sync`) and the taskbar tray folding
(`_attach_tray_overflow`) are not unit-tested here: their interesting branches
need a real open shell menu or a live tray flyout, which a test cannot conjure
without an actual menu on screen. Their common-path contracts ("no popup is
open" returns None) are checked below, and the paths that build real trees are
exercised by the `-m windows` end-to-end tests driving live windows.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from somnus.providers.windows import uia

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="UIA provider is Windows-only"
)


# ---------------------------------------------------------------------------
# cached-property readers
# ---------------------------------------------------------------------------


class _Cached:
    def __init__(self, values: dict) -> None:
        self._values = values

    def GetCachedPropertyValue(self, prop_id):
        if prop_id not in self._values:
            raise KeyError(prop_id)
        return self._values[prop_id]


def test_cached_prop_returns_none_when_the_property_raises():
    """An unreadable property is `None`, never an exception to the caller.

    A dead element (closed mid-read) must not take down a whole snapshot."""
    assert uia._cached_prop(_Cached({}), 1) is None


def test_cached_bool_is_true_only_for_the_boolean_true():
    """`1` is not `True` here: the providers report real booleans, and a
    1/0 masquerading as one would make every state check a guess."""
    assert uia._cached_bool(_Cached({1: True}), 1) is True
    assert uia._cached_bool(_Cached({1: 1}), 1) is False
    assert uia._cached_bool(_Cached({1: False}), 1) is False
    assert uia._cached_bool(_Cached({}), 1) is False


def test_cached_int_rejects_bools():
    """`isinstance(True, int)` is True in Python, so a bool must be excluded
    explicitly or an on/off flag would be read as a numeric value."""
    assert uia._cached_int(_Cached({1: 5}), 1) == 5
    assert uia._cached_int(_Cached({1: True}), 1) is None
    assert uia._cached_int(_Cached({1: 5.0}), 1) is None
    assert uia._cached_int(_Cached({}), 1) is None


def test_cached_str_only_returns_strings():
    assert uia._cached_str(_Cached({1: "hi"}), 1) == "hi"
    assert uia._cached_str(_Cached({1: 5}), 1) is None
    assert uia._cached_str(_Cached({}), 1) is None


def test_stringify_value_maps_plain_scalars_and_rejects_objects():
    """A value the semantic layer can print becomes a string; anything else is
    dropped rather than repr'd, because a native object's repr is not content."""
    assert uia._stringify_value(None) is None
    assert uia._stringify_value("x") == "x"
    assert uia._stringify_value(5) == "5"
    assert uia._stringify_value(5.5) == "5.5"
    assert uia._stringify_value(object()) is None


def test_getattr_returns_the_default_for_a_missing_member():
    """The documented trap: a missing member and a raising member look alike.

    Pinned so the behaviour is deliberate rather than incidental - a caller that
    needs to tell them apart must probe first (as `_build_node` does)."""
    class Obj:
        present = "yes"

        @property
        def raising(self):
            raise OSError("nope")

    assert uia._getattr(Obj(), "present", "d") == "yes"
    assert uia._getattr(Obj(), "absent", "d") == "d"
    assert uia._getattr(Obj(), "raising", "d") == "d"


# ---------------------------------------------------------------------------
# window classification
# ---------------------------------------------------------------------------


class _FakeUser32:
    def __init__(self, *, visible=True, class_name="", ex_style=0, owner=0):
        self._visible = visible
        self._class_name = class_name
        self._ex_style = ex_style
        self._owner = owner

    def IsWindowVisible(self, _hwnd):
        return self._visible

    def GetClassNameW(self, _hwnd, buf, _size):
        buf.value = self._class_name
        return len(self._class_name)

    def GetWindowLongW(self, _hwnd, _index):
        return self._ex_style

    def GetWindow(self, _hwnd, _which):
        return self._owner


@pytest.fixture
def fake_user32(monkeypatch):
    def _install(**kwargs):
        fake = _FakeUser32(**kwargs)
        monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake)
        return fake

    return _install


def test_is_shell_surface_accepts_the_taskbar_and_desktop(fake_user32):
    for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman"):
        fake_user32(visible=True, class_name=cls)
        assert uia._is_shell_surface(1) is True, cls


def test_is_shell_surface_rejects_an_ordinary_window(fake_user32):
    fake_user32(visible=True, class_name="Notepad")
    assert uia._is_shell_surface(1) is False


def test_is_shell_surface_rejects_a_hidden_shell_window(fake_user32):
    """Visibility is checked first: a hidden taskbar is not an operable surface."""
    fake_user32(visible=False, class_name="Shell_TrayWnd")
    assert uia._is_shell_surface(1) is False


def test_is_app_window_accepts_an_unowned_visible_window(fake_user32):
    fake_user32(visible=True, class_name="Notepad", ex_style=0, owner=0)
    assert uia._is_app_window(1) is True


def test_is_app_window_accepts_anything_with_ex_appwindow(fake_user32):
    """WS_EX_APPWINDOW is the shell's own "this belongs on the taskbar" flag."""
    fake_user32(visible=True, ex_style=0x00040000, owner=123)
    assert uia._is_app_window(1) is True


def test_is_app_window_rejects_tool_and_noactivate_windows(fake_user32):
    """Tool windows and no-activate panels are phantoms, not apps."""
    fake_user32(visible=True, ex_style=0x00000080)  # WS_EX_TOOLWINDOW
    assert uia._is_app_window(1) is False
    fake_user32(visible=True, ex_style=0x08000000)  # WS_EX_NOACTIVATE
    assert uia._is_app_window(1) is False


def test_is_app_window_rejects_owned_windows(fake_user32):
    """An owned window is a child dialog, not a top-level app."""
    fake_user32(visible=True, ex_style=0, owner=999)
    assert uia._is_app_window(1) is False


def test_is_app_window_rejects_a_hidden_window(fake_user32):
    fake_user32(visible=False, ex_style=0, owner=0)
    assert uia._is_app_window(1) is False


# ---------------------------------------------------------------------------
# popup menu: the common "no popup is open" path
# ---------------------------------------------------------------------------
#
# The interesting branches of `_popup_menu_snapshot_sync` need a real shell menu
# (a #32768 window, or a live XAML flyout), which a test cannot conjure without
# an actual open menu. What *can* be checked is the answer every ordinary moment
# gives: with no popup on screen it returns None rather than a stale tree - the
# contract the session's context_menu handling relies on to tell "a menu opened"
# from "nothing happened".


class _EmptyWalker:
    def GetFirstChildElement(self, _el):
        return None

    def GetNextSiblingElement(self, _el):
        return None


class _RootElement:
    CurrentNativeWindowHandle = 0


class _EmptyAutomation:
    ControlViewWalker = _EmptyWalker()

    def GetRootElement(self):
        return _RootElement()


def test_popup_snapshot_returns_none_when_nothing_is_open(fake_user32):
    """No popup window means no menu: None, not an empty tree."""
    provider = uia.UiaProvider.__new__(uia.UiaProvider)
    provider._auto = _EmptyAutomation()
    provider._UIA = object()  # only accessed after the first pass finds nothing
    provider._windows = {}

    # No candidate class matches, so the second pass finds nothing either.
    fake_user32(visible=True, class_name="Notepad")
    assert provider._popup_menu_snapshot_sync(max_depth=4) is None
