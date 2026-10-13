"""MCP server (stdio) exposing SomnusUI's four tools.

Uses the official ``mcp`` package when installed. Kept separate from
:mod:`somnus.protocol.jsonrpc` so a host can embed either transport.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from somnus.protocol.tools import TOOL_SPECS, call_tool
from somnus.runtime.session import Session


def build_session(**kwargs: Any) -> Session:
    from somnus.providers.base import get_provider

    provider = get_provider(kwargs.pop("provider", "auto"), **kwargs)
    return Session(provider)


def make_mcp_server(session: Session):
    """Create an MCP ``Server`` bound to ``session`` (requires ``mcp``)."""
    try:
        from mcp.server import Server
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
    from mcp.server.stdio import stdio_server

    server = make_mcp_server(session)
    async with stdio_server() as (read, write):
        init_options = None
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
