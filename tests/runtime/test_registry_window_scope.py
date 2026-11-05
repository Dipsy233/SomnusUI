"""P2-6: a fingerprint answers only for the window that produced it.

A fingerprint is built from the element's role/name/identity and its ancestor
path - all of which describe *this window's* tree. Two windows can produce the
same fingerprint (two Explorer windows on one folder, the same form opened
twice). When matching was keyed by the bare fingerprint, a call on the new
window could silently bind the old window's native element: the "closed the
form, reopened it, old q1 wrote into the new instance" failure.

Isolation is by ``(window_id, fingerprint)``. It is also *per window* on the
way back: switching w1 -> w2 -> w1 must keep w1's refs, so the pruning pass of
w2 may not delete w1's live set.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.runtime.registry import SessionRegistry


def _tree(window_id: str, title: str = "Form") -> RawTree:
    """Two windows with a byte-identical control tree (one fingerprint each)."""
    root = RawNode(
        native=NativeRef(window_id=window_id, runtime_id=(10,), path=()),
        role="pane",
        name="form",
        children=[
            RawNode(
                native=NativeRef(window_id=window_id, runtime_id=(10, 1), path=(1,)),
                role="edit",
                name="姓名",
                value="",
                patterns=frozenset({"value", "text"}),
            ),
            RawNode(
                native=NativeRef(window_id=window_id, runtime_id=(10, 2), path=(2,)),
                role="button",
                name="提交",
                patterns=frozenset({"invoke"}),
            ),
        ],
    )
    return RawTree(
        window=WindowInfo(window_id=window_id, title=title, process="demo.exe", pid=7),
        root=root,
        raw_revision=1,
    )


def _refs(result) -> set[str]:
    return {node.ref for node in result.root.walk()}


def test_two_windows_with_identical_trees_do_not_share_refs():
    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)

    first = compiler.compile(_tree("w1", "Form 1"))
    registry.reconcile(first)
    second = compiler.compile(_tree("w2", "Form 2"))
    report = registry.reconcile(second)

    assert not report.ambiguous, "identical trees in *different* windows are not ambiguous"
    assert not (_refs(first) & _refs(second)), (
        "a ref must name one native element in one window: "
        f"{_refs(first) & _refs(second)}"
    )
    assert all(rec.window_id == "w2" for rec in registry.records.values() if rec.ref in _refs(second))


def test_switching_back_reuses_the_window_s_own_refs():
    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)

    w1_first = compiler.compile(_tree("w1"))
    registry.reconcile(w1_first)
    w1_refs = _refs(w1_first)

    w2 = compiler.compile(_tree("w2"))
    registry.reconcile(w2)

    w1_again = compiler.compile(_tree("w1"))
    report = registry.reconcile(w1_again)

    assert _refs(w1_again) == w1_refs, "returning to w1 must not renumber its refs"
    assert len(report.kept) == len(w1_refs), report
    assert not report.ambiguous


def test_candidates_are_scoped_to_one_window():
    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)
    w1 = compiler.compile(_tree("w1"))
    registry.reconcile(w1)

    fingerprint = next(n.fingerprint for n in w1.root.walk() if n.name == "提交")
    scoped = registry.candidates_for(fingerprint, window_id="w1")
    assert scoped and all(registry.records[r].window_id == "w1" for r in scoped)
    assert registry.candidates_for(fingerprint, window_id="w2") == []


def test_a_same_window_recompile_keeps_its_records():
    """Isolation must not be so strict that one window stops reconciling."""
    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)
    first = compiler.compile(_tree("w1"))
    registry.reconcile(first)
    second = compiler.compile(_tree("w1"))
    report = registry.reconcile(second)
    assert _refs(second) == _refs(first)
    assert not report.ambiguous
    assert report.stale == []
