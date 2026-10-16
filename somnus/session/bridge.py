"""Cross-session transport: one agent's hands and eyes, in another session.

Why there has to be a far-side process
--------------------------------------

Verified live, in both directions: window enumeration, the accessibility tree
and synthetic input are all confined to the caller's **session**. A process in
session 1 enumerates sessions 0 and 1 and nothing else; a process in session 2
enumerates sessions 0 and 2. The start menu's own UIA root shows the same. That
is a kernel boundary, not a permission that can be raised.

The supported way to bridge it was investigated and does not work here:
``Windows.UI.UIAutomation.Core.RemoteAutomationClientSession`` constructs and
``Start()``s from the RDP host, but ``ConnectionRequested`` never fires, and the
provider half (``RemoteAutomationServer.ReportSession``) answers "class not
registered" - on both sides, with a live loopback RDP connection. That API is
for Microsoft's own remote-desktop stack, which sets up a private provider
context this program cannot reproduce.

A **named pipe** does cross the boundary - verified with a client in one session
and a server in the other - so the design is:

* the far session runs a **peer**: a thin process that owns a
  :class:`~somnus.runtime.session.Session` and answers tool calls;
* this session runs the **agent** as usual, and a :class:`PeerBridge` that
  forwards ``observe`` / ``find`` / ``inspect`` / ``call`` over the pipe.

The intelligence, the compiler, the projection, the policy and the ref
bookkeeping all stay on this side. The far side only perceives and actuates -
which is what makes it "one agent", rather than two.

The wire format is line-delimited JSON, the same shape as
:mod:`somnus.protocol.jsonrpc`, so a caller can be either the MCP server or the
REPL without the peer knowing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("somnus.session.bridge")

#: Pipe name prefix. One pipe per child session, so two sessions can never
#: answer each other.
PIPE_PREFIX = r"\\.\pipe\somnus-peer"

#: How long a client waits for the peer to exist before giving up. The peer is
#: started by the child session's own logon, so a short wait here is normal.
DEFAULT_CONNECT_TIMEOUT = 5.0

#: A single request's timeout. Long enough for a settle-and-observe cycle;
#: short enough that a wedged peer does not hang the agent forever.
DEFAULT_CALL_TIMEOUT = 120.0


def pipe_name(session_id: int) -> str:
    """The pipe a given child session's peer listens on."""
    return f"{PIPE_PREFIX}-{session_id}"


def pipe_name_for_current_session() -> str:
    """The pipe name for the peer this process should talk to.

    From the *main* session that is the child session's pipe, not this
    session's: the peer lives on the far side, and the two numbers differ. Only
    when this process is itself inside the child session does "this session"
    name the peer correctly - which is the case for the peer process asking for
    its own listen name.
    """
    from somnus.session import childsession

    if childsession.inside_child_session():
        return pipe_name(childsession.current_session_id())
    child = childsession.child_session_id()
    if child is None:
        # No child session exists; fall back to this session's name so the
        # error is a clean "no peer" rather than a wrong pipe.
        return pipe_name(childsession.current_session_id())
    return pipe_name(child)


