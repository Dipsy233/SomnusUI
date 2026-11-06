"""Stage 4: doctor, replay and eval - the model-free self-experience loop.

Everything here runs on fixtures (``--demo`` or ``--scenario``), so the portable
suite never takes the user's foreground. The child-session path is exercised at
its refusal boundary (a fake "no live session" answer), which is the part that
must never silently fall back to the main desktop.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

from somnus.cli import main
from somnus.ir.node import RawTree, WindowInfo
from somnus.observability.jsonl import JsonlTraceSink
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.selfcheck import heal, nodes, sandbox
from tests.fixtures.builders import _n, demo_scripts, restaurant_demo_tree


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _session() -> Session:
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    return Session(provider)


def _record_trace(root: Path, command: str = "CALL p1.add()") -> Path:
    sink = JsonlTraceSink(root)
    session = _session()
    session.trace_sink = sink
    session.observe()
    session.dispatch(command)
    session.close()
    return root


# ---------------------------------------------------------------------------
# parsing the observation back into identity
# ---------------------------------------------------------------------------


def test_the_parser_reads_type_name_and_ref_in_document_order():
    text = (
        '@rev 1\n'
        'WINDOWS w1 "Restaurant Demo" active\n'
        'WINDOW w1 "Restaurant Demo" active\n'
        '  SCREEN s1 "menu"\n'
        '    LIST l1 "Products" count=3 entity=product +find\n'
        '      PRODUCT p1 "Big Mac" price=5.99 +add\n'
        '      PRODUCT p2 "Fries" price=2.49 +add\n'
        '    +more items=2 use FIND "name"\n'
    )
    lines = nodes.parse_observation(text)
    assert [(line.kind, line.ref, line.name) for line in lines] == [
        ("WINDOW", "w1", "Restaurant Demo"),
        ("WINDOW", "w1", "Restaurant Demo"),
        ("SCREEN", "s1", "menu"),
        ("LIST", "l1", "Products"),
        ("PRODUCT", "p1", "Big Mac"),
        ("PRODUCT", "p2", "Fries"),
    ]
    assert nodes.line_for_ref(text, "p2").name == "Fries"
    # Ordinal is only meaningful among same type + same name: two rows that
    # share a caption need document order to be told apart.
    duplicate = '  BUTTON b1 "Add"\n  BUTTON b2 "Add"\n'
    dup_lines = nodes.parse_observation(duplicate)
    assert nodes.ordinal_of(dup_lines, dup_lines[1]) == 1
    assert nodes.pick_actionable(text).ref == "l1"
    assert nodes.pick_window(text).ref == "w1"


def test_the_compact_desktop_summary_yields_one_line_per_window():
    text = 'WINDOWS w1 "任务栏" | w2 "Program Manager" active\nWINDOW w2 "Program Manager"\n'
    lines = nodes.parse_observation(text)
    assert [(line.ref, line.name) for line in lines if line.is_window][:2] == [
        ("w1", "任务栏"),
        ("w2", "Program Manager"),
    ]
    assert nodes.pick_window(text).ref == "w2"


def test_a_find_miss_is_not_a_list_of_hits():
    miss = (
        "FOUND 0\n"
        'containers you can scope into (FIND "…" in REF) or INSPECT:\n'
        '  WINDOW w2 "Program Manager"\n'
        '  SCREEN s1 "Program Manager"\n'
    )
    assert nodes.parse_found(miss) == []
    assert nodes.parse_found('FOUND 2\nBUTTON b1 "Add"\nBUTTON b2 "Add"\n')[1].ref == "b2"


def test_escaped_quotes_in_a_name_survive_the_round_trip():
    text = '  BUTTON b1 "say \\"hi\\"" +activate\n'
    line = nodes.parse_observation(text)[0]
    assert line.name == 'say "hi"'


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def test_eval_offline_judges_from_runtime_state():
    code, out, _ = run(["--demo", "eval", "--no-trace"])
    assert code == 0, out
    report = json.loads(out)
    assert report["ok"] is True
    assert report["desktop"] == "offline"
    assert report["provider"] == "mock"
    assert [task["task"] for task in report["tasks"]] == ["observe", "find", "call"]
    assert report["passed"] == 3


@pytest.mark.parametrize("screen", ["store", "form", "chat", "console", "tree"])
def test_eval_offline_works_on_every_scenario_screen(screen):
    code, out, _ = run(["--scenario", screen, "eval", "--no-trace"])
    assert code == 0, out
    assert json.loads(out)["ok"] is True


def test_eval_rejects_real_with_an_offline_fixture():
    code, _, err = run(["--demo", "eval", "--real"])
    assert code == 2
    assert "INVALID_ARGUMENT" in err


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_offline_reports_the_environment_and_the_smoke():
    code, out, _ = run(["--demo", "doctor"])
    assert code == 0, out
    assert "SomnusUI doctor" in out
    assert "offline fixtures" in out
    assert "[ok  ] smoke" in out
    assert "PASS 3/3" in out
    assert "@rev" in out


def test_doctor_can_stop_after_the_environment():
    code, out, _ = run(["--demo", "doctor", "--no-smoke"])
    assert code == 0, out
    assert "smoke" in out and "not run" in out
    assert "@rev" not in out


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_round_trip_resolves_the_ref_by_name(tmp_path: Path):
    trace = _record_trace(tmp_path / "trace")
    code, out, _ = run(["--demo", "replay", str(trace), "--no-trace", "--json"])
    assert code == 0, out
    report = json.loads(out)
    assert report["ok"] is True
    assert report["steps"] == 1
    step = report["step_reports"][0]
    assert step["cap"] == "add"
    assert step["resolved_by_name"] is True
    assert step["actual"] == "OK"


def test_replay_names_the_first_divergence(tmp_path: Path):
    trace = _record_trace(tmp_path / "trace")
    path = trace / "trace.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record.get("event") == "call.start":
            record["cap"] = "addd"
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    code, out, _ = run(["--demo", "replay", str(trace), "--no-trace", "--json"])
    assert code == 1, out
    report = json.loads(out)
    assert report["ok"] is False
    first = report["first_divergence"]
    assert first["step"] == 1
    assert first["divergence"] == "status"
    assert first["expected"] == "OK" and first["actual"] == "ERR"


def test_replay_refuses_a_trace_without_calls(tmp_path: Path):
    sink = JsonlTraceSink(tmp_path / "trace")
    session = _session()
    session.trace_sink = sink
    session.observe()
    session.close()

    code, _, err = run(["--demo", "replay", str(tmp_path / "trace"), "--no-trace"])
    assert code == 1
    assert "no call to replay" in err


# ---------------------------------------------------------------------------
# heal
# ---------------------------------------------------------------------------

_BUTTON = frozenset({"invoke"})


def _calc_tree(second_id: str) -> RawTree:
    """Two invokable buttons; only the second one's identity can change."""
    wid = "w1"
    root = _n(
        wid,
        (),
        "pane",
        "面板",
        children=[
            _n(wid, (0,), "button", "七", patterns=_BUTTON, automation_id="seven"),
            _n(wid, (1,), "button", "计算", patterns=_BUTTON,
               automation_id=second_id),
        ],
    )
    return RawTree(
        window=WindowInfo(
            window_id=wid, title="计算器", process="CalculatorApp.exe", pid=1,
            active=True,
        ),
        root=root,
    )


