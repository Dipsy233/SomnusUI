"""Provider abstraction (dev doc section 4.1).

Providers are the *only* platform-specific layer. They:

* enumerate windows,
* return a whole :class:`RawTree` in one batched call (never per-node RPC),
* execute a single native primitive against one native element,
* optionally subscribe to coarse UI events.

All COM/UIA types stay behind this boundary.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawTree, WindowInfo


class Subscription(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class AccessibilityProvider(Protocol):
    name: str

    def list_windows(self) -> list[WindowInfo]: ...

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 12,
    ) -> RawTree: ...

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult: ...

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> Subscription: ...

    def close(self) -> None: ...


class NullSubscription:
    """A subscription that never fires - the polling fallback."""

    def close(self) -> None:  # pragma: no cover - trivial
        return None


def get_provider(name: str = "auto", **kwargs: Any) -> AccessibilityProvider:
    """Factory resolving a provider by name.

    ``auto`` picks the Windows UIA provider when available, else CDP when a
    browser debug port is reachable, else the mock provider (useful for CLI
    smoke tests). ``cdp`` forces the browser provider.
    """

    name = (name or "auto").lower()

    if name in ("cdp", "browser", "chrome", "edge"):
        from somnus.providers.cdp import CdpProvider

        return CdpProvider(**kwargs)

    if name in ("auto", "uia", "windows"):
        try:
            from somnus.providers.windows.uia import UiaProvider

            return UiaProvider(**kwargs)
        except Exception:
            if name != "auto":
                raise
        # On a non-Windows host, or when UIA is unavailable, a browser may still
        # be reachable; prefer it over the mock so `somnus observe` does the
        # useful thing without extra flags.
        if name == "auto":
            try:
                from somnus.providers.cdp import CdpProvider, discover_targets

                if discover_targets():
                    return CdpProvider(**kwargs)
            except Exception:
                pass

    from somnus.providers.mock import MockProvider

    return MockProvider(**kwargs)
