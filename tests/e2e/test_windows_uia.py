"""Windows-only E2E tests against the WPF Restaurant Demo.

These require a real desktop session and the built demo app::

    dotnet build apps/restaurant-demo
    pytest -m windows

They are skipped everywhere else. They are the only tests that exercise the
real UIA provider end to end.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "restaurant-demo" / "bin" / "Debug" / "net8.0-windows" / "RestaurantDemo.exe"


def _windows_only():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")
    if not APP.exists():
        pytest.skip(f"demo app not built: {APP}")


#: How long the demo window may take to become *enumerable*. The app creates
#: its HWND before it is shown and before UIA publishes it: measured across 12
#: launches, the Win32 title existed at ~0.3s while ``IsWindowVisible`` was
#: still false, and UIA listed nothing for another 0.2-0.3s in 5 of them.
_WINDOW_TIMEOUT = 15.0


@pytest.fixture(scope="module")
def demo_session():
    """A session whose first enumeration already sees the demo window.

    The wait is on the predicate the test actually needs - a window matching the
    allow-list in the *UIA* tree - not on a Win32 title. A title exists while the
    window is still hidden, which is how a full-suite run handed the test an
    empty desktop ("demo window not found") while the process was plainly
    running. A timeout fails here, with diagnostics, instead of letting the test
    continue against an empty list.
    """
    _windows_only()
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    proc = subprocess.Popen([str(APP)])
    provider = get_provider("uia", allow_list=["restaurantdemo.exe"])
    session = Session(provider, pack_id="restaurant-demo")
    try:
        deadline = time.monotonic() + _WINDOW_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(
                    f"demo app exited before its window appeared "
                    f"(pid={proc.pid} returncode={proc.poll()})"
                )
            session.refresh_windows()
            if session.windows:
                break
            time.sleep(0.2)
        else:
            pytest.fail(_diagnose_demo_window(proc, session))
        yield session
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - teardown
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()


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


def _diagnose_demo_window(proc, session) -> str:
    """What the desktop looked like when the wait gave up."""
    windows = _win32_titled_windows()
    matching = [w for w in windows if w[4].lower() == "restaurantdemo.exe"]
    return (
        f"no demo window in the UIA list after {_WINDOW_TIMEOUT:.0f}s: "
        f"process pid={proc.pid} returncode={proc.poll()}; "
        f"uia_windows={[(w.window_id, w.title, w.process) for w in session.windows]}; "
        f"win32_titled={len(windows)}; "
        f"allow_list_matches={matching[:5]}; "
        f"allow_list={getattr(session.provider, 'allow_list', None)}"
    )


def test_uia_observe_and_add(demo_session):
    session = demo_session
    assert session.windows, "demo window not found"

    observe = session.observe().text
    assert "PRODUCT" in observe
    assert "p1" in observe

    result = session.dispatch("CALL p1.add()")
    assert result.startswith("OK"), result
