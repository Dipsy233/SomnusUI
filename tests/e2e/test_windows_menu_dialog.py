"""Windows E2E for the menu and dialog screens.

These drive real windows, so they check the shapes the mock cannot: a WPF menu
bar entry really exposes ExpandCollapse rather than Invoke, opening it really
realizes its items (and spawns the popup window a platform menu uses), and a
modal confirmation really appears as its own window whose buttons are reachable
after a re-observe.

Skipped unless the app is built and a desktop is available.
"""

from __future__ import annotations

import subprocess
import sys
import time
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
    """Launch a screen by name and return a session bound to it."""
    _require_app()

    procs: list[subprocess.Popen] = []

    def start(name: str):
        from somnus.providers.base import get_provider
        from somnus.runtime.session import Session

        proc = subprocess.Popen([str(APP), name])
        procs.append(proc)
        provider = get_provider("uia", allow_list=["scenariosuite.exe"])
        session = Session(provider, allow_packs=False)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            session.refresh_windows()
            if any(f"Scenario Suite - {name}" in (w.title or "") for w in session.windows):
                break
            time.sleep(0.3)
        else:
            session.close()
            raise AssertionError(f"scenario {name!r} window did not appear")
        session.observe()
        return session

    try:
        yield start
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - teardown
                proc.kill()


def _find(session, automation_id: str):
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node
    return None


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------


def test_a_menu_bar_entry_reports_expand_and_expand_is_what_works(scenario):
    """The real finding: a WPF menu bar entry advertises `activate` but refuses it.

    UIA reports InvokePattern as available on the menu item, yet invoking it
    returns ACTION_NOT_SUPPORTED on a live window - the pattern is present and
    the call is a no-op shell. What actually opens the menu is `expand`, which
    is why the capability must be read from the printed line and why the fix
    below uses it rather than the gesture that only looks available."""
    session = scenario("menu")
    entry = _find(session, "menu_bar_file")
    assert entry is not None, "menu_bar_file not found"
    caps = entry.cap_names()
    assert "expand" in caps, caps

    # `activate` is advertised but refused - the honest state of this control.
    refused = session.dispatch(f"CALL {entry.ref}.activate()")
    assert refused.startswith("ERR ACTION_NOT_SUPPORTED"), refused


def test_opening_a_menu_realizes_its_items(scenario):
    session = scenario("menu")
    entry = _find(session, "menu_bar_file")
    out = session.dispatch(f"CALL {entry.ref}.expand()")
    assert not out.startswith("ERR"), out
    assert "新建" in out and "保存" in out, out


def test_choosing_a_menu_item_records_it(scenario):
    session = scenario("menu")
    entry = _find(session, "menu_bar_file")
    session.dispatch(f"CALL {entry.ref}.expand()")
    session.observe()

    item = _find(session, "menu_item_save")
    assert item is not None, "menu_item_save not found after opening the menu"
    out = session.dispatch(f"CALL {item.ref}.activate()")
    assert not out.startswith("ERR"), out

    session.observe()
    status = _find(session, "menu_status")
    assert status is not None
    assert "保存" in (status.name or ""), status.name


# ---------------------------------------------------------------------------
# dialog
# ---------------------------------------------------------------------------


def test_the_confirm_dialog_appears_as_its_own_window(scenario):
    """A modal is a window, and it is discoverable only after the action.

    The Delete button does not delete; it opens a modal. The dialog's buttons
    are not addressable until the re-observe, which is the loop this checks."""
    session = scenario("dialog")
    assert _find(session, "dialog_confirm") is None

    delete = _find(session, "dialog_delete")
    out = session.dispatch(f"CALL {delete.ref}.activate()")
    assert not out.startswith("ERR"), out

    session.observe()
    assert _find(session, "dialog_confirm") is not None, "the dialog did not appear"
    assert _find(session, "dialog_cancel") is not None


def test_confirming_the_dialog_deletes_the_row(scenario):
    session = scenario("dialog")
    delete = _find(session, "dialog_delete")
    session.dispatch(f"CALL {delete.ref}.activate()")
    session.observe()

    confirm = _find(session, "dialog_confirm")
    out = session.dispatch(f"CALL {confirm.ref}.activate()")
    assert not out.startswith("ERR"), out

    session.observe()
    rows = _find(session, "dialog_rows")
    assert rows is not None
    assert len(rows.children) == 2, [c.name for c in rows.children]
    status = _find(session, "dialog_status")
    assert (status.name or "") == "已删除", status.name


def test_cancelling_the_dialog_changes_nothing(scenario):
    session = scenario("dialog")
    delete = _find(session, "dialog_delete")
    session.dispatch(f"CALL {delete.ref}.activate()")
    session.observe()

    cancel = _find(session, "dialog_cancel")
    out = session.dispatch(f"CALL {cancel.ref}.activate()")
    assert not out.startswith("ERR"), out

    session.observe()
    rows = _find(session, "dialog_rows")
    assert rows is not None
    assert len(rows.children) == 3, "cancel must not delete a row"
    status = _find(session, "dialog_status")
    assert (status.name or "") == "已取消", status.name
