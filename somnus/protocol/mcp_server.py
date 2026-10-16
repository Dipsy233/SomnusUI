"""MCP server (stdio) exposing SomnusUI's four tools.

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

from somnus.config import load as load_settings
from somnus.protocol.tools import TOOL_SPECS, call_tool
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session


def build_session(**kwargs: Any) -> Session:
    """Build a session with the persistent settings applied.

    A ``policy`` may be passed explicitly (tests, embedding); otherwise one is
    built from the settings file, so the opt-in switches work without a flag.
    """
    from somnus.providers.base import get_provider

    policy = kwargs.pop("policy", None)
    config_path = kwargs.pop("config", None)
    if policy is None:
        settings = load_settings(config_path)
        policy = Policy(
            allow_processes=settings.allow_processes,
            allow_clipboard=settings.allow_clipboard,
            allow_keyboard=settings.allow_keyboard,
        )

    provider = get_provider(kwargs.pop("provider", "auto"), **kwargs)
    return Session(provider, policy=policy)


def make_mcp_server(session: Session):
    """Create an MCP ``Server`` bound to ``session`` (requires ``mcp``)."""
    try:
        from mcp.server import Server
        from mcp.server.lowlevel import NotificationOptions
        from mcp.server.models import InitializationOptions
        from mcp.types import TextContent, Tool
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("the 'mcp' package is required for somnus-mcp") from exc

    server = Server("somnusui")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            )
            for spec in TOOL_SPECS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        text = call_tool(session, name, arguments)
        return [TextContent(type="text", text=text)]

    return server


async def _run_stdio(session: Session) -> None:  # pragma: no cover - runtime
    import mcp
    from mcp.server.lowlevel import NotificationOptions
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server

    server = make_mcp_server(session)
    async with stdio_server() as (read, write):
        init_options = InitializationOptions(
            server_name="somnusui",
            server_version=getattr(mcp, "__version__", "0.0.0"),
            capabilities=server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )
        await server.run(read, write, init_options)


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
