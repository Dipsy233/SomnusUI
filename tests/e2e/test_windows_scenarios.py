"""Windows-only E2E tests against the Scenario Suite WPF app.

These are the tests that drive a *real* desktop through the real UIA provider.
Everything in ``tests/scenarios`` checks the same behaviour against recorded
fixtures and runs anywhere; this file is the proof that the fixtures are
faithful and the runtime works on live windows.

Build and run::

    dotnet build apps/scenario-suite
    pytest -m windows tests/e2e/test_windows_scenarios.py

They are skipped unless the app is built and a desktop session is available.
Each test launches the scenario it needs and terminates it afterwards, so a
scenario is never polluted by another's window.
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

#: How long to wait for the window to appear. WPF's first paint plus UIA's
#: registration is not instant, and a fixed sleep is flaky by construction.
_WINDOW_TIMEOUT = 20.0


def _require_app():
    if not sys.platform.startswith("win"):
        pytest.skip("windows only")
    if not APP.exists():
        pytest.skip(f"scenario suite not built: {APP} (dotnet build apps/scenario-suite)")


#: The observation taken when a session was launched. On a large window the
#: first observe is by far the most expensive step (a UIA walk of every row), so
#: a test that only inspects that first observation reuses it rather than paying
#: for an identical second walk. Keyed by the session object itself (an
#: ``id()`` key can be reused by a later session once the first is collected,
#: which would hand one test another's tree).
_LAUNCH_OBSERVATION: dict[int, object] = {}


def _observe_once(session):
    """Return the launch observation, taking it on first use."""
    key = id(session)
    cached = _LAUNCH_OBSERVATION.get(key)
    if cached is None or cached[0] is not session:
        _LAUNCH_OBSERVATION[key] = (session, session.observe())
    return _LAUNCH_OBSERVATION[key][1]


@pytest.fixture
def scenario():
    """Launch a scenario and return a session bound to it.

    ``scenario("chat")`` starts the chat screen; the fixture finds the window by
    title, so the test never has to know a window id in advance.
    """
    _require_app()
    from somnus.providers.base import get_provider
    from somnus.runtime.session import Session
    from somnus.runtime.policy import Policy

    procs: list[subprocess.Popen] = []
    sessions: list[Session] = []
    created: list[tuple[str, Session, object]] = []

    def start(name: str, **policy_kw) -> tuple[Session, object]:
        proc = subprocess.Popen([str(APP), name])
        procs.append(proc)
        provider = get_provider("uia", allow_list=["scenariosuite.exe"])
        policy = Policy(**policy_kw) if policy_kw else None
        session = Session(provider, policy=policy, allow_packs=False)
        session.refresh_windows()
        deadline = time.monotonic() + _WINDOW_TIMEOUT
        while time.monotonic() < deadline:
            session.refresh_windows()
            if any(f"Scenario Suite - {name}" in (w.title or "") for w in session.windows):
                break
            time.sleep(0.4)
        else:
            raise AssertionError(f"scenario window {name!r} did not appear")
        # Observe once so the session targets it and `session.result` is built,
        # then return.
        _observe_once(session)
        created.append((name, session, proc))
        return session, proc

    try:
        yield start
    finally:
        for _name, session, _proc in created:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - teardown
                pass
            _LAUNCH_OBSERVATION.pop(id(session), None)
        for proc in procs:
            proc.terminate()
        for proc in procs:
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


def _name_of(session, automation_id: str) -> str:
    node = _by_automation(session, automation_id)
    return (node.name or "") if node is not None else ""


def _value_of(session, automation_id: str) -> str:
    node = _by_automation(session, automation_id)
    return (node.value or "") if node is not None else ""


# ---------------------------------------------------------------------------
# store: search + large catalog + cart
# ---------------------------------------------------------------------------


def test_store_search_filters_the_catalog(scenario):
    """The search box is written through its Value pattern and filters the list."""
    session, _ = scenario("store")
    box = _by_automation(session, "store_search")
    assert box is not None, "search box not found in the live tree"
    assert "set_text" in box.cap_names(), "the search box must be writable"

    assert session.dispatch(f'CALL {box.ref}.set_text("Special Item")').startswith("OK")
    button = _by_automation(session, "store_search_button")
    assert session.dispatch(f"CALL {button.ref}.activate()").startswith(("OK", "OK_UNVERIFIED"))

    summary = _name_of(session, "store_result_summary")
    assert "件商品" in summary
    assert summary != "240 件商品", f"filter did not narrow the list: {summary!r}"


def test_store_is_bounded_on_a_live_window(scenario):
    """240 live rows must still produce a small observation."""
    session, _ = scenario("store")
    obs = _observe_once(session)
    assert obs.tokens <= 300, obs.tokens
    assert "240" in obs.text or "count=" in obs.text


def test_store_add_reaches_the_cart(scenario):
    """Find a real row, add it, and see the cart and the record change.

    The row is found by name directly (the live catalog reports all 240 rows
    with `entity=item`), not by searching first: after a search the list is
    rebuilt, and acting on a row from the *rebuilt* list would be testing ref
    migration rather than the add path. Ref migration has its own tests.
    """
    session, _ = scenario("store")
    found = session.dispatch('FIND "Item 7"')
    row = _entity_ref(found)
    assert row, f"row not found:\n{found}"

    out = session.dispatch(f"CALL {row}.add()")
    assert not out.startswith("ERR"), out
    assert _name_of(session, "store_cart_count").startswith("1 items")
    assert "已加入" in _name_of(session, "store_last_action")

# ---------------------------------------------------------------------------
# form: fields, validation, record
# ---------------------------------------------------------------------------


def test_form_readonly_field_is_not_writable(scenario):
    session, _ = scenario("form")
    field = _by_automation(session, "form_id")
    assert field is not None
    assert "set_text" not in field.cap_names()
    assert "readonly" in field.states


def test_form_submit_records(scenario):
    """Fill a real form, submit, and read the record back."""
    session, _ = scenario("form")
    name = _by_automation(session, "form_name")
    assert session.dispatch(f'CALL {name.ref}.set_text("张三")').startswith("OK")
    agree = _by_automation(session, "form_agree")
    assert session.dispatch(f"CALL {agree.ref}.toggle()").startswith(("OK", "OK_UNVERIFIED"))

    out = session.dispatch(f"CALL {_by_automation(session, 'form_submit').ref}.activate()")
    assert not out.startswith("ERR"), out
    assert "张三" in out, "the new record must appear in the delta"
    assert _name_of(session, "form_validation") in ("", "未提交")

# ---------------------------------------------------------------------------
# chat: the self-drawn composer and the send log
# ---------------------------------------------------------------------------


def test_chat_composer_is_an_input_without_a_native_write(scenario):
    """The live RichTextBox is an INPUT and its chain is the fallback chain."""
    session, _ = scenario("chat")
    composer = _by_automation(session, "chat_composer")
    assert composer is not None, "composer not found"
    assert composer.role == "input"
    assert "set_text" in composer.cap_names()
    # It publishes no Value pattern, so the two focus-free routes are gated out
    # and the call must be refused unless a channel is enabled.
    steps = [(s.primitive, s.condition) for s in composer.get_cap("set_text").via]
    assert ("set_text", "has:value") in steps
    assert ("paste_text", "no-hwnd") in steps


def test_chat_send_without_a_channel_is_refused_honestly(scenario):
    """No synthetic channel enabled: a real refusal naming both flags."""
    session, _ = scenario("chat")
    composer = _by_automation(session, "chat_composer")
    out = session.dispatch(f'CALL {composer.ref}.set_text("hello")')
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    assert "--allow-clipboard" in out and "--allow-keyboard" in out


def test_chat_send_with_clipboard_lands_and_is_recorded(scenario):
    """With the clipboard enabled the send really goes out and is logged.

    The whole point of the app: a send produces a message and a log line, both
    readable from the observation, so success is verified rather than assumed.
    """
    session, _ = scenario("chat", allow_clipboard=True, allow_keyboard=True)
    composer = _by_automation(session, "chat_composer")
    out = session.dispatch(f'CALL {composer.ref}.set_text("会议纪要")')
    assert out.startswith("OK"), out

    send = _by_automation(session, "chat_send")
    out = session.dispatch(f"CALL {send.ref}.activate()")
    assert not out.startswith("ERR"), out
    # The record names what was sent.
    assert "会议纪要" in out, out
    log = _by_automation(session, "chat_outbox_list")
    assert log is not None and any("会议纪要" in (c.name or "") for c in log.children)


# ---------------------------------------------------------------------------
# console: the plain text box, written natively
# ---------------------------------------------------------------------------


def test_console_input_uses_the_native_route(scenario):
    """A real WPF TextBox is written through its Value pattern, not the clipboard."""
    session, _ = scenario("console")
    box = _by_automation(session, "console_input")
    assert box is not None and "set_text" in box.cap_names()
    out = session.dispatch(f'CALL {box.ref}.set_text("hello")')
    assert out.startswith("OK"), out
    # The audit log records the primitive that actually ran. The live provider
    # has no `executed` list (that is a mock affordance), so this is the
    # provider-agnostic way to check the route.
    primitives = [entry["primitive"] for entry in session.audit_log()]
    assert primitives == ["set_text"], primitives
    assert "hello" in _value_of(session, "console_input")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entity_ref(find_output: str) -> str | None:
    """The ref of the first entity row in a FIND result."""
    for line in find_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("ITEM", "PRODUCT", "LISTITEM", "GROUP"):
            return parts[1]
    return None
