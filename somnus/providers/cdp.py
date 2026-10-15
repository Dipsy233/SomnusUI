"""Chrome DevTools Protocol: writing into self-drawn web UI.

Some applications draw their own text box: QQ, VS Code and Steam are Electron
apps whose input is a ``contenteditable`` element rendered by Chromium. To the
accessibility layer that element is a plain group with a text interface - it
exposes **no** Value pattern (Chromium implements none for ``contenteditable``),
and its ``LegacyIAccessible`` is a shell: UIA reports the pattern available and
the methods return ``E_NOTIMPL``. There is therefore no native pattern that can
put text into it, and the only remaining native route is synthesising
keystrokes, which is exactly what this project is built to avoid (measured: a
fast ``SendInput`` burst is dropped by the target's message pump, so a
10-character write lands as a correct 4-7 character prefix).

The web platform has its own write interface, and it is reached through the same
door a developer uses: the DevTools Protocol. ``Input.insertText`` is the
browser's own text-insertion command - the pipeline a keystroke or an IME
composition travels - so an editor's model updates exactly as it would for real
typing. This is the mechanism Playwright uses for its Chromium backend (see its
``injectedScript.ts``: focus, select the content, then insert), and it is the
web counterpart of ``ValuePattern.SetValue``: a programmatic write, not
synthetic input. No pointer moves, no keystroke is posted to a window, and the
window does not need to be foreground.

This module is deliberately **not** a whole provider. It is a write bridge that
a provider consults for the primitives the accessibility layer cannot perform,
so observation keeps coming from one source (UIA) and the two never disagree
about which window or element a ref names. It is used only when a Chromium app
is genuinely reachable on a loopback debug port - which most are not, and the
absence is not an error.

Transport and discovery deliberately mirror the deleted v0.3 provider: the tree
and the actions come from the browser's own protocol, never from a screenshot or
from scraping rendered HTML.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("somnus.cdp")

#: Ports a Chromium app is conventionally started with. A debug port is opt-in
#: per application launch, so a miss here is the normal case, not a failure.
DEFAULT_PORTS: tuple[int, ...] = (9222, 9223, 9224)

#: How long to wait for a debug endpoint before deciding the app is not
#: debuggable. Kept *very* short: this runs on the path of every text write, and
#: a closed port costs the full timeout because the failure is a refusal or a
#: silent drop. Deliberately below any human-visible threshold, because the
#: normal case is "this application is not debuggable" and that must be cheap.
_PROBE_TIMEOUT = 0.15

#: Environment variable naming extra ports to probe, comma separated. This is
#: how an application launched on a non-default debug port is reached without
#: changing code, and how the tests point at their own browser instance.
PORTS_ENV = "SOMNUS_CDP_PORTS"


def configured_ports() -> tuple[int, ...]:
    """Ports to probe: the defaults plus anything the environment names.

    A caller may also pass explicit ports to :class:`CdpBridge`; this covers the
    case of wanting an extra port for everything, without threading a parameter
    through the provider.
    """
    extra: list[int] = []
    for chunk in (os.environ.get(PORTS_ENV) or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            port = int(chunk)
        except ValueError:
            continue
        if port not in DEFAULT_PORTS and port not in extra:
            extra.append(port)
    return DEFAULT_PORTS + tuple(extra)


def _http_json(port: int, path: str, timeout: float = _PROBE_TIMEOUT) -> Any:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout
        ) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _devtools_active_port_files() -> list[Path]:
    """``DevToolsActivePort`` files for typical Chromium profiles.

    An app started with ``--remote-debugging-port=0`` lets the browser pick a
    free port and records it here, so attaching does not depend on the user
    having chosen a number.
    """
    roots = [
        Path(os.environ.get("TEMP", "") or ""),
        Path(os.environ.get("TMP", "") or ""),
        Path(os.environ.get("LOCALAPPDATA", "") or "") / "Temp",
        Path("C:/Temp"),
        Path(os.environ.get("LOCALAPPDATA", "") or "") / "Google/Chrome/User Data",
        Path(os.environ.get("LOCALAPPDATA", "") or "") / "Microsoft/Edge/User Data",
    ]
    out: list[Path] = []
    for root in roots:
        if not root or not str(root) or not root.exists():
            continue
        direct = root / "DevToolsActivePort"
        if direct.is_file():
            out.append(direct)
        try:
            out.extend(root.glob("*/DevToolsActivePort"))
        except OSError:
            continue
    return out


def candidate_ports(ports: Iterable[int] = DEFAULT_PORTS) -> list[int]:
    """Configured ports plus any found in a ``DevToolsActivePort`` file."""
    found = list(ports)
    for path in _devtools_active_port_files():
        try:
            port = int(path.read_text(encoding="utf-8").splitlines()[0].strip())
        except (OSError, ValueError, IndexError):
            continue
        if port not in found:
            found.append(port)
    return found


@dataclass(frozen=True)
class CdpTarget:
    """One debuggable page."""

    target_id: str
    title: str
    url: str
    ws_url: str
    port: int
    browser: str = "chromium"


def discover_targets(ports: Iterable[int] = DEFAULT_PORTS) -> list[CdpTarget]:
    """List debuggable pages on the loopback endpoints.

    Only ``127.0.0.1`` is contacted: a browser debug port must never be reached
    over the network. A debug port is an explicit opt-in by whoever launched the
    app, so finding nothing is the expected outcome for most applications.
    """
    out: list[CdpTarget] = []
    for port in candidate_ports(ports):
        # One request decides the port. The endpoint answers both paths or
        # neither, so probing `/json/list` first means a closed port costs a
        # single timeout instead of two - and this runs on every text write to
        # an application that is, in the common case, not debuggable at all.
        items = _http_json(port, "/json/list")
        if not items:
            continue
        version = _http_json(port, "/json/version")
        browser = "chromium"
        if version:
            browser = str(version.get("Browser", "chromium")).split("/")[0]
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            ws = item.get("webSocketDebuggerUrl")
            if not ws:
                continue
            out.append(
                CdpTarget(
                    target_id=str(item.get("id", "")),
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    ws_url=str(ws),
                    port=port,
                    browser=browser,
                )
            )
    return out


class CdpError(RuntimeError):
    """A DevTools protocol error."""


@dataclass
class CdpConnection:
    """A synchronous DevTools websocket session for one page."""

    ws_url: str
    _sock: Any = None
    _next_id: int = 0
    session_id: str | None = None
    _closed: bool = False

    def connect(self, timeout: float = 5.0) -> None:
        try:
            import websocket  # websocket-client, the [cdp] extra
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise CdpError(
                "the CDP channel needs the 'websocket-client' package "
                "(pip install 'somnusui[cdp]')"
            ) from exc
        # Chrome rejects a websocket whose Origin it does not recognise (403).
        # A DevTools client is not a web page, so the header is suppressed
        # rather than requiring --remote-allow-origins at launch.
        self._sock = websocket.create_connection(
            self.ws_url, timeout=timeout, suppress_origin=True
        )
        reply = self.send(
            "Target.attachToTarget",
            {"targetId": _target_from_ws(self.ws_url), "flatten": True},
        )
        self.session_id = reply.get("sessionId")

    def send(
        self, method: str, params: dict | None = None, timeout: float = 10.0
    ) -> dict:
        """Send one command and return its result (raising on protocol error)."""
        if self._sock is None:
            raise CdpError("not connected")
        self._next_id += 1
        message: dict[str, Any] = {
            "id": self._next_id,
            "method": method,
            "params": params or {},
        }
        if self.session_id:
            message["sessionId"] = self.session_id
        self._sock.send(json.dumps(message))
        self._sock.settimeout(timeout)
        while True:
            raw = self._sock.recv()
            if not raw:
                raise CdpError("connection closed")
            reply = json.loads(raw)
            if reply.get("id") != message["id"]:
                continue  # an event; not part of this request/response pair
            if "error" in reply:
                raise CdpError(str(reply["error"].get("message", "cdp error")))
            return reply.get("result", {})

    def close(self) -> None:
        self._closed = True
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
        self._sock = None


def _target_from_ws(ws_url: str) -> str:
    # ws://127.0.0.1:9222/devtools/page/<ID>
    return ws_url.rstrip("/").rsplit("/", 1)[-1]


#: Engine family -> names that identify it, on either side of the comparison.
#:
#: A process name (``msedge.exe``) and a browser product string (``Edg``) never
#: match textually, so both sides are reduced to a family first. Only Chromium
#: engines can be driven this way, which is why the table is short and closed:
#: an unknown process yields no family and therefore no write channel.
_ENGINE_ALIASES: dict[str, tuple[str, ...]] = {
    "chromium": (
        "chrome",
        "chromium",
        "google chrome",
        "msedge",
        "edge",
        "microsoft edge",
        "edg",
        "brave",
        "vivaldi",
        "opera",
        "electron",
    ),
}


def _engine_family(name: str) -> str | None:
    """The engine family a process name or browser product string belongs to.

    Matching is on the product word, not a substring of the whole string, so
    ``msedge`` and ``Edg`` both resolve to ``chromium`` while an application
    that merely happens to contain the letters (``edgeviewer``) does not - the
    distinction matters because a wrong match would write into the wrong
    application.
    """
    text = (name or "").strip().lower()
    if not text:
        return None
    if text.endswith(".exe"):
        text = text[:-4]
    # Compare the leading product word: "msedge.exe" -> "msedge",
    # "Edg/155.0.4283.45" -> "edg", "QQ" -> "qq".
    head = text.split()[0].split("/")[0] if text else ""
    for family, aliases in _ENGINE_ALIASES.items():
        if head in aliases:
            return family
    return None


# ---------------------------------------------------------------------------
# AX tree helpers
# ---------------------------------------------------------------------------


def _ax_prop(node: dict, name: str) -> Any:
    """One property's value out of a CDP AXNode."""
    for prop in node.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name") == name:
            value = prop.get("value", {})
            return value.get("value") if isinstance(value, dict) else value
    return None


