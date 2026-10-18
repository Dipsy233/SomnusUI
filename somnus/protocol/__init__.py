"""Protocol layer: MCP server + plain JSON-RPC, both over the 4 tools."""

from somnus.protocol.tools import TOOL_SPECS, call_tool

__all__ = ["TOOL_SPECS", "call_tool"]
