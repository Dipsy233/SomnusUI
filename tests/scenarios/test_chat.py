"""Scenario S2: chat — a **self-drawn** composer, a history and a send log.

This is the screen the routing chain exists for. The composer is genuinely
self-drawn (the Scenario Suite draws it by hand; QQ/豆包 use Chromium +
ProseMirror): the platform sees a group of pixels with a Text interface for
*reading*, and nothing else. So:

* there is no ValuePattern to set and no element HWND to post to;
* UIA reports it as a ``Group`` with the toolkit's class name, never an `Edit`;
* the runtime must either refuse honestly or use a channel the host enabled.

Every assertion here is about *which route ran*, read from what the provider was
asked to do - not from the final text. A test that only checked "the text
arrived" would pass with the keyboard route silently stealing focus, which is
the exact behaviour the project exists to avoid.
"""

from __future__ import annotations

import pytest

from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from tests.fixtures.scenarios import callbacks, chat_scripts, chat_tree


# ---------------------------------------------------------------------------
# the composer is identifiable and honestly writable
# ---------------------------------------------------------------------------


def test_self_drawn_composer_is_an_input_with_set_text(chat_session):
    """A self-drawn editor must still render as a writable INPUT.

    The platform gives no pattern to recognise it by - it is a painted surface
    with a Text interface - so the class name is the one available signal. This
    is the QQ/豆包 shape: ``group`` + ``ProseMirror ExEditor-...``.
    """
    obs = chat_session.observe().text
    line = next(l for l in obs.splitlines() if "消息编辑器" in l)
    assert line.strip().startswith("INPUT"), line
    assert "+set_text" in line, line


def test_the_composer_publishes_no_native_write_interface(chat_session):
    """Why this screen is hard: the control offers nothing to write through.

    Asserted on the patterns the provider read, because that is what the gate
    decision actually consults. If this ever gained a `value` pattern the whole
    screen would stop testing the fallback chain.
    """
    chat_session.observe()
    node = _composer(chat_session)
    assert "value" not in node.patterns, node.patterns
    source = chat_session.result.sources[node.ref]
    assert source.native.native_window_handle == 0, "a self-drawn control maps no HWND"


def test_an_unknown_class_name_is_not_guessed_to_be_writable(make_session):
    """An editor whose class name is unrecognised is left unwritable.

    The honest half of the rule above: the runtime promotes a control only when
    it has evidence (the toolkit's class name). Faced with a painted surface it
    cannot identify, it says so rather than handing out a `set_text` that would
    fail - or worse, silently type into the wrong thing.
    """
    from tests.fixtures.scenarios import chat_tree

    tree = chat_tree()
    # Rename the composer's class to something no toolkit uses.
    for node in tree.walk():
        if node.native.automation_id == "chat_composer":
            node.native = type(node.native)(
                window_id=node.native.window_id,
                runtime_id=node.native.runtime_id,
                automation_id=node.native.automation_id,
                class_name="SomeCustomSurface",
                path=node.native.path,
            )
    session = make_session(tree)
    session.observe()
    node = _composer(session)
    assert node.role != "input", node.role
    assert "set_text" not in node.cap_names(), node.cap_names()


def test_composer_chain_declares_all_four_routes(chat_session):
    """The chain is complete and ordered by cost to the user."""
    chat_session.observe()
    node = _composer(chat_session)
    steps = [(s.primitive, s.condition) for s in node.get_cap("set_text").via]
    assert steps == [
        ("set_text", "has:value"),
        ("set_text_hwnd", "hwnd"),
        ("paste_text", "no-hwnd"),
        ("type_text", "no-hwnd"),
    ], steps


# ---------------------------------------------------------------------------
# fail loud: neither synthetic channel is on by default
# ---------------------------------------------------------------------------


def test_sending_without_any_channel_is_refused_and_names_both_flags(chat_session):
    """The honest refusal, and it must be actionable.

    A self-drawn editor offers no interface the runtime can use. Saying so - and
    naming the two switches that would change the answer, with each route's own
    reason - is the difference between an agent that can ask for help and one
    that loops.
    """
    chat_session.observe()
    out = chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("hello")')
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    assert "--allow-clipboard" in out
    assert "--allow-keyboard" in out
    # And nothing was typed or pasted: the refusal happened before execution.
    assert [p for _r, p, _v in chat_session.provider.executed] == []


# ---------------------------------------------------------------------------
# the clipboard channel: preferred over the keyboard when both are allowed
# ---------------------------------------------------------------------------


