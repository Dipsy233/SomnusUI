"""Scripted ``run``: CALL steps plus AWAIT, one round trip.

Covers the parser (AWAIT and ``$it`` are script-only), the two executors
(pure-CALL keeps the batch's whole-script dry run; AWAIT resolves step by step
and stops at the first failure), the ``$it`` binding rules, and the menu flow
the feature exists for.
"""

from __future__ import annotations

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import ExecResult
from somnus.protocol.tools import call_tool
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session, DEFAULT_AWAIT_TIMEOUT_S
from somnus.sui.parse import (
    MAX_SCRIPT_STEPS,
    parse_action,
    parse_script,
)
from tests.fixtures import scenarios as S
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


def _menu_session(provider: MockProvider | None = None) -> Session:
    provider = provider or MockProvider(
        fixtures=[S.menu_tree()],
        scripts=S.scenario_scripts(),
        callbacks=S.callbacks(),
    )
    session = Session(provider)
    session.observe()
    return session


def _demo_session(**kw) -> Session:
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    return Session(provider, **kw)


def _ref_of(session: Session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node {automation_id!r}")


class _FailOnCallProvider(MockProvider):
    """A provider whose Nth execute call fails with a real error.

    The capability resolves and the pattern exists, so the dry run cannot see
    it; the step runs and the element refuses - the shape a live failure has.
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


def test_script_parses_call_await_and_the_it_step():
    steps = parse_script(
        'CALL mn2.activate()\nAWAIT MENUITEM "保存" timeout=1\nCALL $it.activate()'
    )
    assert [s.kind for s in steps] == ["call", "await", "call"]
    assert steps[0].ref == "mn2" and steps[0].capability == "activate"
    assert steps[1].query_type == "menuitem" and steps[1].query == "保存"
    assert steps[1].timeout == 1.0
    assert steps[2].ref == "$it" and steps[2].capability == "activate"


def test_await_type_is_optional_and_timeout_defaults_to_the_runtime():
    awaited = parse_script('AWAIT "退出"')[0]
    assert awaited.kind == "await"
    assert awaited.query_type is None and awaited.query == "退出"
    assert awaited.timeout is None
    assert DEFAULT_AWAIT_TIMEOUT_S > 0


def test_await_scope_is_parsed():
    (awaited,) = parse_script('AWAIT MENUITEM "保存" in mn2 timeout=0.5')
    assert awaited.scope == "mn2"


def test_await_outside_a_script_is_refused_with_a_pointer():
    with pytest.raises(SomnusError) as err:
        parse_action('AWAIT MENUITEM "保存"')
    assert err.value.code == ErrorCode.PARSE_ERROR
    assert "run" in (err.value.message or "")


def test_it_is_not_a_node_ref_outside_a_script():
    with pytest.raises(SomnusError) as err:
        parse_action("CALL $it.activate()")
    assert err.value.code == ErrorCode.PARSE_ERROR


def test_a_non_script_statement_names_its_line():
    with pytest.raises(SomnusError) as err:
        parse_script('CALL p1.add()\nFIND "Big Mac"')
    assert err.value.code == ErrorCode.PARSE_ERROR
    assert "line 2" in (err.value.message or "")


def test_a_quoted_string_is_required_by_await():
    with pytest.raises(SomnusError) as err:
        parse_script("AWAIT MENUITEM 保存")
    assert err.value.code == ErrorCode.PARSE_ERROR
    # The error names the form, and the line: inside a script "AWAIT is only
    # valid inside a run script" would be plainly false.
    assert "invalid AWAIT" in (err.value.message or "")
    assert "line 1" in (err.value.message or "")


def test_script_overflow_is_refused():
    script = "\n".join("CALL p1.add()" for _ in range(MAX_SCRIPT_STEPS + 1))
    with pytest.raises(SomnusError) as err:
        parse_script(script)
    assert "at most" in (err.value.message or "")


def test_blank_lines_and_comments_are_skipped():
    steps = parse_script('CALL p1.add()\n# wait for it\n\nAWAIT "Fries"')
    assert [s.kind for s in steps] == ["call", "await"]


# ------------------------------------------------------- pure CALL: the batch


def test_pure_call_script_runs_as_one_batch_and_says_run():
    session = _demo_session()
    session.observe()
    out = call_tool(
        session, "run", {"script": "CALL p1.add()\nCALL p2.add()"}
    )
    assert out.startswith("RUN OK steps=2/2"), out
    # One settle, one rev, one merged delta - exactly the batch's answer.
    assert "~ c1 count 0→2" in out
    assert "  1. CALL p1.add() -> ok" in out


def test_pure_call_script_keeps_the_whole_script_dry_run():
    session = _demo_session()
    session.observe()
    out = call_tool(
        session, "run", {"script": "CALL p1.add()\nCALL p1.nosuch()"}
    )
    assert out.startswith("ERR CAPABILITY_NOT_FOUND"), out
    # Nothing ran: a step that could never work refuses the script for free.
    assert session.provider.executed == []


def test_pure_call_script_still_refuses_context_menu_as_a_step():
    session = _demo_session()
    session.observe()
    out = call_tool(
        session, "run", {"script": "CALL p1.add()\nCALL p1.context_menu()"}
    )
    assert out.startswith("ERR INVALID_ARGUMENT"), out
    assert session.provider.executed == []


# --------------------------------------------------------------- $it binding


def test_unbound_it_is_refused_before_anything_runs():
    session = _demo_session()
    session.observe()
    out = call_tool(session, "run", {"script": "CALL $it.activate()"})
    assert out.startswith("ERR INVALID_ARGUMENT $it"), out
    assert "AWAIT" in out
    assert session.provider.executed == []


def test_it_used_before_its_await_in_a_longer_script_is_also_refused():
    session = _demo_session()
    session.observe()
    with pytest.raises(SomnusError) as err:
        session.run_script(
            parse_script('CALL $it.activate()\nAWAIT "Add"')
        )
    assert err.value.code == ErrorCode.INVALID_ARGUMENT
    assert "step 1" in (err.value.message or "")
    assert session.provider.executed == []


# ----------------------------------------------------- AWAIT: the menu flow


def test_the_menu_flow_runs_in_one_call():
    """expand -> AWAIT -> click the item, one ``run``."""
    session = _menu_session()
    entry = _ref_of(session, "menu_bar_文件")
    script = (
        f"CALL {entry}.activate()\n"
        'AWAIT MENUITEM "保存" timeout=1\n'
        "CALL $it.activate()"
    )
    out = call_tool(session, "run", {"script": script})
    assert out.startswith("RUN OK steps=3/3"), out
    assert "  2. AWAIT MENUITEM \"保存\" timeout=1 -> ok ($it=" in out
    session.observe()
    status = _ref_of(session, "menu_status")
    node = session.result.find(status)
    assert node is not None and "保存" in (node.name or "")


def test_await_can_be_scoped_to_the_container():
    session = _menu_session()
    entry = _ref_of(session, "menu_bar_文件")
    script = (
        f"CALL {entry}.activate()\n"
        f'AWAIT MENUITEM "退出" in {entry}\n'
        "CALL $it.activate()"
    )
    out = call_tool(session, "run", {"script": script})
    assert out.startswith("RUN OK steps=3/3"), out


def test_await_timeout_stops_and_reports_the_steps_that_ran():
    session = _menu_session()
    entry = _ref_of(session, "menu_bar_文件")
    script = (
        f"CALL {entry}.activate()\n"
        'AWAIT MENUITEM "不存在的项" timeout=0.3\n'
        "CALL $it.activate()"
    )
    out = call_tool(session, "run", {"script": script})
    assert out.startswith("RUN PARTIAL steps=1/3 failed-at=2"), out
    assert "  2. AWAIT MENUITEM \"不存在的项\" timeout=0.3 -> ERR TARGET_NOT_FOUND" in out
    assert "  3. CALL $it.activate() -> not-run" in out
    assert out.count("ERR TARGET_NOT_FOUND") >= 1 and "hint=observe" in out
    # The step that ran really ran, and its delta is in the same reply.
    assert "+ MENUITEM" in out


def test_a_failure_inside_an_await_script_reports_steps_and_delta():
    provider = _FailOnCallProvider(
        fixtures=[S.menu_tree()],
        scripts=S.scenario_scripts(),
        callbacks=S.callbacks(),
        fail_on_call=2,
    )
    session = _menu_session(provider)
    entry = _ref_of(session, "menu_bar_文件")
    script = (
        f"CALL {entry}.activate()\n"
        'AWAIT MENUITEM "保存" timeout=1\n'
        "CALL $it.activate()"
    )
    out = call_tool(session, "run", {"script": script})
    assert out.startswith("RUN PARTIAL steps=2/3 failed-at=3"), out
    assert "  3. CALL $it.activate() -> ERR TARGET_DISABLED" in out
    assert "ERR TARGET_DISABLED" in out
    # The delta covers what the steps before the failure produced.
    assert "保存" in out and "+ MENUITEM" in out


def test_nothing_runs_when_the_first_await_misses():
    session = _menu_session()
    out = call_tool(session, "run", {"script": 'AWAIT MENUITEM "无" timeout=0.3'})
    assert out.startswith("RUN PARTIAL steps=0/1 failed-at=1"), out
    assert "the failing step ran first" in out
    assert session.provider.executed == []


# ------------------------------------------------------------ no confirmation


def test_a_critical_cap_executes_directly_in_every_tool():
    """There is no confirmation gate in this build: `call` and `run` both run it.

    `+submit!` is metadata about the capability (it prints ``!``), not a
    permission request - so the action is really dispatched, not deferred.
    """
    session = _demo_session()
    session.observe()

    refused = call_tool(session, "call", {"action": "CALL b5.submit()"})
    assert refused.startswith("OK"), refused
    assert [primitive for _ref, primitive, _value in session.provider.executed] == [
        "activate"
    ]

    out = call_tool(session, "run", {"script": "CALL b5.submit()"})
    assert out.startswith("RUN OK steps=1/1"), out
    assert not out.startswith("ERR"), out
    assert len(session.provider.executed) == 2


# ------------------------------------------------------------------- the tool


def test_a_write_inside_a_script_still_echoes_what_landed():
    """The write echo is not part of the tree delta, so the script reply keeps it."""
    session = Session(
        MockProvider(
            fixtures=[S.form_tree()],
            scripts=S.scenario_scripts(),
            callbacks=S.callbacks(),
        )
    )
    session.observe()
    name = _ref_of(session, "form_name")
    out = call_tool(
        session,
        "run",
        {"script": f'CALL {name}.set_text("张三")\nAWAIT "同意条款" timeout=0.5'},
    )
    assert out.startswith("RUN OK steps=2/2"), out
    assert '~ ' in out and 'text="张三"' in out, out


def test_run_needs_a_script():
    session = _demo_session()
    session.observe()
    for args in ({}, {"script": "   "}):
        out = call_tool(session, "run", args)
        assert out.startswith("ERR PARSE_ERROR"), out


def test_run_rejects_a_non_string_window():
    session = _demo_session()
    session.observe()
    out = call_tool(session, "run", {"script": "CALL p1.add()", "window": 3})
    assert out.startswith("ERR INVALID_ARGUMENT"), out
