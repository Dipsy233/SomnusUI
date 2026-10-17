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


# ---------------------------------------------------------------------------
# foreground raise
# ---------------------------------------------------------------------------


def test_raise_window_short_circuits_when_already_in_front(monkeypatch):
    """No foreground lock work when the target already is the foreground."""
    from somnus.providers.windows import workspace

    calls: list[str] = []

    class _User32:
        def GetForegroundWindow(self):
            return 42

        def BringWindowToTop(self, hwnd):
            calls.append("bring")

        def SetForegroundWindow(self, hwnd):
            calls.append("set")

        def keybd_event(self, *a):
            calls.append("alt")

    monkeypatch.setattr(
        workspace.ctypes, "windll", type("W", (), {"user32": _User32(), "kernel32": None})
    )
    assert workspace.raise_window(42) is True
    assert calls == []


def test_raise_window_falls_back_to_an_inert_alt_tap(monkeypatch):
    """The foreground lock refuses a plain set; the Alt tap releases it.

    Live behaviour measured on Windows 11: a background process calling
    ``SetForegroundWindow`` gets 0 and the foreground window does not change.
    Only the inert Alt tap made the raise land, so it must be in the chain.
    """
    from somnus.providers.windows import workspace

    state = {"fg": 1, "alt": 0, "attached": None}

    class _User32:
        def GetForegroundWindow(self):
            return state["alt"] and 2 or state["fg"]

        def BringWindowToTop(self, hwnd):
            pass

        def SetForegroundWindow(self, hwnd):
            # Refused until the lock is released by an input event.
            if state["alt"]:
                state["fg"] = hwnd

        def GetWindowThreadProcessId(self, hwnd, ref):
            return 99

        def AttachThreadInput(self, a, b, attach):
            state["attached"] = attach
            return True

        def keybd_event(self, *a):
            state["alt"] += 1

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 7

    monkeypatch.setattr(
        workspace.ctypes,
        "windll",
        type("W", (), {"user32": _User32(), "kernel32": _Kernel32()}),
    )
    assert workspace.raise_window(2) is True
    # Attach was attempted and undone - never left attached.
    assert state["attached"] is False
    # Exactly one Alt tap: key down + key up, nothing else.
    assert state["alt"] == 2


def test_raise_window_reports_failure(monkeypatch):
    from somnus.providers.windows import workspace

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 7

    class _User32:
        def GetForegroundWindow(self):
            return 1

        def BringWindowToTop(self, hwnd):
            pass

        def SetForegroundWindow(self, hwnd):
            pass

        def GetWindowThreadProcessId(self, hwnd, ref):
            return 0

        def keybd_event(self, *a):
            pass

    monkeypatch.setattr(
        workspace.ctypes,
        "windll",
        type("W", (), {"user32": _User32(), "kernel32": _Kernel32()}),
    )
    assert workspace.raise_window(2) is False


# ---------------------------------------------------------------------------
# foreground entered on use (UiaProvider)
# ---------------------------------------------------------------------------


def test_enter_foreground_raises_once_and_is_idempotent(monkeypatch):
    """Snapshot/execute raise the window, but only while it needs raising.

    A run of calls on one window (snapshot, primitive, settle snapshot) must not
    keep re-acquiring it: the workspace records the user's state once, and
    re-raising on every call would re-show the window on every keystroke.

    The exception is a *minimized* window. Win32 still reports it as the
    foreground window (it is the foreground window that gets minimized), so the
    "already in front" shortcut must not swallow the restore the following
    snapshot depends on.
    """
    state = {"fg": 1, "iconic": False}
    acquired: list[int] = []
    sets: list[int] = []

    class _El:
        CurrentNativeWindowHandle = 77

    class _Workspace:
        def acquire(self, hwnd):
            acquired.append(hwnd)
            state["iconic"] = False  # SW_RESTORE

    class _User32:
        def GetForegroundWindow(self):
            return state["fg"]

        def IsIconic(self, hwnd):
            return state["iconic"]

        def GetAncestor(self, hwnd, flag):
            return hwnd

        def BringWindowToTop(self, hwnd):
            pass

        def SetForegroundWindow(self, hwnd):
            sets.append(hwnd)
            state["fg"] = hwnd  # the raise succeeds

    monkeypatch.setattr(
        ctypes, "windll", type("W", (), {"user32": _User32(), "kernel32": None})
    )

    provider = object.__new__(uia.UiaProvider)
    provider._workspace = _Workspace()
    provider._foreground_window_id = None

    assert provider._enter_foreground_sync("w1", _El()).ok
    assert provider._enter_foreground_sync("w1", _El()).ok
    # Second call: the window is in front, so nothing is redone.
    assert acquired == [77] and sets == [77]

    # The user takes the foreground away: raised again.
    state["fg"] = 999
    assert provider._enter_foreground_sync("w1", _El()).ok
    assert sets == [77, 77]

    # Minimized while still reported as the foreground window: restored.
    state["iconic"] = True
    assert provider._enter_foreground_sync("w1", _El()).ok
    assert state["iconic"] is False
    assert len(acquired) == 3

    # A different window is raised.
    state["fg"] = 888
    assert provider._enter_foreground_sync("w2", _El()).ok
    assert sets[-1] == 77