def _ax_str(node: dict, key: str) -> str:
    field_value = node.get(key)
    if isinstance(field_value, dict):
        v = field_value.get("value")
        return v if isinstance(v, str) else ("" if v is None else str(v))
    return ""


def _ax_is_editable(node: dict) -> bool:
    """Whether an AXNode is a text field this channel may write into.

    ``editable`` is the browser's own answer and the primary signal. A
    ``textbox`` role counts too: it is what the browser calls an editable
    region (including ``contenteditable``) even when the editable property is
    not spelled out.
    """
    if _ax_prop(node, "editable") in (True, "true", "richtext", "plaintext"):
        return True
    role = _ax_str(node, "role").lower()
    return role in ("textbox", "searchbox")


def _ax_match_score(node: dict, hint_class: str, hint_name: str) -> int:
    """How well an AXNode matches the element the caller asked about.

    Higher is better. The class hint is weighted most because UIA and Chromium
    report the same ``class`` string, then the accessible name, then the shape
    of an editor (focused / multiline).
    """
    score = 0
    ax_class = str(_ax_prop(node, "className") or "")
    if hint_class and ax_class:
        wanted = hint_class.split()[0].lower()
        if wanted and wanted in ax_class.lower():
            score += 100
    ax_name = _ax_str(node, "name")
    if hint_name and ax_name and hint_name.strip() == ax_name.strip():
        score += 20
    if _ax_prop(node, "focused") in (True, "true"):
        score += 5
    if _ax_prop(node, "multiline") in (True, "true"):
        score += 2
    return score


