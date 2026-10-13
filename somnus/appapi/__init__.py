"""App API channel: reach an application's own interface, not its pixels.

Dev doc section 20 names UFO's "unified GUI-API action layer" as the thing worth
borrowing, and it is emphatically *not* a vision feature: UFO drives Word, Excel,
PowerPoint and the shell through their COM/CLI interfaces, reading and writing
structured data directly.

This module is that channel for SomnusUI. A capability step can name an
``app_api`` action instead of a native primitive, and the runtime routes it to a
registered :class:`AppApiHandler` for the target application. Nothing about the
LLM surface changes: it still calls ``CALL x.do()``; only the resolution of the
last step differs.

Why this matters even though UIA already works:

* **Exactness.** ``get_range_values`` returns the cell values; clicking "copy"
  and reading the clipboard is guesswork. A spreadsheet has a data model, and
  reaching it is both cheaper and more reliable than driving its controls.
* **Breadth.** Some operations have no control at all (set a document's author,
  rename a sheet, run a CLI). UIA cannot express them; the app's API can.
* **Verification.** API calls return values, so an ``Effect`` can be checked
  against real data instead of an observed delta.

Handlers are ordinary Python objects registered by process name, exactly like
packs. The runtime never exposes the handler's parameters to the LLM beyond what
a pack declares, so the same allow-list discipline applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from somnus.ir.action import ExecResult


class AppApiHandler(Protocol):
    """An application-specific interface (COM, CLI, script, ...)."""

    #: Process names this handler serves, lower-case (e.g. ``("excel.exe",)``).
    processes: tuple[str, ...]

    def invoke(self, action: str, params: dict[str, Any]) -> ExecResult:
        """Run one named action and return its result.

        ``ExecResult.value`` carries any structured data the action produced, so
        an observation or an effect can use it directly rather than scraping it
        back out of the UI.
        """
        ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


@dataclass
class AppApiRegistry:
    """Maps a process name to the handler that can speak its native API."""

    _by_process: dict[str, AppApiHandler] = field(default_factory=dict)

    def register(self, handler: AppApiHandler) -> AppApiHandler:
        for process in getattr(handler, "processes", ()):
            self._by_process[process.lower()] = handler
        return handler

    def for_process(self, process: str) -> AppApiHandler | None:
        return self._by_process.get((process or "").lower())

    def processes(self) -> list[str]:
        return sorted(self._by_process)

    def close(self) -> None:
        for handler in set(self._by_process.values()):
            try:
                handler.close()
            except Exception:
                pass
        self._by_process.clear()


#: The session-wide registry. Handlers are registered at import time, mirroring
#: how packs register themselves.
REGISTRY = AppApiRegistry()


def register(handler: AppApiHandler) -> AppApiHandler:
    return REGISTRY.register(handler)


# ---------------------------------------------------------------------------
# a small, dependency-free handler for the Windows shell
# ---------------------------------------------------------------------------


@dataclass
class ShellApiHandler:
    """The shell's own interface: filesystem and process queries.

    Demonstrates the shape without pulling in COM. A real deployment would add
    handlers for the Office suite next to it; the runtime treats them all the
    same.
    """

    processes: tuple[str, ...] = ("explorer.exe",)
    #: Only these actions may be called; anything else is refused.
    allowed: frozenset[str] = frozenset({"list", "stat"})

    def invoke(self, action: str, params: dict[str, Any]) -> ExecResult:
        if action not in self.allowed:
            return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=action)
        from pathlib import Path

        target = str(params.get("path", "."))
        try:
            path = Path(target).expanduser()
            if action == "list":
                if not path.is_dir():
                    return ExecResult(
                        ok=False, error="INVALID_ARGUMENT", message="not a directory"
                    )
                entries = sorted(p.name for p in path.iterdir())[:200]
                return ExecResult(ok=True, value=entries)
            if action == "stat":
                if not path.exists():
                    return ExecResult(
                        ok=False, error="TARGET_NOT_FOUND", message=target
                    )
                st = path.stat()
                return ExecResult(
                    ok=True,
                    value={
                        "name": path.name,
                        "is_dir": path.is_dir(),
                        "size": st.st_size,
                    },
                )
        except OSError as exc:
            return ExecResult(
                ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc)
            )
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=action)

    def close(self) -> None:  # pragma: no cover - nothing to release
        return None


register(ShellApiHandler())