def _calc_session(second_id: str = "compute_v1") -> Session:
    provider = MockProvider(fixtures=[_calc_tree(second_id)])
    session = Session(provider, allow_packs=False)
    session.observe()
    return session


def _script_for(session: Session) -> str:
    refs = {node.name: node.ref for node in session.result.root.walk() if node.name}
    return (
        f"CALL {refs['七']}.activate()\n"
        f"CALL {refs['计算']}.activate()"
    )


def test_heal_relocates_a_stale_batch_step_and_reruns_it():
    session = _calc_session()
    script = _script_for(session)
    # The second element was rebuilt under a new native id; the batch dry run
    # now refuses before any step runs. The old ref is stale, its name is not.
    session.provider._trees["w1"] = _calc_tree("compute_v2")
    session.observe()

    report = heal.heal_script(session, script)
    session.close()

    assert report.failed_at == 2, report.as_dict()
    assert report.error_code == "STALE_TARGET", report.output
    assert report.patched_script is not None
    assert report.steps[1].healed_ref
    assert report.steps[1].healed_ref != report.steps[1].ref
    assert report.ok is True and report.rerun == "PASS", report.as_dict()
    assert report.patched_script.splitlines()[1].startswith(
        f"CALL {report.steps[1].healed_ref}."
    )


def test_heal_reports_a_step_it_cannot_relocate():
    session = _calc_session()
    script = _script_for(session)
    # The second element is gone, and no same-named control exists to stand in.
    gone = RawTree(
        window=_calc_tree("unused").window,
        root=_n(
            "w1", (), "pane", "面板",
            children=[
                _n("w1", (0,), "button", "七", patterns=_BUTTON,
                   automation_id="seven"),
                # Keep a second child so the first button's sibling role-index
                # (and therefore its fingerprint) does not change.
                _n("w1", (1,), "text", "提示", patterns=frozenset({"text"}),
                   automation_id="hint"),
            ],
        ),
    )
    session.provider._trees["w1"] = gone
    session.observe()

    report = heal.heal_script(session, script)
    session.close()

    assert report.failed_at == 2, report.as_dict()
    assert report.error_code == "STALE_TARGET"
    assert report.ok is False
    assert report.patched_script is None
    assert report.candidates == []


def test_heal_offline_cli_passes_a_script_that_needs_no_heal(tmp_path: Path):
    script = tmp_path / "script.txt"
    script.write_text("CALL p1.add()\n", encoding="utf-8")

    code, out, _ = run(["--demo", "heal", str(script), "--no-trace", "--json"])
    assert code == 0, out
    report = json.loads(out)
    assert report["ok"] is True
    assert report["healed"] is False
    assert report["rerun"] == "not needed"


def test_heal_offline_cli_reports_an_unreadable_script(tmp_path: Path):
    code, _, err = run(["--demo", "heal", str(tmp_path / "missing.txt")])
    assert code == 1
    assert "SCRIPT_READ" in err


# ---------------------------------------------------------------------------
# sandbox refusal: no silent fallback to the main desktop
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="child-session availability is a Windows check",
)
def test_the_default_sandbox_refuses_when_no_child_session_is_live(monkeypatch):
    from somnus.session import childsession

    monkeypatch.setattr(childsession, "inside_child_session", lambda: False)
    monkeypatch.setattr(childsession, "live_child_session", lambda: None)
    session = _session()
    try:
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox.enter(session)
    finally:
        session.close()
    message = str(excinfo.value)
    assert "child session" in message
    assert "--real" in message


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="child-session availability is a Windows check",
)
def test_real_is_refused_inside_the_child_session(monkeypatch):
    from somnus.session import childsession

    monkeypatch.setattr(childsession, "inside_child_session", lambda: True)
    session = _session()
    try:
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox.enter(session, real=True)
    finally:
        session.close()
    assert "main desktop is not reachable" in str(excinfo.value)
