"""CDP provider: browsers as a first-class accessibility source (v0.3).

Dev doc section 16 names CDP as the browser Provider. A web page is *not* a
native window, but it is the same shape of problem: an accessibility tree with
roles, states, names and actions. The provider therefore speaks the same
:class:`~somnus.providers.base.AccessibilityProvider` interface as UIA and
produces the same :class:`~somnus.ir.node.RawNode` trees, so the compiler,
projection and runtime need no changes at all.

The tree comes from ``Accessibility.getFullAXTree`` (the browser's own
accessibility tree - the web equivalent of UIA), never from a screenshot or from
raw DOM scraping. Actions go through ``DOM.resolveNode`` +
``Runtime.callFunctionOn`` against the same backend node the AX tree named, which
is the web counterpart of a native pattern: it triggers the real event handlers
instead of synthesising input at coordinates.

Transport: the DevTools Protocol over a websocket, discovered from the loopback
HTTP endpoint (``/json/list``). No browser extension or driver is required.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.base import NullSubscription

log = logging.getLogger("somnus.cdp")

#: Default debugging ports browsers listen on.
DEFAULT_PORTS = (9222, 9223, 9224)

#: CDP accessibility role -> portable role. CDP sends camelCase / PascalCase.
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

#: CDP states that map onto our portable state vocabulary.
STATE_PROPERTIES: dict[str, str] = {
    "disabled": "disabled",
    "focused": "focused",
    "selected": "selected",
    "checked": "checked",
    "expanded": "expanded",
    "readonly": "readonly",
    "required": "required",
    "invalid": "invalid",
    "busy": "loading",
}

#: Roles whose nodes expose an actionable DOM element.
_ACTIONABLE_ROLES = frozenset(
    {"button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "option",
     "listitem", "treeitem", "row", "combobox", "select", "textbox", "input", "slider"}
)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class CdpTarget:
    """One debuggable page (a browser tab)."""

    target_id: str
    title: str
    url: str
    ws_url: str
    port: int
    browser: str = "chromium"


@dataclass
class CdpConnection:
    """A synchronous DevTools websocket session for one target."""

    ws_url: str
    _sock: Any = None
    _next_id: int = 0
    session_id: str | None = None
    _closed: bool = False

    def connect(self, timeout: float = 5.0) -> None:
        import websocket  # websocket-client

        # Chrome rejects a websocket whose Origin header it does not recognise
        # (403 Forbidden). A DevTools client is not a web page, so suppress the
        # Origin header instead of requiring --remote-allow-origins on launch.
        self._sock = websocket.create_connection(
            self.ws_url,
            timeout=timeout,
            suppress_origin=True,
        )
        # One session per page target keeps the command stream unambiguous.
        reply = self.send(
            "Target.attachToTarget",
            {"targetId": _target_from_ws(self.ws_url), "flatten": True},
        )
        self.session_id = reply.get("sessionId")

    def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        """Send one CDP command and return the result (or raise on error)."""
        if self._sock is None:
            raise RuntimeError("not connected")
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if self.session_id:
            message["sessionId"] = self.session_id
        self._sock.send(json.dumps(message))
        self._sock.settimeout(timeout)
        while True:
            raw = self._sock.recv()
            if not raw:
                raise RuntimeError("connection closed")
            reply = json.loads(raw)
            if reply.get("id") == message["id"]:
                if "error" in reply:
                    raise CdpError(reply["error"].get("message", "cdp error"))
                return reply.get("result", {})
            # Otherwise it is an event; ignore it here (v0.3 polls for changes).

    def close(self) -> None:
        self._closed = True
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None


class CdpError(RuntimeError):
    """A DevTools protocol error."""


def _target_from_ws(ws_url: str) -> str:
    # ws://127.0.0.1:9222/devtools/page/<ID>
    return ws_url.rstrip("/").rsplit("/", 1)[-1]


def discover_targets(ports: Iterable[int] = DEFAULT_PORTS) -> list[CdpTarget]:
    """List debuggable pages on the loopback CDP endpoints.

    Only ``127.0.0.1`` is contacted: a browser debug port must never be reached
    over the network (dev doc section 12). Ports given explicitly are tried
    first; in addition, any browser launched with ``--remote-debugging-port=0``
    is found by reading its ``DevToolsActivePort`` file, which is the reliable way
    to attach when a fixed port is unavailable.
    """
    out: list[CdpTarget] = []
    for port in _candidate_ports(ports):
        version = _http_json(port, "/json/version")
        browser = "chromium"
        if version:
            browser = str(version.get("Browser", "chromium")).split("/")[0]
        for item in _http_json(port, "/json/list") or []:
            if item.get("type") != "page":
                continue
            ws = item.get("webSocketDebuggerUrl")
            if not ws:
                continue
            out.append(
                CdpTarget(
                    target_id=item.get("id", ""),
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    ws_url=ws,
                    port=port,
                    browser=browser,
                )
            )
    return out


def _candidate_ports(ports: Iterable[int]) -> list[int]:
    """Configured ports plus any found in a browser's DevToolsActivePort file."""
    found = list(ports)
    for path in _devtools_active_port_files():
        try:
            port = int(path.read_text(encoding="utf-8").splitlines()[0].strip())
        except (OSError, ValueError, IndexError):
            continue
        if port not in found:
            found.append(port)
    return found


