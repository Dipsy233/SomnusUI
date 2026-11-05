"""The launch tool: local creation, peer forwarding, bounded window wait."""

from __future__ import annotations

import os
import subprocess

import pytest

from somnus.ir.node import WindowInfo
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


class _Clock:
    """A monotonic clock whose sleep advances it, so a 3s wait runs instantly."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _freeze_clock(monkeypatch) -> _Clock:
    import time as real_time
    from types import SimpleNamespace

    import somnus.runtime.session as session_module

    clock = _Clock()
    monkeypatch.setattr(
        session_module,
        "time",
        SimpleNamespace(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            perf_counter=real_time.perf_counter,
            time=real_time.time,
        ),
    )
    return clock


def _session(policy=None, provider=None):
    return Session(provider or MockProvider(fixtures=[restaurant_demo_tree()]), policy=policy)


def _window(window_id: str, title: str, pid: int = 77) -> WindowInfo:
    return WindowInfo(
        window_id=window_id,
        title=title,
        process="fake.exe",
        pid=pid,
        hwnd=hash(window_id) & 0xFFFF,
    )


def test_main_launch_returns_the_pid_without_observing(monkeypatch, tmp_path):
    monkeypatch.setattr("somnus.runtime.session.subprocess.Popen", _MockPopen)
    _MockPopen.calls.clear()
    session = _session()
    out = session.launch("notepad.exe", args=["one"], cwd=str(tmp_path), wait_s=0)
    assert out == "OK pid=4242\n~ no window in 0s"
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
    out = _session().launch("notepad-missing.exe", wait_s=0)
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
        {"command": "notepad.exe", "wait_s": -1},
        {"command": "notepad.exe", "wait_s": "3"},
        {"command": "notepad.exe", "wait_s": True},
        {"command": "notepad.exe", "wait_s": float("nan")},
        {"command": "notepad.exe", "wait_s": float("inf")},
    ],
)
def test_launch_rejects_bad_arguments(kwargs):
    out = _session().launch(**kwargs)
    assert out.startswith("ERR INVALID_ARGUMENT"), out


class _FakePeer:
    name = "peer"

    def __init__(self, windows=None, adds=None):
        self.calls = []
        self.windows = list(windows or [])
        self.adds = list(adds or [])
        self.sig = ("fake", len(self.windows))

    def list_windows(self):
        return list(self.windows)

    def top_level_signature(self):
        return self.sig

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
        self.windows.extend(self.adds)
        self.sig = ("fake", len(self.windows))
        return 77

    def close(self):
        return None


class _RefusingPeer(_FakePeer):
    def start_process(self, *args, **kwargs):
        raise PeerProcessError("PERMISSION_DENIED", "not allowed")


def test_window_child_and_switch_child_reach_the_same_peer_call(monkeypatch):
    peer = _FakePeer()
    in_child = _session(provider=peer)
    in_child._session_label = "child"
    out_child = in_child.launch("notepad.exe", args=["a"], cwd=".", wait_s=0)
    assert out_child == "OK pid=77\n~ no window in 0s"

    import somnus.session.peerprovider as peer_module

    monkeypatch.setattr(peer_module, "PeerProvider", lambda: _ConnectedPeer(peer))
    from_main = _session()
    out_main = from_main.launch(
        "notepad.exe", args=["a"], cwd=".", window="child", wait_s=0
    )
    assert out_main.startswith("OK pid=77"), out_main
    assert peer.calls[0]["stdout_path"] == os.devnull
    assert "confirmed" not in out_child.lower()


class _ConnectedPeer:
    """A connected peer. The `connect()` gate is what the session calls."""

    def __init__(self, inner: _FakePeer):
        self.inner = inner

    def connect(self):
        return True

    def list_windows(self):
        return self.inner.list_windows()

    def top_level_signature(self):
        return self.inner.top_level_signature()

    def start_process(self, *args, **kwargs):
        return self.inner.start_process(*args, **kwargs)

    def close(self):
        return None


def test_child_launch_passes_the_peer_error_code():
    session = _session(provider=_RefusingPeer())
    session._session_label = "child"
    out = session.launch("calc.exe", window="child")
    assert out.startswith("ERR PERMISSION_DENIED"), out
    assert "not allowed" in out


def test_launch_reports_the_windows_that_appeared(monkeypatch):
    _freeze_clock(monkeypatch)
    peer = _FakePeer(adds=[_window("w8", "Untitled - Notepad")])
    session = _session(provider=peer)
    session._session_label = "child"
    out = session.launch("notepad.exe")
    assert out.startswith("OK pid=77\n+ WINDOW w"), out
    assert "Untitled - Notepad" in out
    assert "no window" not in out


def test_launch_says_so_when_no_window_appears(monkeypatch):
    _freeze_clock(monkeypatch)
    peer = _FakePeer()
    session = _session(provider=peer)
    session._session_label = "child"
    out = session.launch("notepad.exe")
    assert out == "OK pid=77\n~ no window in 3s", out


def test_many_windows_from_one_launch_are_counted(monkeypatch):
    _freeze_clock(monkeypatch)
    adds = [_window(f"w{n}", f"Doc {n} - Notepad") for n in range(2, 10)]
    peer = _FakePeer(adds=adds)
    session = _session(provider=peer)
    session._session_label = "child"
    out = session.launch("notepad.exe")
    assert out.startswith("OK pid=77")
    assert out.count("+ WINDOW") == 6, out
    assert "+ 2 more" in out
    assert "~ 8 windows from one launch" in out


def test_launch_on_child_from_main_counts_windows_without_refs(monkeypatch):
    _freeze_clock(monkeypatch)
    peer = _FakePeer(adds=[_window("w8", "Calculator"), _window("w9", "Settings")])
    import somnus.session.peerprovider as peer_module

    monkeypatch.setattr(peer_module, "PeerProvider", lambda: _ConnectedPeer(peer))
    out = _session().launch("calc.exe", window="child")
    assert out.startswith("OK pid=77"), out
    assert '+ WINDOW "Calculator"' in out
    assert "~ 2 windows from one launch" in out
    assert "~ on child; switch child to address them" in out


def test_the_tool_passes_wait_s_to_the_session(monkeypatch):
    _freeze_clock(monkeypatch)
    peer = _FakePeer()
    session = _session(provider=peer)
    session._session_label = "child"

    from somnus.protocol.tools import call_tool

    out = call_tool(
        session,
        "launch",
        {"command": "notepad.exe", "wait_s": 0},
    )

    assert out == "OK pid=77\n~ no window in 0s", out
