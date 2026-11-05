"""P2-1 / P2-5: combo boxes are selection controls, not text fields.

The live failure these pin: a WPF combo box, expanded, exposes every drop-down
row twice (a ghost copy plus the real row). The compiler gave the copies the
same fingerprint (ambiguous -> every call refused) and granted the *ghost*
``activate`` (which the element refuses) while never offering ``select`` —
the one gesture pywinauto proved works (``SelectionItemPattern.Select``,
137 ms, 2/2). At the same time ``select`` was listed as a text-entry role, so
every combo box also advertised ``set_text`` — a write it cannot perform.

These are pure compiler/projection assertions; the execution read-back is
covered in ``tests/runtime/test_executor_select_readback.py`` and the
end-to-end selection in ``tests/runtime/test_dropdown_selection.py``.
"""

from __future__ import annotations

from somnus.compiler.caps import primitive_capabilities
from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import apply_projection
from somnus.sui.serialize import render_document

WID = "w1"


def _n(
    path: tuple[int, ...],
    role: str,
    name: str | None = None,
    *,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    value: str | None = None,
    automation_id: str | None = None,
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id=WID,
            runtime_id=tuple(1000 + i for i in path),
            automation_id=automation_id,
            class_name="demo",
            control_type=role,
            path=path,
        ),
        role=role,
        name=name,
        value=value,
        states=states,
        patterns=patterns,
        children=children or [],
    )


def _combo_tree(selected: str = "低") -> RawTree:
    """A combo box with three drop-down rows, ``selected`` picked."""
    items = []
    for i, label in enumerate(("低", "中", "高")):
        items.append(
            _n(
                (0, i),
                "listitem",
                label,
                patterns=frozenset({"selectionitem", "invoke"}),
                states=frozenset({"selected"}) if label == selected else frozenset(),
            )
        )
    combo = _n(
        (0,),
        "select",
        "优先级",
        patterns=frozenset({"expandcollapse", "selection"}),
        automation_id="form_priority",
        children=items,
    )
    root = _n((), "pane", "form", children=[combo])
    return RawTree(
        window=WindowInfo(window_id=WID, title="form", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def test_a_combo_box_is_selectable_and_not_writable():
    result = Compiler().compile(_combo_tree())
    combo = result.find("sel1")
    assert combo is not None
    assert "select" in combo.cap_names()
    assert "set_text" not in combo.cap_names(), (
        "a combo box chooses from a list; advertising set_text is a false promise"
    )
    # The declared route starts with SelectionItem, the gesture that works.
    assert [s.primitive for s in combo.get_cap("select").via][0] == "select"


def test_dropdown_rows_offer_select_with_invoke_as_escalation():
    result = Compiler().compile(_combo_tree())
    row = result.find("li2")
    assert row is not None
    steps = [s.primitive for s in row.get_cap("select").via]
    assert steps[0] == "select"
    assert steps[1:] == ["activate"]


def test_the_current_selection_is_projected_as_the_combo_value():
    """`+select` alone is not enough: the observation must say what is picked.

    The selected item's state lives on the *item*, not the combo box, and the
    delta compares attributes - not states. Lifting it onto the box gives the
    agent a readable current value and gives a selection change a delta line.
    """
    tree = _combo_tree(selected="中")
    result = Compiler().compile(tree)
    combo = result.find("sel1")
    assert combo is not None
    assert combo.attrs.get("value") == "中"

    plan = ProjectionEngine().plan(
        result.root, apply_projection(result.root, [])
    )
    text = render_document(plan.lines)
    line = next(l for l in text.splitlines() if "优先级" in l)
    assert 'value="中"' in line, line
    assert "+set_text" not in text, "the phantom write capability must not be projected"


def test_changing_the_selection_changes_the_projected_value():
    before = Compiler().compile(_combo_tree(selected="低")).find("sel1")
    after = Compiler().compile(_combo_tree(selected="高")).find("sel1")
    assert before is not None and after is not None
    assert before.attrs.get("value") == "低"
    assert after.attrs.get("value") == "高"


# ---------------------------------------------------------------------------
# P2-5: a container with children is not an input surface
# ---------------------------------------------------------------------------


def _has_set_text(node: RawNode) -> bool:
    return "set_text" in {c.name for c in primitive_capabilities(node)}


def test_a_pane_with_children_is_not_advertised_as_an_input():
    pane = _n(
        (1,),
        "pane",
        "面板",
        patterns=frozenset({"value", "text"}),
        children=[_n((1, 0), "text", "内容")],
    )
    assert not _has_set_text(pane)


def test_a_group_with_children_is_not_advertised_as_an_input():
    group = _n(
        (2,),
        "group",
        "分组",
        patterns=frozenset({"value", "text"}),
        children=[_n((2, 0), "button", "确定", patterns=frozenset({"invoke"}))],
    )
    assert not _has_set_text(group)


def test_a_document_with_children_is_not_advertised_as_an_input():
    document = _n(
        (3,),
        "document",
        "文档",
        patterns=frozenset({"value", "text"}),
        children=[_n((3, 0), "text", "正文")],
    )
    assert not _has_set_text(document)


def test_a_leaf_with_a_writable_value_pattern_still_takes_text():
    """The P2-5 rule is about containers, not about the role."""
    leaf = _n((4,), "pane", "编辑器", patterns=frozenset({"value", "text"}))
    assert _has_set_text(leaf)


def test_the_dead_role_default_table_is_gone():
    """`ROLE_DEFAULT_CAPS` had no reader anywhere in the tree."""
    from somnus.compiler import caps as caps_module

    assert not hasattr(caps_module, "ROLE_DEFAULT_CAPS")