# ---------------------------------------------------------------------------
# DOM actions
# ---------------------------------------------------------------------------

#: The web counterpart of a native pattern: a call that runs *inside the page*
#: against the element the accessibility tree named, so the application's own
#: handlers run. Never coordinates, never synthetic keystrokes.
_ACTION_SCRIPT: dict[str, str] = {
    "activate": "function(){ this.click(); return true; }",
    "select": "function(){ this.click(); if (this.focus) this.focus(); return true; }",
    "focus": "function(){ this.focus(); return true; }",
    "toggle": "function(){ this.click(); return true; }",
    "expand": "function(){ this.click(); return true; }",
    "collapse": "function(){ this.click(); return true; }",
    "read": "function(){ return true; }",
    "scroll_into_view": (
        "function(){ this.scrollIntoView({block:'center'}); return true; }"
    ),
}

#: Prepare an element to receive text, run *in the page* before ``insert_text``.
#:
#: This mirrors Playwright's ``selectText`` (its ``injectedScript.ts``): focus
#: the element and select what it already holds, so the text that follows
#: replaces it instead of appending. A ``contenteditable`` needs a DOM Range
#: rather than ``.select()`` because it has no selection API of its own, and
#: the editor frameworks track the selection, not a value property.
#:
#: Returns the element kind so the caller can tell an editable field from a
#: shape it must refuse.
_PREPARE_SCRIPT = """
function(){
  var el = this;
  var tag = (el.nodeName || '').toLowerCase();
  if (el.focus) el.focus();
  if (tag === 'input' || tag === 'textarea') {
    try { el.select(); } catch (e) { /* some inputs refuse selection */ }
    return 'field';
  }
  if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
    // An editor only becomes the active editing host after the previous one
    // blurs, which matters when focus moves between two editors in one page.
    var active = el.ownerDocument.activeElement;
    if (active && active !== el && active.blur) active.blur();
    el.focus();
    var range = el.ownerDocument.createRange();
    range.selectNodeContents(el);
    var sel = el.ownerDocument.defaultView.getSelection();
    if (sel) { sel.removeAllRanges(); sel.addRange(range); }
    return 'editable';
  }
  return 'unsupported';
}
"""