def test_clipboard_is_preferred_over_the_keyboard(chat_session):
    """Both channels enabled → the clipboard runs, typing does not.

    Paste sends one message with the text already on the clipboard, so the cost
    does not grow with length and nothing queues behind the target's message
    pump. Typing does both, so it must stay the last resort.
    """
    _enable(chat_session, allow_clipboard=True, allow_keyboard=True)
    chat_session.observe()
    out = chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("hi")')
    assert out.startswith("OK"), out
    primitives = [p for _r, p, _v in chat_session.provider.executed]
    assert primitives == ["paste_text"], primitives


def test_keyboard_alone_still_reaches_the_composer(chat_session):
    """Enabling only the keyboard works, through the keyboard route."""
    _enable(chat_session, allow_keyboard=True)
    chat_session.observe()
    out = chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("typed")')
    assert out.startswith("OK"), out
    primitives = [p for _r, p, _v in chat_session.provider.executed]
    assert primitives == ["type_text"], primitives


def test_the_written_text_lands_in_the_composer(chat_session):
    """The fallback is not merely *attempted* - the value ends up on the field."""
    _enable(chat_session, allow_clipboard=True)
    chat_session.observe()
    chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("会议纪要")')
    assert _composer(chat_session).value == "会议纪要"


# ---------------------------------------------------------------------------
# send: the action, its record and the message history
# ---------------------------------------------------------------------------


def test_send_appends_to_history_and_the_send_log(chat_session):
    """Send writes the message into the thread *and* a line into the log.

    The log is the verification surface: it is what lets a caller confirm the
    send happened without trusting the click's status word.
    """
    _enable(chat_session, allow_clipboard=True)
    chat_session.observe()
    chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("你好")')
    out = chat_session.dispatch(f'CALL {_send_ref(chat_session)}.activate()')
    assert not out.startswith("ERR"), out

    assert "你好" in _thread_text(chat_session)
    log = _outbox_lines(chat_session)
    assert len(log) == 1
    assert "已发送" in log[0] and "你好" in log[0]


def test_send_reports_the_new_message_and_log_line_as_deltas(chat_session):
    """The response itself shows the message and the log line appearing."""
    _enable(chat_session, allow_clipboard=True)
    chat_session.observe()
    chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("ping")')
    out = chat_session.dispatch(f'CALL {_send_ref(chat_session)}.activate()')
    assert "ping" in out, out
    assert "已发送" in out, out


def test_send_clears_the_composer(chat_session):
    """The composer is empty after sending, so the next send is unambiguous."""
    _enable(chat_session, allow_clipboard=True)
    chat_session.observe()
    chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("one")')
    chat_session.dispatch(f"CALL {_send_ref(chat_session)}.activate()")
    assert (_composer(chat_session).value or "") == ""


def test_two_sends_accumulate_in_the_log(chat_session):
    """Repeated sends are recorded in order, one line each."""
    _enable(chat_session, allow_clipboard=True)
    chat_session.observe()
    for text in ("first", "second"):
        chat_session.dispatch(f'CALL {_composer(chat_session).ref}.set_text("{text}")')
        chat_session.dispatch(f"CALL {_send_ref(chat_session)}.activate()")
    log = _outbox_lines(chat_session)
    assert len(log) == 2
    assert "first" in log[0] and "second" in log[1]


# ---------------------------------------------------------------------------
# the history is a scrollable list and the log is addressable
# ---------------------------------------------------------------------------


def test_history_is_folded_but_searchable(chat_session):
    """A long history collapses in the overview and is reachable by FIND."""
    obs = chat_session.observe().text
    assert "count=8" in obs  # the eight seeded messages
    found = chat_session.dispatch('FIND "历史消息 3"')
    assert found.startswith("FOUND")
    assert "历史消息 3" in found


def test_the_send_log_starts_empty_and_is_visible(chat_session):
    """An empty log is still a node, so 'nothing was sent' is observable."""
    obs = chat_session.observe().text
    assert "发送记录" in obs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _enable(session: Session, **flags) -> None:
    """Rebuild the session with the given channels enabled.

    The policy is fixed at construction, so enabling a channel means a new
    session - which is also how the CLI works (a flag per run).
    """
    tree = session.provider._trees["w1"]
    new = Session(
        MockProvider(
            fixtures=[tree],
            scripts=chat_scripts(),
            callbacks=callbacks(),
        ),
        policy=Policy(**flags),
    )
    session.__dict__.update(new.__dict__)


def _composer(session):
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "chat_composer":
            return node
    raise AssertionError("composer not found")


def _send_ref(session) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "chat_send":
            return node.ref
    raise AssertionError("send button not found")


def _outbox_lines(session) -> list[str]:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "chat_outbox_list":
            return [c.name or "" for c in node.children]
    raise AssertionError("outbox not found")


def _thread_text(session) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "chat_messages":
            return " ".join(c.name or "" for c in node.children)
    raise AssertionError("thread not found")
