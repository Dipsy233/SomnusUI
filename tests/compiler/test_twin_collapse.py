"""P2-2: duplicate siblings are folded before they poison the registry.

The live shape: an expanded WPF combo box (or a tray flyout holding a hidden
copy of a taskbar button) hands the same element back twice. Both copies get
one fingerprint, so ``SessionRegistry`` reports the fingerprint as ambiguous -
and ``Session`` then refuses every call on those refs with
``TARGET_AMBIGUOUS`` even though each ref names exactly one node.

The collapse must be conservative: two genuinely different elements that only
share an accessibility identity may not be merged (that would make one of them
unreachable). When the proof is missing, both are kept and annotated
(``twin=true``) rather than silently dropped.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.runtime.registry import SessionRegistry

WID = "w1"


def _row(
    path: tuple[int, ...],
    *,
    runtime_id: tuple[int, ...],
    role: str = "listitem",
    patterns: frozenset[str] = frozenset({"selectionitem", "invoke"}),
    children: list[RawNode] | None = None,
) -> RawNode:
    """An *unnamed* selection row, the shape that produces twin fingerprints."""
    return RawNode(
        native=NativeRef(
            window_id=WID,
            runtime_id=runtime_id,
            class_name="ComboBoxItem",
            control_type=role,
            path=path,
        ),
        role=role,
        patterns=patterns,
        children=children or [],
    )


def _tree(children: list[RawNode]) -> RawTree:
    root = RawNode(
        native=NativeRef(window_id=WID, runtime_id=(99,), class_name="List"),
        role="list",
        name="选项",
        children=children,
    )
    return RawTree(
        window=WindowInfo(window_id=WID, title="combo", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def _compile(tree: RawTree):
    return Compiler().compile(tree)


def test_the_same_runtime_element_reached_twice_is_folded():
    tree = _tree([_row((0,), runtime_id=(1, 7)), _row((1,), runtime_id=(1, 7))])
    result = _compile(tree)
    rows = [n for n in result.root.walk() if n.role == "listitem"]
    assert len(rows) == 1, "one native element reached twice must render once"
    assert rows[0].attrs.get("dup") == 1
    assert result.warnings


def test_a_ghost_copy_is_folded_onto_the_actionable_row():
    """The ghost has no operable pattern; the real row answers SelectionItem."""
    tree = _tree(
        [
            _row((0,), runtime_id=(1, 7), patterns=frozenset()),
            _row((1,), runtime_id=(2, 7), patterns=frozenset({"selectionitem", "invoke"})),
        ]
    )
    result = _compile(tree)
    rows = [n for n in result.root.walk() if n.role == "listitem"]
    assert len(rows) == 1
    assert "selectionitem" in rows[0].patterns


def test_two_equally_actionable_rows_are_kept_and_annotated():
    """Same fingerprint, different runtime elements, both operable: do not merge.

    They may be two genuine controls that merely share an accessibility identity;
    folding one away would make it unreachable. Keeping both with ``twin=true``
    is the documented "annotate instead of silently drop" choice.
    """
    tree = _tree(
        [
            _row((0,), runtime_id=(1, 7)),
            _row((1,), runtime_id=(2, 7)),
        ]
    )
    result = _compile(tree)
    rows = [n for n in result.root.walk() if n.role == "listitem"]
    assert len(rows) == 2
    assert all(n.attrs.get("twin") is True for n in rows)
    assert all(n.attrs.get("dup") is None for n in rows)


def test_clearly_different_children_are_not_twins():
    """Different runtime ids -> different fingerprints -> nothing to fold."""
    tree = _tree(
        [
            _row((0,), runtime_id=(1, 7)),
            _row((1,), runtime_id=(2, 8)),
        ]
    )
    result = _compile(tree)
    rows = [n for n in result.root.walk() if n.role == "listitem"]
    assert len(rows) == 2
    assert all(n.attrs.get("twin") is None for n in rows)


def test_a_folded_twin_never_reaches_the_registry_as_ambiguous():
    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)
    tree = _tree(
        [
            _row((0,), runtime_id=(1, 7)),
            _row((1,), runtime_id=(2, 7), patterns=frozenset()),
        ]
    )
    first = compiler.compile(tree)
    registry.reconcile(first)
    # A fresh compile of the same duplicate-shaped tree: the surviving node
    # keeps its ref and no fingerprint is ever reported ambiguous.
    second = compiler.compile(_tree(
        [
            _row((0,), runtime_id=(1, 7)),
            _row((1,), runtime_id=(2, 7), patterns=frozenset()),
        ]
    ))
    report = registry.reconcile(second)
    assert not report.ambiguous
    assert len({n.ref for n in second.root.walk() if n.role == "listitem"}) == 1