#: The shapes that need a property assignment rather than an insert. A
#: ``<select>`` takes a value; nothing else is written at all, because a blind
#: write into an arbitrary element would corrupt an application's DOM.
_FALLBACK_SCRIPT = """
function(v){
  var el = this;
  var tag = (el.nodeName || '').toLowerCase();
  if (tag === 'select') {
    el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  }
  return false;
}
"""

#: Primitives the bridge can perform. ``set_text`` is listed because it is a
#: protocol-level operation (see :meth:`CdpBridge._set_text`), not a page call.
SUPPORTED_PRIMITIVES: frozenset[str] = frozenset(_ACTION_SCRIPT) | {"set_text"}


@dataclass
class CdpBridge:
    """Per-application CDP write channel, discovered lazily and cached.

    The bridge is *found*, never assumed: a Chromium application is only
    debuggable when it was started with a debug port, so :meth:`for_process`
    returns ``None`` for the ordinary case and the caller falls back to its own
    route. Nothing about observation depends on this class.
    """

    #: Extra ports to probe beyond the defaults, which the environment may
    #: extend (``SOMNUS_CDP_PORTS``).
    ports: tuple[int, ...] = ()
    #: process name (lowercased) -> targets seen on a probe. Cached so a
    #: non-debuggable application is not re-probed on every call.
    _probed: dict[str, list[CdpTarget]] = field(default_factory=dict)
    _connections: dict[str, CdpConnection] = field(default_factory=dict)

    def discover(self, *, refresh: bool = False) -> list[CdpTarget]:
        if refresh or not self._probed:
            ports = self.ports or configured_ports()
            self._probed["__all__"] = discover_targets(ports)
        return self._probed.get("__all__", [])

    def target_for_process(self, process: str) -> CdpTarget | None:
        """A debuggable page belonging to ``process``, or None.

        Matching is by process name, because that is what a provider knows about
        a window. A debug endpoint that belongs to a *different* application is
        deliberately not used: writing into another app's page would be a
        cross-application surprise.
        """
        wanted = (process or "").lower()
        if not wanted:
            return None
        targets = self._probed.get(wanted)
        if targets is None:
            targets = self._probe_process(wanted)
            self._probed[wanted] = targets
        return targets[0] if targets else None

    def _probe_process(self, process: str) -> list[CdpTarget]:
        """Targets that belong to ``process``.

        A target does not name its hosting application: ``/json/version``
        reports the *browser engine* (``Edg``, ``Chrome``, or the Electron
        app's own product name), while the caller asks about the process a UIA
        window belongs to (``msedge.exe``, ``QQ.exe``). The two are tied
        together by normalising both to an engine family.

        The engine check is deliberately strict rather than a substring test:
        writing into the wrong application's page would be a cross-process
        surprise, so an unmatched process yields no targets and the caller
        keeps its own route.
        """
        family = _engine_family(process)
        if family is None:
            return []
        out: list[CdpTarget] = []
        for target in self.discover(refresh=True):
            if _engine_family(target.browser) == family:
                out.append(target)
        return out

    def connection(self, target: CdpTarget) -> CdpConnection:
        conn = self._connections.get(target.target_id)
        if conn is not None and not conn._closed:
            return conn
        conn = CdpConnection(target.ws_url)
        conn.connect()
        self._connections[target.target_id] = conn
        return conn

    # -- AX tree lookup -----------------------------------------------------

    def find_editable_node(
        self,
        process: str,
        *,
        hint_class: str = "",
        hint_name: str = "",
    ) -> int | None:
        """backendDOMNodeId of the page's editable element, or None.

        UIA cannot give a DOM id, so the two trees are matched on what they
        agree about: the element's own identity. A hint class is the strongest
        signal available (UIA reports the same ``class`` Chromium does, e.g.
        ``ProseMirror ExEditor-qq-msg-editor``), and the accessibility tree's
        ``editable``/``focused``/``multiline`` properties pick out the editor
        when the class name carries no hint.

        Only an **editable** node is ever returned: writing is the sole purpose
        of this channel, so returning a non-editable node would only produce a
        refused write later.
        """
        target = self.target_for_process(process)
        if target is None:
            return None
        try:
            conn = self.connection(target)
            result = conn.send("Accessibility.getFullAXTree", {"depth": -1})
        except (CdpError, Exception):  # noqa: BLE001 - treated as "not reachable"
            return None

        nodes = [n for n in (result.get("nodes") or []) if isinstance(n, dict)]
        best: tuple[int, int] | None = None
        for node in nodes:
            if not _ax_is_editable(node):
                continue
            backend = node.get("backendDOMNodeId")
            if not backend:
                continue
            score = _ax_match_score(node, hint_class, hint_name)
            if best is None or score > best[0]:
                best = (score, int(backend))
        if best is None:
            return None
        # A weak match is still the only editable node on the page, which is
        # enough: the bridge is asked for "the page's editor", and a Chromium
        # page has one.
        return best[1]

    def execute(
        self,
        process: str,
        backend_node_id: int,
        primitive: str,
        value: Any = None,
        *,
        timeout_s: float = 3.0,
    ) -> Any:
        """Run one primitive in the page. Returns an ExecResult-shaped value.

        Imported lazily to keep this module free of IR imports; the caller
        passes the id through unchanged.
        """
        from somnus.ir.action import ExecResult

        if primitive not in SUPPORTED_PRIMITIVES:
            return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED")
        if primitive == "set_text":
            # Not a single page call: the protocol-level insert is the point.
            return self._set_text(
                process, backend_node_id, value, timeout_s=timeout_s
            )
        target = self.target_for_process(process)
        if target is None:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="no debuggable page for this application",
            )
        try:
            conn = self.connection(target)
            object_id = self._resolve_object(conn, backend_node_id)
            if object_id is None:
                return ExecResult(ok=False, error="STALE_TARGET")
            result = conn.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _ACTION_SCRIPT[primitive],
                    "arguments": [{"value": value}],
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=timeout_s,
            )
        except CdpError as exc:
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - transport failure, reported
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))

        # The script returns false when the element is a shape it refuses to
        # write. Surfacing that is the whole point: a silent success here would
        # be the same lie the keyboard path used to tell.
        returned = (result or {}).get("result", {}).get("value")
        if primitive == "set_text" and returned is False:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="element is not an editable field",
            )
        return ExecResult(ok=True)

    def _resolve_object(self, conn: CdpConnection, backend_node_id: int) -> str | None:
        """objectId for a DOM node id, retrying once by objectGroup lookup.

        A node id goes stale when the page re-renders (the common case for a
        live app), in which case the caller has already re-located the node and
        passes a fresh id; a single retry covers a render that landed between
        the AX read and this call.
        """
        try:
            resolved = conn.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
        except CdpError:
            return None
        return (resolved.get("object") or {}).get("objectId")

    def set_text(
        self,
        process: str,
        value: str,
        *,
        hint_class: str = "",
        hint_name: str = "",
        timeout_s: float = 3.0,
    ) -> Any:
        """Write ``value`` into the page's editor through the DOM.

        The whole operation - locate the editor, resolve it, run the write -
        with a re-lookup when the node id has gone stale from a re-render.
        """
        from somnus.ir.action import ExecResult

        if self.target_for_process(process) is None:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="no debuggable page for this application",
            )
        backend = self.find_editable_node(
            process, hint_class=hint_class, hint_name=hint_name
        )
        if backend is None:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="no editable element found in the page",
            )
        result = self._set_text(process, backend, value, timeout_s=timeout_s)
        if result.ok:
            return result
        # The node id can expire between the AX read and the resolve (the page
        # re-rendered). One re-lookup covers that race; a second failure is a
        # real one and is reported as such.
        if result.error == "STALE_TARGET":
            backend = self.find_editable_node(
                process, hint_class=hint_class, hint_name=hint_name
            )
            if backend is not None:
                return self._set_text(process, backend, value, timeout_s=timeout_s)
        return result

    def _set_text(
        self,
        process: str,
        backend_node_id: int,
        value: Any,
        *,
        timeout_s: float = 3.0,
    ) -> Any:
        """Write text into one DOM node, as a real text insertion.

        Two steps, the way a browser itself does it (and the way Playwright's
        Chromium backend does it):

        1. focus the element and select what it holds, so the write replaces
           the content instead of appending to it;
        2. ``Input.insertText``, which is the browser's own text-insertion
           command - the same pipeline a keystroke or an IME composition goes
           through. An editor's own model therefore updates, which is why this
           works on ProseMirror/CodeMirror/Monaco when assigning
           ``textContent`` would desync them.

        Crucially this is *not* synthetic input at the OS level: no keystroke
        is posted to a window and no focus is stolen from the user. The command
        is delivered to the page's own editing machinery, which is what makes
        it safe to run against a window in the background.
        """
        from somnus.ir.action import ExecResult

        try:
            target = self.target_for_process(process)
            if target is None:
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message="no debuggable page for this application",
                )
            conn = self.connection(target)
            object_id = self._resolve_object(conn, backend_node_id)
            if object_id is None:
                return ExecResult(ok=False, error="STALE_TARGET")

            prepared = conn.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": _PREPARE_SCRIPT,
                    "returnByValue": True,
                },
                timeout=timeout_s,
            )
            kind = (prepared.get("result") or {}).get("value")
            if kind == "unsupported":
                # Not a field and not an editable region. A few shapes take a
                # property assignment instead; everything else is refused so
                # the caller reports an honest failure.
                fallback = conn.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": _FALLBACK_SCRIPT,
                        "arguments": [{"value": "" if value is None else str(value)}],
                        "returnByValue": True,
                    },
                    timeout=timeout_s,
                )
                if (fallback.get("result") or {}).get("value") is True:
                    return ExecResult(ok=True)
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message="element is not an editable field",
                )

            # The browser's own text insertion. This is the step that reaches
            # a contenteditable editor's real input pipeline.
            conn.send(
                "Input.insertText",
                {"text": "" if value is None else str(value)},
                timeout=timeout_s,
            )
        except CdpError as exc:
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - transport failure, reported
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        return ExecResult(ok=True)

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


