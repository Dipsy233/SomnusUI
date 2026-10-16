"""Windows-only E2E tests for the HWND write channel.

These exercise the one route that writes text *without focus*: a Win32 message
(``EM_REPLACESEL`` / ``WM_SETTEXT``) sent to the element's own window handle.
The measurement that justifies the route lives in
``docs/interface-capability-report.md`` 3.2; these tests make it regress-able.

Two properties matter and both are asserted here, because either alone would be
a misleading pass:

* **the text actually lands** - and lands *exactly*, including CJK and a string
  long enough to be chunked;
* **the target window is in the background the whole time** - the foreground
  window before and after is compared, and the target is deliberately pushed to
  the back first. A "write" that steals focus is the behaviour this route exists
  to avoid, so a test that only checked the text would pass for the wrong
  reason (the keyboard route also lands text, while stealing focus and dropping
  characters).

Run with::

    pytest -m windows tests/e2e/test_hwnd_write.py

Skipped everywhere else, and skipped when Notepad cannot be started.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows

ROOT = Path(__file__).resolve().parents[2]

#: Clipboard-free way to read a Win32 control's text back.
WM_GETTEXT = 0x000D
#: Overwrite a control's text (used to *empty* it before closing).
WM_SETTEXT = 0x000C
WM_CLOSE = 0x0010
#: `SetForegroundWindow` needs a nudge on Windows 11; try a few times.
_FG_ATTEMPTS = 5

#: How long to wait for the Notepad window to appear. WPF/WinUI's first paint
#: plus UIA registration is not instant; a fixed sleep would be either slow or
#: flaky, so the fixture polls instead.
_WINDOW_TIMEOUT = 15.0
#: How long to keep closing Notepad windows in `_close_notepad` before giving
#: up on the graceful path (each round clears and closes every live window).
_CLOSE_TIMEOUT = 12.0


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _windows_only():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")


def _foreground() -> tuple[int, str]:
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 2)
    return int(hwnd), buf.value


def _text_of(hwnd: int, cap: int = 8192) -> str:
    user32 = _user32()
    buf = ctypes.create_unicode_buffer(cap)
    user32.SendMessageTimeoutW(
        wintypes.HWND(hwnd), WM_GETTEXT, cap, ctypes.byref(buf), 0x0002, 2000, None
    )
    return buf.value


# ---------------------------------------------------------------------------
# Notepad lifecycle
#
# Windows 11's Notepad restores the previous session's tabs - unsaved ones
# included - when it starts. A test that `terminate()`s the process therefore
# leaves the *next* run showing stale text (observed live: a fresh launch came
# up as `*dirty - Notepad`). The fix is to close Notepad the way a user would:
# empty the editor, then close the window, and wait until it is really gone.
# ---------------------------------------------------------------------------


def _window_title(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 2)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def _top_levels(predicate) -> list[int]:
    """Top-level window handles matching ``predicate(hwnd)``."""
    user32 = _user32()
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if predicate(int(hwnd)):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _children(hwnd: int) -> list[int]:
    user32 = _user32()
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(child, _lparam):
        found.append(int(child))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(_cb), 0)
    return found


def _notepad_windows() -> list[int]:
    return _top_levels(lambda h: "notepad" in _window_title(h).lower())


def _close_notepad() -> bool:
    """Close every Notepad window, emptying its editor first.

    Returns True when none remain. Each round empties the editor (so the window
    is not dirty when it closes) and posts ``WM_CLOSE``; a window that is still
    busy next round gets the same treatment. Windows appear and close one per
    tab, so this loops rather than acting once.
    """
    user32 = _user32()
    deadline = time.monotonic() + _CLOSE_TIMEOUT
    while time.monotonic() < deadline:
        windows = _notepad_windows()
        if not windows:
            return True
        for hwnd in windows:
            for child in _children(hwnd):
                cls = _class_name(child)
                if "RichEdit" in cls or cls == "Edit":
                    user32.SendMessageW(wintypes.HWND(child), WM_SETTEXT, 0, "")
            user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)
        time.sleep(0.4)
    return not _notepad_windows()


@pytest.fixture
def temp_tree():
    """A compiled one-node tree for an editor with no HWND and no Value pattern.

    This is the QQ ProseMirror shape: a focusable text surface that publishes
    nothing writable. Built through the real compiler so the capability chain is
    the one production would produce, not a hand-made stub.
    """
    from somnus.compiler.compile import Compiler
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    editor = RawNode(
        native=NativeRef(
            window_id="w1", runtime_id=(1,), class_name="ProseMirror ExEditor"
        ),
        role="group",
        name="editor",
        patterns=frozenset({"text"}),
    )
    root = RawNode(native=NativeRef(window_id="w1", runtime_id=(0,)),
                   role="window", name="t", children=[editor])
    tree = RawTree(
        window=WindowInfo(window_id="w1", title="t", process="t.exe", pid=1),
        root=root,
    )
    result = Compiler().compile(tree, pack=None)
    node = next(n for n in result.root.walk() if n.role == "input")
    return result, node


@pytest.fixture(scope="module")
def notepad():
    """A real Notepad window, and its editor element.

    Notepad is the fixture because its editor is a native ``RichEditD2DPT`` with
    a real HWND, which is exactly the case the route serves. Any windows left
    over from an earlier run are closed first - Notepad restores unsaved tabs on
    start, so without that a previous run's text would be this run's content.
    """
    _windows_only()
    # A clean session: close anything already open (emptying the editors) so the
    # window this fixture starts cannot inherit stale text.
    _close_notepad()
    try:
        proc = subprocess.Popen(["notepad.exe"])
    except OSError as exc:  # pragma: no cover - environment
        pytest.skip(f"cannot start notepad: {exc}")

    from somnus.providers.windows.uia import UiaProvider
    from somnus.runtime.session import Session

    provider = UiaProvider()
    try:
        # Poll for the window instead of a fixed sleep: it is usually ready in
        # well under a second, and a fixed wait is pure added test time.
        target = None
        deadline = time.monotonic() + _WINDOW_TIMEOUT
        while time.monotonic() < deadline:
            windows = provider._executor.submit(
                provider._list_windows_sync
            ).result(timeout=60)
            target = next(
                (w for w in windows if "notepad" in (w.process or "").lower() and w.title),
                None,
            )
            if target is not None:
                break
            time.sleep(0.2)
        if target is None:
            pytest.skip("notepad window not found")

        session = Session(provider)
        session.observe(target.window_id)

        node = hwnd = None
        for n in session.result.root.walk():
            source = session.result.sources.get(n.ref)
            if source and "RichEdit" in (source.native.class_name or ""):
                node, hwnd = n, source.native.native_window_handle
                break
        if node is None or not hwnd:
            pytest.skip("notepad editor element has no HWND in this build")

        yield {
            "session": session,
            "node": node,
            "hwnd": hwnd,
            "provider": provider,
            "window_id": target.window_id,
        }
    finally:
        provider.close()
        # Close cleanly (empty the editor, then close the window). Only if that
        # leaves something behind - a hung window, a build with a modal prompt -
        # is the process killed, and this one process only.
        if not _close_notepad():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - teardown
                proc.kill()


def test_editor_element_maps_an_hwnd(notepad):
    """The precondition is the element's own handle, not a window-class guess.

    ``CurrentNativeWindowHandle`` is what the resolver's ``hwnd`` gate reads. If
    this were 0 the route would be skipped and the test below would be testing
    something else entirely.
    """
    assert notepad["hwnd"] != 0
    ref = notepad["session"].result.sources[notepad["node"].ref]
    assert ref.native.native_window_handle == notepad["hwnd"]


def test_set_text_chain_offers_the_hwnd_route(notepad):
    """The chain declares all three routes with the right gates."""
    cap = notepad["node"].get_cap("set_text")
    steps = [(s.primitive, s.condition) for s in cap.via]
    assert ("set_text", "has:value") in steps, steps
    assert ("set_text_hwnd", "hwnd") in steps, steps
    assert steps[-1] == ("type_text", "no-hwnd"), steps


def test_writes_exactly_with_the_window_in_the_background(notepad):
    """The whole point: text lands while the target is not foreground.

    The target is pushed behind another window first, and the foreground window
    is compared before and after every write. If focus were stolen the assertion
    fails even though the text arrived - which is the distinction between this
    route and typing.
    """
    session = notepad["session"]
    node = notepad["node"]
    hwnd = notepad["hwnd"]
    user32 = _user32()

    np_root = user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT

    # Push somebody else forward so Notepad really is in the background. Any
    # other top-level window will do; the console running pytest is the usual
    # candidate. If none can be raised the focus property simply cannot be
    # observed here, which is reported as a skip rather than a pass.
    other = _find_other_foregroundable(np_root)
    for _ in range(_FG_ATTEMPTS):
        if not other:
            break
        user32.SetForegroundWindow(wintypes.HWND(other))
        time.sleep(0.4)
        if user32.GetForegroundWindow() != np_root:
            break

    fg_before = _foreground()[0]
    backgrounded = fg_before != np_root

    cases = ["plain-ascii", "提交后验证中文", "x" * 300]
    for sent in cases:
        out = session.dispatch(f'CALL {node.ref}.set_text("{sent}")')
        assert out.startswith("OK"), out
        # Poll for the result instead of sleeping a fixed amount. A fixed sleep
        # is flaky by construction: the value has to travel back through UIA and
        # the control has to finish applying it, and how long that takes is not
        # a property of this test. Observed failing once exactly this way - the
        # read returned the *previous* case's text.
        got = ""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            got = _text_of(hwnd)
            if got == sent:
                break
            time.sleep(0.05)
        assert got == sent, f"round-trip mismatch: sent {sent!r} got {got!r}"
        if backgrounded:
            assert _foreground()[0] != np_root, "focus was stolen from the user"

    if not backgrounded:
        pytest.skip("could not background the target window; focus property untested")


def _find_other_foregroundable(exclude: int) -> int:
    """Some other top-level window that can hold the foreground."""
    user32 = _user32()
    found: list[int] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if hwnd == exclude:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        found.append(int(hwnd))
        return len(found) < 8

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else 0


def test_a_handle_less_element_is_never_sent_a_message(notepad):
    """The gate is a safety property: a message to HWND 0 goes to the desktop.

    Asserted at the resolver, where the decision is made, so the test does not
    depend on finding a self-drawn control on the test machine.
    """
    from somnus.ir.capability import PrimitiveStep
    from somnus.runtime.resolver import Resolver

    session = notepad["session"]
    node = notepad["node"]
    resolver = Resolver(session.result)

    hwnd_step = PrimitiveStep(
        primitive="set_text_hwnd", target="$self", value="$text", condition="hwnd"
    )
    assert resolver._condition_holds(hwnd_step, node) is True

    no_hwnd_step = PrimitiveStep(
        primitive="set_text_hwnd", target="$self", value="$text", condition="no-hwnd"
    )
    assert resolver._condition_holds(no_hwnd_step, node) is False


def _portable_tree():
    """A compiled editor with no HWND and no Value pattern - the QQ shape."""
    from somnus.compiler.compile import Compiler
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    editor = RawNode(
        native=NativeRef(
            window_id="w1", runtime_id=(1,), class_name="ProseMirror ExEditor"
        ),
        role="group",
        name="editor",
        patterns=frozenset({"text"}),
    )
    root = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(0,)),
        role="window",
        name="t",
        children=[editor],
    )
    tree = RawTree(
        window=WindowInfo(window_id="w1", title="t", process="t.exe", pid=1),
        root=root,
    )
    result = Compiler().compile(tree, pack=None)
    node = next(n for n in result.root.walk() if n.role == "input")
    return result, node


def test_a_handle_less_element_gates_the_route_out():
    """An element with no HWND must not resolve the HWND step.

    Complement of the test above, on a synthetic tree so it runs without a real
    self-drawn control being present.
    """
    from somnus.runtime.resolver import Resolver

    result, node = _portable_tree()
    resolver = Resolver(result)
    cap = node.get_cap("set_text")
    assert cap is not None
    steps = resolver._build_steps(node, cap, {"text": "x", "_0": "x"})
    primitives = [s.primitive for s in steps]
    # No HWND and no Value pattern: only typing may remain.
    assert "set_text_hwnd" not in primitives, primitives
    assert "type_text" in primitives, primitives


# ---------------------------------------------------------------------------
# The typing verifier must read a surface that actually moves
# ---------------------------------------------------------------------------


class _FakeTextPattern:
    """Stands in for IUIAutomationTextPattern's document range."""

    def __init__(self, store: dict) -> None:
        self._store = store
        self.DocumentRange = self

    def GetText(self, _cap):
        return self._store["text"]


