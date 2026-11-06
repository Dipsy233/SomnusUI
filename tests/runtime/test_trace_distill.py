"""Trace → run-script distillation: the agent's experience becomes a recipe.

The loop this closes: record a flow once (``--trace``), distil it (``somnus
trace to-script``), replay it as one ``run`` call from then on, and let
``heal`` repair it when the UI drifts. These tests pin the deterministic part:
which steps become ``AWAIT`` + ``CALL $it``, which become ``# TODO``, and
that the draft parses with the grammar the runtime will actually run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from somnus.errors import SomnusError
from somnus.observability.jsonl import JsonlTraceSink
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.selfcheck.distill_script import (
    distill_script,
    _render_args,
    _render_value,
)
from somnus.sui.parse import parse_script
from tests.fixtures.builders import demo_scripts, notepad_tree, restaurant_demo_tree


def _recorded_session(tmp_path: Path) -> Session:
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    session = Session(provider)
    session.trace_sink = JsonlTraceSink(tmp_path / "trace")
    return session


def _distill(path: Path):
    report = distill_script(path)
    # Every draft must survive the parser the runtime will run it with. A
    # draft with no distilled step is a review checklist, not a script.
    if report.distilled:
        parse_script(report.script)
    return report


def test_a_recorded_flow_becomes_await_and_call(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.dispatch('FIND product "Big Mac"')
    session.dispatch("CALL p1.add()")
    session.trace_sink.close()
    session.close()

    report = _distill(tmp_path / "trace")
    assert report.distilled == 1 and report.skipped == 0
    # The recorded ref dies with the session, so the draft names the element
    # the way an AWAIT does: by the type and name it printed.
    assert 'AWAIT PRODUCT "Big Mac" timeout=0' in report.script
    assert "CALL $it.add()" in report.script
    assert "TODO" not in report.script
    assert 'window="Restaurant Demo"' in report.script


def test_failed_steps_are_not_distilled(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.dispatch("CALL zz9.activate()")  # no such target: ERR
    session.dispatch("CALL p1.add()")
    session.trace_sink.close()
    session.close()

    report = _distill(tmp_path / "trace")
    assert report.distilled == 1
    assert "zz9" not in report.script.replace("# TODO", "")
    assert "CALL $it.add()" in report.script


def test_reads_are_not_distilled(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.dispatch('FIND product "Big Mac"')
    session.dispatch("CALL p1.add()")
    session.trace_sink.close()
    session.close()

    report = _distill(tmp_path / "trace")
    lines = [
        line
        for line in report.script.splitlines()
        if line and not line.startswith("#")
    ]
    # One AWAIT, one CALL: observe/find shaped the experience, the call is
    # the recipe.
    assert lines == ['AWAIT PRODUCT "Big Mac" timeout=0', "CALL $it.add()"]


def test_consecutive_steps_on_one_element_share_the_binding(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.dispatch("CALL p1.add()")
    session.dispatch("CALL p1.add()")
    session.trace_sink.close()
    session.close()

    report = _distill(tmp_path / "trace")
    body = [
        line
        for line in report.script.splitlines()
        if line and not line.startswith("#")
    ]
    assert body == [
        'AWAIT PRODUCT "Big Mac" timeout=0',
        "CALL $it.add()",
        "CALL $it.add()",
    ]


def test_an_unnameable_step_becomes_a_todo(tmp_path):
    """A ref outside the step's snapshot cannot be named, so it is a comment."""
    sink = JsonlTraceSink(tmp_path / "handwritten")
    before = sink.snapshot('WINDOW w1 "Shop"\n  BUTTON b1 "Buy"\n')
    sink.emit(
        "call.start",
        tool="call",
        call_id="s:1",
        ref="zz9",
        cap="activate",
        args={},
        before=before,
    )
    sink.emit("call.end", call_id="s:1", status="OK")
    sink.close()

    report = _distill(tmp_path / "handwritten")
    assert report.distilled == 0 and report.skipped == 1
    assert "# TODO zz9.activate() - no observation recorded" in report.script



def test_a_window_step_becomes_a_window_todo(tmp_path):
    """``run`` takes window= as an argument; CALL wN.focus() is not a step."""
    sink = JsonlTraceSink(tmp_path / "windowstep")
    before = sink.snapshot('WINDOW w1 "Shop" active\n  BUTTON b1 "Buy"\n')
    sink.emit(
        "call.start",
        tool="call",
        call_id="s:1",
        ref="w1",
        cap="focus",
        args={},
        before=before,
    )
    sink.emit("call.end", call_id="s:1", status="OK")
    sink.close()

    report = _distill(tmp_path / "windowstep")
    assert report.skipped == 1
    assert 'window="Shop"' in report.script


def test_args_survive_the_round_trip(tmp_path):
    """set_text's argument comes back as the same quoted text."""
    provider = MockProvider(fixtures=[notepad_tree("old")])
    session = Session(provider)
    session.trace_sink = JsonlTraceSink(tmp_path / "notepad")
    session.observe()
    reply = session.dispatch('FIND input "Text Editor"')
    ref = next(
        word for word in reply.split() if word[0:1].islower() and word[-1].isdigit()
    )
    session.dispatch(f'CALL {ref}.set_text("hello world")')
    session.trace_sink.close()
    session.close()

    report = _distill(tmp_path / "notepad")
    assert 'CALL $it.set_text("hello world")' in report.script


def test_a_trace_without_successful_calls_is_refused(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.trace_sink.close()
    session.close()

    with pytest.raises(SomnusError):
        distill_script(tmp_path / "trace")


def test_await_timeout_is_rendered_when_asked(tmp_path):
    session = _recorded_session(tmp_path)
    session.observe()
    session.dispatch("CALL p1.add()")
    session.trace_sink.close()
    session.close()

    report = distill_script(tmp_path / "trace", await_timeout=1.5)
    assert 'AWAIT PRODUCT "Big Mac" timeout=1.5' in report.script


# ---------------------------------------------------------------------------
# the value renderer
# ---------------------------------------------------------------------------


def test_render_args_keeps_positional_then_named_order():
    assert _render_args({"_0": "a", "_1": 2, "qty": 3}) == '"a", 2, qty=3'


def test_render_value_covers_the_literal_forms():
    assert _render_value(None) == "null"
    assert _render_value(True) == "true"
    assert _render_value(False) == "false"
    assert _render_value(2) == "2"
    assert _render_value(1.5) == "1.5"


def test_render_value_escapes_quotes_and_backslashes():
    # Only \\ and \" are escapes in the grammar, so only they get written.
    assert _render_value('say "hi" \\ ok') == '"say \\"hi\\" \\\\ ok"'
