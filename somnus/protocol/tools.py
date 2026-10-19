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
            "Semantic overview (L0) of the CURRENT screen: screens, entities "
            "and their capabilities. Always reflects what is on screen right "
            "now - the header names the current app (SCREEN home / SCREEN "
            "qq). Never pixels or native ids. Call it first - it assigns the "
            "refs every other tool uses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "find",
        "description": (
            "Search the CURRENT screen (re-snapshotted first), including "
            "inside folded lists. Returns whole entity lines with their "
            "capabilities."
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
            "Navigate the phone itself - no UI tree needed: home (launcher), "
            "back, next/prev (launcher pages), recents (task switcher), "
            "notifications / quick_settings (the pull-down shades; on merged-"
            "shade devices both open the same panel), launch (open an app "
            "through the platform's own API with `app`), open (deep-link: "
            "jump straight to an in-app page via a scheme URI in `app`, e.g. "
            "taobao://home; ERR APP_NOT_FOUND means no app handles it - fall "
            "back to launch + UI navigation). Discards every ref."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": [
                        "home", "back", "next", "prev", "recents",
                        "notifications", "quick_settings", "launch", "open",
                    ],
                    "description": (
                        "home = launcher; back = previous screen; next/prev = "
                        "pager page; recents = task switcher; notifications / "
                        "quick_settings = the pull-down panels; launch = open "
                        "the app named in `app`; open = deep-link URI in `app`"
                    ),
                },
                "app": {
                    "type": "string",
                    "description": (
                        "with target=launch: app label or package, e.g. '微信' "
                        "or 'com.tencent.mm'; with target=open: scheme URI, "
                        "e.g. 'taobao://home'"
                    ),
                },
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
            return session.observe().text
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
            # Route through dispatch, not session.call: the action string is
            # SUI-Text, which includes the session-level forms (SYSTEM back,
            # APP launch, FIND ... scroll) that parse_action accepts but
            # session.call would mis-handle as a node CALL (found live: MCP
            # `call {"action": "SYSTEM home"}` errored with STALE_TARGET).
            from somnus.sui.parse import parse_action

            action = parse_action(args.get("action", ""), rev=args.get("rev"))
            return session.run(action, confirmed=args.get("confirmed"))
        if name == "switch":
            target = (args.get("target") or "home").strip().lower()
            return session.switch_session(target, app=args.get("app"))
        return SomnusError(ErrorCode.PARSE_ERROR, message=f"unknown tool {name}").render()
    except SomnusError as err:
        return err.render()
