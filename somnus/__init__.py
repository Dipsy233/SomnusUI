"""SomnusUI: compile native GUIs into a low-token semantic API for LLM agents.

Public entry points live in :mod:`somnus.runtime` (session) and
:mod:`somnus.protocol` (MCP server). The compiler, projection and IR layers are
pure and deterministic; nothing in this package imports an LLM.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