class PeerUnavailable(RuntimeError):
    """No peer is listening: the far session has no agent running."""


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def encode(request: dict[str, Any]) -> bytes:
    """One request as a length-prefixed JSON line."""
    return (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


# ---------------------------------------------------------------------------
# Client side (this session)
# ---------------------------------------------------------------------------


@dataclass
class PeerBridge:
    """Forward tool calls to the peer in another session.

    One request at a time per connection: the peer holds the only ``Session``,
    and interleaving two ``call``s would race its ref bookkeeping. The lock is
    what makes that explicit rather than accidental.
    """

    session_id: int | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT

    _handle: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _closed: bool = field(default=False, repr=False)

    # -- connection ----------------------------------------------------------

    def _name(self) -> str:
        if self.session_id is not None:
            return pipe_name(self.session_id)
        return pipe_name_for_current_session()

    def connect(self) -> bool:
        """Open the pipe, waiting briefly for the peer to be listening.

        Returns False rather than raising when nobody is there: "the peer is
        not running" is an ordinary state to report, not an exception to
        propagate through the tool layer.
        """
        import pywintypes
        import win32file

        deadline = time.monotonic() + self.connect_timeout
        name = self._name()
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._handle = win32file.CreateFile(
                    name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                log.info("connected to peer on %s", name)
                return True
            except pywintypes.error as exc:
                # 2 = file not found (no peer yet), 231 = all instances busy.
                last = exc
                time.sleep(0.2)
        log.debug("no peer on %s: %s", name, last)
        return False

    @property
    def connected(self) -> bool:
        return self._handle is not None and not self._closed

    def close(self) -> None:
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            import win32file

            win32file.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - teardown
            pass

    # -- calls ---------------------------------------------------------------

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one request and read one response."""
        import pywintypes
        import win32file

        if self._handle is None:
            raise PeerUnavailable("not connected to a peer")
        with self._lock:
            try:
                win32file.WriteFile(self._handle, encode(payload))
                buffer = b""
                deadline = time.monotonic() + self.call_timeout
                while b"\n" not in buffer:
                    if time.monotonic() > deadline:
                        raise PeerUnavailable("peer did not answer in time")
                    _rc, chunk = win32file.ReadFile(self._handle, 65536)
                    if not chunk:
                        break
                    buffer += chunk
            except pywintypes.error as exc:
                raise PeerUnavailable(f"pipe error: {exc}") from exc
        if not buffer:
            raise PeerUnavailable("peer closed the connection")
        return decode(buffer.split(b"\n", 1)[0])

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call one of the four tools on the peer; returns SUI-Text."""
        response = self.request(
            {"method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        )
        if "error" in response:
            return f"ERR {response['error'].get('code')} {response['error'].get('message')}"
        content = (response.get("result") or {}).get("content") or []
        return "".join(part.get("text", "") for part in content)

    def windows(self) -> list[dict[str, Any]]:
        response = self.request({"method": "windows"})
        return (response.get("result") or {}).get("windows", [])


# ---------------------------------------------------------------------------
# Server side (the far session)
# ---------------------------------------------------------------------------


@dataclass
class PeerServer:
    """Serve tool calls from an agent in another session.

    Holds the only :class:`Session`. Everything this process does is perception
    and actuation; no policy, no planning, no model.
    """

    session_id: int | None = None
    #: Settings file to read (None = the platform default).
    config_path: Any = None
    #: Optional explicit pack; None means "generic, plus any App Pack that matches".
    pack_id: str | None = None
    allow_packs: bool = True
    #: Optional explicit policy (tests); otherwise built from the settings file.
    policy: Any = None

    _session: Any = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def _build_session(self):
        """The peer's own session: a real UIA provider over this session's windows.

        Built the same way the local CLI builds one - settings applied, policy
        from the config - so behaviour does not depend on which side of the pipe
        a call arrived from.
        """
        from somnus.config import load as load_settings
        from somnus.providers.base import get_provider
        from somnus.runtime.policy import Policy
        from somnus.runtime.session import Session

        settings = load_settings(self.config_path)
        provider = get_provider("uia", allow_list=list(settings.allow_processes) or None)
        policy = self.policy or Policy(
            allow_processes=settings.allow_processes,
            allow_clipboard=settings.allow_clipboard,
            allow_keyboard=settings.allow_keyboard,
        )
        return Session(
            provider,
            policy=policy,
            pack_id=self.pack_id,
            allow_packs=self.allow_packs,
        )

    def serve_forever(self) -> int:
        """Listen for one connection at a time until stopped."""
        import win32file
        import win32pipe

        from somnus.session import childsession

        self.session_id = self.session_id or childsession.current_session_id()
        name = pipe_name(self.session_id)
        self._session = self._build_session()

        log.info("peer listening on %s (session %s)", name, self.session_id)
        print(f"PEER=ready session={self.session_id} pipe={name}", flush=True)

        try:
            while not self._stop.is_set():
                handle = win32pipe.CreateNamedPipe(
                    name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT,
                    win32pipe.PIPE_UNLIMITED_INSTANCES,
                    65536,
                    65536,
                    0,
                    None,
                )
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                    print("PEER=connected", flush=True)
                    self._serve_connection(handle)
                except Exception as exc:  # noqa: BLE001 - keep serving
                    log.debug("connection ended: %s", exc)
                finally:
                    try:
                        win32file.DisconnectNamedPipe(handle)
                    except Exception:  # noqa: BLE001
                        pass
                    win32file.CloseHandle(handle)
                    print("PEER=disconnected", flush=True)
        finally:
            self.close()
        return 0

    def _serve_connection(self, handle) -> None:
        import pywintypes
        import win32file

        buffer = b""
        while not self._stop.is_set():
            try:
                _rc, chunk = win32file.ReadFile(handle, 65536)
            except pywintypes.error:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                response = self._handle(line)
                win32file.WriteFile(handle, encode(response))

    def _handle(self, line: bytes) -> dict[str, Any]:
        from somnus.protocol.tools import call_tool

        try:
            request = decode(line)
        except Exception as exc:  # noqa: BLE001
            return {"error": {"code": -32700, "message": f"parse error: {exc}"}}
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "tools/call":
                text = call_tool(self._session, params.get("name"), params.get("arguments"))
                return {"result": {"content": [{"type": "text", "text": text}]}}
            if method == "windows":
                self._session.refresh_windows()
                return {
                    "result": {
                        "windows": [
                            {
                                "window_id": w.window_id,
                                "title": w.title,
                                "process": w.process,
                                "active": w.active,
                            }
                            for w in self._session.windows
                        ]
                    }
                }
            if method == "sui":
                return {"result": {"text": self._session.dispatch(params.get("text", ""))}}
            if method == "ping":
                return {"result": {"pong": True}}
            if method.startswith("provider."):
                return self._handle_provider(method, params)
            return {"error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return {"error": {"code": -32000, "message": str(exc)}}

    def _handle_provider(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Serve the provider interface, so the far session looks local.

        The agent on the other side swaps in a ``PeerProvider`` and keeps its
        whole runtime; these three calls are the entire surface that has to
        cross the boundary for that to work.
        """
        from somnus.session import wire

        provider = self._session.provider

        if method == "provider.list_windows":
            windows = provider.list_windows()
            return {"result": {"windows": [wire.window_to_dict(w) for w in windows]}}

        if method == "provider.snapshot":
            scope = params.get("scope")
            tree = provider.snapshot(
                params["window_id"],
                scope=wire.native_ref_from_dict(scope) if scope else None,
                max_depth=int(params.get("max_depth", 20)),
            )
            return {"result": {"tree": wire.raw_tree_to_dict(tree)}}

        if method == "provider.execute":
            result = provider.execute(
                wire.native_ref_from_dict(params["ref"]),
                params["primitive"],
                params.get("value"),
                float(params.get("timeout_s", 3.0)),
            )
            return {"result": {"exec": wire.exec_result_to_dict(result)}}

        return {"error": {"code": -32601, "message": f"unknown provider method {method}"}}

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def agent_should_use_peer(settings) -> bool:
    """Whether this process should drive the far session rather than the local one.

    Only when the user asked for it *and* a peer is actually there. Silently
    falling back to the local session would be worse than refusing: the agent
    would report windows the user never meant it to touch.
    """
    if not getattr(settings, "child_session", False):
        return False
    bridge = PeerBridge()
    try:
        return bridge.connect()
    finally:
        bridge.close()