class _FakeElement:
    """An element shaped like a real self-drawn editor.

    Measured live on QQ's ProseMirror editor and reproduced here:

    * the Value property answers **successfully with an empty string** - it does
      not raise, and it never changes no matter what is typed;
    * the Text pattern's document range reports the live content.

    ``value_raises`` switches to the other provider shape (a Value read that
    raises), so both "dead property" and "absent property" are covered.
    """

    def __init__(self, *, text: str = "", value: str = "", value_raises: bool = False):
        self._store = {"text": text}
        self._value = value
        self._value_raises = value_raises
        self.pattern_calls = 0

    def GetCurrentPropertyValue(self, prop):
        if prop != 30045:
            raise KeyError(prop)
        if self._value_raises:
            raise OSError("no such property")
        return self._value

    def GetCurrentPattern(self, pid):
        self.pattern_calls += 1
        if pid != 10014:  # UIA_TextPatternId
            raise OSError(f"no pattern {pid}")
        return _FakeTextPattern(self._store)

    def type(self, chars: str) -> None:
        self._store["text"] += chars


def test_the_verifier_reads_the_text_pattern_when_value_is_dead():
    """回归：Value 返回空串不算"能读"，必须改读 Text pattern。

    实测 QQ 的 ProseMirror 编辑器：``GetCurrentPropertyValue(30045)`` 成功返回
    ``''``（不是抛错），且打字后永远是 ``''``；真正的内容在 Text pattern 里。

    旧实现只看 Value，于是 ``len('') - 0 >= count`` 恒为 False，每个字符都
    跑满重试预算并在其间狂发 SHIFT——实测 3 字符 1.89s、6 字符 3.41s、
    10 字符直接被外层超时打断。这个测试钉住"信号修好了"，而不是"超时调大了"。
    """
    from somnus.providers.windows.executor import _text_landed_verifier

    el = _FakeElement(text="base", value="")
    verify = _text_landed_verifier(el, "abc")
    assert verify is not None, "a Text pattern is available, so a verifier must exist"

    # Nothing typed yet: the baseline is the existing content, so one more
    # character has not arrived.
    assert verify(1) is False

    # Type one character: the Text pattern moves, and the verifier must see it
    # *immediately* rather than waiting out its retry budget.
    el.type("a")
    assert verify(1) is True

    el.type("bc")
    assert verify(3) is True


