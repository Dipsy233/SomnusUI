"""IUIAutomationCacheRequest construction for batched property/pattern fetches.

Fetching one property at a time over COM is the dominant cost in UIA
automation. A single cache request + ``BuildUpdatedCache``
turns "one round trip per node per property" into "one round trip per batch".

This module owns the *set* of properties and patterns SomnusUI consumes, so the
provider never requests anything it does not map.

Three tiers exist, because the *set* (not the walk) is what the provider pays
for - a ``TreeScope_Subtree`` request materialises every descendant up front,
so narrowing the walk saves nothing and narrowing the request saves a lot:

``full``
    what the compiler consumes (14 properties + 16 patterns). Default.
``lean``
    ``full`` minus the four patterns nothing reads (selection/grid/table/
    tableitem). An experiment, not the default: it touches the resolver's view
    of what an element can do.
``sig``
    only what ``settle``'s signature compares (role/name/value/child-count/
    states). Used exclusively by the cheap probe, never by a compiled tree.

The tier is chosen by ``SOMNUS_CACHE_TIER`` so the comparison can be run on the
same window without changing code; the default keeps the previous behaviour.
"""

from __future__ import annotations

import os
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
    # docs/text-input-channels.md 3.2 and NativeRef.native_window_handle.
    30020: "native_window_handle",
    # Screen rectangle. Read only by the viewport capture (docs/viewport-capture
    # -proposal.md): `IsOffscreen` is not trustworthy for scrolled-out rows
    # (measured on this build's WPF store list: 0 of 240 rows reported offscreen
    # while only 12 were on screen), so visibility is "rect intersects the
    # window". Costs ~+6% on one full snapshot, which buys the viewport capture.
    30001: "bounding_rect",
}

#: The narrow "row peek" set: enough to decide whether a collection row is on
#: screen and to print its one-line summary, and nothing else. Patterns are
#: deliberately absent - the whole point is that a 240-row list costs ~194ms to
#: peek instead of ~477ms to read in full, and only rows that turn out visible
#: are then read completely.
PEEK_PROPERTIES: dict[int, str] = {
    30005: "name",
    30003: "control_type",
    30010: "is_enabled",
    30001: "bounding_rect",
}

#: Rectangle only: the probe's row-visibility scan does not need names or types
#: (the rows it keeps are read with the probe profile, which has them).
RECT_PROPERTIES: dict[int, str] = {
    30001: "bounding_rect",
}

#: The layered *probe* profile is defined below ``SIGNATURE_PROPERTIES`` (it is
#: that set plus the rectangle); see ``PROBE_PROPERTIES``.

#: The UIA Selection pattern id. The snapshot reads a *selection container's*
#: current selection through it (``uia._current_selection_label``) because a
#: provider may implement SelectionPattern on the container only, without
#: exposing per-item ``IsSelected`` - the fallback that makes a combo box's
#: value readable when its rows do not report ``selected``.
SELECTION_PATTERN_ID = 10001

#: Patterns requested in every cache (id -> portable name).
CACHED_PATTERNS: dict[int, str] = {
    10000: "invoke",
    SELECTION_PATTERN_ID: "selection",
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

#: Patterns the ``lean`` tier drops: nothing in the runtime reads any of these
#: (the select/table work that would consumes them is a separate change).
LEAN_DROPPED_PATTERNS: frozenset[int] = frozenset({10001, 10006, 10011, 10012})

#: Each signature state and the cached property it is derived from. The ``sig``
#: tier must contain every id in here, or a state flip could be invisible to the
#: probe while a full snapshot still reports it - the premature-settled failure
#: mode. Kept as data so the guarantee is testable rather than a comment.
SIGNATURE_STATE_PROPERTIES: dict[str, int] = {
    "disabled": 30010,
    "offscreen": 30022,
    "focused": 30008,
    "selected": 30034,
    "checked": 30042,
    "unchecked": 30042,
    "expanded": 30053,
    "collapsed": 30053,
    "readonly": 30046,
}

#: Properties the signature probe requests: the structural fields
#: (role/name/value) plus every state source above.
SIGNATURE_PROPERTIES: dict[int, str] = {
    30003: "control_type",
    30005: "name",
    30045: "value",
    **{pid: CACHED_PROPERTIES[pid] for pid in sorted(set(SIGNATURE_STATE_PROPERTIES.values()))},
}

#: Patterns the signature probe requests. ``value`` is here even though the
#: signature does not compare patterns directly: the ``readonly`` state is only
#: reported for a node that also exposes the value pattern, so omitting it would
#: make the probe disagree with a full snapshot on a read-only text field.
SIGNATURE_PATTERNS: dict[int, str] = {
    10002: "value",
}

#: The layered *probe* profile: exactly what the settle signature compares -
#: the signature fields, every state source, and the rectangle used to decide
#: visibility. Patterns are limited to the value pattern because it is the one
#: that takes part in the ``readonly`` state; the probe tree is never compiled,
#: so no capability needs the others.
PROBE_PROPERTIES: dict[int, str] = {
    **SIGNATURE_PROPERTIES,
    30001: "bounding_rect",
}

CACHE_TIER_ENV = "SOMNUS_CACHE_TIER"
CACHE_TIERS = ("full", "lean", "sig", "peek", "rect", "probe")


def cache_tier_from_env(default: str = "full") -> str:
    """The tier named by ``SOMNUS_CACHE_TIER`` (default ``full``)."""
    raw = os.environ.get(CACHE_TIER_ENV, "").strip().lower()
    return raw if raw in CACHE_TIERS else default


def properties_for(tier: str) -> dict[int, str]:
    if tier == "sig":
        return dict(SIGNATURE_PROPERTIES)
    if tier == "probe":
        return dict(PROBE_PROPERTIES)
    if tier == "peek":
        return dict(PEEK_PROPERTIES)
    if tier == "rect":
        return dict(RECT_PROPERTIES)
    return dict(CACHED_PROPERTIES)


def patterns_for(tier: str) -> dict[int, str]:
    if tier in ("sig", "probe"):
        return dict(SIGNATURE_PATTERNS)
    if tier in ("peek", "rect"):
        return {}
    if tier == "lean":
        return {
            pid: name
            for pid, name in CACHED_PATTERNS.items()
            if pid not in LEAN_DROPPED_PATTERNS
        }
    return dict(CACHED_PATTERNS)


def build_cache_request(automation: Any, UIA: Any, tier: str = "full") -> Any:
    """Create a cache request pulling the properties/patterns of ``tier``.

    ``TreeScope_Subtree`` is deliberate and is what makes the walk cheap: the
    snapshot calls ``BuildUpdatedCache`` **once**, on the window root, and then
    reads descendants through ``GetCachedChildren``. One round trip for the
    whole window.

    The scope and the walk must stay in step. If the walker ever goes back to
    calling ``BuildUpdatedCache`` per element (or to ``FindAll`` for children),
    a Subtree scope means every element re-fetches its entire subtree: measured
    live on a 1467-node window, that is 14.4s instead of 2.5s for a byte-identical
    tree. See ``UiaProvider._build_node``.
    """
    if tier not in CACHE_TIERS:
        raise ValueError(f"unknown cache tier {tier!r}; expected one of {CACHE_TIERS}")
    req = automation.CreateCacheRequest()
    for prop_id in properties_for(tier):
        req.AddProperty(prop_id)
    for pattern_id in patterns_for(tier):
        req.AddPattern(pattern_id)
    req.TreeScope = UIA.TreeScope_Subtree
    req.AutomationElementMode = UIA.AutomationElementMode_Full
    return req
