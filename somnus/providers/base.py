"""Provider abstraction.

Providers are the *only* platform-specific layer. They:

* enumerate windows,
* return a whole :class:`RawTree` in one batched call (never per-node RPC),
* execute a single native primitive against one native element,
* optionally subscribe to coarse UI events.

All COM/UIA types stay behind this boundary.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, runtime_checkable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawTree, WindowInfo


class Subscription(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class AccessibilityProvider(Protocol):
    name: str

    #: True when snapshots only change as a direct result of an action - i.e.
    #: the application has no thread of its own that can redraw, animate or
    #: finish delivering input after ``execute`` returns.
    #:
    #: The settler uses this to skip its quiet window entirely. On a real
    #: desktop the quiet window is load-bearing: a synthetic channel (clipboard /
    #: keyboard) puts text in the target's *message queue*, so the value is not
    #: there yet when the call returns, and a redraw can change enabled/disabled
    #: state a moment later. Replaying a recorded tree has neither, so waiting
    #: only spends real time to observe the same bytes.
    #:
    #: Absent or False means "assume the UI can move on its own" - the safe
    #: default, which is what a third-party provider should get.
    is_deterministic: bool

    def list_windows(self) -> list[WindowInfo]:
        """Enumerate top-level windows. Must not raise or activate any window."""
        ...

    def peek(self, window_id: str) -> WindowInfo | None:
        """One window's current metadata, raising that window first.

        Optional. Providers that can put a window in the foreground implement
        it so the runtime can refresh the *target* window's title/flags without
        falling back to a full enumeration - and so that the refresh is the
        moment the target is made the window the user is looking at, which is
        what a self-drawn application needs before it renders anything. Without
        it the runtime falls back to ``list_windows``, which raises nothing.
        """
        ...

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 20,
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


def _wire_android_recovery(provider: Any) -> None:
    """Give the Android provider a recovery callback (mobilerun's pattern):
    after consecutive RPC failures, re-enable the accessibility service via
    adb - ROMs like MIUI silently drop it (measured live: the service vanishes
    from `dumpsys accessibility` after an app update or a force-stop, and
    nothing re-enables it)."""
    import subprocess

    service = "dev.somnus.portal/dev.somnus.portal.service.SomnusAccessibilityService"

    def recover() -> None:
        subprocess.run(
            ["adb", "shell", "settings", "put", "secure",
             "enabled_accessibility_services", service],
            check=True, timeout=15, capture_output=True,
        )
        subprocess.run(
            ["adb", "shell", "settings", "put", "secure", "accessibility_enabled", "1"],
            check=True, timeout=15, capture_output=True,
        )
        time.sleep(2)

    provider.recovery = recover


def get_provider(name: str = "auto", **kwargs: Any) -> AccessibilityProvider:
    """Factory resolving a provider by name.

    ``auto`` picks the Windows UIA provider when available, else the mock
    provider (useful for CLI smoke tests on any platform).
    """

    name = (name or "auto").lower()

    # SomnusDroid: the Android provider is registered like any other platform
    # provider. Import is lazy so a Windows-only host never touches it.
    if name in ("android", "droid"):
        from somnus.providers.android.provider import AndroidProvider

        provider = AndroidProvider(**kwargs)
        _wire_android_recovery(provider)
        return provider

    if name in ("auto", "uia", "windows"):
        try:
            from somnus.providers.windows.uia import UiaProvider

            return UiaProvider(**kwargs)
        except Exception:
            if name != "auto":
                raise

    from somnus.providers.mock import MockProvider

    return MockProvider(**kwargs)
