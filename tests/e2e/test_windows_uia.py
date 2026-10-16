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


@pytest.fixture(scope="module")
def demo_app():
    _windows_only()
    proc = subprocess.Popen([str(APP)])
    # Poll for the window instead of a fixed sleep: the app is usually ready in
    # well under a second, and a fixed wait is pure added test time.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _demo_window_present():
            break
        time.sleep(0.2)
    try:
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()


def _demo_window_present() -> bool:
    """True when the demo's main window is up, without paying for a full tree."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 2)
        if "restaurant" in buf.value.lower():
            found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return bool(found)


def test_uia_observe_and_add(demo_app):
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    provider = get_provider("uia", allow_list=["restaurantdemo.exe"])
    session = Session(provider, pack_id="restaurant-demo")
    session.refresh_windows()
    assert session.windows, "demo window not found"

    observe = session.observe().text
    assert "PRODUCT" in observe
    assert "p1" in observe

    result = session.dispatch("CALL p1.add()")
    assert result.startswith("OK"), result
    session.close()
