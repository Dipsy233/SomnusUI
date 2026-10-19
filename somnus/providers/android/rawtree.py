"""Portal JSON -> RawTree conversion.

The Portal app exports a whole-screen snapshot in one batch (SomnusDroid doc
§4.3). This module is the only place that knows the wire format; everything
downstream sees ordinary :class:`RawNode` trees.

Wire shape::

    {
      "display": 0,
      "windows": [
        {"id": 12, "type": "application", "pkg": "com.android.settings",
         "title": "设置", "active": true,
         "root": {"key": "12/0", "cls": "...", "text": null, "desc": null,
                  "rid": "android:id/title_container",
                  "actions": ["CLICK", "LONG_CLICK"], "enabled": true,
                  "bounds": [0, 420, 1080, 600], "editable": false,
                  "password": false, "checked": null, "selected": false,
                  "collection": null, "item": null, "range": null,
                  "children": [...]}}
      ]
    }

Design rules carried over from the Windows side:

* identity (keys, resource ids, class names, bounds) lives only here and in
  the fingerprint; it never reaches the LLM;
* the conversion must not raise on malformed nodes - a skipped node is better
  than a dead snapshot, and a shape the compiler cannot read is reported
  ``UNKNOWN`` downstream, not guessed at.
"""

from __future__ import annotations

from typing import Any

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

# ---------------------------------------------------------------------------
# AccessibilityAction -> portable role / pattern evidence
# ---------------------------------------------------------------------------

#: Actions the Portal reports. Only actions *proven* by the platform map to
#: capabilities (doc §5.2) - ``isClickable`` alone never grants one.
A_CLICK = "CLICK"
A_LONG_CLICK = "LONG_CLICK"
A_SET_TEXT = "SET_TEXT"
A_PASTE = "PASTE"
A_SCROLL_FORWARD = "SCROLL_FORWARD"
A_SCROLL_BACKWARD = "SCROLL_BACKWARD"
A_SCROLL_LEFT = "SCROLL_LEFT"
A_SCROLL_RIGHT = "SCROLL_RIGHT"
A_SCROLL_TO_POSITION = "SCROLL_TO_POSITION"
A_SHOW_ON_SCREEN = "SHOW_ON_SCREEN"
A_EXPAND = "EXPAND"
A_COLLAPSE = "COLLAPSE"
A_DISMISS = "DISMISS"
A_IME_ENTER = "IME_ENTER"
A_SELECT = "SELECT"
A_CLEAR_SELECTION = "CLEAR_SELECTION"
A_SET_PROGRESS = "SET_PROGRESS"
A_FOCUS = "FOCUS"
A_NEXT = "ACTION_NEXT_AT_MOVEMENT_GRANULARITY"

#: Action names -> the portable pattern vocabulary the compiler reads.
#: ``set_text`` needs either the action or ``editable`` (a plain ``EditText``
#: publishes ACTION_SET_TEXT; some editable Compose fields only say
#: ``isEditable``), so both are normalized to the same pattern.
ACTION_TO_PATTERN: dict[str, str] = {
    A_CLICK: "invoke",
    A_LONG_CLICK: "long_click",
    A_SET_TEXT: "value",
    A_PASTE: "paste",
    A_SCROLL_FORWARD: "scroll",
    A_SCROLL_BACKWARD: "scroll",
    A_SCROLL_LEFT: "scroll",
    A_SCROLL_RIGHT: "scroll",
    A_SCROLL_TO_POSITION: "scrollitem",
    A_SHOW_ON_SCREEN: "scrollitem",
    A_EXPAND: "expandcollapse",
    A_COLLAPSE: "expandcollapse",
    A_DISMISS: "dismiss",
    A_IME_ENTER: "submit",
    A_SELECT: "selectionitem",
    A_CLEAR_SELECTION: "selectionitem",
    A_SET_PROGRESS: "rangevalue",
}

#: Android className fragments -> portable roles. Checked in order; the first
#: match wins. Compose/Flutter expose ``android.view.View`` for everything, so
#: class-name matching is deliberately a *fallback*: patterns and semantic
#: properties decide first (doc §5.1).
CLASS_ROLE_RULES: tuple[tuple[str, str], ...] = (
    ("edittext", "input"),
    ("textview", "text"),
    ("button", "button"),
    ("imagebutton", "button"),
    ("checkbox", "checkbox"),
    ("checkedtextview", "checkbox"),
    ("radiobutton", "radio"),
    ("switch", "switch"),
    ("togglebutton", "switch"),
    ("seekbar", "slider"),
    ("ratingbar", "slider"),
    ("spinner", "select"),
    ("viewpager", "tab"),
    ("tablayout", "tab"),
    ("tabwidget", "tab"),
    ("scrollview", "pane"),
    ("recyclerview", "list"),
    ("listview", "list"),
    ("gridview", "table"),
    ("viewflipper", "pane"),
    ("webview", "document"),
    ("videoview", "unknown"),
    ("imageview", "image"),
    ("textclock", "text"),
    ("linearlayout", "group"),
    ("framelayout", "group"),
    ("relativelayout", "group"),
    ("coordinatorlayout", "group"),
    ("constraintlayout", "group"),
    ("viewgroup", "group"),
    ("view", "group"),
    ("dialog", "dialog"),
)


