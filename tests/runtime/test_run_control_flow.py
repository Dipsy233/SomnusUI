"""Script control flow: EACH loops, TRY tolerance, IF gating.

Covers the parser (new line forms, prefix rules, structure refusals) and the
step-by-step executor (per-iteration rebinding, zero-match loops, budgets,
failure tolerance and conditional steps) on the restaurant demo fixtures.
"""

from __future__ import annotations

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import ExecResult
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.sui.parse import parse_script
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


def _demo_session(provider: MockProvider | None = None, **kw):
    provider = provider or MockProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
    )
    session = Session(provider, **kw)
    session.observe()
    return session


def _ref_of(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node {automation_id!r}")


class _FailOnCallProvider(MockProvider):
    """A provider whose Nth execute call fails with a real error."""

    def __init__(self, *args, fail_on_call: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fail_on_call = fail_on_call
        self._calls = 0

    def execute(self, ref, primitive, value=None, timeout_s=3.0):  # type: ignore[override]
        self._calls += 1
        if self._calls == self._fail_on_call:
            return ExecResult(ok=False, error="TARGET_DISABLED")
        return super().execute(ref, primitive, value, timeout_s)


# --------------------------------------------------------------------- parser


def test_each_parses_query_scope_and_max():
    steps = parse_script('EACH BUTTON "Add" in l1 max=5\nCALL $it.activate()\nEND')
    assert [s.kind for s in steps] == ["each", "call", "end"]
    each = steps[0]
    assert each.query_type == "button" and each.query == "Add"
    assert each.scope == "l1" and each.max_iter == 5


def test_try_and_if_are_step_modifiers_and_raw_keeps_the_full_line():
    steps = parse_script('TRY CALL b1.activate()\nIF failed CALL b1.select()')
    assert steps[0].on_fail == "continue" and steps[0].cond == "none"
    assert steps[1].cond == "if_failed" and steps[1].on_fail == "stop"
    assert steps[0].raw == "TRY CALL b1.activate()"


def test_try_on_await_is_allowed():
    (step,) = parse_script('TRY AWAIT DIALOG "提示" timeout=0')
    assert step.kind == "await" and step.on_fail == "continue"


@pytest.mark.parametrize(
    "script,fragment",
    [
        ('EACH BUTTON "A"\nEACH BUTTON "B"\nEND\nEND', "nested EACH"),
        ("END", "END without a matching EACH"),
        ('EACH BUTTON "A"\nCALL $it.activate()', "EACH without a matching END"),
        ('IF ok CALL b1.activate()', "IF must follow"),
        ('EACH BUTTON "A"\nIF ok CALL $it.activate()\nEND', "IF must follow"),
        ('TRY TRY CALL b1.activate()', "duplicate TRY"),
        ('IF ok IF failed CALL b1.activate()', "duplicate IF"),
        ('TRY EACH BUTTON "A"\nEND', "does not take TRY/IF"),
        ("EACH BUTTON A", "invalid EACH"),
    ],
)
def test_structure_refusals_name_the_rule(script, fragment):
    with pytest.raises(SomnusError) as err:
        parse_script(script)
    assert err.value.code == ErrorCode.PARSE_ERROR
    assert fragment in (err.value.message or "")


# ------------------------------------------------------------------ executor


def test_each_iterates_current_matches_and_rebinds_it():
    session = _demo_session()
    out = session.run_script(parse_script('EACH BUTTON "Add"\nCALL $it.activate()\nEND'))
    assert out.startswith("RUN OK steps=6/6"), out
    # One binding line per iteration (1.1-1.3) and one call per iteration.
    assert "1.1" in out and "1.3" in out and "2.2" in out
    assert "count 0→3" in out, out


def test_each_with_no_matches_skips_the_body():
    session = _demo_session()
    out = session.run_script(parse_script('EACH BUTTON "Nope"\nCALL $it.activate()\nEND'))
    assert out.startswith("RUN OK"), out
    assert "ok (0 matches)" in out
    assert "-> not-run" in out


def test_each_respects_max():
    session = _demo_session()
    out = session.run_script(
        parse_script('EACH BUTTON "Add" max=2\nCALL $it.activate()\nEND')
    )
    assert "count 0→2" in out, out


def test_try_records_the_failure_and_runs_on():
    provider = _FailOnCallProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts(), fail_on_call=1
    )
    session = _demo_session(provider)
    first = _ref_of(session, "product_0_add")
    second = _ref_of(session, "product_1_add")
    out = session.run_script(
        parse_script(f"TRY CALL {first}.activate()\nCALL {second}.activate()")
    )
    assert out.startswith("RUN OK steps=1/2"), out
    assert "ERR TARGET_DISABLED" in out
    assert "count 0→1" in out, out


def test_if_gates_on_the_previous_executed_step():
    provider = _FailOnCallProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts(), fail_on_call=1
    )
    session = _demo_session(provider)
    first = _ref_of(session, "product_0_add")
    second = _ref_of(session, "product_1_add")
    out = session.run_script(
        parse_script(
            f"TRY CALL {first}.activate()\n"
            f"IF ok CALL {second}.activate()\n"
            f"IF failed CALL {second}.activate()"
        )
    )
    # The first IF is skipped (its step failed), the second runs.
    assert "-> skipped" in out, out
    assert "count 0→1" in out, out


def test_the_executed_step_budget_stops_a_loop():
    tree = restaurant_demo_tree(catalog=[(f"Item {i}", 1.0) for i in range(50)])
    session = _demo_session(MockProvider(fixtures=[tree], scripts=demo_scripts()))
    out = session.run_script(
        parse_script('EACH BUTTON "Add" max=50\nCALL $it.activate()\nEND')
    )
    assert "reason=budget" in out, out


def test_it_unbound_is_still_refused_before_any_step_runs():
    session = _demo_session()
    with pytest.raises(SomnusError) as err:
        session.run_script(parse_script("CALL $it.activate()"))
    assert err.value.code == ErrorCode.INVALID_ARGUMENT
