"""Peer boot: the Run key's entry point logs and retries the startup race."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from somnus.session import peerboot


@dataclass
class FakeResult:
    returncode: int
    stdout: str = ""


@dataclass
class FakeRunner:
    """Scripted subprocess.run stand-in; each entry is (returncode, stdout)."""

    script: list[tuple[int, str]]
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        returncode, stdout = self.script.pop(0)
        return FakeResult(returncode=returncode, stdout=stdout)


def _clock(readings: list[float]):
    """A clock whose readings are consumed in order (boot reads it twice per attempt)."""
    times = iter(readings)
    return lambda: next(times)


def _no_sleep(calls: list[float]):
    return calls.append


def test_quick_exit_is_retried_and_then_serves(tmp_path: Path):
    runner = FakeRunner([
        (1, "Traceback: import refused"),
        (1, "Traceback: import refused"),
        (0, "PEER=ready session=4"),
    ])
    log = tmp_path / "peer-run.log"

    code = peerboot.boot(
        tmp_path,
        log_path=log,
        probe=lambda: False,
        clock=_clock([0, 1, 10, 12, 100, 1099]),
        sleep=_no_sleep([]),
        runner=runner,
    )

    assert code == 0
    assert len(runner.calls) == 3
    text = log.read_text(encoding="utf-8")
    assert "attempt 1/3" in text and "attempt 3/3" in text
    assert "retrying" in text
    assert "PEER=ready" in text


def test_a_peer_that_ran_is_never_restarted(tmp_path: Path):
    # It served for a while and then ended - that is a normal life, not the race.
    runner = FakeRunner([(0, "peer starting")])
    log = tmp_path / "peer-run.log"

    code = peerboot.boot(
        tmp_path,
        log_path=log,
        probe=lambda: False,
        clock=_clock([0, 500]),
        sleep=_no_sleep([]),
        runner=runner,
    )

    assert code == 0
    assert len(runner.calls) == 1
    assert "retrying" not in log.read_text(encoding="utf-8")


def test_attempts_are_exhausted_and_the_code_reported(tmp_path: Path):
    runner = FakeRunner([(1, "boom")] * 3)
    log = tmp_path / "peer-run.log"

    code = peerboot.boot(
        tmp_path,
        log_path=log,
        probe=lambda: False,
        clock=_clock([0, 0.5, 10, 10.5, 20, 20.5]),
        sleep=_no_sleep([]),
        runner=runner,
    )

    assert code == 1
    assert len(runner.calls) == 3
    assert "boom" in log.read_text(encoding="utf-8")


def test_sleep_is_taken_between_attempts(tmp_path: Path):
    runner = FakeRunner([(1, "boom"), (0, "ready")])
    sleeps: list[float] = []

    peerboot.boot(
        tmp_path,
        log_path=tmp_path / "log",
        probe=lambda: False,
        clock=_clock([0, 1, 100, 200]),
        sleep=sleeps.append,
        runner=runner,
    )

    assert sleeps == [peerboot.RETRY_DELAY_S]


def test_an_oversized_log_is_truncated_not_grown(tmp_path: Path):
    log = tmp_path / "peer-run.log"
    log.write_text("x" * (peerboot.MAX_LOG_BYTES + 1), encoding="utf-8")

    peerboot.boot(
        tmp_path,
        log_path=log,
        probe=lambda: False,
        clock=_clock([0, 500]),
        sleep=_no_sleep([]),
        runner=FakeRunner([(0, "ready")]),
    )

    text = log.read_text(encoding="utf-8")
    assert len(text) < peerboot.MAX_LOG_BYTES
    assert "truncated" in text
    assert "ready" in text


def test_command_is_passed_through_to_the_runner(tmp_path: Path):
    runner = FakeRunner([(0, "")])
    peerboot.boot(
        tmp_path,
        log_path=tmp_path / "log",
        probe=lambda: False,
        clock=_clock([0, 100]),
        sleep=_no_sleep([]),
        runner=runner,
    )
    command = runner.calls[0]
    assert "-m" in command and "somnus.cli" in command
    assert str(tmp_path) in command


def test_a_peer_already_listening_ends_the_boot_before_spawning(tmp_path: Path):
    # The Run key can fire twice per logon (a retried logon attempt runs it
    # again); the second chain must not spawn a peer that can only fail
    # against the pipe the first one holds.
    runner = FakeRunner([(0, "should never run")])

    code = peerboot.boot(
        tmp_path,
        log_path=tmp_path / "log",
        probe=lambda: True,
        runner=runner,
    )

    assert code == 0
    assert runner.calls == []
    assert "already listening" in (tmp_path / "log").read_text(encoding="utf-8")


def test_probe_saying_no_does_not_block_the_boot(tmp_path: Path):
    runner = FakeRunner([(0, "ready")])
    code = peerboot.boot(
        tmp_path,
        log_path=tmp_path / "log",
        probe=lambda: False,
        clock=_clock([0, 100]),
        sleep=_no_sleep([]),
        runner=runner,
    )
    assert code == 0 and len(runner.calls) == 1


def test_build_command_targets_the_boot_module():
    """The Run key must start the logging/retrying boot, not a bare peer."""
    import pytest

    if sys.platform != "win32":
        pytest.skip("peerstart writes Windows registry commands")
    from somnus.session import peerstart

    command = peerstart.build_command(python="pythonw.exe", project=r"E:\proj")
    assert "-m somnus.session.peerboot" in command
    assert '"E:\\proj"' in command
    assert "child-session peer" not in command
