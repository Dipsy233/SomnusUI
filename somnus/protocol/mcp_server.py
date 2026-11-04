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
    """The one-time ``initialize`` note: the timing in force and its trade-offs.

    MCP sends ``instructions`` when the client initializes, so this costs
    nothing per turn. It is generated from the live session, not from the
    defaults, because a settings file may already have changed them.
    """
    settler = getattr(session, "settler", None)
    quiet = getattr(settler, "quiet_ms", DEFAULT_SETTLE_QUIET_MS)
    poll = getattr(settler, "poll_interval_ms", DEFAULT_SETTLE_POLL_MS)
    timeout = getattr(settler, "timeout_s", DEFAULT_SETTLE_TIMEOUT_S)
    enum = bool(getattr(session, "post_action_enum", DEFAULT_POST_ACTION_ENUM))
    return (
        f"Settings in this process: settle_quiet_ms={quiet} "
        f"settle_poll_ms={poll} settle_timeout_s={timeout} "
        f"post_action_enum={'on' if enum else 'off'}. "
        "settle_quiet_ms = how long the UI must stay unchanged before an action "
        "counts as done: smaller = faster calls, too small can read an animation "
        "mid-flight and mistake a still-disabled button for the settled state; "
        "larger = safer but every call waits longer. settle_poll_ms = probe "
        "spacing. settle_timeout_s = hard cap for one settle (the reply then "
        "says settled=false and still reports the delta). "
        "post_action_enum=on reports a window the action opened; off saves ~100ms "
        "per call and can hide it. Change with configure({...}) - session scope; "
        "persist=true also writes the global config. "
        "Every observe reply opens with a WINDOWS line "
        '(`WINDOWS w1 "QQ" ops=12 | w2 "..." active`): ops is that window\'s '
        "printed capability count, active marks the focused one; pick a wN from "
        "it instead of a separate observe-windows call. find accepts "
        "timeout=<seconds> to wait for a match before answering; omit = answer "
        "from the current tree immediately. run takes script (one step per "
        "line, max 40) plus an optional window to focus first: steps are "
        "CALL ref.cap(args), or AWAIT [TYPE] \"name\" [in ref] [timeout=s] "
        "(2s default) which waits for the element an earlier step opened and "
        "binds it to $it (CALL $it.cap(args)). Without an AWAIT the whole "
        "script is validated before the first step runs; with one, each step "
        "settles before the next and the first failure reports the steps that "
        "ran. A critical (!) capability runs directly in every tool - this "
        "build has no confirmation gate. "
        "Output is RUN OK|PARTIAL steps=N/M, one line per step, then the merged "
        "delta."
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
