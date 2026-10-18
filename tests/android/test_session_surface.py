"""Session-level Android surface: SYSTEM / APP dispatch (doc §6.1)."""

from __future__ import annotations

from somnus.providers.android.mock import AndroidMockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from tests.fixtures import android_scenarios as A


def make_session(screen: str = "settings") -> Session:
    provider = AndroidMockProvider(snapshots=[A.SCREENS[screen]()])
    provider.launchable = [
        {"package": "com.android.settings", "label": "设置"},
        {"package": "com.example.chat", "label": "聊天"},
    ]
    return Session(provider, policy=Policy())


def test_system_back_discards_refs():
    session = make_session()
    session.observe()
    out = session.dispatch("SYSTEM back")
    assert out.startswith("OK system=back"), out
    assert "refs discarded" in out
    # The registry is fresh: an old ref is refused, not silently rebound.
    out2 = session.dispatch("CALL q1.focus()")
    assert out2.startswith("ERR"), out2


def test_system_on_windows_provider_is_refused_cleanly():
    """A provider without global actions gets a precise error, not a crash."""
    from somnus.providers.mock import MockProvider
    from tests.fixtures.builders import notepad_tree

    session = Session(MockProvider(fixtures=[notepad_tree()]))
    session.observe()
    out = session.dispatch("SYSTEM back")
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out


def test_app_open_by_label():
    session = make_session()
    session.observe()
    out = session.dispatch('APP launch "设置"')
    assert out.startswith("OK app=com.android.settings"), out


def test_app_open_by_package():
    session = make_session()
    session.observe()
    out = session.dispatch('APP launch "com.example.chat"')
    assert out.startswith("OK app=com.example.chat"), out


def test_app_open_unknown_is_app_not_found():
    session = make_session()
    session.observe()
    out = session.dispatch('APP launch "不存在的应用"')
    assert out.startswith("ERR APP_NOT_FOUND"), out


def test_app_search_lists_matches():
    session = make_session()
    session.observe()
    out = session.dispatch('APP search "聊天"')
    assert out.startswith("FOUND 1"), out
    assert "com.example.chat" in out


def test_set_text_then_send_on_chat_screen():
    """The full loop on the routing-hard screen: write, then send.

    The composer has no SET_TEXT action, but it IS editable-shaped (its role
    normalized to input), so the mock's default set_text route lands through
    it. The delta proves the write happened (doc §7.2: the route is decided
    at execution time and verified by the tree, not by the return code).
    """
    session = make_session("chat")
    session.observe()
    composer = next(
        n for n in session.result.root.walk()
        if "编辑器" in (n.name or "")
    )
    send = next(n for n in session.result.root.walk() if (n.name or "") == "发送")
    out = session.dispatch(f'CALL {composer.ref}.set_text("会议纪要")')
    assert out.startswith("OK"), out
    assert 'text="会议纪要"' in out  # the echo proves what landed
    out = session.dispatch(f"CALL {send.ref}.activate()")
    assert out.startswith("OK"), out
    # The sent message appears in the message list.
    found = session.dispatch('FIND "会议纪要"')
    assert found.startswith("FOUND"), found
