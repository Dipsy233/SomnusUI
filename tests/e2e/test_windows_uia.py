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
    time.sleep(3.0)
    try:
        yield proc
    finally:
        proc.terminate()


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
