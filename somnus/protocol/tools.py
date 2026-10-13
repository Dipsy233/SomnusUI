"""The four tools exposed to an LLM (dev doc section 6.4).

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
            "Return the current semantic overview (L0) of the target window as "
            "SUI-Text. Contains windows, screens, entities and their "
            "capabilities. No pixels, coordinates or native ids.\n"
            "Pass windows_only=true to get just the desktop window list, which "
            "is how you switch to another window (CALL wN.focus())."
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
]


def call_tool(session: Session, name: str, arguments: dict[str, Any] | None = None) -> str:
    """Execute one tool call and return SUI-Text (never raises to the caller)."""
    args = arguments or {}
    try:
        if name == "observe":
            return session.observe(
                args.get("window"), windows_only=bool(args.get("windows_only"))
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
        return SomnusError(ErrorCode.PARSE_ERROR, message=f"unknown tool {name}").render()
    except SomnusError as err:
        return err.render()