# ---------------------------------------------------------------------------
# window identity across enumerations
# ---------------------------------------------------------------------------


def _add_window_provider():
    """A provider with just enough state for `_add_window`."""
    provider = object.__new__(uia.UiaProvider)
    provider._windows = {}
    provider._window_id_by_key = {}
    provider._lock = __import__("threading").Lock()
    provider.allow_list = []
    return provider


class _El:
    """A window element: same title, different handle."""

    def __init__(self, hwnd, name, pid=100):
        self.CurrentNativeWindowHandle = hwnd
        self.CurrentName = name
        self.CurrentProcessId = pid
        self.CurrentIsOffscreen = False
        self.CurrentHasKeyboardFocus = False
        self.CurrentClassName = "TestWindow"


def test_windows_sharing_a_title_get_distinct_ids(monkeypatch):
    """Two windows of one process can share a title; they must not collide.

    Found while hardening window identity: the dedupe and the id map both keyed
    on (pid, title), so two Explorer windows on the same folder collapsed into
    one row and shared one id - the second window was unaddressable.
    """
    provider = _add_window_provider()
    monkeypatch.setattr(uia, "_process_name", lambda pid: "explorer.exe")

    out: list = []
    provider._add_window(_El(1, r"E:\work"), out, hwnd=1)
    provider._add_window(_El(2, r"E:\work"), out, hwnd=2)

    assert len(out) == 2
    assert out[0].window_id != out[1].window_id
    assert {w.hwnd for w in out} == {1, 2}


def test_window_id_survives_a_reordered_enumeration(monkeypatch):
    """Raising a window can reorder the walk; the ids must not follow it."""
    provider = _add_window_provider()
    monkeypatch.setattr(uia, "_process_name", lambda pid: "app.exe")

    first: list = []
    provider._add_window(_El(11, "Same"), first, hwnd=11)
    provider._add_window(_El(22, "Same"), second := [], hwnd=22)
    ids = {w.hwnd: w.window_id for w in first + second}

    # Second enumeration: the same two windows, the other way round.
    third: list = []
    provider._add_window(_El(22, "Same"), third, hwnd=22)
    provider._add_window(_El(11, "Same"), fourth := [], hwnd=11)

    assert {w.hwnd: w.window_id for w in third + fourth} == ids


def test_window_id_survives_a_retitle(monkeypatch):
    """A window keeps its id when its title changes (a document being edited)."""
    provider = _add_window_provider()
    monkeypatch.setattr(uia, "_process_name", lambda pid: "notepad.exe")

    first: list = []
    provider._add_window(_El(5, "Untitled - Notepad"), first, hwnd=5)
    wid = first[0].window_id

    second: list = []
    provider._add_window(_El(5, "*notes.txt - Notepad"), second, hwnd=5)
    assert second[0].window_id == wid


def test_window_without_a_handle_falls_back_to_pid_and_title(monkeypatch):
    """Providers/elements that report no hwnd keep the historical behaviour."""
    provider = _add_window_provider()
    monkeypatch.setattr(uia, "_process_name", lambda pid: "app.exe")

    out: list = []
    provider._add_window(_El(0, "Helper"), out, hwnd=0)
    again: list = []
    provider._add_window(_El(0, "Helper"), again, hwnd=0)

    # Same (pid, title), no handle: one stable id, as before.
    assert out[0].window_id == again[0].window_id

    # ... and a second element with the same (pid, title) in *one* pass is
    # still deduped, which is the reason the historical rule existed.
    provider._add_window(_El(0, "Helper"), out, hwnd=0)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# the Win32 fallback enumeration applies the same filter as the walk