# ---------------------------------------------------------------------------
# Provider (driving a page directly, without a native window)
# ---------------------------------------------------------------------------

#: CDP accessibility role -> portable role. CDP sends camelCase/PascalCase.
ROLE_MAP: dict[str, str] = {
    "button": "button",
    "buttonrole": "button",
    "checkbox": "checkbox",
    "radiobutton": "radio",
    "radio": "radio",
    "switch": "switch",
    "slider": "slider",
    "spinbutton": "slider",
    "textbox": "input",
    "textfield": "input",
    "searchbox": "input",
    "combobox": "select",
    "option": "listitem",
    "listbox": "list",
    "popupbutton": "select",
    "menu": "menu",
    "menubar": "menu",
    "menuitem": "menuitem",
    "menuitemcheckbox": "menuitem",
    "menuitemradio": "menuitem",
    "tab": "tab",
    "tablist": "tab",
    "tree": "tree",
    "treeitem": "treeitem",
    "list": "list",
    "listitem": "listitem",
    "table": "table",
    "row": "row",
    "grid": "table",
    "gridcell": "row",
    "cell": "row",
    "link": "link",
    "image": "image",
    "img": "image",
    "statictext": "text",
    "inlinetextbox": "text",
    "text": "text",
    "heading": "text",
    "paragraph": "text",
    "labeltext": "text",
    "listmarker": "text",
    "caption": "text",
    "descriptionlistterm": "text",
    "descriptionlistdetail": "text",
    "linebreak": "text",
    "canvas": "canvas",
    "graphicsobject": "canvas",
    "group": "group",
    "genericcontainer": "group",
    "generic": "group",
    "none": "group",
    "region": "group",
    "document": "group",
    "webarea": "group",
    "rootwebarea": "group",
    "iframe": "group",
    "section": "group",
    "article": "group",
    "main": "group",
    "navigation": "group",
    "banner": "group",
    "contentinfo": "group",
    "form": "group",
    "figure": "group",
    "blockquote": "group",
    "definitionlist": "list",
    "descriptionlist": "list",
    "dialog": "dialog",
    "alertdialog": "dialog",
    "alert": "dialog",
    "toolbar": "toolbar",
    "progressbar": "progressbar",
    "separator": "separator",
}

