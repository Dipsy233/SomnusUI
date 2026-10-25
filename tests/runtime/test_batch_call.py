"""Batch CALL: several statements, one round trip.

Covers the parser (quote-aware statement split), the session's batch path
(dry-run refusal, sequential execution, one settle / one delta / one rev,
cumulative effect verification) and the two v1 exclusions (window focus,
context_menu).
"""

from __future__ import annotations

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import Action, ExecResult
from somnus.protocol.tools import call_tool
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from somnus.sui.parse import MAX_BATCH_STATEMENTS, parse_call_block
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


def _new_session(**kw) -> Session:
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    return Session(provider, **kw)


class _FailOnCallProvider(MockProvider):
    """A provider whose Nth execute call fails with a real error.

    This is the shape a mid-batch failure has on a live provider too: the
    capability resolved, the pattern exists, the element refused - so the
    dry run passed and the batch stops at that step with the steps before
    it already performed.
    """

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


def test_block_parses_each_line():
    actions = parse_call_block("CALL p1.add()\nCALL p2.add()")
    assert [a.ref for a in actions] == ["p1", "p2"]
    assert all(a.kind == "call" for a in actions)


def test_block_single_statement():
    (action,) = parse_call_block("CALL p1.add()")
    assert action.ref == "p1" and action.capability == "add"


def test_block_tolerates_bare_call():
    (action,) = parse_call_block("p1.add()")
    assert action.kind == "call"


def test_quoted_newline_stays_inside_the_statement():
    # A text argument may contain a real line feed (JSON delivers one
    # unescaped); the split must not cut the statement in half.
    block = 'CALL q1.set_text("第一行\n第二行")\nCALL p1.add()'
    actions = parse_call_block(block)
    assert len(actions) == 2
    assert list(actions[0].args.values()) == ["第一行\n第二行"]


def test_blank_lines_and_comments_are_skipped():
    actions = parse_call_block("CALL p1.add()\n# a comment\n\nCALL p2.add()")
    assert [a.ref for a in actions] == ["p1", "p2"]


def test_non_call_statement_is_refused_with_the_line_number():
    with pytest.raises(SomnusError) as err:
        parse_call_block("CALL p1.add()\nFIND \"Big Mac\"")
    assert err.value.code == ErrorCode.PARSE_ERROR
    assert "line 2" in (err.value.message or "")


def test_statement_overflow_is_refused():
    block = "\n".join(f"CALL p1.add()" for _ in range(MAX_BATCH_STATEMENTS + 1))
    with pytest.raises(SomnusError) as err:
        parse_call_block(block)
    assert "at most" in (err.value.message or "")


# ----------------------------------------------------------------- batch path


def test_batch_runs_and_reports_one_delta():
    session = _new_session()
    session.observe()
    actions = parse_call_block("CALL p1.add()\nCALL p2.add()")
    out = session.call_batch(actions)
    assert out.text.startswith("BATCH OK steps=2/2 rev=2")
    # One settle, one rev bump for the whole batch: the two adds land as a
    # single transition 0 -> 2, not two separate observations.
    assert "~ c1 count 0→2" in out.text
    assert "~ c1 total 0→8.48" in out.text
    assert "  1. CALL p1.add() -> ok" in out.text
    assert "  2. CALL p2.add() -> ok" in out.text


def test_batch_cumulative_effects_are_verified_as_a_sum():
    # Each add declares `cart.count += 1`; against the combined delta the
    # final count is 2, and checking each step's own +1 would be a false
    # violation. The grouped verifier expects baseline + 2.
    session = _new_session()
    session.observe()
    out = session.call_batch(parse_call_block("CALL p1.add()\nCALL p2.add()"))
    assert "EFFECT_NOT_CONFIRMED" not in out.text
    assert out.status == "OK"


def test_batch_through_the_call_tool():
    session = _new_session()
    session.observe()
    out = call_tool(session, "call", {"action": "CALL p1.add()\nCALL p2.add()"})
    assert out.startswith("BATCH OK steps=2/2")


def test_single_statement_keeps_the_single_call_response():
    # One statement must behave exactly as it always did: `OK rev=...`, no
    # batch header.
    session = _new_session()
    session.observe()
    out = call_tool(session, "call", {"action": "CALL p1.add()"})
    assert out.startswith("OK rev=2")
    assert "~ c1 count 0→1" in out


def test_dry_run_refuses_the_whole_batch_and_executes_nothing():
    session = _new_session()
    session.observe()
    # Step 2 can never work; refusing it must not cost step 1.
    out = call_tool(session, "call", {"action": "CALL p1.add()\nCALL p1.nosuch()"})
    assert out.startswith("ERR CAPABILITY_NOT_FOUND")
    assert session.provider.executed == []


def test_dry_run_refuses_unconfirmed_critical_step():
    session = _new_session(policy=Policy(confirmed=False))
    session.observe()
    out = call_tool(
        session, "call", {"action": "CALL p1.add()\nCALL b5.submit()"}
    )
    assert out.startswith("ERR CONFIRMATION_REQUIRED")
    # Nothing ran: the confirmation gate is part of the dry run.
    assert session.provider.executed == []


def test_dry_run_refuses_stale_ref_without_executing():
    session = _new_session()
    session.observe()
    actions = parse_call_block("CALL zz9.activate()\nCALL p1.add()")
    with pytest.raises(SomnusError) as err:
        session.call_batch(actions)
    assert err.value.code == ErrorCode.STALE_TARGET
    assert session.provider.executed == []


def test_mid_batch_failure_stops_and_reports_where():
    provider = _FailOnCallProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts(), fail_on_call=2
    )
    session = Session(provider)
    session.observe()
    actions = parse_call_block("CALL p1.add()\nCALL p2.add()")
    out = session.call_batch(actions)
    assert out.text.startswith("BATCH PARTIAL steps=1/2 failed-at=2")
    # The step that ran really ran; the one after it was never sent.
    assert "  1. CALL p1.add() -> ok" in out.text
    assert "  2. CALL p2.add() -> ERR TARGET_DISABLED" in out.text
    assert len(provider.executed) == 1
    # The state the completed step produced is in the same reply.
    assert "~ c1 count 0→1" in out.text
    assert "ERR TARGET_DISABLED" in out.text


def test_context_menu_is_refused_at_the_dry_run():
    session = _new_session()
    session.observe()
    actions = parse_call_block("CALL p1.add()\nCALL p1.context_menu()")
    with pytest.raises(SomnusError) as err:
        session.call_batch(actions)
    assert err.value.code == ErrorCode.INVALID_ARGUMENT
    assert "settle" in (err.value.message or "")
    assert session.provider.executed == []


def test_window_focus_is_refused_at_the_dry_run():
    session = _new_session()
    session.observe()
    actions = parse_call_block("CALL w1.focus()\nCALL p1.add()")
    with pytest.raises(SomnusError) as err:
        session.call_batch(actions)
    assert err.value.code == ErrorCode.INVALID_ARGUMENT
    assert "separately" in (err.value.message or "")
    assert session.provider.executed == []


def test_empty_batch_is_a_parse_error():
    session = _new_session()
    with pytest.raises(SomnusError) as err:
        session.call_batch([])
    assert err.value.code == ErrorCode.PARSE_ERROR