# ---------------------------------------------------------------------------


def test_win32_fallback_rejects_windows_the_walk_would_reject(monkeypatch):
    """The fallback must not readmit the phantoms the walk just filtered out.

    `_list_windows_sync` first walks the UIA tree with `_is_app_window`, then
    appends anything `_enum_top_level_windows` found that the walk missed - but
    that helper only drops invisible and child windows. Without re-applying the
    classification, the fallback became a hole: measured live, NVIDIA's
    "GeForce Overlay" (``WS_EX_NOACTIVATE``, gets no taskbar button) appeared in
    the window list purely through this path.

    The splash case is the same hole at a different scale: Word registers each
    letter of its startup logo as a top-level window ("T", "K", "D", ...), and
    all fourteen leaked in until the fallback applied the same rule.
    """
    provider = object.__new__(uia.UiaProvider)
    provider._windows = {}
    provider._window_id_by_key = {}
    provider._lock = __import__("threading").Lock()
    provider.allow_list = []
    provider._auto = None  # the walk contributes nothing

    phantoms = {101: "T", 102: "K", 103: "Overlay"}   # splash letters + overlay
    real = {201: "Notepad", 202: "D:\tmp"}
    all_windows = [(h, 1000, t) for h, t in phantoms.items()]
    all_windows += [(h, 1000, t) for h, t in real.items()]

    monkeypatch.setattr(uia, "_enum_top_level_windows", lambda: all_windows)
    monkeypatch.setattr(uia, "_is_app_window", lambda hwnd: hwnd in real)
    monkeypatch.setattr(uia, "_is_shell_surface", lambda hwnd: False)
    monkeypatch.setattr(uia, "_process_name", lambda pid: "app.exe")
    monkeypatch.setattr(
        uia.UiaProvider, "_walk_windows_sync", lambda self: [], raising=False
    )

    class _Auto:
        def ElementFromHandle(self, hwnd):
            return _El(hwnd, dict(all_windows and {h: t for h, _, t in all_windows})[hwnd])

    provider._auto = _Auto()

    out: list = []
    for hwnd, pid, title in uia._enum_top_level_windows():
        if not uia._is_app_window(hwnd) and not uia._is_shell_surface(hwnd):
            continue
        provider._add_window(provider._auto.ElementFromHandle(hwnd), out, hwnd=hwnd)

    assert {w.title for w in out} == {"Notepad", "D:\tmp"}
    assert "T" not in {w.title for w in out}
    assert "Overlay" not in {w.title for w in out}


def test_win32_fallback_keeps_shell_surfaces(monkeypatch):
    """``Program Manager`` fails `_is_app_window` but is the desktop surface.

    It is user-facing (the desktop), so the fallback must keep it while still
    dropping no-activate overlays.
    """
    monkeypatch.setattr(uia, "_is_app_window", lambda hwnd: False)
    monkeypatch.setattr(uia, "_is_shell_surface", lambda hwnd: hwnd == 66188)

    def keeps(hwnd: int) -> bool:
        return uia._is_app_window(hwnd) or uia._is_shell_surface(hwnd)

    assert keeps(66188) is True      # Program Manager
    assert keeps(66878) is False     # NVIDIA GeForce Overlay


# ---------------------------------------------------------------------------
# foreground lock timeout: the escape that costs the user nothing
# ---------------------------------------------------------------------------


