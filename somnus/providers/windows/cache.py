"""IUIAutomationCacheRequest construction for batched property/pattern fetches.

Fetching one property at a time over COM is the dominant cost in UIA
automation (dev doc section 13). A single cache request + ``BuildUpdatedCache``
turns "one round trip per node per property" into "one round trip per batch".

This module owns the *set* of properties and patterns SomnusUI consumes, so the
provider never requests anything it does not map.
"""

from __future__ import annotations

from typing import Any

#: Properties requested in every cache (id -> portable name).
CACHED_PROPERTIES: dict[int, str] = {
    30005: "name",
    30011: "automation_id",
    30012: "class_name",
    30003: "control_type",
    30045: "value",
    30022: "is_offscreen",
    30010: "is_enabled",
    30008: "has_keyboard_focus",
    30034: "is_selected",
    30042: "toggle_state",
    30053: "expand_collapse_state",
    30046: "is_readonly",
    30004: "description",
    # The element's own Win32 HWND (0 when it maps none). Needed because the
    # WM_SETTEXT / EM_REPLACESEL write channel is gated on *this* value, not on
    # whether the process owns an Edit child window. See
    # docs/interface-capability-report.md 2.9.3 and NativeRef.native_window_handle.
    30020: "native_window_handle",
}

#: Patterns requested in every cache (id -> portable name).
CACHED_PATTERNS: dict[int, str] = {
    10000: "invoke",
    10001: "selection",
    10002: "value",
    10003: "rangevalue",
    10004: "scroll",
    10005: "expandcollapse",
    10006: "grid",
    10007: "griditem",
    10010: "selectionitem",
    10011: "tableitem",
    10012: "table",
    10014: "text",
    10015: "toggle",
    10016: "scrollitem",
    10017: "window",
    10018: "legacyiaccessible",
}


def build_cache_request(automation: Any, UIA: Any) -> Any:
    """Create a cache request pulling all properties/patterns we consume.

    ``TreeScope_Subtree`` + a depth clamp means the whole window is fetched in a
    bounded number of round trips.
    """
    req = automation.CreateCacheRequest()
    for prop_id in CACHED_PROPERTIES:
        req.AddProperty(prop_id)
    for pattern_id in CACHED_PATTERNS:
        req.AddPattern(pattern_id)
    req.TreeScope = UIA.TreeScope_Subtree
    req.AutomationElementMode = UIA.AutomationElementMode_Full
    return req
