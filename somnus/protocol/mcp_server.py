"""MCP server (stdio) exposing SomnusUI's tools.

Uses the official ``mcp`` package when installed. Kept separate from
:mod:`somnus.protocol.jsonrpc` so a host can embed either transport.

**Settings are read here too.** An MCP client launches this process itself, so it
is exactly the case a command-line flag cannot serve: the user's one-off opt-in
would have no way to reach it. The persistent settings file (``somnus/config.py``)
applies to every transport, and a caller passing explicit policy overrides still
wins.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from somnus.config import (
    DEFAULT_POST_ACTION_ENUM,
    DEFAULT_SETTLE_POLL_MS,
    DEFAULT_SETTLE_QUIET_MS,
    DEFAULT_SETTLE_TIMEOUT_S,
    load as load_settings,
)
from somnus.protocol.tools import TOOL_SPECS, call_tool
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from somnus.runtime.settle import Settler


def build_session(**kwargs: Any) -> Session:
    """Build a session with the persistent settings applied.

    A ``policy`` may be passed explicitly (tests, embedding); otherwise one is
    built from the settings file, so the opt-in switches work without a flag.
    The same file now also sets the settle timing and the post-action window
    enumeration, which are applied to the session object here.
    """
    from somnus.providers.base import get_provider

    policy = kwargs.pop("policy", None)
    config_path = kwargs.pop("config", None)
    settings = kwargs.pop("settings", None)
    if settings is None:
        settings = load_settings(config_path)
    if policy is None:
        policy = Policy(
            allow_processes=settings.allow_processes,
            allow_clipboard=settings.allow_clipboard,
            allow_keyboard=settings.allow_keyboard,
        )

    provider_name = (kwargs.pop("provider", "auto") or "auto").lower()
    provider = get_provider(provider_name, **kwargs)
    session = Session(
        provider,
        policy=policy,
        settler=Settler(
            quiet_ms=settings.settle_quiet_ms,
            poll_interval_ms=settings.settle_poll_ms,
            timeout_s=settings.settle_timeout_s,
            skip_when_deterministic=bool(getattr(provider, "is_deterministic", False)),
        ),
    )
    # Session-scope switch; ``apply_settings`` (session side) updates it later.
    session.post_action_enum = settings.post_action_enum
    # Remember how to rebuild a local provider: ``SWITCH main`` from the child
    # session swaps the provider (only one may hold UIA state, so the old one
    # is released on the way out) and has to construct a fresh one rather than
    # reuse a closed one. Without this the MCP server could enter the child
    # session but never leave it. "auto" resolves to the real Windows provider
    # here; where it falls back to the replay provider the factory simply
    # rebuilds that, and an empty window list is the honest answer.
    if provider_name in ("auto", "uia", "windows"):
        session._local_provider_factory = lambda: get_provider(provider_name)
    return session


def _make_list_tools(session: Session):
    """Build the ``on_list_tools`` handler bound to *session*."""
    from mcp.types import ListToolsResult, Tool

    async def _list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=spec["name"],
                    description=spec["description"],
                    inputSchema=spec["inputSchema"],
                )
                for spec in TOOL_SPECS
            ]
        )

    return _list_tools


def _make_call_tool(session: Session):
    """Build the ``on_call_tool`` handler bound to *session*."""
    from mcp.types import CallToolRequestParams, CallToolResult, TextContent

    async def _call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
        text = call_tool(session, params.name, params.arguments)
        return CallToolResult(content=[TextContent(type="text", text=text)])

    return _call_tool


def settings_brief(session: Session) -> str:
    """The one-time ``initialize`` note: the shared rules and live timing.

    MCP sends ``instructions`` when the client initializes, so this costs
    nothing per turn. It carries the rules every tool relies on once; tool
    descriptions carry only what is specific to that tool. It is generated
    from the live session, not from the defaults, because a settings file may
    already have changed them.
    """
    settler = getattr(session, "settler", None)
    quiet = getattr(settler, "quiet_ms", DEFAULT_SETTLE_QUIET_MS)
    poll = getattr(settler, "poll_interval_ms", DEFAULT_SETTLE_POLL_MS)
    timeout = getattr(settler, "timeout_s", DEFAULT_SETTLE_TIMEOUT_S)
    enum = bool(getattr(session, "post_action_enum", DEFAULT_POST_ACTION_ENUM))
    return (
        "Operate a GUI by calling capabilities on entities; replies are text, "
        "no pixels/ids.\n"
        "Refs (w1, p3) come from observe and name elements of the current "
        "tree; a switch discards every ref. STALE_TARGET = ref no longer "
        "resolves, find again; relocation prints ~ healed old→new.\n"
        "observe raises the target window (windows_only does not); an unknown "
        "window ref observes the current target, or the active/first window "
        "when none is set.\n"
        "Syntax: CALL ref.cap(args). A capability belongs to the node printing "
        "it (+add on PRODUCT = CALL p1.add()). Args: name=value or one "
        "positional; quote spaces/commas/parentheses; = is the only separator. "
        'A text write echoes: ~ q1 text="Hello".\n'
        "observe = tree + refs; find = named-node search incl folded lists, "
        'query = `FIND [ref] [type] "text" [in ref]` or a keyword; inspect = one '
        "node's attrs, caps, params, relations.\n"
        "Implicit caps: focus everywhere, read where text is readable; "
        "activate where Invoke/LegacyIAccessible exists or role = "
        "LINK/MENUITEM; toggle CHECKBOX/SWITCH; select "
        "SELECT(combo)/LISTITEM/TAB/RADIO/ROW/TREEITEM; scroll "
        "LIST/TREE/TABLE/PANE; context_menu on operable controls except "
        "MENU/MENUITEM; other roles get these from their pattern. set_text only "
        "where the node is writable.\n"
        "select picks (selection only); activate runs the default action "
        "(press a button, open a file/list row); context_menu opens a menu, no "
        "menu = OK_UNVERIFIED.\n"
        "Reply: ~ changed, + added, - removed; trust the delta over the status "
        "word. OK_UNVERIFIED = ran, no verified effect; empty delta + OK = no "
        "tree change, inspect. $it binds at AWAIT; earlier use is "
        "INVALID_ARGUMENT. PARTIAL = steps=N/M, failed-at=K (BATCH may say "
        "stopped-at=K), rev=R; not-run = unsent. A find-timeout hit from one "
        "escalated full read is marked (full read).\n"
        "Limits: call 20 statements, run 40 steps, find prints <=50 hit lines, "
        "extra hits fold as `… +N more`; strings 80 chars; read returns a "
        "200-char preview from "
        "<=4096 chars (truncated=true), inspect with start/end reads more. "
        "settle_quiet_ms/settle_poll_ms in ms; find/AWAIT/settle timeouts in "
        "seconds.\n"
        "L0: 40 nodes, depth 3, 300 tokens; a small untruncated viewport "
        "(<=120 nodes) may get 80 nodes, depth 10, 700 tokens. Over a limit, "
        "nodes are omitted and the reply ends with `… +N more`.\n"
        'observe opens with `WINDOWS w1 "QQ" | w2 "..." active` (<=6, then '
        "+N more); active = focused. windows_only returns `WINDOWS count=N`, "
        'then `WINDOW wN "title"` with obs/active flags.\n'
        "Errors: `ERR CODE ref hint=...`. PARSE_ERROR syntax; "
        "TARGET_NOT_FOUND/STALE_TARGET find or observe; TARGET_AMBIGUOUS "
        "refine; CAPABILITY_NOT_FOUND/INVALID_ARGUMENT inspect; "
        "ACTION_NOT_SUPPORTED/PERMISSION_DENIED report; TARGET_DISABLED "
        "precondition; EFFECT_NOT_CONFIRMED/TIMEOUT observe.\n"
        f"Settings at initialize: settle_quiet_ms={quiet} "
        f"settle_poll_ms={poll} settle_timeout_s={timeout} "
        f"post_action_enum={'on' if enum else 'off'}. quiet_ms: settle wait; "
        "smaller = faster, too small can read an animation mid-flight; larger "
        "= safer. timeout_s: caps one settle (settled=false, delta still "
        "reported). post_action_enum=on reports an opened window; off saves "
        "~100ms per call and can hide it. configure(...) sets it; persist=true "
        "writes global config. Critical (!) caps run directly; no confirmation "
        "gate."
    )


def make_mcp_server(session: Session):
    """Create an MCP ``Server`` bound to ``session`` (requires ``mcp>=2``).

    mcp 2.x dropped the ``@server.list_tools()`` / ``@server.call_tool()``
    decorators in favour of constructor-registered ``on_*`` handlers that take
    ``(ctx, params)`` and return full result models.
    """
    try:
        from mcp.server import Server
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("the 'mcp' package (>=2) is required for somnus-mcp") from exc

    return Server(
        "somnusui",
        instructions=settings_brief(session),
        on_list_tools=_make_list_tools(session),
        on_call_tool=_make_call_tool(session),
    )


async def _run_stdio(session: Session) -> None:  # pragma: no cover - runtime
    from mcp.server.stdio import stdio_server

    server = make_mcp_server(session)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    argv = argv if argv is not None else sys.argv[1:]
    provider = argv[0] if argv else "auto"
    session = build_session(provider=provider)
    try:
        asyncio.run(_run_stdio(session))
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