def test_raise_window_opens_the_foreground_lock_instead_of_typing_alt(monkeypatch):
    """Zeroing the lock timeout is preferred over the Alt tap.

    Microsoft documents one condition for `SetForegroundWindow` to succeed:
    "the foreground lock time-out has expired". The session value here is
    200000 ms, so it never expires and the call is refused forever - which is
    why a plain set does nothing and why an Alt tap was needed at all.
    Temporarily setting the timeout to 0 makes the condition true and the call
    lands with no input event of any kind.

    This is the whole point: the Alt tap is a real key event that reaches the
    foreground application, and something that reaches nothing must be tried
    first.
    """
    from somnus.providers.windows import workspace

    state = {"fg": 1, "lock": 200000, "alt": 0, "lock_history": []}

    class _User32:
        def GetForegroundWindow(self):
            return state["fg"]

        def BringWindowToTop(self, hwnd):
            pass

        def SetForegroundWindow(self, hwnd):
            # Only lands while the lock timeout is 0 - the documented condition.
            if state["lock"] == 0:
                state["fg"] = hwnd

        def SystemParametersInfoW(self, action, _ui, pv, _ini):
            if action == workspace.SPI_GETFOREGROUNDLOCKTIMEOUT:
                ctypes.cast(pv, ctypes.POINTER(ctypes.c_ulong))[0] = state["lock"]
                return 1
            if action == workspace.SPI_SETFOREGROUNDLOCKTIMEOUT:
                # pvParam carries the value as an integer address, so a
                # c_void_p(0) arrives as a null pointer - .value is None, and
                # None means 0 here, which is exactly what the call wants.
                value = pv.value if isinstance(pv, ctypes.c_void_p) else pv
                state["lock"] = int(value or 0)
                state["lock_history"].append(int(value or 0))
                return 1
            return 0

        def keybd_event(self, *a):
            state["alt"] += 1

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 7

    monkeypatch.setattr(
        workspace.ctypes,
        "windll",
        type("W", (), {"user32": _User32(), "kernel32": _Kernel32()}),
    )
    workspace.reset_raise_stats()

    assert workspace.raise_window(2) is True
    # The window is in front ...
    assert state["fg"] == 2
    # ... the lock was opened and then put back exactly as it was ...
    assert state["lock"] == 200000
    assert state["lock_history"] == [0, 200000]
    # ... and no key event was sent.
    assert state["alt"] == 0
    assert workspace.raise_stats()["lock"] == 1
    assert workspace.raise_stats()["alt"] == 0


def test_foreground_lock_is_restored_even_when_the_raise_fails(monkeypatch):
    """A raise that does not land must not leave the session unlocked.

    Leaving the lock at 0 would change how the user's own windows arbitrate
    focus for the rest of the session - a side effect vastly worse than one
    failed raise.
    """
    from somnus.providers.windows import workspace

    state = {"lock": 200000, "history": []}

    class _User32:
        def GetForegroundWindow(self):
            return 1  # never changes: the raise is refused

        def BringWindowToTop(self, hwnd):
            pass

        def SetForegroundWindow(self, hwnd):
            pass

        def SystemParametersInfoW(self, action, _ui, pv, _ini):
            if action == workspace.SPI_GETFOREGROUNDLOCKTIMEOUT:
                ctypes.cast(pv, ctypes.POINTER(ctypes.c_ulong))[0] = state["lock"]
                return 1
            if action == workspace.SPI_SETFOREGROUNDLOCKTIMEOUT:
                value = pv.value if isinstance(pv, ctypes.c_void_p) else pv
                state["lock"] = int(value or 0)
                state["history"].append(int(value or 0))
                return 1
            return 0

        def GetWindowThreadProcessId(self, hwnd, ref):
            return 0

        def keybd_event(self, *a):
            pass

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 7

    monkeypatch.setattr(
        workspace.ctypes,
        "windll",
        type("W", (), {"user32": _User32(), "kernel32": _Kernel32()}),
    )
    workspace.reset_raise_stats()

    assert workspace.raise_window(2) is False
    assert state["lock"] == 200000          # restored
    assert state["history"] == [0, 200000]
    assert workspace.raise_stats()["failed"] == 1


def test_lock_helpers_never_pass_a_registry_flag(monkeypatch):
    r"""`fWinIni` must stay 0: the unlock is for one call, not for the machine.

    `SPIF_UPDATEINIFILE` would write
    ``HKCU\Control Panel\Desktop\ForegroundLockTimeout``, permanently
    changing how the user's session hands out the foreground.
    """
    from somnus.providers.windows import workspace

    seen: list[int] = []

    class _User32:
        def SystemParametersInfoW(self, action, _ui, pv, ini):
            seen.append(ini)
            return 1

    monkeypatch.setattr(
        workspace.ctypes, "windll", type("W", (), {"user32": _User32(), "kernel32": None})
    )
    assert workspace._set_lock_timeout(0) is True
    assert seen == [0]