#: AX state property -> portable state.
_STATE_MAP: dict[str, str] = {
    "disabled": "disabled",
    "focused": "focused",
    "selected": "selected",
    "expanded": "expanded",
    "readonly": "readonly",
    "required": "required",
    "invalid": "invalid",
    "busy": "loading",
}


def ax_node_to_raw(
    node: dict,
    nodes_by_id: dict[str, dict],
    *,
    window_id: str,
    path: tuple[int, ...] = (),
) -> Any:
    """Convert one CDP AXNode (and its subtree) into a :class:`RawNode`.

    The web counterpart of a native tree read: roles, names, states and values
    come from the browser's own accessibility tree, never from a screenshot or
    from scraping rendered HTML.
    """
    from somnus.ir.node import NativeRef, RawNode

    role_raw = _ax_str(node, "role")
    role = ROLE_MAP.get(role_raw.lower(), "unknown")
    props = {
        prop.get("name"): (
            prop.get("value", {}).get("value")
            if isinstance(prop.get("value"), dict)
            else prop.get("value")
        )
        for prop in (node.get("properties") or [])
        if isinstance(prop, dict) and "name" in prop
    }

    states: set[str] = set()
    for ax_key, portable in _STATE_MAP.items():
        if props.get(ax_key) in (True, "true"):
            states.add(portable)
    checked = props.get("checked")
    if checked in (True, "true"):
        states.add("checked")
    elif checked in (False, "false"):
        states.add("unchecked")
    elif checked == "mixed":
        states.add("mixed")

    patterns = _ax_patterns(role, node)
    backend_id = node.get("backendDOMNodeId")
    native = NativeRef(
        window_id=window_id,
        runtime_id=(int(backend_id),) if backend_id else (),
        class_name=str(props.get("className") or "") or None,
        control_type=role,
        path=path,
    )
    raw = RawNode(
        native=native,
        role=role,
        name=_ax_str(node, "name") or None,
        value=_ax_str(node, "value") or None,
        states=frozenset(states),
        patterns=frozenset(patterns),
        children=[],
    )
    for i, child_id in enumerate(node.get("childIds") or []):
        child = nodes_by_id.get(child_id)
        if child is None:
            continue
        # InlineTextBox nodes are rendering fragments of a StaticText; walking
        # them would duplicate every paragraph once per fragment.
        if _ax_str(child, "role").lower() == "inlinetextbox":
            continue
        raw.children.append(
            ax_node_to_raw(child, nodes_by_id, window_id=window_id, path=path + (i,))
        )
    return raw


