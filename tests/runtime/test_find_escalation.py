"""FIND's wait may spend one full read on a partial-but-small capture.

The live shape: a popup/pane is not in the viewport layer, so `find(timeout=)`
sees `0 matches` while a full read does contain the element. The wait must not
turn every poll into a multi-second full read; one escalation, only when the
tree is small and untruncated, and only inside the deadline.

These are replay tests (no desktop); the provider below serves a partial tree
from `snapshot_viewport` and the complete tree from `snapshot`.
"""

from __future__ import annotations

import copy

from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n


def _tree(
    *,
    window_id: str = "w1",
    title: str = "Form",
    extra: tuple[str, ...] = (),
) -> RawTree:
    children = [
        _n(window_id, (0,), "button", "提交", patterns=frozenset({"invoke"}))
    ]
    children += [
        _n(window_id, (i + 1,), "button", name, patterns=frozenset({"invoke"}))
        for i, name in enumerate(extra)
    ]
    return RawTree(
        window=WindowInfo(
            window_id=window_id, title=title, process="demo.exe", pid=1, active=True
        ),
        root=_n(window_id, (), "pane", title, children=children),
        raw_revision=1,
    )


class ViewportSequence(MockProvider):
    """Partial from the viewport read, complete from the full read."""

    def __init__(self, full: RawTree, partial: RawTree, *, meta=None):
        super().__init__(fixtures=[full])
        self.partial = partial
        self.viewport_calls = 0
        self.full_calls = 0
        self.viewport_meta = {}
        self._meta = meta or {}

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.viewport_calls += 1
        self.viewport_meta = copy.deepcopy(self._meta)
        self.last_viewport_partial = True
        return copy.deepcopy(self.partial)

    def snapshot(self, window_id, scope=None, max_depth=12):
        self.full_calls += 1
        return copy.deepcopy(self._trees[window_id])


def _adopt(session: Session, tree: RawTree, *, partial: bool = True) -> None:
    """Put a partial result in place without spending a read on observe."""
    session.refresh_windows()
    session.target_window = tree.window.window_id
    session._note_capture(copy.deepcopy(tree), partial)
    session.result = session._recompile_with(copy.deepcopy(tree), remember=not partial)
    session._apply_viewport_meta(session.result)


def _missing_viewport():
    """A partial tree that does not contain ``Late``; the full one does."""
    full = _tree(extra=("Late",))
    partial = _tree()
    return ViewportSequence(full, partial)


def test_a_partial_capture_escalates_once_and_marks_the_hit():
    provider = _missing_viewport()
    session = Session(provider)
    _adopt(session, provider.partial)

    out = session.find("Late", timeout_s=0.35)
    assert out.startswith("FOUND 1 (full read)"), out
    assert provider.full_calls == 1, provider.full_calls
    # The wait stopped on the full-read hit; it did not keep polling afterwards.
    assert provider.viewport_calls >= 3, provider.viewport_calls


def test_a_hit_without_a_timeout_does_not_escalate_or_claim_full_read():
    provider = _missing_viewport()
    session = Session(provider)
    _adopt(session, provider.partial)

    out = session.find("Late")
    assert out.startswith("FOUND 1") and "(full read)" not in out, out
    assert provider.full_calls == 1, "the existing no-timeout full retry still runs"
    # No wait, so no viewport polling.
    assert provider.viewport_calls == 0


def test_a_truncated_collection_never_escalates_and_names_the_next_step():
    partial = _tree()
    full = _tree(extra=("Outside",))
    meta = {
        "w1": {(0,): {"items_total": 20, "items_visible": 1, "items_truncated": True}}
    }
    provider = ViewportSequence(full, partial, meta=meta)
    session = Session(provider)
    _adopt(session, provider.partial)

    out = session.find("Outside", timeout_s=0.25)
    assert out.startswith("ERR TARGET_NOT_FOUND"), out
    assert "rows outside the captured viewport were not searched" in out, out
    assert "OBSERVE expand/full" in out, out
    assert provider.full_calls == 0, "a truncated tree's full read is the 3-4s case"


def test_a_capture_too_large_to_relocate_does_not_escalate():
    """The node count gate: a 200+-node partial tree is not a small window."""
    children = [
        _n("w1", (i,), "text", f"Item {i}")
        for i in range(220)
    ]
    partial = RawTree(
        window=WindowInfo(
            window_id="w1", title="Big", process="demo.exe", pid=1, active=True
        ),
        root=_n("w1", (), "pane", "Big", children=children),
        raw_revision=1,
    )
    full = _tree(extra=("Outside",))
    provider = ViewportSequence(full, partial)
    session = Session(provider)
    _adopt(session, provider.partial)

    out = session.find("Outside", timeout_s=0.2)
    assert out.startswith("ERR TARGET_NOT_FOUND"), out
    assert provider.full_calls == 0, "a big tree's full read must stay out of a short wait"


def test_a_read_that_cannot_fit_the_remaining_deadline_is_not_started():
    """The cost gate: the estimate must fit in the time still left."""
    children = [_n("w1", (i,), "text", f"Item {i}") for i in range(150)]
    partial = RawTree(
        window=WindowInfo(
            window_id="w1", title="Medium", process="demo.exe", pid=1, active=True
        ),
        root=_n("w1", (), "pane", "Medium", children=children),
        raw_revision=1,
    )
    full = _tree(extra=("Outside",))
    provider = ViewportSequence(full, partial)
    session = Session(provider)
    _adopt(session, provider.partial)

    out = session.find("Outside", timeout_s=0.2)
    assert out.startswith("ERR TARGET_NOT_FOUND"), out
    assert provider.full_calls == 0, "the estimated full read does not fit the deadline"
