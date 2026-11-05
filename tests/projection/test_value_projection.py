"""Readable text is part of the observation, not something to discover by typing.

Before this change `observe` printed an INPUT without its content and
`CALL q1.read()` answered only `OK`. These tests pin the two projection rules
that close the gap:

* text-like nodes carry a truncated `value="…"` preview (80 chars, which is
  +80 tokens worst-case inside L0's 300-token budget);
* values equal to the node's own name are not duplicated, and long documents
  keep the serializer's existing `len=/preview=` summary instead of a second
  copy of the same text.
"""

from __future__ import annotations

from somnus.compiler.caps import primitive_capabilities
from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import apply_projection
from somnus.sui.serialize import render_document

WID = "w1"


def _n(
    path: tuple[int, ...],
    role: str,
    name: str | None = None,
    *,
    value: str | None = None,
    patterns: frozenset[str] = frozenset(),
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
        patterns=patterns,
        children=children or [],
    )


def _tree() -> RawTree:
    root = _n(
        (),
        "pane",
        "Form",
        children=[
            _n((0,), "edit", "Name", value="hello",
               patterns=frozenset({"value", "text"}), automation_id="f_name"),
            _n((1,), "edit", "Empty", value="",
               patterns=frozenset({"value"}), automation_id="f_empty"),
            _n((2,), "text", "标题", value="",
               patterns=frozenset({"text"}), automation_id="f_label"),
            _n((3,), "statusbar", "计数", value="5 个字符",
               automation_id="f_count"),
            _n((4,), "edit", "Long", value="x" * 200,
               patterns=frozenset({"value", "text"}), automation_id="f_long"),
            _n((5,), "edit", "Chinese", value="第一行\n第二行\tX",
               patterns=frozenset({"value", "text"}), automation_id="f_cjk"),
            _n((6,), "button", "OK", patterns=frozenset({"invoke"}),
               automation_id="f_ok"),
        ],
    )
    return RawTree(
        window=WindowInfo(window_id=WID, title="Form", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def _render() -> str:
    result = Compiler().compile(_tree())
    engine = ProjectionEngine(ObservationBudget().for_level(0), l0=True)
    plan = engine.plan(result.root, apply_projection(result.root, []))
    return render_document(plan.lines, rev=1)


def _line(text: str, marker: str) -> str:
    return next(line for line in text.splitlines() if marker in line)


def test_a_short_input_value_is_visible_in_one_observe():
    line = _line(_render(), "Name")
    assert 'value="hello"' in line, line
    assert line.strip().startswith("INPUT"), line


def test_an_empty_input_says_it_is_empty():
    line = _line(_render(), "Empty")
    assert 'value=""' in line, line


def test_a_text_label_does_not_repeat_its_value_when_it_equals_the_name():
    line = _line(_render(), "标题")
    assert line.count("value=") == 0, line


def test_a_status_line_carries_its_value():
    line = _line(_render(), "计数")
    assert 'value="5 个字符"' in line, line


def test_long_text_keeps_the_existing_len_preview_and_is_not_duplicated():
    line = _line(_render(), "Long")
    assert "len=200" in line and "preview=" in line, line
    assert "value=" not in line, "the same text must not be printed twice"


def test_chinese_newlines_and_tabs_stay_visible_and_on_one_line():
    text = _render()
    line = _line(text, "Chinese")
    # A real newline would split the observation; the serializer escapes it.
    assert "\\n" in line, line
    assert "\\t" in line, "a tab must be escaped so the node stays one line"
    assert "\t" not in line, "no raw tab may reach the line"
    assert "第一行" in line and "第二行" in line


def test_cr_crlf_tab_quotes_and_backslashes_never_split_a_node_line():
    """Windows reports a line break as a bare CR; every value must stay one line."""
    values = {
        "Crlf": "L1\r\nL2",
        "CrOnly": "L1\rL2",
        "Tabbed": "L1\tL2",
        "Quoted": 'a"b',
        "Backslashed": "a\\b",
    }
    root = _n((), "pane", "Form", children=[
        _n((i,), "edit", name, value=value, patterns=frozenset({"value"}))
        for i, (name, value) in enumerate(values.items())
    ])
    tree = RawTree(window=WindowInfo(window_id=WID, title="Form",
                                     process="demo.exe", pid=1, active=True),
                   root=root, raw_revision=1)
    result = Compiler().compile(tree)
    plan = ProjectionEngine(ObservationBudget().for_level(0), l0=True).plan(
        result.root, apply_projection(result.root, [])
    )
    text = render_document(plan.lines, rev=1)
    lines = text.splitlines()

    # @rev + desktop WINDOW summary + root + one line per child: each node
    # occupies exactly one physical line.
    assert len(lines) == 1 + 2 + len(values), lines
    assert "\r" not in text and "\t" not in text, repr(text)
    for name in values:
        assert sum(1 for line in lines if name in line) == 1, (name, lines)
    assert 'value="L1\\r\\nL2"' in text
    assert 'value="L1\\rL2"' in text
    assert 'value="L1\\tL2"' in text
    assert 'value="a\\"b"' in text
    assert 'value="a\\\\b"' in text


def test_an_over_preview_value_is_truncated_with_an_ellipsis():
    root = _n((), "pane", "Form", children=[
        _n((0,), "edit", "Typed", value="a" * 120,
           patterns=frozenset({"value"}))
    ])
    tree = RawTree(window=WindowInfo(window_id=WID, title="Form",
                                     process="demo.exe", pid=1, active=True),
                   root=root, raw_revision=1)
    result = Compiler().compile(tree)
    plan = ProjectionEngine(ObservationBudget().for_level(0), l0=True).plan(
        result.root, apply_projection(result.root, [])
    )
    line = _line(render_document(plan.lines), "Typed")
    assert "a" * 120 not in line
    assert "…" in line, line
    # Preview is 80 chars including the ellipsis.
    assert "a" * 79 in line


def test_the_value_preview_stays_inside_the_l0_budget():
    text = _render()
    assert count_tokens(text) <= 300, count_tokens(text)


def test_read_is_only_offered_where_a_surface_was_proven():
    """INPUT with a Value pattern can be read; a named button cannot."""
    result = Compiler().compile(_tree())
    by_name = {n.name: n for n in result.root.walk() if n.name}

    assert "read" in by_name["Name"].cap_names()
    assert "read" in by_name["Chinese"].cap_names()
    assert "read" in by_name["计数"].cap_names()  # text/status role: name is content
    assert "read" not in by_name["OK"].cap_names(), (
        "a button's name is its label, not readable content"
    )
    for node in result.root.walk():
        if node.role == "button":
            assert "read" not in node.cap_names()


def test_a_group_with_a_name_gets_no_read_capability():
    root = _n((), "pane", "Panel", children=[_n((0,), "text", "body", patterns=frozenset({"text"})),
                                             _n((1,), "text", "tail", patterns=frozenset({"text"}))])
    caps = {c.name for c in primitive_capabilities(root)}
    assert "read" not in caps
