"""Fixtures shared by the live-desktop E2E tests.

A launched application creates its HWND before it is shown and before UIA
publishes it. Measured on this machine: the Win32 title existed while
``IsWindowVisible`` was still false, and ``list_windows`` returned nothing for
another 0.2-0.3s in 5 of 12 launches. A fixed sleep either wastes time or races;
the shared wait below polls the predicate the tests actually use - an
allow-listed window that is enumerated *and* visible.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

import pytest


def _window_is_visible(hwnd: int) -> bool:
    """Whether Win32 reports this window as visible.

    ``hwnd == 0`` is a UIA-listed element with no top-level handle (an inner
    UWP ``CoreWindow``); there is no Win32 flag to consult and UIA has already
    dropped offscreen nodes, so the enumeration is the evidence.
    """
    if not hwnd:
        return True
    import ctypes

    return bool(ctypes.WinDLL("user32").IsWindowVisible(hwnd))


def _win32_titled_windows() -> list[tuple[int, str, bool, int, str]]:
    """Every titled top-level window: hwnd, title, visible, pid, process."""
    import ctypes
    from ctypes import wintypes

    from somnus.providers.windows.uia import _process_name

    user32 = ctypes.WinDLL("user32")
    found: list[tuple[int, str, bool, int, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 2)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        found.append(
            (
                int(hwnd),
                buf.value,
                bool(user32.IsWindowVisible(hwnd)),
                int(pid.value),
                _process_name(int(pid.value)),
            )
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _diagnose(
    session,
    proc: subprocess.Popen | None,
    process: str | None,
    title: str | None,
    timeout: float,
    polls: int,
) -> str:
    """What the desktop looked like when the wait gave up."""
    windows = _win32_titled_windows()
    allow_list = list(getattr(session.provider, "allow_list", None) or [])
    targets = {p.casefold() for p in allow_list}
    if process:
        targets.add(process.casefold())
    matches = [
        w
        for w in windows
        if w[4].casefold() in targets or (title is not None and title in w[1])
    ]
    uia_windows = [
        (w.window_id, w.title, w.process, w.hwnd, _window_is_visible(w.hwnd))
        for w in session.windows
    ]
    pid = proc.pid if proc is not None else None
    returncode = proc.poll() if proc is not None else None
    return (
        f"no allow-listed visible window after {timeout:.1f}s ({polls} polls): "
        f"process={process!r} title={title!r}; pid={pid} returncode={returncode}; "
        f"uia_windows={uia_windows}; win32_titled={len(windows)}; "
        f"allow_list_matches={matches[:5]}; allow_list={allow_list}"
    )


def _wait_for_window(
    session,
    *,
    process: str | None = None,
    title: str | None = None,
    timeout: float = 20.0,
    proc: subprocess.Popen | None = None,
    poll_interval: float = 0.2,
    fail_if_exited: bool = False,
) -> Any:
    """Poll until an allow-listed window is enumerable and Win32-visible.

    The predicate is the one the test needs: the process is on the provider's
    allow-list, the title contains *title* when given, and ``IsWindowVisible``
    says the window is really on screen. ``refresh_windows`` already applies the
    allow-list; it is re-checked here because ``peek`` can momentarily replace
    an entry with metadata for a window outside it.

    A process that exited before producing a window cannot recover, so callers
    waiting on the application itself pass ``fail_if_exited=True``. A launcher
    such as ``calc.exe`` exits after activating its UWP app, so its caller
    leaves that off and waits for the child process.
    """
    targets = {process.casefold()} if process else set()
    allow_list = {
        p.casefold() for p in (getattr(session.provider, "allow_list", None) or [])
    }
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        if fail_if_exited and proc is not None and proc.poll() is not None:
            pytest.fail(_diagnose(session, proc, process, title, timeout, polls))
        session.refresh_windows()
        polls += 1
        for window in session.windows:
            if process is not None and window.process.casefold() not in targets:
                continue
            if title is not None and title not in (window.title or ""):
                continue
            if allow_list and window.process.casefold() not in allow_list:
                continue
            if not _window_is_visible(window.hwnd):
                continue
            return window
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(_diagnose(session, proc, process, title, timeout, polls))
        # Poll interval, not a readiness delay: the next refresh is the check.
        time.sleep(min(poll_interval, remaining))


@pytest.fixture(scope="session")
def wait_for_window():
    """The shared wait, as a fixture so module-scoped fixtures can use it."""
    return _wait_for_window
