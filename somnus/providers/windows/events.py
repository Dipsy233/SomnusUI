"""UIA event subscription.

Real UIA events require a handler COM object and a message pump on the client
thread. v0.1 keeps polling as the reliable path (``Session``'s ``event_source``
is an event counter), and exposes this module so a future implementation can
drop in without touching the runtime.

When events are unavailable, ``revision`` falls back to polling with a quiet
window (``runtime/settle.py``) - the delta is still correct, only later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from somnus.ir.action import UiEvent

log = logging.getLogger("somnus.uia.events")

#: UIA event ids we care about (portable name -> UIA_*EventId).
EVENT_IDS = {
    "structure": 20002,  # UIA_StructureChangedEventId
    "property": 20005,  # UIA_PropertyChangedEventId
    "focus": 20005,  # focus changes arrive as PropertyChanged(HasKeyboardFocus)
    "window_open": 20016,  # UIA_Window_WindowOpenedEventId
    "window_close": 20017,
}


@dataclass
class EventCounter:
    """A monotonic counter the Settler polls; incremented by event handlers."""

    count: int = 0

    def bump(self) -> None:
        self.count += 1

    def __call__(self) -> int:
        return self.count


def make_handler(on_event: Callable[[UiEvent], None], counter: EventCounter) -> Any:
    """Construct a UIA event handler (requires ``comtypes`` + Windows).

    Returns ``None`` when the platform cannot provide handlers, in which case the
    runtime uses polling.
    """
    try:
        import comtypes
        from comtypes.gen import UIAutomationClient as UIA
    except Exception:  # pragma: no cover - non-Windows
        return None

    class _Handler(comtypes.COMObject):
        _com_interfaces_ = [UIA.IUIAutomationStructureChangedEventHandler]

        def HandleStructureChangedEvent(self, sender, change_type, runtime_id):  # noqa: N802
            counter.bump()
            on_event(UiEvent(window_id="", kind="structure", native=runtime_id))

    return _Handler()
