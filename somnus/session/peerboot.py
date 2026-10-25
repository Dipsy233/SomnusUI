"""Boot the child-session peer with a log and a bounded retry.

Why this exists
---------------

The Run key fires during the earliest phase of the child session's logon, and
the peer it starts runs under ``pythonw`` - no console, nowhere for a traceback
to go. Measured live: on a cold start the peer sometimes exits within its first
seconds (an import-time race with the logon still settling), silently, and the
agent side then waits out its whole 60 s connect timeout for a process that is
already gone. Because nothing was written down, every occurrence cost a
debugging session.

This module is therefore the Run key's entry point instead of the peer command
directly. It appends everything the peer prints - and the fact that it ran at
all - to a small log file, and when a peer exits *quickly* it treats that as
the startup race and retries. A peer that ran for a while was really serving
and ended normally, so it is never restarted.

Deliberately portable (no ``winreg``, no provider imports): the retry logic is
what the unit tests exercise, on any platform.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

#: A peer that exits within this window is treated as the startup race.
#: A real peer session lasts minutes; an import-time refusal dies in one.
QUICK_EXIT_S = 20.0
ATTEMPTS = 3
RETRY_DELAY_S = 5.0
#: Keep the log bounded: it is diagnostic output, not a record.
MAX_LOG_BYTES = 256 * 1024


def default_log_path() -> Path:
    """Where the boot log lives (next to the settings file's directory)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "somnusui" / "peer-run.log"


def peer_command(project: Path) -> list[str]:
    """The peer command this module supervises."""
    return [
        sys.executable,
        "-m",
        "somnus.cli",
        "child-session",
        "peer",
        "--project",
        str(project),
    ]


def _peer_already_listening() -> bool | None:
    """Whether this session's peer pipe already has a server.

    The Run key can fire more than once per logon (a logon attempt that fails
    and is retried runs it again), and a second boot chain whose peer cannot
    take the already-held pipe just burns retries against the first one. When
    a server answers, this boot has nothing to do. ``None`` = cannot tell
    (not Windows, or the session id was unavailable) - the caller proceeds.

    Self-contained on kernel32 so the module stays importable (and testable)
    off Windows.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sid = wintypes.DWORD()
        if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return None
        handle = k32.CreateFileW(
            f"\\\\.\\pipe\\somnus-peer-{sid.value}",
            0x80000000,  # GENERIC_READ - a client connect is the whole probe
            0,
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
    except Exception:  # noqa: BLE001 - the check must never block the boot
        return None
    if handle not in (0, -1):
        k32.CloseHandle(handle)
        return True
    # ERROR_FILE_NOT_FOUND (2): no server. ERROR_PIPE_BUSY (231): a server
    # exists and is serving someone else - which still means "one is running".
    return k32.GetLastError() not in (2,)


def boot(
    project: Path,
    *,
    log_path: Path | None = None,
    attempts: int = ATTEMPTS,
    quick_exit_s: float = QUICK_EXIT_S,
    retry_delay_s: float = RETRY_DELAY_S,
    clock=time.monotonic,
    sleep=time.sleep,
    runner=None,
    probe=None,
) -> int:
    """Run the peer with retries; return the last exit code.

    A peer that exits within ``quick_exit_s`` is retried up to ``attempts``
    times - the early-logon race, which a later attempt does not hit. A peer
    that ran longer was really serving and simply ended, so it is left alone.

    ``runner`` is a :func:`subprocess.run` stand-in for tests; it must accept
    ``(command, stdout=..., stderr=..., text=..., errors=...)`` and return an
    object with ``returncode`` and ``stdout``. ``clock``, ``sleep`` and
    ``probe`` (the already-listening check) are injectable for the same reason.
    """
    runner = runner or subprocess.run
    probe = probe or _peer_already_listening
    log_path = log_path or default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.write_text("(previous log truncated)\n", encoding="utf-8")

    command = peer_command(project)
    last_code = -1
    for attempt in range(1, attempts + 1):
        # Re-checked per attempt: a first boot chain may take the pipe while
        # this one is sleeping off a retry. Serving nothing is the right answer
        # when someone else already is.
        listening = probe()
        if listening:
            with open(log_path, "ab", buffering=0) as log:
                log.write("=== peer already listening; nothing to do ===\n".encode())
            return 0
        # Unbuffered: the whole point of the log is to answer "did the Run key
        # fire" *while* the peer is running, and a buffered header would sit in
        # memory until the peer exited - exactly when it is no longer needed.
        with open(log_path, "ab", buffering=0) as log:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"=== peer boot {stamp} attempt {attempt}/{attempts} ===\n".encode())
            started = clock()
            result = runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            elapsed = clock() - started
            if result.stdout:
                log.write(result.stdout.encode("utf-8", errors="replace"))
            last_code = result.returncode
            if result.returncode != 0:
                log.write(f"--- attempt {attempt} exited {result.returncode} after {elapsed:.1f}s ---\n".encode())
            else:
                log.write(f"--- attempt {attempt} ended after {elapsed:.1f}s ---\n".encode())
        # A quick non-zero exit is the race: the logon was still settling and
        # something at import time refused. A long run (any exit code) was a
        # real peer session ending, which a retry cannot help.
        if elapsed >= quick_exit_s or attempt == attempts:
            break
        if result.returncode != 0:
            with open(log_path, "ab", buffering=0) as log:
                log.write(
                    f"--- quick exit, retrying in {retry_delay_s:.0f}s ---\n".encode()
                )
            sleep(retry_delay_s)
    return last_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="child-session peer boot wrapper: log + bounded retry"
    )
    parser.add_argument(
        "--project",
        default=str(Path(__file__).resolve().parents[2]),
        help="project root to run the peer from (the Run key passes this)",
    )
    parser.add_argument("--log", default=None, help="override the log file location")
    args = parser.parse_args(argv)
    return boot(Path(args.project), log_path=Path(args.log) if args.log else None)


if __name__ == "__main__":
    raise SystemExit(main())
