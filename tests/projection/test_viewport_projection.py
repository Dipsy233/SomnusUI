"""Viewport-aware observe projection (docs/archive/performance-2026-11.md).

The contract: a container the collector had to cut at the viewport edge carries
``items_total`` / ``items_visible`` / ``items_truncated`` / ``scroll_pct``, and
its children are *every complete visible row*. Projection must then show

* one summary line (`rows=` / `visible=` / `scroll=`),
* 1-3 complete example rows (never compressed),
* one `+more` marker naming the hidden count and the two real ways to ask for
more (`FIND` and `OBSERVE ... expand`),

while a node *without* those fields must render byte-for-byte as before.
"""

from __future__ import annotations

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
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id=WID,
            runtime_id=tuple(1000 + i for i in path),
            class_name="demo",
            control_type=role,
            path=path,
        ),
        role=role,
        name=name,
        children=children or [],
    )


def _tree(visible_rows: int) -> RawTree:
    """A list with `visible_rows` captured rows, mirroring the collector output."""
    rows = [_n((1, i), "listitem", f"Item {i + 1}") for i in range(visible_rows)]
    root = _n((), "pane", "Shop", children=[_n((1,), "list", "商品", children=rows)])
    return RawTree(
        window=WindowInfo(window_id=WID, title="Shop", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def _mixed_tree(visible_rows: int) -> RawTree:
    """A `group` whose children have mixed roles, so compile grants it no `find`."""
    rows = [_n((1, i), "text", f"Line {i + 1}") for i in range(visible_rows)]
    detail = _n((1,), "group", "详情", children=rows)
    root = _n((), "pane", "Shop", children=[detail])
    return RawTree(
        window=WindowInfo(window_id=WID, title="Shop", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def _compile_with_viewport(
    visible_rows: int,
    *,
    total: int | None,
    visible: int | None,
    truncated: bool,
    scroll: object | None = None,
):
    result = Compiler().compile(_tree(visible_rows))
    container = next(n for n in result.root.walk() if n.role == "list")
    if total is not None:
        container.attrs["items_total"] = total
    if visible is not None:
        container.attrs["items_visible"] = visible
    container.attrs["items_truncated"] = truncated
    if scroll is not None:
        container.attrs["scroll_pct"] = scroll
    return result, container


def _render(
    result,
    *,
    budget: ObservationBudget | None = None,
    expanded: set[int] | None = None,
    full: bool = False,
) -> str:
    engine = ProjectionEngine(budget or ObservationBudget().for_level(0), l0=True)
    plan = engine.plan(
        result.root,
        apply_projection(result.root, []),
        expanded=expanded or set(),
        full=full,
    )
    return render_document(plan.lines, rev=1)


def _metadata_and_row_cost(result) -> tuple[int, int, int]:
    """The engine-side counter values: after the forced lines, one row, marker.

    The engine's counter tracks only the lines it plans (the ``@rev`` header
    is rendered but never counted), and the row admissibility check re-adds
    the row and marker costs per candidate row. Deriving the budgets from
    these measured values keeps the contract identical under tiktoken and
    the estimator.
    """
    lines = _render(result).splitlines()
    meta_all = sum(count_tokens(l) for l in lines if '"Item ' not in l)
    row = count_tokens(next(l for l in lines if '"Item ' in l))
    marker = count_tokens(next(l for l in lines if "+more" in l))
    rev = count_tokens(lines[0])
    return meta_all - rev - marker, row, marker


def test_240_rows_shows_summary_examples_and_a_more_marker():
    result, container = _compile_with_viewport(
        12, total=240, visible=12, truncated=True, scroll=5
    )
    text = _render(result)
    lines = text.splitlines()

    summary = next(l for l in lines if "商品" in l)
    # No `count=` contradiction: rows= is the authoritative total.
    assert "rows=240" in summary and "visible=12" in summary and "scroll=5%" in summary
    assert "count=" not in summary, summary

    examples = [l for l in lines if '"Item ' in l]
    assert len(examples) == 3, examples
    # A row is rendered in full (its name is not abbreviated or summarised).
    assert examples[0].strip().startswith("LISTITEM"), examples[0]

    marker = next(l for l in lines if l.strip().startswith("+more"))
    assert marker.strip() == (
        '+more items=228 use FIND "<name>" or OBSERVE w1 expand l1'
    ), marker


def test_1000_rows_reports_the_hidden_total():
    result, _ = _compile_with_viewport(
        10, total=1000, visible=10, truncated=True, scroll=0
    )
    text = _render(result)
    summary = next(l for l in text.splitlines() if "商品" in l)
    assert "rows=1000" in summary and "visible=10" in summary and "scroll=0%" in summary
    marker = next(l for l in text.splitlines() if "+more" in l)
    assert "items=990" in marker, marker


def test_scroll_is_omitted_when_the_provider_could_not_read_it():
    """WPF keeps the scroll position on a child ScrollViewer, so store reports
    None. The summary must simply lose `scroll=` - never print `scroll=`."""
    result, _ = _compile_with_viewport(
        12, total=240, visible=12, truncated=True, scroll=None
    )
    text = _render(result)
    summary = next(l for l in text.splitlines() if "商品" in l)
    assert "rows=240" in summary and "visible=12" in summary
    assert "scroll=" not in summary, summary
    assert "scroll_pct" not in text and "None" not in summary


def test_the_marker_does_not_promise_find_when_the_container_lacks_it():
    """A mixed-content pane gets no `find` capability; the marker names only
    the route that really exists (`OBSERVE ... expand`)."""
    result = Compiler().compile(_mixed_tree(4))
    container = next(n for n in result.root.walk() if n.role == "group")
    container.attrs.update(
        {"items_total": 40, "items_visible": 4, "items_truncated": True}
    )
    text = _render(result)
    summary = next(l for l in text.splitlines() if "详情" in l)
    assert "+find" not in summary, summary
    marker = next(l for l in text.splitlines() if "+more" in l)
    assert marker.strip() == f"+more items=36 use OBSERVE w1 expand {container.ref}", marker
    assert "FIND" not in marker


def test_the_summary_survives_a_tight_token_budget_while_examples_shrink():
    result, _ = _compile_with_viewport(
        12, total=240, visible=12, truncated=True, scroll=5
    )
    # The budgets are derived from the live engine counter (tiktoken when it
    # is installed, the documented estimator otherwise) so the test states the
    # same contract under either path: `meta + 2 rows + marker` admits exactly
    # two example rows, while `meta` admits neither row but still gets both
    # metadata lines. This is the documented priority: the container summary
    # is never traded away for an example.
    meta, row, marker = _metadata_and_row_cost(result)
    two_rows = _render(result, budget=ObservationBudget(
        max_nodes=200, max_depth=8, max_tokens=meta + 2 * row + marker))
    no_rows = _render(result, budget=ObservationBudget(
        max_nodes=200, max_depth=8, max_tokens=meta))

    for text, expected_rows in ((two_rows, 2), (no_rows, 0)):
        lines = text.splitlines()
        summary = next(l for l in lines if "商品" in l)
        assert "rows=240" in summary and "visible=12" in summary and "scroll=5%" in summary
        marker = next(l for l in lines if "+more" in l)
        assert "items=228" in marker
        assert len([l for l in lines if '"Item ' in l]) == expected_rows


def test_expand_renders_every_captured_row():
    result, container = _compile_with_viewport(
        12, total=240, visible=12, truncated=True, scroll=5
    )
    text = _render(result, expanded={id(container)})
    assert len([l for l in text.splitlines() if '"Item ' in l]) == 12
    # The marker stays: those 12 rows are still one page of a 240-row list.
    assert "+more items=228" in text


def test_zero_rows_and_a_full_view_render_exactly_as_before():
    for total, visible, truncated in ((0, 0, True), (12, 12, False)):
        with_fields, _ = _compile_with_viewport(
            total, total=total, visible=visible, truncated=truncated, scroll=5
        )
        baseline = _render(Compiler().compile(_tree(total)))
        assert _render(with_fields) == baseline, (total, visible, truncated)


def test_a_container_without_the_fields_renders_exactly_as_before():
    baseline = _render(Compiler().compile(_tree(12)))
    result, container = _compile_with_viewport(
        12, total=None, visible=None, truncated=False, scroll=None
    )
    assert _render(result) == baseline
    assert "rows=" not in _render(result)


def test_the_fields_never_leak_into_the_observation():
    """`items_total` etc. are transport, not vocabulary: they must not be printed."""
    result, _ = _compile_with_viewport(
        12, total=240, visible=12, truncated=True, scroll=5
    )
    text = _render(result)
    assert "items_total" not in text
    assert "items_visible" not in text
    assert "items_truncated" not in text
    assert "scroll_pct" not in text
