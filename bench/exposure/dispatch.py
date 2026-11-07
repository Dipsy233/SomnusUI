"""Route a tool call on any scheme's surface to the flat :func:`call_tool`.

The signature matches :func:`somnus.protocol.tools.call_tool` so the loop and
the tests treat every scheme identically. Flat tools are forwarded with their
arguments untouched; merged tools are routed by which *parameter* the caller
used (or by ``kind``, for the action tool), and the router parameter itself is
stripped before the flat tool sees the arguments.

Errors are whatever the flat tool returns — the router never rewords them, so
a folded surface cannot look more (or less) forgiving than the baseline.
"""

from __future__ import annotations

from typing import Any

from somnus.errors import ErrorCode, SomnusError
from somnus.protocol.tools import call_tool


def dispatch_tool(session, scheme, name: str, arguments: dict[str, Any] | None) -> str:
    """Execute one tool call on ``scheme``'s surface."""
    args = arguments or {}
    for exposed, base in scheme.flat:
        if name == exposed:
            return call_tool(session, base, args)
    for exposed, router in scheme.merged:
        if name == exposed:
            return _route(session, router, args)
    return SomnusError(
        ErrorCode.PARSE_ERROR, message=f"unknown tool {name}"
    ).render()


def _route(session, router: str, args: dict[str, Any]) -> str:
    if router in ("look", "look_help"):
        return _route_look(session, router, args)
    return _route_act(session, router, args)


def _route_look(session, router: str, args: dict[str, Any]) -> str:
    """overview · query=find · ref=inspect · topic=help (``look_help`` only)."""
    if args.get("query") is not None:
        return call_tool(
            session,
            "find",
            {"query": args["query"], "timeout": args.get("timeout")},
        )
    if args.get("ref") is not None:
        return call_tool(
            session,
            "inspect",
            {
                "ref": args["ref"],
                "depth": args.get("depth"),
                "start": args.get("start"),
                "end": args.get("end"),
            },
        )
    if router == "look_help" and args.get("topic") is not None:
        return call_tool(session, "help", {"topic": args["topic"]})
    return call_tool(
        session,
        "observe",
        {
            "window": args.get("window"),
            "windows_only": args.get("windows_only"),
            "include_other_windows": args.get("include_other_windows"),
        },
    )


def _route_act(session, router: str, args: dict[str, Any]) -> str:
    """kind=invoke|run|launch · target=switch · configure={…} (``_switch_configure``)."""
    if router == "act_switch_configure":
        if args.get("target") is not None:
            return call_tool(session, "switch", {"target": args["target"]})
        if args.get("configure") is not None:
            patch = args["configure"]
            if not isinstance(patch, dict):
                return SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message="configure expected an object",
                ).render()
            return call_tool(session, "configure", dict(patch))

    kind = args.get("kind")
    if kind is None:
        kind = (
            "run"
            if args.get("script") is not None
            else "launch"
            if args.get("command") is not None
            else "invoke"
        )
    rest = {k: v for k, v in args.items() if k != "kind"}
    if kind == "invoke":
        return call_tool(session, "call", rest)
    if kind == "run":
        return call_tool(
            session, "run", {"script": rest.get("script"), "window": rest.get("window")}
        )
    if kind == "launch":
        return call_tool(session, "launch", rest)
    return SomnusError(
        ErrorCode.INVALID_ARGUMENT,
        message=f"kind expected invoke|run|launch got={kind!r}",
    ).render()