def role_for(cls: str | None, actions: set[str], editable: bool) -> str:
    """Map an Android className to the portable role.

    Patterns win over class names (Compose reports ``android.view.View`` for
    everything, so the class name is nearly meaningless there): an element
    with SET_TEXT is an input regardless of what it calls itself, and one with
    only SCROLL_* is a pane.
    """
    if editable or A_SET_TEXT in actions:
        return "input"
    lowered = (cls or "").lower()
    if A_SCROLL_FORWARD in actions or A_SCROLL_BACKWARD in actions:
        # A scrollable that the class names as a list-ish container keeps that
        # role; otherwise it is an anonymous scroll pane.
        for fragment, role in CLASS_ROLE_RULES:
            if fragment in lowered and role in ("list", "table"):
                return role
        return "pane"
    # The generic ``view`` fragment would swallow ``android.view.View`` - the
    # shape of a Canvas / Compose surface nothing can be read from - so it is
    # checked before the loop and reported ``unknown``: the LLM-facing line
    # then renders ``? ... reason=visual`` (doc §1.2 fail-loud) instead of
    # masquerading as an addressable group.
    if lowered in ("android.view.view", "view", ""):
        return "unknown"
    for fragment, role in CLASS_ROLE_RULES:
        if fragment in lowered:
            return role
    return "unknown"


# ---------------------------------------------------------------------------
# node conversion
# ---------------------------------------------------------------------------


def _parse_key(key: str | None, window_id: str) -> tuple[int, ...]:
    """``"12/0.3.1"`` -> ``(12, 0, 3, 1)``.

    The key is the Portal's stable path for the node within its window; it is
    what makes ``runtime_id`` survive a re-render the way UIA's RuntimeId does.
    A malformed key falls back to a hash so identity is still distinct - the
    fingerprint, not the id, is what the registry trusts.
    """
    if not key:
        return (0,)
    try:
        body = key.split("/", 1)[1] if "/" in key else key
        # The Portal emits slash-separated segments ("6642/0/0"); the dotted
        # form ("12/0.3.1", doc §4.3) is accepted too. Both canonicalise to
        # the same tuple so host and Portal can never disagree on identity.
        return tuple(int(p) for p in body.replace("/", ".").split("."))
    except (ValueError, IndexError):
        return (abs(hash(key)) % 1_000_000,)