def _ax_patterns(role: str, node: dict) -> set[str]:
    """The portable patterns a CDP node supports.

    These are the web counterparts of native patterns, so the existing
    capability derivation works unchanged: an ``invoke`` node becomes
    ``activate``, a ``value`` node becomes ``set_text``, and so on.
    """
    patterns: set[str] = set()
    if role in ("button", "link", "menuitem", "tab", "listitem", "treeitem", "row", "option"):
        patterns.add("invoke")
    if role in ("input", "select", "slider"):
        patterns.add("value")
    if role in ("checkbox", "switch", "radio"):
        patterns.add("toggle")
    if role in ("listitem", "treeitem", "tab", "radio", "option", "row"):
        patterns.add("selectionitem")
    if role in ("select", "menu", "dialog"):
        patterns.add("expandcollapse")
    if role == "slider":
        patterns.add("rangevalue")
    if role in ("list", "table", "tree", "group"):
        patterns.add("scroll")
    if node.get("actions"):
        patterns.add("invoke")
    return patterns


@dataclass
class CdpProvider:
    """AccessibilityProvider backed directly by the DevTools Protocol.

    Used when a page should be driven on its own, without a native window. An
    Electron *application* does not need this: its window is observed through
    UIA, which already gives correct window and ref identity, and only the
    writes go through the protocol (:class:`CdpBridge`).
    """

    name: str = "cdp"
    ports: tuple[int, ...] = DEFAULT_PORTS
    _targets: dict[str, CdpTarget] = field(default_factory=dict)
    _bridge: CdpBridge = field(default_factory=CdpBridge)

    def list_windows(self) -> list[Any]:
        from somnus.ir.node import WindowInfo

        self._targets.clear()
        out: list[WindowInfo] = []
        for i, target in enumerate(discover_targets(self.ports)):
            wid = f"w{i + 1}"
            self._targets[wid] = target
            out.append(
                WindowInfo(
                    window_id=wid,
                    title=target.title or target.url or "(untitled)",
                    process=target.browser,
                    pid=target.port,
                    active=i == 0,
                )
            )
        return out

    def snapshot(
        self, window_id: str, scope: Any = None, max_depth: int = 12
    ) -> Any:
        from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

        target = self._targets.get(window_id)
        if target is None:
            self.list_windows()
            target = self._targets.get(window_id)
        if target is None:
            raise KeyError(window_id)
        conn = self._bridge.connection(target)
        result = conn.send("Accessibility.getFullAXTree", {"depth": max_depth})
        nodes = [n for n in (result.get("nodes") or []) if isinstance(n, dict)]
        by_id = {n["nodeId"]: n for n in nodes if "nodeId" in n}
        child_ids = {c for n in nodes for c in (n.get("childIds") or [])}
        root_node = next(
            (n for n in nodes if n["nodeId"] not in child_ids and n.get("role")),
            nodes[0] if nodes else None,
        )
        window = WindowInfo(
            window_id=window_id,
            title=target.title or "(untitled)",
            process=target.browser,
            pid=target.port,
            active=True,
        )
        if root_node is None:
            root = RawNode(
                native=NativeRef(window_id=window_id), role="group", name=window.title
            )
        else:
            root = ax_node_to_raw(root_node, by_id, window_id=window_id)
            root.role = "window"
            root.name = root.name or window.title
        return RawTree(window=window, root=root)

    def execute(
        self, ref: Any, primitive: str, value: Any = None, timeout_s: float = 3.0
    ) -> Any:
        backend_id = ref.runtime_id[0] if ref.runtime_id else None
        if backend_id is None:
            from somnus.ir.action import ExecResult

            return ExecResult(ok=False, error="STALE_TARGET")
        # A page provider is single-page in practice: route through whichever
        # target owns this ref's window, falling back to the first.
        target = self._targets.get(ref.window_id)
        process = target.browser if target else ""
        return self._bridge.execute(process, backend_id, primitive, value, timeout_s=timeout_s)

    def subscribe(self, window_id: str, on_event: Any) -> Any:
        from somnus.providers.base import NullSubscription

        return NullSubscription()

    def close(self) -> None:
        self._bridge.close()
