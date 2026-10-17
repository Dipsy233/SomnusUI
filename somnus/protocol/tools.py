"""The tools exposed to an LLM.

Model-agnostic: each tool is a thin function over a :class:`Session`. The MCP
server and the JSON-RPC server both call :func:`call_tool`, so behaviour is
identical regardless of transport.

The tool *contracts* are deliberately small and closed. An LLM that only speaks
text uses the same syntax via ``llm/parser``; nothing here depends on function
calling.
"""

from __future__ import annotations

from typing import Any

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import Action
from somnus.runtime.session import Session

#: Names + JSON schemas advertised to the LLM.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "observe",
        "description": (
            "Return the semantic overview (L0) of one window as SUI-Text: "
            "its screens, entities and their capabilities. No pixels, "
            "coordinates or native ids.\n"
            "Only the target window is described; set include_other_windows "
            "to also list the rest of the desktop, or windows_only to get "
            "just the window list (which is how you switch windows with "
            "CALL wN.focus())."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "optional window ref to focus and observe",
                },
                "windows_only": {
                    "type": "boolean",
                    "description": "list windows only, without any control tree",
                },
                "include_other_windows": {
                    "type": "boolean",
                    "description": (
                        "also list the other desktop windows (off by default, "
                        "they are not what you asked for)"
                    ),
                },
            },
        },
    },
    {
        "name": "find",
        "description": (
            "Search the current screen for entities/controls. Returns matching "
            "entity lines. Use FIND syntax: FIND [type] \"query\" [in ref]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": 'e.g. FIND product "Big Mac"'},
            },
            "required": ["query"],
        },
    },
    {
        "name": "inspect",
        "description": (
            "Drill into one node (L1/L2): all attributes, capability parameters "
            "and relations. Optional depth and text range=start:end."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "depth": {"type": "integer"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "call",
        "description": (
            "Invoke a capability: CALL <ref>.<cap>(args). A capability belongs to "
            "the node it is printed on - to add a product call CALL p1.add(), "
            "not CALL c1.add(). Returns OK/ERR plus a delta of what changed. "
            "Critical capabilities require confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "e.g. CALL p1.add()"},
                "rev": {"type": "integer"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "switch",
        "description": (
            "Move this agent between the two Windows sessions, or open / close "
            "the separate one. The agent drives ONE session at a time: after "
            "switching, every ref (windows included) names the new desktop, "
            "and all refs from before are gone.\n"
            "Use `open` when the user asks for something to happen on a "
            "separate desktop the agent can be parked in (there its input "
            "cannot disturb the user); `close` ends it; `child` / `main` move "
            "between them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["main", "child", "open", "close", "detach"],
                    "description": (
                        "main = the user's own desktop (the default); "
                        "child = move to the separate session; "
                        "open = start that session and move into it; "
                        "close = end that session and return to main; "
                        "detach = leave it running after this program exits"
                    ),
                }
            },
            "required": ["target"],
        },
    },
]


def call_tool(session: Session, name: str, arguments: dict[str, Any] | None = None) -> str:
    """Execute one tool call and return SUI-Text (never raises to the caller)."""
    args = arguments or {}
    try:
        if name == "observe":
            return session.observe(
                args.get("window"),
                windows_only=bool(args.get("windows_only")),
                include_other_windows=bool(args.get("include_other_windows")),
            ).text
        if name == "find":
            # The `query` argument accepts the FIND syntax verbatim
            # (``FIND product "Big Mac"``) or a bare query string.
            raw = (args.get("query") or "").strip()
            from somnus.sui.parse import parse_action

            if raw.upper().startswith("FIND"):
                action = parse_action(raw)
            else:
                action = Action(kind="find", query=raw)
            return session.find(action)
        if name == "inspect":
            rng = None
            if args.get("start") is not None and args.get("end") is not None:
                rng = (int(args["start"]), int(args["end"]))
            action = Action(
                kind="inspect",
                ref=args.get("ref"),
                depth=args.get("depth"),
                range=rng,
            )
            return session.inspect(action).text
        if name == "call":
            from somnus.sui.parse import parse_action

            action = parse_action(args.get("action", ""), rev=args.get("rev"))
            return session.call(action, confirmed=args.get("confirmed")).text
        if name == "switch":
            target = (args.get("target") or "main").strip().lower()
            if target == "open":
                return session.open_child_session()
            if target == "close":
                return session.close_child_session()
            if target == "detach":
                return session.detach_child_session()
            return session.switch_session(target)
        return SomnusError(ErrorCode.PARSE_ERROR, message=f"unknown tool {name}").render()
    except SomnusError as err:
        return err.render()