def convert_node(data: dict[str, Any], window_id: str) -> RawNode | None:
    """One Portal JSON node -> RawNode (recursing into ``children``).

    Returns None only for entries that are not dictionaries - a broken child
    is skipped, not fatal.
    """
    if not isinstance(data, dict):
        return None

    key = data.get("key")
    runtime_id = _parse_key(key, window_id)
    cls = data.get("cls")
    actions = set(data.get("actions") or ())
    editable = bool(data.get("editable"))
    patterns = frozenset(
        p for p in (ACTION_TO_PATTERN.get(a) for a in actions) if p
    )
    if editable:
        patterns = patterns | {"value"}

    role = role_for(cls, actions, editable)

    # States: only what the platform reports. ``enabled`` is the default and
    # never carried; ``checked`` is tri-state (None = not checkable).
    states: set[str] = set()
    if not data.get("enabled", True):
        states.add("disabled")
    if data.get("password"):
        states.add("password")
    if data.get("selected"):
        states.add("selected")
    if data.get("focused"):
        states.add("focused")
    # The view's own clickable claim, independent of the action list. Normally
    # ACTION_CLICK rides along with it (-> invoke -> activate); when an app
    # publishes one without the other, the compiler grants `tap` on exactly
    # this evidence instead of guessing from role or shape.
    if data.get("clickable"):
        states.add("clickable")
    if data.get("long_clickable"):
        states.add("long_clickable")
    checked = data.get("checked")
    if checked is True:
        states.add("checked")
    elif checked is False and _is_checkable(data):
        states.add("unchecked")
    if data.get("offscreen") or _offscreen(data.get("bounds")):
        states.add("offscreen")

    # Editable fields carry their current content as a value (the placeholder
    # lives in ``hint``): an input's value is what ``set_text`` would replace.
    value = data.get("text")
    if value is None:
        value = data.get("hint")
    # A password field's content never leaves the Portal (doc §9): report it
    # as filled/unfilled so the agent can still tell what state it is in.
    if data.get("password") and data.get("text"):
        value = "•••"

    node = RawNode(
        native=NativeRef(
            window_id=window_id,
            runtime_id=runtime_id,
            automation_id=data.get("rid"),
            class_name=cls,
            control_type=role,
            path=runtime_id,
            # The identity the Portal verifies before acting on a key: after
            # a re-render the key's path can land on a different node, and
            # only cls+label distinguish "the element I snapshotted" from
            # "whatever sits at that index now".
            label=(data.get("desc") or data.get("text") or None),
        ),
        role=role,
        # Android's ``text`` IS the visible label: it becomes ``name`` (the
        # LLM-facing string), while ``contentDescription`` is the richer
        # accessibility name when present. ``hint`` only fills in when there
        # is no text, so an empty EditText shows its placeholder.
        name=data.get("desc") or data.get("text") or None,
        value=None,
        description=data.get("state_desc") or data.get("pane_title"),
        states=frozenset(states),
        patterns=patterns,
        children=[],
    )
    if data.get("desc") and data.get("text"):
        # Both present: the text is the value the user sees, the desc the
        # canonical name.
        node.value = data.get("text")

    # Range info (sliders): carried as a numeric value the projection renders.
    rng = data.get("range")
    if isinstance(rng, dict) and rng.get("value") is not None:
        node.value = str(rng.get("value"))
        node.description = (
            f"range {rng.get('min', 0)}..{rng.get('max', 100)}"
            if node.description is None
            else node.description
        )

    collection = data.get("collection")
    if isinstance(collection, dict):
        count = collection.get("count")
        # count=-1 means "unknown row count" in the Portal wire format; -1 also
        # marks the whole node virtualised, the compiler's convention.
        if count in (None, -1) or collection.get("virtualized"):
            node.virtual_count = -1
    item = data.get("item")
    if isinstance(item, dict) and item.get("row") is not None:
        node.description = (
            f"row {item['row']}"
            if node.description is None
            else f"{node.description} row {item['row']}"
        )

    for child in data.get("children") or ():
        if not isinstance(child, dict):
            # A null placeholder: a live child the Portal skipped as inert.
            # It occupies a key-path slot and carries nothing else.
            continue
        converted = convert_node(child, window_id)
        if converted is not None:
            node.children.append(converted)
    return node


def _is_checkable(data: dict[str, Any]) -> bool:
    """Whether a ``checked: false`` node is a checkable thing at all.

    The Portal reports ``checked`` as null on non-checkable nodes; ``false``
    on a checkable one. A defensive shape check keeps a sloppy Portal from
    minting ``unchecked`` flags on every TextView.
    """
    return "checked" in data and data.get("checked") is False and (
        data.get("checkable") or A_CLICK in (data.get("actions") or ())
    )


def _offscreen(bounds: Any) -> bool:
    """A node whose bounds are empty or degenerate is not on screen."""
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return False
    l, t, r, b = bounds
    return r <= l or b <= t


# ---------------------------------------------------------------------------
# tree conversion
# ---------------------------------------------------------------------------


def portal_json_to_tree(snapshot: dict[str, Any]) -> list[RawTree]:
    """A Portal ``snapshot`` reply -> one RawTree per window.

    The Windows provider returns one tree per window; the session picks the
    target. Android's equivalent of "a window" is one interactive window of
    one display, so a snapshot of the screen yields several.
    """
    trees: list[RawTree] = []
    display = snapshot.get("display", 0)
    for win in snapshot.get("windows") or ():
        if not isinstance(win, dict):
            continue
        window_id = f"{display}/{win.get('id', 0)}"
        root = win.get("root")
        if not isinstance(root, dict):
            continue
        node = convert_node(root, window_id)
        if node is None:
            continue
        trees.append(
            RawTree(
                window=WindowInfo(
                    window_id=window_id,
                    title=win.get("title") or win.get("pkg") or "",
                    process=win.get("pkg") or "",
                    pid=int(win.get("pid") or 0),
                    active=bool(win.get("active")),
                    visible=bool(win.get("visible", True)),
                ),
                root=node,
                raw_revision=int(snapshot.get("rev") or 1),
            )
        )
    return trees


def portal_json_to_windows(snapshot: dict[str, Any]) -> list[WindowInfo]:
    """Window metadata only (for ``list_windows`` without a full snapshot)."""
    display = snapshot.get("display", 0)
    out: list[WindowInfo] = []
    for win in snapshot.get("windows") or ():
        if not isinstance(win, dict):
            continue
        out.append(
            WindowInfo(
                window_id=f"{display}/{win.get('id', 0)}",
                title=win.get("title") or win.get("pkg") or "",
                process=win.get("pkg") or "",
                pid=int(win.get("pid") or 0),
                active=bool(win.get("active")),
                visible=bool(win.get("visible", True)),
            )
        )
    return out
