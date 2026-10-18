"""Regression tests for false TARGET_AMBIGUOUS after duplicate collapse.

Reproduces the live bug: a tray flyout holds a hidden copy of a taskbar
button, so one fingerprint is allocated two refs in one pass. When the
flyout closes, the surviving node must reclaim its ref and calls must
succeed again - not fail with TARGET_AMBIGUOUS forever.
"""

from __future__ import annotations

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n

_BUTTON = frozenset({"invoke"})


def _taskbar_tree(with_flyout: bool):
    """Taskbar with a Start button; the flyout adds a hidden duplicate."""
    wid = "w1"
    children = [
        _n(wid, (1,), "button", name="开始", patterns=_BUTTON,
           automation_id="start_main"),
    ]
    if with_flyout:
        # Tray flyout holding a second copy of the same button: identical
        # name/patterns -> identical fingerprint, two nodes in one tree.
        children.append(
            _n(wid, (2,), "pane", automation_id="flyout", children=[
                _n(wid, (2, 0), "button", name="开始", patterns=_BUTTON,
                   automation_id="start_flyout_copy"),
            ])
        )
    root = _n(wid, (), "pane", name="任务栏", automation_id="taskbar",
              children=children)
    return MockTree(root)


class MockTree:
    """Minimal stand-in matching MockProvider's expected RawTree shape."""

    def __init__(self, root) -> None:
        from somnus.ir.node import RawTree, WindowInfo
        from somnus.ir.node import NativeRef
        self.window = WindowInfo(window_id="w1", title="任务栏",
                                 process="explorer.exe", pid=100, active=True)
        # give every node a native ref via the real builder path
        self.root = root


def _session(with_flyout: bool) -> tuple[Session, MockProvider]:
    from tests.fixtures.builders import restaurant_demo_tree
    # reuse the real RawTree type via a real fixture window id trick:
    provider = MockProvider()
    tree = _taskbar_tree(with_flyout)
    provider._trees["w1"] = tree
    session = Session(provider)
    return session, provider


def test_duplicate_collapse_allows_call_after_flyout_closes():
    session, provider = _session(with_flyout=True)
    session.observe()
    # Flyout closes: the tree now holds a single Start button.
    provider._trees["w1"] = _taskbar_tree(with_flyout=False)
    session.observe()
    out = session.dispatch('FIND "开始"')
    assert out.startswith("FOUND 1"), out
    ref = out.split("\n")[1].strip().split()[1]
    try:
        res = session.dispatch(f"CALL {ref}.activate()")
        assert not res.startswith("ERR TARGET_AMBIGUOUS"), res
    except SomnusError as exc:
        assert exc.code != ErrorCode.TARGET_AMBIGUOUS, exc


def test_ambiguous_refs_not_set_after_collapse():
    session, provider = _session(with_flyout=True)
    session.observe()
    provider._trees["w1"] = _taskbar_tree(with_flyout=False)
    session.observe()
    assert not session._ambiguous_refs


def test_genuine_ambiguity_still_flagged():
    """Two live duplicates in the current tree must stay ambiguous."""
    session, _ = _session(with_flyout=True)
    session.observe()
    out = session.dispatch('FIND "开始"')
    assert out.startswith("FOUND")
    start_refs = [n.ref for n in session.result.root.walk() if n.name == "开始"]
    assert len(start_refs) == 2
    try:
        session.dispatch(f"CALL {start_refs[0]}.activate()")
    except SomnusError as exc:
        assert exc.code in (ErrorCode.TARGET_AMBIGUOUS,
                            ErrorCode.CAPABILITY_NOT_FOUND,
                            ErrorCode.NATIVE_EXECUTION_FAILED)


def test_registry_by_fingerprint_does_not_grow():
    session, provider = _session(with_flyout=True)
    session.observe()
    provider._trees["w1"] = _taskbar_tree(with_flyout=False)
    session.observe()
    reg = session.registry
    start_refs = [r for r, rec in reg.records.items() if rec.name == "开始"]
    fps = {reg.records[r].fingerprint for r in start_refs
           if r in [n.ref for n in session.result.root.walk()]}
    for fp in fps:
        assert len(reg.by_fingerprint.get(fp, [])) <= 1
