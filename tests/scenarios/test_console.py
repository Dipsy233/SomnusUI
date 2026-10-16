"""Scenario S4: console — a plain text box, and the routing contrast it provides.

The chat screen proves the fallback chain runs when a control publishes nothing.
This screen proves the opposite half of the same rule: a control that *does*
publish a Value pattern is written through it, and the synthetic channels are
never reached even when they are enabled.

Having both screens is what makes the routing decision observable rather than
assumed: if the chain were "always paste", the chat tests would pass and these
would fail, and vice versa.
"""

from __future__ import annotations


def test_plain_textbox_uses_the_native_route_with_all_channels_enabled(make_session, console_session):
    """Value pattern wins even with clipboard and keyboard authorised."""
    from tests.fixtures.scenarios import console_scripts, console_tree

    session = make_session(
        console_tree(), scripts=console_scripts(), allow_clipboard=True, allow_keyboard=True
    )
    session.observe()
    ref = _ref(session, "console_input")
    out = session.dispatch(f'CALL {ref}.set_text("hello")')
    assert out.startswith("OK"), out
    primitives = [p for _r, p, _v in session.provider.executed]
    assert primitives == ["set_text"], primitives


def test_console_composer_is_a_writable_input(console_session):
    obs = console_session.observe().text
    line = next(l for l in obs.splitlines() if "输入" in l and "INPUT" in l)
    assert "+set_text" in line


def test_console_send_records_the_message(console_session):
    """The send writes into the thread and names itself in the status line."""
    console_session.observe()
    console_session.dispatch(f'CALL {_ref(console_session, "console_input")}.set_text("ping")')
    out = console_session.dispatch(f"CALL {_ref(console_session, 'console_send')}.activate()")
    assert not out.startswith("ERR"), out

    status = _name(console_session, "console_status")
    assert "已发送" in status and "ping" in status
    assert "ping" in _thread(console_session)
    # And the composer is cleared, exactly like the chat screen.
    assert (_field(console_session, "console_input") or "") == ""


def test_contact_rows_are_recognised_as_entities(console_session):
    """A titled row with an action is an item, with no pack.

    This is the generic entity heuristic: "title + verb button" or "three or
    more same-shaped titled siblings". The contacts are both, so they must be
    promoted rather than left as anonymous groups.
    """
    console_session.observe()
    found = console_session.dispatch('FIND "联系人 3"')
    assert found.startswith("FOUND")
    line = next(l for l in found.splitlines() if "联系人 3" in l)
    # Promoted rows carry `open` (the conventional activation).
    assert "+open" in line, line


def test_selecting_a_contact_changes_the_title(console_session):
    """Acting on a row has a visible, checkable effect.

    The assertion is on the *title* (what the application shows the user), not
    on a `selected` flag the mock would have to fake: the app's ListBox answers
    a selection by swapping the visible thread, so that is the effect to check.
    """
    console_session.observe()
    assert _name(console_session, "console_title") == "联系人 1"
    found = console_session.dispatch('FIND "联系人 5"')
    ref = next(
        l.split()[1] for l in found.splitlines()
        if "联系人 5" in l and l.split()[0] in ("ITEM", "LISTITEM")
    )
    out = console_session.dispatch(f"CALL {ref}.select()")
    assert not out.startswith("ERR"), out
    assert _name(console_session, "console_title") == "联系人 5"
    assert "联系人 5" in _thread(console_session)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node {automation_id!r}")


def _field(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.value or ""
    raise AssertionError(f"no field {automation_id!r}")


def _name(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.name or ""
    raise AssertionError(f"no node {automation_id!r}")


def _thread(session) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "console_thread":
            return " ".join(c.name or "" for c in node.children)
    raise AssertionError("thread not found")
