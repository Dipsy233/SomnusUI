"""The command line interface, command by command.

`main()` is what a person and a host script actually run, so a break here is
visible to every user even when the library underneath is fine. Each test drives
it the way a shell would - argv in, stdout captured, exit code checked - rather
than calling the `cmd_*` functions directly, so the argument parser is covered
too.

The provider is always the mock (``--demo`` or ``--scenario``), so the whole
file runs anywhere.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from somnus.cli import main


def run(argv: list[str]) -> tuple[int, str, str]:
    """Run ``main(argv)`` and return ``(exit_code, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# observe / call
# ---------------------------------------------------------------------------


def test_observe_on_the_demo_prints_the_window():
    code, out, _ = run(["--demo", "observe"])
    assert code == 0
    assert 'WINDOW w1 "Restaurant Demo"' in out
    assert "PRODUCT" in out


def test_call_runs_the_action_and_prints_the_delta():
    code, out, _ = run(["--demo", "call", "CALL p1.add()"])
    assert code == 0
    assert out.startswith("OK"), out
    assert "count" in out


def test_call_returns_a_nonzero_exit_on_an_error():
    """A failed call is a failed command: a shell script must be able to tell."""
    code, out, _ = run(["--demo", "call", "CALL ghost.add()"])
    assert code == 1
    assert out.startswith("ERR"), out


def test_call_reports_a_parse_error_as_one_line_and_exit_one():
    """A bad action is reported as a single SUI-Text line, not a traceback.

    `dispatch` never raises to the caller - it renders the error - so the line
    goes to stdout with a nonzero exit code, which is what a script keys on."""
    code, out, err = run(["--demo", "call", 'CALL p1.add("unclosed'])
    assert code == 1
    assert out.startswith("ERR PARSE_ERROR"), out
    assert "Traceback" not in out + err, out + err


def test_call_rejects_a_primitive_verb():
    """`click(x, y)` is the thing this project exists to refuse."""
    code, out, _ = run(["--demo", "call", "click(312,528)"])
    assert code == 1
    assert out.startswith("ERR PARSE_ERROR"), out


# ---------------------------------------------------------------------------
# windows / scenario replay
# ---------------------------------------------------------------------------


def test_windows_lists_the_demo_window():
    code, out, _ = run(["--demo", "windows"])
    assert code == 0
    assert "Restaurant Demo" in out


@pytest.mark.parametrize("screen", ["store", "form", "chat", "console", "tree", "native", "menu", "dialog"])
def test_every_scenario_screen_replays_offline(screen):
    """`--scenario NAME` is the suite's offline entry point; every screen works.

    The list is spelled out rather than read from SCREENS so adding a screen
    without wiring it into the CLI fails here."""
    code, out, _ = run(["--scenario", screen, "observe"])
    assert code == 0, out
    assert f"Scenario Suite - {screen}" in out
    assert out.startswith("@rev"), out


def test_an_unknown_scenario_names_the_choices():
    code, _, err = run(["--scenario", "nope", "observe"])
    assert code == 1
    assert "unknown scenario" in err
    assert "store" in err  # the choices are listed


def test_no_pack_replays_the_scenario_generically():
    code, out, _ = run(["--scenario", "store", "--no-pack", "observe"])
    assert code == 0
    assert "Scenario Suite - store" in out


# ---------------------------------------------------------------------------
# record: the round trip a fixture depends on
# ---------------------------------------------------------------------------


def test_record_writes_a_reloadable_fixture(tmp_path):
    """record -> file -> --fixture is the offline replay path, end to end."""
    fixture = tmp_path / "recorded.json"
    code, out, _ = run(["--scenario", "store", "record", "w1", "-o", str(fixture)])
    assert code == 0, out
    assert fixture.exists(), out
    assert "recorded" in out

    # The recorded file reloads and observes to the same screen.
    code, out, _ = run(["--fixture", str(fixture), "observe"])
    assert code == 0, out
    assert "Scenario Suite - store" in out


def test_a_recorded_fixture_keeps_its_nodes(tmp_path):
    fixture = tmp_path / "recorded.json"
    run(["--scenario", "form", "record", "w1", "-o", str(fixture)])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["window"]["title"] == "Scenario Suite - form"
    assert data["root"]["children"], "the recorded tree must carry its content"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_writes_defaults_when_the_file_is_absent(tmp_path):
    path = tmp_path / "config.toml"
    code, out, _ = run(["--config", str(path), "config"])
    assert code == 0
    assert path.exists()
    assert "wrote" in out


def test_config_prints_only_the_path_with_path_flag(tmp_path):
    path = tmp_path / "config.toml"
    code, out, _ = run(["--config", str(path), "config", "--path"])
    assert code == 0
    assert out.strip() == str(path)


def test_config_persists_the_opt_in_and_it_reaches_the_session(tmp_path):
    """The whole point of the command: a decision that survives the process."""
    path = tmp_path / "config.toml"
    code, _, _ = run(["--config", str(path), "config", "--allow-clipboard"])
    assert code == 0
    assert "allow_clipboard = true" in path.read_text(encoding="utf-8")

    # A later run reads it back (the flag is absent; the file decides).
    from somnus.cli import build_parser, build_session

    args = build_parser().parse_args(
        ["--config", str(path), "--scenario", "chat", "observe"]
    )
    session = build_session(args)
    try:
        assert session.policy.allow_clipboard is True
    finally:
        session.close()


def test_a_flag_still_overrides_the_config_for_one_run(tmp_path):
    path = tmp_path / "config.toml"
    run(["--config", str(path), "config", "--no-allow-clipboard"])

    from somnus.cli import build_parser, build_session

    args = build_parser().parse_args(
        ["--config", str(path), "--allow-clipboard", "--scenario", "chat", "observe"]
    )
    session = build_session(args)
    try:
        assert session.policy.allow_clipboard is True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# parser behaviour
# ---------------------------------------------------------------------------


def test_a_command_is_required():
    code_parser = __import__("somnus.cli", fromlist=["build_parser"]).build_parser()
    with pytest.raises(SystemExit):
        code_parser.parse_args([])


def test_an_unknown_provider_is_rejected():
    parser = __import__("somnus.cli", fromlist=["build_parser"]).build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--provider", "telepathy", "observe"])


# ---------------------------------------------------------------------------
# repl and rpc
# ---------------------------------------------------------------------------


def test_repl_runs_commands_until_quit(monkeypatch):
    """The interactive loop dispatches lines and stops on `quit`."""
    lines = iter(["CALL p1.add()", "quit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    code, out, _ = run(["--demo", "repl"])
    assert code == 0
    assert "WINDOW w1" in out, "the repl opens on the window list"
    assert "OK" in out, out


def test_repl_stops_cleanly_on_eof(monkeypatch):
    """Closing the input (Ctrl-D / EOF) ends the loop without a traceback."""
    def _eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    code, out, err = run(["--demo", "repl"])
    assert code == 0
    assert "Traceback" not in out + err


def test_rpc_serves_line_delimited_requests(monkeypatch):
    """`somnus rpc` reads JSON-RPC from stdin and answers on stdout."""
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    monkeypatch.setattr("sys.stdin", io.StringIO(request + "\n"))
    code, out, _ = run(["--demo", "rpc"])
    assert code == 0
    reply = json.loads(out.strip().splitlines()[0])
    assert [t["name"] for t in reply["result"]["tools"]] == [
        "observe", "find", "inspect", "call"
    ]
