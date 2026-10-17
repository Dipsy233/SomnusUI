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
            "Semantic overview (L0) of one window: screens, entities and "
            "their capabilities. Never pixels or native ids. Call it first - "
            "it assigns the refs every other tool uses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "window ref to focus and observe",
                },
                "windows_only": {
                    "type": "boolean",
                    "description": "window list only, no control tree",
                },
                "include_other_windows": {
                    "type": "boolean",
                    "description": "also list the rest of the desktop",
                },
            },
        },
    },
    {
        "name": "find",
        "description": (
            "Search the current window, including inside folded lists. Returns "
            "whole entity lines with their capabilities."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": 'keyword, or FIND syntax: product "Big Mac" [in ref]',
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "inspect",
        "description": (
            "One node in full (L1/L2): attributes, capability params, relations. "
            "Use it when a call's capability set looks wrong."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "depth": {"type": "integer", "description": "subtree depth"},
                "start": {"type": "integer", "description": "text range start"},
                "end": {"type": "integer", "description": "text range end"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "call",
        "description": (
            "Invoke a capability printed on a node: CALL p1.add(). It belongs to "
            "that node, not its container. Returns OK/ERR plus a delta, which "
            "beats the status word. Critical caps need confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "CALL ref.cap(args)"},
                "rev": {"type": "integer", "description": "the rev you saw"},
                "confirmed": {"type": "boolean", "description": "user consented"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "switch",
        "description": (
            "Move between the user's desktop (main) and a separate session "
            "(child). One desktop at a time: a switch discards every ref. Use "
            "`open` when the work must not disturb the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["main", "child", "open", "close", "detach"],
                    "description": (
                        "main = user's desktop; child = the separate session; "
                        "open = start it and move in; close = end it, back to "
                        "main; detach = keep it after this process exits"
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