def _devtools_active_port_files() -> list[Any]:
    """Locate ``DevToolsActivePort`` files for typical browser profiles.

    A browser started with ``--remote-debugging-port=0`` picks a free port and
    records it here; scanning the conventional profile locations is what lets
    SomnusUI attach without asking the user for a port number.
    """
    import os
    from pathlib import Path

    roots = [
        Path(os.environ.get("TEMP", "")) ,
        Path(os.environ.get("TMP", "")),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Temp",
        Path("C:/Temp"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data",
    ]
    out: list[Any] = []
    for root in roots:
        if not root or not root.exists():
            continue
        direct = root / "DevToolsActivePort"
        if direct.is_file():
            out.append(direct)
        try:
            for child in root.glob("*/DevToolsActivePort"):
                out.append(child)
        except OSError:
            continue
    return out


def _http_json(port: int, path: str) -> Any:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2.0) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# AX tree -> RawNode
# ---------------------------------------------------------------------------

#: CDP AX value types.
_AX_VALUE = ("value", "checked", "expanded", "selected", "disabled", "focused",
             "readonly", "required", "invalid", "busy", "modal")


def ax_node_to_raw(
    node: dict,
    nodes_by_id: dict[str, dict],
    *,
    window_id: str,
    path: tuple[int, ...] = (),
) -> RawNode:
    """Convert one CDP ``AXNode`` (with its subtree) into a :class:`RawNode`."""
    role_raw = _ax_str(node, "role")
    role = ROLE_MAP.get(role_raw.lower(), "unknown")

    name = _ax_str(node, "name")
    value = _ax_str(node, "value")
    props = _ax_properties(node)
    states: set[str] = set()
    for key, portable in STATE_PROPERTIES.items():
        if props.get(key) is True:
            states.add(portable)
    # `checked` is tri-state in CDP ("true"/"false"/"mixed").
    checked = props.get("checked")
    if checked is True or checked == "true":
        states.add("checked")
    elif checked is False or checked == "false":
        states.add("unchecked")
    elif checked == "mixed":
        states.add("mixed")

    # Patterns are the analogue of native patterns: what this node can do.
    patterns = _ax_patterns(role, props, node)

    backend_id = node.get("backendDOMNodeId")
    native = NativeRef(
        window_id=window_id,
        runtime_id=(int(backend_id),) if backend_id else (),
        automation_id=_ax_dom_id(node, nodes_by_id),
        class_name=role_raw or None,
        control_type=role,
        path=path,
    )

    raw = RawNode(
        native=native,
        role=role,
        name=name or None,
        value=value or None,
        description=_ax_str(node, "description") or None,
        states=frozenset(states),
        patterns=frozenset(patterns),
        children=[],
    )
    for i, child in enumerate(node.get("childIds") or []):
        child_node = nodes_by_id.get(child)
        if child_node is None:
            continue
        # ``InlineTextBox`` nodes are the rendering fragments of a
        # ``StaticText``; CDP emits one per run, with no extra semantics. Walking
        # them would duplicate every paragraph once per fragment, so they are
        # folded into the parent text node.
        if _ax_str(child_node, "role").lower() == "inlinetextbox":
            continue
        raw.children.append(
            ax_node_to_raw(
                child_node, nodes_by_id, window_id=window_id, path=path + (i,)
            )
        )
    return raw


def _ax_str(node: dict, key: str) -> str:
    field_value = node.get(key)
    if isinstance(field_value, dict):
        v = field_value.get("value")
        return v if isinstance(v, str) else ("" if v is None else str(v))
    return ""


