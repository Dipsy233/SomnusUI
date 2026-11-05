"""Peer viewport capture: forwarding, metadata, fallback and find escalation."""

from __future__ import annotations

import time

from somnus.ir.action import Action
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.runtime.session import Session
from somnus.session import bridge, wire
from somnus.session.peerprovider import PeerProvider
from somnus.session.bridge import PeerServer


def _node(path, role, name=None, *, children=None, patterns=frozenset()):
    return RawNode(
        native=NativeRef("w1", path=tuple(path), class_name="stub"),
        role=role,
        name=name,
        patterns=frozenset(patterns),
        children=children or [],
    )


def _tree(*, late: bool) -> RawTree:
    children = [
        _node((0,), "input", "Text Editor", patterns={"value"}),
    ]
    if late:
        children.append(_node((1,), "button", "Late", patterns={"invoke"}))
    root = _node((), "pane", "Demo", children=children)
    return RawTree(
        window=WindowInfo("w1", "Demo", "demo.exe", 1, True),
        root=root,
    )


class _Provider:
    """A far-side provider; subclasses optionally add snapshot_viewport."""

    name = "stub"
    last_viewport_partial = False
    viewport_meta: dict = {}

    def list_windows(self):
        return [WindowInfo("w1", "Demo", "demo.exe", 1, True)]

    def peek(self, window_id):
        return WindowInfo("w1", "Demo", "demo.exe", 1, True)

    def snapshot(self, window_id, scope=None, max_depth=20):
        return _tree(late=True)

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        from somnus.ir.action import ExecResult

        return ExecResult(ok=True)


class _ViewportProvider(_Provider):
    def snapshot_viewport(self, window_id, max_depth=20, expanded_paths=None):
        self.last_viewport_partial = True
        self.viewport_meta = {
            "w1": {
                (0,): {
                    "items_total": 10,
                    "items_visible": 3,
                    "items_truncated": False,
                    "scroll_pct": 25,
                }
            }
        }
        return _tree(late=False)


class _FakeBridge:
    def __init__(self, server):
        self.server = server
        self.calls = []

    def request(self, payload):
        self.calls.append(payload["method"])
        return self.server._handle(bridge.encode(payload))

    def close(self):
        return None


def _client(server):
    provider = PeerProvider()
    fake = _FakeBridge(server)
    provider._bridge = fake
    provider._connected = True
    session = Session(provider)
    session.target_window = "w1"
    return session, provider, fake


def _server(provider):
    server = PeerServer()
    server._session = Session(provider)
    return server


def test_peer_viewport_meta_reaches_the_session_and_enables_small_window_logic():
    far = _ViewportProvider()
    session, provider, fake = _client(_server(far))
    out = session.observe()
    assert "INPUT q1" in out.text
    node = session.result.find("q1")
    assert node.attrs["items_total"] == 10
    assert node.attrs["items_visible"] == 3
    assert node.attrs["items_truncated"] is False
    assert node.attrs["scroll_pct"] == 25
    # A partial, untruncated, small capture opens the same adaptation branch
    # the local provider uses.
    assert session._small_observe_window == "w1"
    assert session._viewport_limited is True
    assert provider.viewport_meta["w1"][(0,)]["items_visible"] == 3
    # The next observe knows this window fits whole and takes the full read.
    before_full = fake.calls.count("provider.snapshot")
    session.observe()
    assert fake.calls.count("provider.snapshot") == before_full + 1
    session.close()


def test_peer_without_viewport_falls_back_once_with_a_warning():
    far = _Provider()  # no snapshot_viewport
    session, provider, fake = _client(_server(far))
    out = session.observe()
    assert out.text.count("peer has no viewport capture; full snapshot budget applies") == 1
    assert provider.viewport_fallback_note
    assert provider.last_viewport_partial is False
    assert session._watch_natives == []
    assert "provider.snapshot" in fake.calls
    session.close()


def test_viewport_meta_survives_the_wire():
    meta = {
        "w1": {
            (1, 2): {
                "items_total": 240,
                "items_visible": 12,
                "items_truncated": True,
                "scroll_pct": 5,
            }
        }
    }
    encoded = wire.viewport_meta_to_dict(meta)
    assert encoded["w1"][0]["native_path"] == [1, 2]
    decoded = wire.viewport_meta_from_dict(encoded)
    assert decoded["w1"][(1, 2)]["items_visible"] == 12
    assert decoded["w1"][(1, 2)]["items_truncated"] is True
    assert "native_path" not in decoded["w1"][(1, 2)]


def test_find_escalates_to_one_full_read_through_the_peer():
    class _LateProvider(_ViewportProvider):
        def snapshot(self, window_id, scope=None, max_depth=20):
            return _tree(late=True)

        def snapshot_viewport(self, window_id, max_depth=20, expanded_paths=None):
            self.last_viewport_partial = True
            self.viewport_meta = {}
            return _tree(late=False)

    far = _LateProvider()
    session, provider, fake = _client(_server(far))
    session.observe()
    started = time.monotonic()
    out = session.find(Action(kind="find", query="Late"), timeout_s=1.5)
    elapsed = time.monotonic() - started
    assert "Late" in out, out
    assert "(full read)" in out, out
    assert fake.calls.count("provider.snapshot") >= 1
    assert fake.calls.count("snapshot.viewport") >= 3
    assert elapsed < 3.0
    session.close()
