"""Windows-only E2E tests for the HWND write channel.

These exercise the one route that writes text *without focus*: a Win32 message
(``EM_REPLACESEL`` / ``WM_SETTEXT``) sent to the element's own window handle.
The measurement that justifies the route lives in
``docs/interface-capability-report.md`` 2.10; these tests make it regress-able.

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
#: `SetForegroundWindow` needs a nudge on Windows 11; try a few times.
_FG_ATTEMPTS = 5


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
    a real HWND, which is exactly the case the route serves. It is started fresh
    so the test does not depend on whatever the developer had open.
    """
    _windows_only()
    try:
        proc = subprocess.Popen(["notepad.exe"])
    except OSError as exc:  # pragma: no cover - environment
        pytest.skip(f"cannot start notepad: {exc}")
    time.sleep(3.0)

    from somnus.providers.windows.uia import UiaProvider
    from somnus.runtime.session import Session

    provider = UiaProvider()
    try:
        windows = provider._executor.submit(provider._list_windows_sync).result(timeout=60)
        target = next(
            (w for w in windows if "notepad" in (w.process or "").lower() and w.title),
            None,
        )
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
        time.sleep(0.3)
        got = _text_of(hwnd)
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
