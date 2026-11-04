"""Real-window checks for FIND's wait and the desktop summary.

The menu screen is the shape the wait exists for: its items are only realized
once the menu is opened, so an agent that searches immediately misses them and
has to come back. Here the menu is opened *outside* the settled call path (a
direct provider gesture), which is exactly the "it appeared a moment later"
case a wait has to catch.

The desktop summary is checked on two live windows of the acceptance app: the
observation must name both without walking either one's tree.
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
_WINDOW_TIMEOUT = 20.0


def _require_app():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")
    if not APP.exists():
        pytest.skip(f"scenario suite not built: {APP}")


def _session_for(proc, title_part: str):
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session

    provider = get_provider("uia", allow_list=["scenariosuite.exe"])
    session = Session(provider, allow_packs=False)
    deadline = time.monotonic() + _WINDOW_TIMEOUT
    while time.monotonic() < deadline:
        session.refresh_windows()
        if any(title_part in (w.title or "") for w in session.windows):
            break
        time.sleep(0.4)
    else:
        session.close()
        raise AssertionError(f"window {title_part!r} did not appear")
    session.observe()
    return session


def _by_automation(session, automation_id: str):
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node
    return None


def test_find_waits_for_menu_items_realized_after_the_search_starts():
    _require_app()
    proc = subprocess.Popen([str(APP), "menu"])
    session = None
    try:
        session = _session_for(proc, "Scenario Suite - menu")
        assert _by_automation(session, "menu_item_new") is None, (
            "menu items must not be in the tree before the menu opens"
        )
        entry = _by_automation(session, "menu_bar_file")
        source = session.result.sources[entry.ref]

        # Open the menu outside the settled action path: the items are realized
        # while the search is already running.
        result = session.provider.execute(source.native, "expand")
        assert result.ok, result

        started = time.monotonic()
        out = session.find("新建", timeout_s=1.0)
        elapsed = time.monotonic() - started
        # The realized menu can appear twice (the window's control view and the
        # popup that hosts it), so the wait is checked by "matched", not by a
        # fixed hit count.
        assert out.startswith("FOUND ") and '"新建"' in out, out
        assert elapsed < 1.0, f"the hit should arrive before the deadline: {elapsed:.2f}s"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()


def test_find_waits_about_as_long_as_its_deadline():
    _require_app()
    proc = subprocess.Popen([str(APP), "menu"])
    session = None
    try:
        session = _session_for(proc, "Scenario Suite - menu")
        started = time.monotonic()
        out = session.find("这个目标永远不存在", timeout_s=1.0)
        elapsed = time.monotonic() - started
        assert out.startswith("ERR TARGET_NOT_FOUND after="), out
        assert "hint=observe" in out
        # One poll may overshoot the deadline by its own read (~70ms here).
        assert 0.9 <= elapsed <= 1.6, f"deadline overshoot too large: {elapsed:.2f}s"
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - teardown
            proc.kill()


def test_the_desktop_summary_names_every_live_window():
    _require_app()
    import json

    procs = [
        subprocess.Popen([str(APP), "store"]),
        subprocess.Popen([str(APP), "form"]),
    ]
    session = None
    try:
        from somnus.providers.base import get_provider
        from somnus.runtime.session import Session

        provider = get_provider("uia", allow_list=["scenariosuite.exe"])
        session = Session(provider, allow_packs=False)
        deadline = time.monotonic() + _WINDOW_TIMEOUT
        wanted = ("Scenario Suite - store", "Scenario Suite - form")
        while time.monotonic() < deadline:
            session.refresh_windows()
            if all(any(t in (w.title or "") for w in session.windows) for t in wanted):
                break
            time.sleep(0.4)
        else:
            raise AssertionError(f"both windows did not appear: {[w.title for w in session.windows]}")

        obs = session.observe()
        line = next(
            (ln for ln in obs.text.splitlines() if ln.startswith("WINDOWS ")), None
        )
        assert line is not None, obs.text
        assert "Scenario Suite - store" in line, line
        assert "Scenario Suite - form" in line, line
        # ops needs a shallow read per window (~111ms for five windows): the
        # structure deliberately leaves it out.
        assert "ops=" not in line, line
        print(json.dumps(line, ensure_ascii=False))
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - teardown
                proc.kill()
