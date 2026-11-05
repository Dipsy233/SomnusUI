"""Exactly one window may be printed as `active`.

Root cause of the live bug (three Notepad windows all `active`): the provider's
window list marked **every window whose process id matched the focused
element's pid**, so one process with several windows produced several
`active` flags. Two things are pinned here:

* the provider helper now compares the foreground *window handle*, not the
  process (and resets stale flags);
* the session's rendered surfaces (`OBSERVE windows` and the desktop summary)
  still print at most one `active`, even if an older provider double claims
  several - the target wins, because it was just raised.
"""

from __future__ import annotations

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.providers.windows.uia import _mark_active_window
from somnus.runtime.session import Session


def _w(wid: str, hwnd: int, *, active: bool, pid: int = 7, title: str | None = None):
    return WindowInfo(
        window_id=wid,
        title=title or f"Window {wid}",
        process="notepad.exe",
        pid=pid,
        active=active,
        hwnd=hwnd,
    )


def _tree(window: WindowInfo) -> RawTree:
    root = RawNode(
        native=NativeRef(window_id=window.window_id, runtime_id=(1,), path=()),
        role="pane",
        name=window.title,
        children=[
            RawNode(
                native=NativeRef(window_id=window.window_id, runtime_id=(2,), path=(1,)),
                role="button",
                name="确定",
                patterns=frozenset({"invoke"}),
            )
        ],
    )
    return RawTree(window=window, root=root, raw_revision=1)


# ---------------------------------------------------------------------------
# provider rule
# ---------------------------------------------------------------------------


def test_the_foreground_handle_marks_one_of_several_same_process_windows():
    windows = [
        _w("w1", 101, active=True),
        _w("w2", 102, active=True),   # same pid, different handle (notepad tab)
        _w("w3", 103, active=True),
    ]
    _mark_active_window(windows, 102)
    assert [w.active for w in windows] == [False, True, False]


def test_no_foreground_handle_clears_every_stale_flag():
    windows = [_w("w1", 101, active=True), _w("w2", 102, active=True)]
    _mark_active_window(windows, 0)
    assert [w.active for w in windows] == [False, False]


# ---------------------------------------------------------------------------
# session rendering
# ---------------------------------------------------------------------------


def _session(*windows: WindowInfo) -> Session:
    return Session(MockProvider(fixtures=[_tree(w) for w in windows]))


def test_observe_windows_prints_only_one_active_with_same_process_windows():
    session = _session(
        _w("w1", 101, active=True),
        _w("w2", 102, active=True),
        _w("w3", 103, active=True),
    )
    text = session.observe(windows_only=True).text
    assert text.count("active") == 1, text
    # The target (first active by default) is the one kept.
    assert session.target_window == "w1"
    assert next(l for l in text.splitlines() if "active" in l).strip().endswith("active")


def test_the_active_flag_follows_a_foreground_change():
    first = _w("w1", 101, active=True)
    second = _w("w2", 102, active=False)
    session = _session(first, second)
    session.observe(windows_only=True)
    assert 'WINDOW' in session.observe(windows_only=True).text

    # The user alt-tabs to window 2.
    first.active = False
    second.active = True
    text = session.observe(windows_only=True).text
    assert text.count("active") == 1, text
    active_line = next(l for l in text.splitlines() if "active" in l)
    assert "Window w2" in active_line, active_line


def test_the_desktop_summary_line_also_marks_only_one_active():
    session = _session(
        _w("w1", 101, active=True),
        _w("w2", 102, active=True),  # a stale duplicate flag from the provider
    )
    text = session.observe().text
    summary = next(l for l in text.splitlines() if l.startswith("WINDOWS "))
    assert summary.count("active") == 1, summary
    assert '"Window w1" active' in summary, summary
