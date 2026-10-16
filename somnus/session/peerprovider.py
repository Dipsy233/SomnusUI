"""The far session, seen as an ordinary accessibility provider.

This is the piece that makes "two sessions, one agent" cheap rather than a
second copy of the runtime. :class:`~somnus.runtime.session.Session` is written
against :class:`~somnus.providers.base.AccessibilityProvider`, so a session on
the other side of the boundary is just another provider implementation:

    {main session}  Session(UiaProvider())      <- local windows
    {main session}  Session(PeerProvider(...))  <- windows in the child session

Switching sessions is therefore swapping the provider, not rebuilding the
runtime. The compiler, projection, registry, resolver, policy and the ref
namespace all stay on this side and behave identically either way - which is
what the LLM sees: one agent, one tool surface, a different set of windows.

What is deliberately *not* here: no caching of snapshots. A cached tree would
be stale in exactly the case that matters (the far session is being driven by a
person at the same time), and a snapshot is one round trip. The cost is a few
milliseconds on a local pipe; the alternative is answering from a tree that no
longer matches the screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawTree, WindowInfo
from somnus.session import wire
from somnus.session.bridge import PeerBridge, PeerUnavailable

log = logging.getLogger("somnus.session.peerprovider")


@dataclass
class PeerProvider:
    """An ``AccessibilityProvider`` backed by the peer in another session.

    Implements the same protocol as :class:`~somnus.providers.windows.uia.UiaProvider`
    by forwarding each call over the pipe. ``execute`` is the load-bearing one:
    a primitive runs *where the element is*, which is the whole point - the
    far session owns the window, so the far session must perform the action.
    """

    #: Which child session to talk to. None = whatever the OS reports as the
    #: current child session, which is what a reconnect gives a new id for.
    session_id: int | None = None
    connect_timeout: float = 5.0
    call_timeout: float = 120.0

    name: str = "peer"

    _bridge: PeerBridge | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> bool:
        """Open the pipe. Idempotent; safe to call before every request."""
        if self._connected and self._bridge is not None:
            return True
        bridge = PeerBridge(
            session_id=self.session_id,
            connect_timeout=self.connect_timeout,
            call_timeout=self.call_timeout,
        )
        if not bridge.connect():
            bridge.close()
            return False
        self._bridge = bridge
        self._connected = True
        return True

    def ensure(self) -> PeerBridge:
        """The live bridge, or raise :class:`PeerUnavailable`.

        Raising rather than returning None is deliberate: every caller here is
        about to use the result, and a provider that silently returns empty
        windows when the peer is gone would look like "the far desktop has no
        windows" - a wrong and confusing answer.
        """
        if self.connect() and self._bridge is not None:
            return self._bridge
        raise PeerUnavailable(
            "no peer is running in the child session; start it there with "
            "`somnus child-session peer` (or register it to start with the "
            "session via `somnus child-session peer-setup`)"
        )

    @property
    def connected(self) -> bool:
        return self._connected

    # -- AccessibilityProvider ----------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        result = self.ensure().request({"method": "provider.list_windows"})
        windows = (result.get("result") or {}).get("windows") or []
        return [wire.window_from_dict(w) for w in windows]

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 20,
    ) -> RawTree:
        payload: dict[str, Any] = {
            "method": "provider.snapshot",
            "params": {"window_id": window_id, "max_depth": max_depth},
        }
        if scope is not None:
            payload["params"]["scope"] = wire.native_ref_to_dict(scope)
        result = self.ensure().request(payload)
        tree = (result.get("result") or {}).get("tree")
        if tree is None:
            error = result.get("error") or {}
            raise PeerUnavailable(f"snapshot failed: {error.get('message')}")
        return wire.raw_tree_from_dict(tree)

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult:
        payload = {
            "method": "provider.execute",
            "params": {
                "ref": wire.native_ref_to_dict(ref),
                "primitive": primitive,
                "value": value,
                "timeout_s": timeout_s,
            },
        }
        result = self.ensure().request(payload)
        data = (result.get("result") or {}).get("exec")
        if data is None:
            error = result.get("error") or {}
            return ExecResult(
                ok=False,
                error="NATIVE_EXECUTION_FAILED",
                message=str(error.get("message")),
            )
        return wire.exec_result_from_dict(data)

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> Any:
        """No event forwarding.

        Event subscriptions are a local optimisation (they let the settler
        stop polling). Pushing them across the pipe would need a second
        channel and a background reader, for a saving that is invisible next
        to the round trip each observation already costs. The session falls
        back to polling, which is what it does for any provider without
        subscriptions.
        """
        from somnus.providers.base import NullSubscription

        return NullSubscription()

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.close()
        self._bridge = None
        self._connected = False


def make_peer_provider(session_id: int | None = None) -> PeerProvider:
    """A provider bound to ``session_id`` (or the current child session)."""
    return PeerProvider(session_id=session_id)
