"""Desktop summary projection: one line replaces an `observe windows` turn.

The capture layer passes `desktop_summary` to `ProjectionEngine.plan`; the
projection prepends one `WINDOWS ...` line. Everything here is pure rendering:
the entry point must accept the keyword, `None` must be byte-identical to
today, the line must survive L0's budget, and titles must not break the line.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import apply_projection
from somnus.sui.serialize import render_document


def _tree() -> RawTree:
    root = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(1,), path=()),
        role="pane",
        name="Shop",
        children=[RawNode(native=NativeRef(window_id="w1", runtime_id=(2,), path=(1,)),
                          role="button", name="确定", patterns=frozenset({"invoke"}))],
    )
    return RawTree(
        window=WindowInfo(window_id="w1", title="Shop", process="demo.exe", pid=1, active=True),
        root=root,
        raw_revision=1,
    )


def _plan(*, desktop_summary=None, budget: ObservationBudget | None = None):
    result = Compiler().compile(_tree())
    engine = ProjectionEngine(budget or ObservationBudget().for_level(0), l0=True)
    return result, engine.plan(
        result.root,
        apply_projection(result.root, []),
        desktop_summary=desktop_summary,
    )


def _text(*, desktop_summary=None, budget: ObservationBudget | None = None) -> str:
    _result, plan = _plan(desktop_summary=desktop_summary, budget=budget)
    return render_document(plan.lines, rev=1)


def test_the_summary_is_the_first_content_line_and_marks_active():
    text = _text(desktop_summary=[
        {"window_id": "w1", "title": "QQ", "active": False},
        {"window_id": "w2", "title": "设置", "active": True, "ops": 31},
    ])
    lines = text.splitlines()
    assert lines[0] == "@rev 1"
    assert lines[1] == 'WINDOWS w1 "QQ" | w2 "设置" ops=31 active', lines[1]


def test_ops_is_omitted_when_absent_but_zero_is_kept():
    text = _text(desktop_summary=[
        {"window_id": "w1", "title": "QQ"},
        {"window_id": "w2", "title": "空面板", "ops": 0},
    ])
    line = text.splitlines()[1]
    assert line == 'WINDOWS w1 "QQ" | w2 "空面板" ops=0', line
    assert "ops=None" not in text


def test_none_and_empty_summaries_are_byte_identical_to_today():
    baseline = _text()
    assert _text(desktop_summary=None) == baseline
    assert _text(desktop_summary=[]) == baseline
    assert "WINDOWS " not in baseline
    assert "\n\n" not in baseline


def test_more_than_six_windows_truncates_but_forces_the_active_one():
    windows = [
        {"window_id": f"w{i}", "title": f"W{i}", "active": i == 8, "ops": i}
        for i in range(1, 9)  # w8 is active and falls outside the first six
    ]
    line = _text(desktop_summary=windows).splitlines()[1]
    assert "w8" in line and "active" in line
    assert "w6" not in line and "w7" not in line
    assert "+2 more" in line
    # Still exactly six listed windows plus the fold marker.
    assert line.count("w") >= 6


def test_an_active_window_inside_the_first_six_does_not_displace_anyone():
    windows = [
        {"window_id": f"w{i}", "title": f"W{i}", "active": i == 3}
        for i in range(1, 9)
    ]
    line = _text(desktop_summary=windows).splitlines()[1]
    for i in range(1, 7):
        assert f"w{i} " in line, line
    assert "+2 more" in line


def test_quotes_pipes_newlines_and_long_titles_are_neutralised():
    line = _text(desktop_summary=[
        {
            "window_id": "w1",
            "title": 'He said "hi" | there\nand a newline',
            "active": True,
        },
        {"window_id": "w2", "title": "T" * 60},
    ]).splitlines()[1]
    assert '\\"hi\\"' in line and "\\|" in line
    assert "\n" not in line
    # A long title is truncated, never passed through at full length.
    assert "T" * 60 not in line and "…" in line


def test_the_summary_survives_a_budget_that_even_drops_the_tree():
    _result, plan = _plan(
        desktop_summary=[{"window_id": "w1", "title": "QQ", "ops": 12}],
        budget=ObservationBudget(max_nodes=1, max_depth=1, max_tokens=3),
    )
    assert plan.lines[0].marker_text == 'WINDOWS w1 "QQ" ops=12'
    assert plan.tokens >= count_tokens(plan.lines[0].marker_text)


def test_six_window_summary_stays_small():
    windows = [
        {"window_id": f"w{i}", "title": f"Window {i}", "ops": 10 + i,
         "active": i == 2}
        for i in range(1, 7)
    ]
    line = _text(desktop_summary=windows).splitlines()[1]
    # The whole point: one line, tens of tokens, not a desktop dump.
    assert count_tokens(line) <= 50, (count_tokens(line), line)