def _ax_properties(node: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prop in node.get("properties") or []:
        if isinstance(prop, dict) and "name" in prop:
            value = prop.get("value", {})
            out[prop["name"]] = value.get("value") if isinstance(value, dict) else value
    return out


def _ax_dom_id(node: dict, nodes_by_id: dict[str, dict]) -> str | None:
    """A stable-ish identity: the DOM id if the AX node carries one."""
    for prop in node.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name") in ("domNodeId", "id"):
            value = prop.get("value", {})
            got = value.get("value") if isinstance(value, dict) else value
            if got:
                return str(got)
    return None


def _ax_patterns(role: str, props: dict[str, Any], node: dict) -> set[str]:
    """Derive the portable patterns a CDP node supports.

    These are the web counterparts of native patterns, so the *existing*
    capability derivation works unchanged: an ``invoke``-capable node becomes
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
    # A node can advertise an action through the AX `actions` property.
    if node.get("actions"):
        patterns.add("invoke")
    return patterns


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass
class CdpProvider:
    """AccessibilityProvider backed by the Chrome DevTools Protocol."""

    name: str = "cdp"
    ports: tuple[int, ...] = DEFAULT_PORTS
    _targets: dict[str, CdpTarget] = field(default_factory=dict)
    _connections: dict[str, CdpConnection] = field(default_factory=dict)
    #: window_id -> cached AX tree (refreshed on every snapshot).
    _trees: dict[str, RawTree] = field(default_factory=dict)
    allow_list: tuple[str, ...] = ()

    # -- discovery ---------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
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

    def _connection(self, window_id: str) -> CdpConnection:
        conn = self._connections.get(window_id)
        if conn is not None and not conn._closed:
            return conn
        target = self._targets.get(window_id)
        if target is None:
            self.list_windows()
            target = self._targets.get(window_id)
        if target is None:
            raise KeyError(window_id)
        conn = CdpConnection(target.ws_url)
        conn.connect()
        self._connections[window_id] = conn
        return conn

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 12,
    ) -> RawTree:
        conn = self._connection(window_id)
        result = conn.send("Accessibility.getFullAXTree", {"depth": max_depth})
        nodes = result.get("nodes") or []
        by_id = {n["nodeId"]: n for n in nodes if "nodeId" in n}

        # Find the root: the node nobody lists as a child.
        child_ids = {c for n in nodes for c in (n.get("childIds") or [])}
        root_node = next(
            (n for n in nodes if n["nodeId"] not in child_ids and n.get("role")),
            nodes[0] if nodes else None,
        )
        target = self._targets.get(window_id)
        window = WindowInfo(
            window_id=window_id,
            title=(target.title if target else "") or "(untitled)",
            process=target.browser if target else "chromium",
            pid=target.port if target else 0,
            active=True,
        )
        if root_node is None:
            empty = RawNode(
                native=NativeRef(window_id=window_id), role="group", name=window.title
            )
            tree = RawTree(window=window, root=empty)
        else:
            root = ax_node_to_raw(root_node, by_id, window_id=window_id)
            root.role = "window"
            root.name = root.name or window.title
            tree = RawTree(window=window, root=root)
        self._trees[window_id] = tree
        return tree

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult:
        conn = self._connection(ref.window_id)
        backend_id = ref.runtime_id[0] if ref.runtime_id else None
        if backend_id is None:
            return ExecResult(ok=False, error="STALE_TARGET")
        try:
            resolved = conn.send("DOM.resolveNode", {"backendNodeId": backend_id})
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                return ExecResult(ok=False, error="STALE_TARGET")
            script = _ACTION_SCRIPT.get(primitive)
            if script is None:
                return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED")
            args = [{"value": value}]
            conn.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": script,
                    "arguments": args,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                timeout=timeout_s,
            )
        except CdpError as exc:
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - transport failure
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        return ExecResult(ok=True)

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> NullSubscription:
        # v0.3 polls (Session's settle loop); CDP events can drop in here later.
        return NullSubscription()

    def close(self) -> None:
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()


#: The web counterpart of a native pattern: a DOM call that triggers the real
#: handlers. Never coordinates, never synthetic keystrokes at a position.
_ACTION_SCRIPT: dict[str, str] = {
    "activate": "function(){ this.click(); return true; }",
    "select": "function(){ this.click(); if (this.focus) this.focus(); return true; }",
    "focus": "function(){ this.focus(); return true; }",
    "set_text": (
        "function(v){ this.focus(); "
        "if ('value' in this) { this.value = v; } "
        "else { this.textContent = v; } "
        "this.dispatchEvent(new Event('input', {bubbles:true})); "
        "this.dispatchEvent(new Event('change', {bubbles:true})); return true; }"
    ),
    "toggle": "function(){ this.click(); return true; }",
    "expand": "function(){ this.click(); return true; }",
    "collapse": "function(){ this.click(); return true; }",
    "read": "function(){ return true; }",
    "scroll_into_view": "function(){ this.scrollIntoView({block:'center'}); return true; }",
}
