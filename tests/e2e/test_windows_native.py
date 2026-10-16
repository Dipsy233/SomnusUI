"""Windows E2E for the native screen: real Win32 controls in a real window.

The mock scenario (``tests/scenarios/test_native.py``) checks the shapes; this
checks they are faithful - that a WinForms-hosted control really reports an
HWND and a Value pattern on a live window, and that a write through the Value
route lands.

Skipped unless the app is built and a desktop is available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows

ROOT = Path(__file__).resolve().parents[2]
APP = (
    ROOT
    / "apps"
    / "scenario-suite"
    / "bin"
    / "Debug"
    / "net8.0-windows"
    / "ScenarioSuite.exe"
)


def _require_app():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")
    if not APP.exists():
        pytest.skip(f"scenario suite not built: {APP} (dotnet build apps/scenario-suite)")


@pytest.fixture
def scenario():
    """Launch the native screen and return a session bound to it."""
    _require_app()
    import subprocess
    import time

    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    proc = subprocess.Popen([str(APP), "native"])
    provider = get_provider("uia", allow_list=["scenariosuite.exe"])
    session = Session(provider, allow_packs=False)
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            session.refresh_windows()
            if any("Scenario Suite - native" in (w.title or "") for w in session.windows):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("native window did not appear")
        session.observe()
        yield session
    finally:
        session.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()


def _by_automation(session, automation_id: str):
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node
    return None


def test_native_edit_maps_a_real_hwnd(scenario):
    """The hosted Win32 EDIT really carries a handle - the premise of the screen."""
    node = _by_automation(scenario, "native_edit")
    assert node is not None, "native_edit not found"
    source = scenario.result.sources[node.ref]
    assert source.native.native_window_handle != 0, vars(source.native)


def test_native_edit_selects_the_value_write_route(scenario):
    """A native EDIT publishes a Value pattern, so that route is what runs."""
    node = _by_automation(scenario, "native_edit")
    assert node is not None
    steps = [(s.primitive, s.condition) for s in node.get_cap("set_text").via]
    assert steps[0] == ("set_text", "has:value"), steps


def test_writing_the_native_edit_lands_and_is_read_back(scenario):
    """The write really reaches the control, read back through UIA.

    The control publishes a Value pattern, so the value it holds is the direct
    confirmation - no status word to trust.
    """
    node = _by_automation(scenario, "native_edit")
    assert node is not None
    out = scenario.dispatch(f'CALL {node.ref}.set_text("hello-native")')
    assert not out.startswith("ERR"), out

    scenario.observe()
    node = _by_automation(scenario, "native_edit")
    assert node is not None
    assert (node.value or "") == "hello-native", node.value

    # The screen's own record of what it holds agrees, so a write is visible
    # from the observation without reading the control directly.
    status = _by_automation(scenario, "native_status")
    assert status is not None
    assert "hello-native" in (status.name or ""), status.name


def test_the_readonly_native_control_is_reported_readonly(scenario):
    node = _by_automation(scenario, "native_readonly")
    assert node is not None
    assert "readonly" in node.states
    assert "set_text" not in node.cap_names(), node.cap_names()
