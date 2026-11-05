"""Peer `process.start` / `process.status` / `process.kill` contract.

The peer is the only process that lives on the child desktop, so it is the only
one that can start a program *there* without privileges. These tests pin the
schema, the two authorization switches and the status semantics; they run a
harmless child interpreter, not a GUI app.
"""

from __future__ import annotations

import sys
import time

from somnus.session.bridge import PeerServer, encode


def _peer(*, allow_spawn: bool = True, allow_processes: tuple[str, ...] = ()) -> PeerServer:
    server = PeerServer()
    server._allow_spawn = allow_spawn
    server._allow_processes = allow_processes
    return server


def _call(server: PeerServer, method: str, params: dict) -> dict:
    return server._handle(encode({"method": method, "params": params}))


def _start(server: PeerServer, command: list[str], tmp_path, cwd=None) -> dict:
    return _call(
        server,
        "process.start",
        {
            "command": command[0],
            "args": command[1:],
            "cwd": str(cwd) if cwd is not None else None,
            "stdout_path": str(tmp_path / "out.txt"),
            "stderr_path": str(tmp_path / "err.txt"),
        },
    )


def _wait_exit(server: PeerServer, pid: int, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _call(server, "process.status", {"pid": pid})["result"]
        if not result["alive"]:
            return result
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} did not exit in {timeout}s")


def test_start_requires_a_non_empty_command(tmp_path):
    response = _call(
        _peer(),
        "process.start",
        {
            "command": "",
            "args": [],
            "cwd": None,
            "stdout_path": str(tmp_path / "out.txt"),
            "stderr_path": str(tmp_path / "err.txt"),
        },
    )
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_start_requires_output_paths(tmp_path):
    response = _call(
        _peer(),
        "process.start",
        {"command": sys.executable, "args": ["-c", "pass"]},
    )
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_start_rejects_a_bad_cwd(tmp_path):
    response = _start(_peer(), [sys.executable, "-c", "pass"], tmp_path, cwd=tmp_path / "nope")
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_start_is_refused_when_the_spawn_switch_is_off(tmp_path):
    response = _start(_peer(allow_spawn=False), [sys.executable, "-c", "pass"], tmp_path)
    assert response["error"]["code"] == "PERMISSION_DENIED"


def test_start_honours_the_process_allow_list(tmp_path):
    response = _start(
        _peer(allow_processes=("notepad.exe",)),
        [sys.executable, "-c", "pass"],
        tmp_path,
    )
    assert response["error"]["code"] == "PERMISSION_DENIED"


def test_start_reports_pid_and_status_reports_the_exit_code(tmp_path):
    server = _peer()
    response = _start(
        server,
        [sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"],
        tmp_path,
    )
    assert "result" in response, response
    pid = response["result"]["pid"]
    assert pid > 0

    final = _wait_exit(server, pid)
    assert final["exit_code"] == 3, final
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "hello"


def test_status_requires_a_positive_pid():
    response = _call(_peer(), "process.status", {"pid": 0})
    assert response["error"]["code"] == "INVALID_ARGUMENT"


def test_kill_ends_a_running_process(tmp_path):
    server = _peer()
    response = _start(
        server,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
    )
    pid = response["result"]["pid"]
    killed = _call(server, "process.kill", {"pid": pid})
    assert killed["result"]["killed"] is True
    final = _wait_exit(server, pid, timeout=5.0)
    assert final["alive"] is False
