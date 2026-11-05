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
def demo_session(wait_for_window):
    """A session whose first enumeration already sees the demo window.

    The wait is on the predicate the test actually needs - an allow-listed
    window that UIA enumerates and Win32 reports visible - not on a Win32 title.
    A title exists while the window is still hidden, which is how a full-suite
    run handed the test an empty desktop ("demo window not found") while the
    process was plainly running. The shared ``wait_for_window`` fails with
    diagnostics instead, so the test never continues against an empty list.
    """
    _windows_only()
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    proc = subprocess.Popen([str(APP)])
    provider = get_provider("uia", allow_list=["restaurantdemo.exe"])
    session = Session(provider, pack_id="restaurant-demo")
    try:
        wait_for_window(
            session,
            process="restaurantdemo.exe",
            timeout=_WINDOW_TIMEOUT,
            proc=proc,
            fail_if_exited=True,
        )
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


def test_uia_observe_and_add(demo_session):
    session = demo_session
    assert session.windows, "demo window not found"

    observe = session.observe().text
    assert "PRODUCT" in observe
    assert "p1" in observe

    result = session.dispatch("CALL p1.add()")
    assert result.startswith("OK"), result
