"""The launch tool: local creation, peer forwarding and its error paths."""

from __future__ import annotations

import os
import subprocess

import pytest

from somnus.ir.action import ExecResult
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from somnus.session.peerprovider import PeerProcessError
from tests.fixtures.builders import restaurant_demo_tree


class _MockPopen:
    calls = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        _MockPopen.calls.append(self)


def _session(policy=None, provider=None):
    return Session(provider or MockProvider(fixtures=[restaurant_demo_tree()]), policy=policy)


def test_main_launch_returns_the_pid_without_observing(monkeypatch, tmp_path):
    monkeypatch.setattr("somnus.runtime.session.subprocess.Popen", _MockPopen)
    _MockPopen.calls.clear()
    session = _session()
    out = session.launch("notepad.exe", args=["one"], cwd=str(tmp_path))
    assert out == "OK pid=4242\n~ observe windows to see it"
    assert "confirmed" not in out.lower()
    call = _MockPopen.calls[-1]
    assert call.argv == ["notepad.exe", "one"]
    assert call.kwargs["cwd"] == str(tmp_path)
    assert call.kwargs["close_fds"] is True
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["stdout"] == subprocess.DEVNULL
    assert call.kwargs["stderr"] == subprocess.DEVNULL
    session.close()


def test_main_launch_reports_a_missing_executable(monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("notepad-missing.exe")

    monkeypatch.setattr("somnus.runtime.session.subprocess.Popen", _missing)
    out = _session().launch("notepad-missing.exe")
    assert out.startswith("ERR NATIVE_EXECUTION_FAILED"), out


def test_main_launch_honours_allow_processes(monkeypatch):
    def _unexpected(*args, **kwargs):
        raise AssertionError("Popen must not run when the policy refuses")

    monkeypatch.setattr("somnus.runtime.session.subprocess.Popen", _unexpected)
    session = _session(policy=Policy(allow_processes=("notepad.exe",)))
    out = session.launch("calc.exe")
    assert out.startswith("ERR PERMISSION_DENIED"), out


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command": ""},
        {"command": "notepad.exe", "args": "one"},
        {"command": "notepad.exe", "args": [1]},
        {"command": "notepad.exe", "cwd": "no-such-dir"},
        {"command": "notepad.exe", "window": "wat"},
    ],
)
def test_launch_rejects_bad_arguments(kwargs):
    out = _session().launch(**kwargs)
    assert out.startswith("ERR INVALID_ARGUMENT"), out


class _FakePeer:
    name = "peer"

    def __init__(self):
        self.calls = []

    def start_process(self, command, args=None, *, cwd=None, stdout_path, stderr_path):
        self.calls.append(
            {
                "command": command,
                "args": list(args or []),
                "cwd": cwd,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
            }
        )
        return 77

    def close(self):
        return None


class _RefusingPeer(_FakePeer):
    def start_process(self, *args, **kwargs):
        raise PeerProcessError("PERMISSION_DENIED", "not allowed")


def test_window_child_and_switch_child_reach_the_same_peer_call():
    in_child = _session(provider=_FakePeer())
    in_child._session_label = "child"
    out_child = in_child.launch("notepad.exe", args=["a"], cwd=".")
    assert out_child == "OK pid=77\n~ observe windows to see it"

    from_main = _session(provider=_FakePeer())
    out_main = from_main.launch("notepad.exe", args=["a"], cwd=".", window="child")
    assert out_main == out_child
    assert in_child.provider.calls == from_main.provider.calls
    assert in_child.provider.calls[0]["stdout_path"] == os.devnull
    assert "confirmed" not in out_child.lower()


def test_child_launch_passes_the_peer_error_code():
    out = _session(provider=_RefusingPeer()).launch("calc.exe", window="child")
    assert out.startswith("ERR PERMISSION_DENIED"), out
    assert "not allowed" in out
