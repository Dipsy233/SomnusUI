"""Ending a child session, and cleaning up after one that ended badly.

A child session exists exactly as long as its RDP connection does, so "close the
session" means "stop the loopback". That much is simple. What is not simple is
everything the *previous* attempts leave behind, because those leftovers make the
next attempt fail in a way that looks like something else:

* **The loopback process.** If it is not stopped, the session it holds stays
  alive, and a later start attaches to it instead of making a new one - which
  looks like "the settings did not take effect".
* **The peer inside the session.** It is started by the session's own logon, so
  it dies with the session; but a peer whose pipe is still open makes a stale
  connection look live.
* **The session-arbitration leftover.** A refused logon still makes
  ``WTSGetChildSessionId`` report an id, for a session that is not in the session
  list. Anything that acts on that id gets a confusing answer, so it is cleared
  by resetting the session - which also frees the far-side programs.

The functions here are deliberately separate from the tool layer: closing has to
be callable at process exit and on interrupt, where there is no session object to
call a method on.

Ownership
---------

A session started and ``detach``\ed on purpose is meant to outlive the process
that opened it - that is how the agent parks a desktop and comes back to it. An
automatic cleanup at exit must therefore not kill it. So starting a session
registers it here, detaching it *un*registers it, and :func:`cleanup_owned` at
process exit acts only on what is still registered.
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import threading
import time

from somnus.session import childsession

log = logging.getLogger("somnus.session.lifecycle")

#: The loopback host's process name. It is what holds the child session open.
HOST_PROCESS = "somnus-rdp-host.exe"

#: The peer runs as pythonw (a Run-key entry should not flash a console), but a
#: hand-started one is python.exe; both are matched by the command line.
PEER_MARKER = "child-session peer"


def stop_loopback(timeout: float = 15.0) -> int:
    """Stop every RDP loopback process. Returns how many were stopped.

    Killing the process is what ends the session: the session is a property of
    the connection, so there is nothing else to shut down. No ``logoff`` call is
    used - measured here, ``logoff``/``rwinsta`` can block indefinitely on a
    session whose logon never completed, which is precisely the state a cleanup
    is most likely to be needed in.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    PROCESS_TERMINATE = 0x0001
    stopped = 0
    for pid in _pids_named(HOST_PROCESS):
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            log.debug("cannot open %s (%s)", pid, kernel32.GetLastError())
            continue
        try:
            if kernel32.TerminateProcess(handle, 1):
                stopped += 1
        finally:
            kernel32.CloseHandle(handle)

    # Give the OS a moment to tear the session down before anyone looks.
    if stopped:
        deadline = time.monotonic() + min(timeout, 5.0)
        while time.monotonic() < deadline and _pids_named(HOST_PROCESS):
            time.sleep(0.2)
    return stopped


def stop_peers() -> int:
    """Stop any peer processes that are still running. Returns the count.

    Only needed when a session outlived its loopback (a peer started by hand, for
    instance). A peer started by the Run key exits with its session.
    """
    return sum(1 for pid in _pids_matching_command(PEER_MARKER) if _terminate(pid))


def reset_session(session_id: int, timeout: float = 12.0) -> bool:
    """Destroy a session left over from a failed attempt.

    ``rwinsta`` is run with a hard timeout and its result is not trusted: on a
    session stuck at a logon screen it can block for a long time without
    finishing. That is why this is best-effort - a leftover session is harmless
    once :func:`live_child_session` stops reporting it, and the caller's real
    question is answered by that check, not by this call.
    """
    try:
        result = subprocess.run(
            ["rwinsta", str(session_id)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.debug("rwinsta %s timed out; leaving the session to be reclaimed", session_id)
        return False
    except OSError as exc:
        log.debug("rwinsta %s failed: %s", session_id, exc)
        return False


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

#: Session ids this process started and still owns. Guarded by a lock because
#: the reader thread can touch the state while the main thread starts or
#: detaches a session.
_owned: set[int] = set()
_owned_lock = threading.Lock()
_atexit_hooked = False


def claim(session_id: int | None) -> None:
    """Record that this process owns *session_id* and must clean it up."""
    if session_id is None:
        return
    with _owned_lock:
        _owned.add(session_id)
    _ensure_atexit()


def disown(session_id: int | None) -> None:
    """Stop considering *session_id* this process's to clean up.

    Called when a session is deliberately handed off (``detach``): it was meant
    to outlive this process, so killing it at exit would undo the caller's
    intent - and it is how the agent leaves a desktop running for later.
    """
    if session_id is None:
        return
    with _owned_lock:
        _owned.discard(session_id)


def owned() -> set[int]:
    with _owned_lock:
        return set(_owned)


def _ensure_atexit() -> None:
    """Register the exit hook once, on first claim."""
    global _atexit_hooked
    if _atexit_hooked:
        return
    _atexit_hooked = True
    atexit.register(cleanup_owned)


def cleanup_owned() -> dict[str, object]:
    """Clean up only the sessions this process still owns.

    Registered with :mod:`atexit`, so it runs on a normal return and on an
    unhandled exception. It does **not** run on ``SIGKILL`` - nothing can - which
    is why :func:`cleanup` also exists for the explicit path, and why a stale
    loopback is treated as a recoverable condition rather than an impossible one.

    A detached session is not owned, so this leaves it alone: the whole point of
    detaching is that the desktop keeps running.
    """
    if not owned():
        return {"skipped": "no owned session"}
    report = cleanup()
    with _owned_lock:
        _owned.clear()
    return report


def cleanup(*, reset_leftovers: bool = True) -> dict[str, object]:
    """Stop the loopback, then clear anything a failed attempt left behind.

    Returns a report rather than raising: this runs on the way out of a process,
    where an exception would replace the real error with a teardown error.
    """
    report: dict[str, object] = {}
    report["loopback_stopped"] = stop_loopback()
    report["peers_stopped"] = stop_peers()

    # Any id the arbitration still reports, that is not a usable session, is a
    # leftover: reset it so the next open starts from a clean slate.
    if reset_leftovers:
        hinted = childsession.child_session_id()
        if hinted is not None and childsession.live_child_session() is None:
            report["reset_session"] = hinted
            report["reset_ok"] = reset_session(hinted)
        else:
            report["reset_session"] = None
    return report


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------


def _pids_named(name: str) -> list[int]:
    """Process ids whose image name is *name*."""
    return _pids(name, match_command=False)


def _pids_matching_command(marker: str) -> list[int]:
    """Process ids of python processes whose command line contains *marker*."""
    return _pids(marker, match_command=True)


def _pids(needle: str, *, match_command: bool) -> list[int]:
    """Find pids by image name, or by a marker in the command line.

    Uses CIM through PowerShell rather than psutil: psutil is not a dependency,
    and this only runs during cleanup, where a few hundred milliseconds is
    irrelevant. The marker form is needed because a peer may be python.exe or
    pythonw.exe and is identified only by its arguments.
    """
    if match_command:
        script = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{needle}*' -and "
            f"$_.Name -match 'python' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
    else:
        script = (
            f"Get-Process -Name '{needle.replace('.exe', '')}' "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"
        )
    try:
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("process lookup for %r failed: %s", needle, exc)
        return []
    pids = []
    for line in result.stdout.split():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _terminate(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)
