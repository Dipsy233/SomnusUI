"""SWITCH must either move the desktop completely or not at all.

A failed switch that half-rebuilt the provider/registry would leave refs from
one desktop resolving against another. These tests pin the two states: failure
leaves the desktop, provider and ref map untouched; success replaces all three.
"""

from __future__ import annotations

from somnus.ir.node import WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.session import childsession  # noqa: F401 - patched below
from tests.fixtures.builders import restaurant_demo_tree


class _Peer:
    """A minimal connected peer: enough for a switch and its window list."""

    def __init__(self, windows=None, *, connect=True, fail_list=False):
        self.windows = list(windows or [])
        self._connect = connect
        self.fail_list = fail_list
        self.closed = False

    def connect(self) -> bool:
        return self._connect

    def list_windows(self):
        if self.fail_list:
            raise RuntimeError("desktop read failed")
        return list(self.windows)

    def top_level_signature(self):
        return ("peer", len(self.windows))

    def close(self):
        self.closed = True


def _window(window_id: str, title: str) -> WindowInfo:
    return WindowInfo(
        window_id=window_id,
        title=title,
        process="Peer.exe",
        pid=88,
        hwnd=0x88,
    )


def _main_session():
    return Session(MockProvider(fixtures=[restaurant_demo_tree()]))


def _observe_a_ref(session) -> str:
    session.observe(windows_only=True)
    session.refresh_windows()
    window_ref = session._window_refs_by_id[session.target_window]
    session.observe(window_ref)
    assert session.result is not None
    ref = session.result.root.children[0].ref
    return ref


def test_a_failed_connect_keeps_the_desktop_and_every_ref(monkeypatch):
    monkeypatch.setattr("somnus.session.childsession.inside_child_session", lambda: False)
    monkeypatch.setattr("somnus.session.childsession.child_session_id", lambda: 41)
    session = _main_session()
    ref = _observe_a_ref(session)
    refs_before = dict(session._window_refs)
    provider_before = session.provider
    peer = _Peer(connect=False)

    out = session.switch_session("child", peer_factory=lambda: peer)

    assert out.startswith("ERR PERMISSION_DENIED"), out
    assert "still on main" in out, out
    assert session.desktop == "main"
    assert session.provider is provider_before
    assert session._window_refs == refs_before
    assert session.result is not None and session.result.find(ref) is not None
    assert peer.closed


def test_a_failed_window_read_does_not_half_swap(monkeypatch):
    monkeypatch.setattr("somnus.session.childsession.inside_child_session", lambda: False)
    session = _main_session()
    ref = _observe_a_ref(session)
    refs_before = dict(session._window_refs)
    provider_before = session.provider
    peer = _Peer([_window("w1", "Peer Window")], fail_list=True)

    out = session.switch_session("child", peer_factory=lambda: peer)

    assert out.startswith("ERR NATIVE_EXECUTION_FAILED"), out
    assert "still on main" in out, out
    assert session.desktop == "main"
    assert session.provider is provider_before
    assert session._window_refs == refs_before
    assert session.result is not None and session.result.find(ref) is not None
    assert peer.closed


def test_a_bad_target_keeps_the_desktop(monkeypatch):
    monkeypatch.setattr("somnus.session.childsession.inside_child_session", lambda: False)
    session = _main_session()
    ref = _observe_a_ref(session)
    refs_before = dict(session._window_refs)

    out = session.switch_session("moon")

    assert out.startswith("ERR INVALID_ARGUMENT"), out
    assert "still on main" in out, out
    assert session.desktop == "main"
    assert session._window_refs == refs_before
    assert session.result is not None and session.result.find(ref) is not None


def test_a_successful_switch_drops_the_old_refs(monkeypatch):
    monkeypatch.setattr("somnus.session.childsession.inside_child_session", lambda: False)
    session = _main_session()
    ref = _observe_a_ref(session)
    old_records = dict(session.registry.records)

    out = session.switch_session("child", peer_factory=lambda: _Peer([_window("w1", "Peer")]))

    assert out.startswith("OK session=child"), out
    assert session.desktop == "child"
    assert session.result is None
    assert session.registry.records == {}
    assert old_records and all(
        session.registry.records.get(name) is None for name in old_records
    )
    assert session._window_refs.get(ref) is None


def test_switching_to_the_desktop_you_are_on_is_a_noop(monkeypatch):
    monkeypatch.setattr("somnus.session.childsession.inside_child_session", lambda: False)
    session = _main_session()
    session._session_label = "child"
    ref = _observe_a_ref(session)
    refs_before = dict(session._window_refs)

    out = session.switch_session("child")

    assert out == "OK session=child (already here)", out
    assert session.desktop == "child"
    assert session._window_refs == refs_before
    assert session.result is not None and session.result.find(ref) is not None


def test_inside_the_child_asking_for_child_is_a_noop():
    session = _main_session()
    ref = _observe_a_ref(session)
    refs_before = dict(session._window_refs)

    with _inside_child(True):
        out = session.switch_session("child")

    assert out == "OK session=child (already here)", out
    assert session.desktop == "child"
    assert session._window_refs == refs_before
    assert session.result is not None and session.result.find(ref) is not None


def test_inside_the_child_main_is_refused_and_reported():
    session = _main_session()

    with _inside_child(True):
        out = session.switch_session("main")

    assert out.startswith("ERR PERMISSION_DENIED"), out
    assert "still on child" in out, out


class _inside_child:
    """Patch `inside_child_session` for the duration of one switch."""

    def __init__(self, value: bool):
        self.value = value

    def __enter__(self):
        from somnus.session import childsession

        self._module = childsession
        self._original = childsession.inside_child_session
        childsession.inside_child_session = lambda: self.value

    def __exit__(self, *exc):
        self._module.inside_child_session = self._original
        return False