def test_the_verifier_still_prefers_a_live_value_property():
    """Value 真的能读时仍然用它，不去碰 Text pattern。

    普通 Edit 控件走 Value，读起来更便宜，也没有 Text pattern 的额外开销。
    """
    from somnus.providers.windows.executor import _text_landed_verifier

    el = _FakeElement(text="ignored", value="base")
    verify = _text_landed_verifier(el, "xy")
    assert verify is not None
    assert el.pattern_calls == 0, "a live Value property must not fall back to Text"

    assert verify(1) is False
    el._value = "basex"
    assert verify(1) is True


def test_the_verifier_is_absent_when_neither_surface_works():
    """两个面都读不到时返回 None（=没有可用校验器），而不是一个永远 False 的。

    返回一个恒 False 的校验器，等于让发送方在每个字符上白等重试预算。
    诚实的"没有信号"应当让调用退回到"定速投递并如实说明"。
    """
    from somnus.providers.windows.executor import _text_landed_verifier

    el = _FakeElement(value_raises=True)
    el.GetCurrentPattern = lambda pid: (_ for _ in ()).throw(OSError("no text pattern"))
    assert _text_landed_verifier(el, "abc") is None


def test_an_empty_but_readable_value_still_yields_a_verifier():
    """Value 能读但为空、且也没有 Text pattern 时，仍给出校验器（如实报"没到"）。

    这比返回 None 更好：调用方至少知道"目标没变"，而不是把不可观测
    当成"已经到达"。
    """
    from somnus.providers.windows.executor import _text_landed_verifier

    el = _FakeElement(value="")
    el.GetCurrentPattern = lambda pid: (_ for _ in ()).throw(OSError("no text pattern"))
    verify = _text_landed_verifier(el, "abc")
    assert verify is not None
    assert verify(1) is False
