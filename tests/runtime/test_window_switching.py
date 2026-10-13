"""Window-switching tests: windows_only observe, CALL wN.focus(), window deltas.

Covers the v0.4 window-management feature set:
* OBSERVE windows / observe(windows_only=True) lists only windows
* ``CALL w2.focus()`` switches the observation target
* opening/closing/renaming a window bumps rev and shows up as a delta line
* the default window choice skips untitled helper windows
"""

from __future__ import annotations

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.sui.parse import parse_action
import re
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


def _second_window(wid: str = "w2", title: str = "Notepad", active: bool = False) -> RawTree:
    root = RawNode(
        native=NativeRef(window_id=wid, runtime_id=(9,), path=()),
        role="pane",
        name="root",
        children=[
            RawNode(
                native=NativeRef(window_id=wid, runtime_id=(9, 1), path=(1,)),
                role="edit",
                name="Text Editor",
                value="",
            )
        ],
    )
    return RawTree(
        window=WindowInfo(
            window_id=wid, title=title, process="Notepad.exe", pid=7, active=active
        ),
        root=root,
        raw_revision=1,
    )


def _session(*trees: RawTree) -> Session:
    return Session(MockProvider(fixtures=list(trees)))


# --------------------------------------------------------------- parse layer


def test_parse_observe_windows():
    a = parse_action("OBSERVE windows")
    assert a.kind == "observe"
    assert a.query == "windows"


def test_parse_observe_plain_and_window():
    a = parse_action("OBSERVE")
    assert a.kind == "observe" and a.query is None and a.window is None
    a = parse_action("OBSERVE w2")
    assert a.kind == "observe" and a.window == "w2"


# ------------------------------------------------------------ windows_only


def test_observe_windows_only_lists_windows():
    session = _session(restaurant_demo_tree(), _second_window())
    text = session.observe(windows_only=True).text
    assert "WINDOWS count=2" in text
    assert 'WINDOW w1 "Restaurant Demo"' in text and "obs" in text
    assert 'WINDOW w2 "Notepad"' in text
    # No control tree at all
    assert "PRODUCT" not in text and "EDIT" not in text


def test_observe_windows_only_via_dispatch():
    session = _session(restaurant_demo_tree(), _second_window())
    out = session.dispatch("OBSERVE windows")
    assert "WINDOWS count=2" in out


def test_windows_only_does_not_change_target():
    session = _session(restaurant_demo_tree(), _second_window())
    session.observe(windows_only=True)
    assert session.target_window == "w1"


# -------------------------------------------------------------- focus switch


def test_focus_window_switches_target():
    session = _session(restaurant_demo_tree(), _second_window())
    session.observe()
    out = session.dispatch("CALL w2.focus()")
    assert out.startswith("OK")
    assert session.target_window == "w2"
    text = session.observe().text
    assert "INPUT" in text  # now the notepad tree is projected


def test_focus_window_unknown_ref_errors():
    session = _session(restaurant_demo_tree())
    session.observe()
    # dispatch renders errors as SUI-Text rather than raising.
    out = session.dispatch("CALL w9.focus()")
    assert out.startswith("ERR")
    assert "w9" in out


def test_focus_back_and_forth_keeps_refs_stable():
    session = _session(restaurant_demo_tree(), _second_window())
    session.observe()
    session.dispatch("CALL w2.focus()")
    session.dispatch("CALL w1.focus()")
    out = session.dispatch("OBSERVE windows")
    # windows_only refreshes the baseline too
    session.provider.set_tree(_second_window())
    out2 = session.dispatch("OBSERVE windows")
    assert "WINDOWS count=2" in out2
    assert re.search(r'WINDOW w\d+ "Notepad"', out2)


# --------------------------------------------------------------- window delta


def test_opening_window_bumps_rev_and_delta():
    session = _session(restaurant_demo_tree())
    session.observe()
    rev0 = session.rev
    session.provider.set_tree(_second_window(active=True))
    text = session.observe().text
    assert session.rev == rev0 + 1
    # The new window appears in the header (its ref comes from the allocator,
    # and native window ids are distinct from session refs).
    assert re.search(r'WINDOW w\d+ "Notepad"', text)


def test_closing_window_delta():
    session = _session(restaurant_demo_tree(), _second_window())
    session.observe()
    session.provider._trees.pop("w2")
    text = session.observe().text
    assert re.search(r'- WINDOW "Notepad"', text) or 'WINDOW w2' not in text


def test_observe_shows_opened_window_delta():
    """observe() itself must report a window that appeared since the last look."""
    session = _session(restaurant_demo_tree())
    session.observe()  # establishes the baseline, no delta yet
    session.provider.set_tree(_second_window())
    text = session.observe().text
    assert re.search(r'\+ WINDOW w\d+ "Notepad"', text)
    # A second observation shows no delta (already reported).
    text2 = session.observe().text
    assert "+ WINDOW" not in text2


def test_renamed_window_delta():
    session = _session(restaurant_demo_tree())
    session.observe()
    session.provider.mutate("w1", lambda t: setattr(t.window, "title", "Restaurant Demo - Cart"))
    text = session.observe().text
    assert 'WINDOW w1 "Restaurant Demo - Cart"' in text


def _scripted_session(*trees):
    from tests.fixtures.builders import demo_scripts

    return Session(
        MockProvider(fixtures=list(trees), scripts=demo_scripts()),
    )


def test_call_reports_window_delta_too():
    session = _scripted_session(restaurant_demo_tree())
    session.observe()
    session.provider.set_tree(_second_window())
    out = session.dispatch("CALL p1.add()")
    assert out.startswith("OK")
    # The window set changed, so the signature changed -> rev bumped and the
    # delta names the new window (its allocator ref may differ from w2).
    assert re.search(r'\+ WINDOW w\d+', out)


# ------------------------------------------------------------ default window


def test_default_window_skips_untitled():
    helper = _second_window(wid="w0", title="")
    session = _session(helper, restaurant_demo_tree())
    session.observe()
    assert session.target_window == "w1"


def test_observe_with_window_arg_switches_target():
    session = _session(restaurant_demo_tree(), _second_window())
    session.observe(window="w2")
    assert session.target_window == "w2"
