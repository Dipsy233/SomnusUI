"""Normalization: native role/state/pattern spelling -> portable vocabulary.

Providers already do a first mapping (UIA control type -> portable role), but
this stage is the contract boundary: it guarantees the compiler only ever sees
the values listed here, regardless of provider quirks.
"""

from __future__ import annotations

import re

from somnus.ir.node import RawNode

#: Portable primitive roles (dev doc section 4.3).
ROLES: frozenset[str] = frozenset(
    {
        "button",
        "input",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "select",
        "menu",
        "menuitem",
        "tab",
        "tree",
        "treeitem",
        "list",
        "listitem",
        "table",
        "row",
        "link",
        "text",
        "image",
        "canvas",
        "group",
        "pane",
        "window",
        "dialog",
        "document",
        "toolbar",
        "statusbar",
        "progressbar",
        "separator",
        "unknown",
    }
)

#: Structural layers assigned by the compiler, never by providers.
LAYERS: frozenset[str] = frozenset({"app", "window", "screen", "region", "dialog"})

#: Native role spelling -> portable role.
ROLE_ALIASES: dict[str, str] = {
    "edit": "input",
    "textbox": "input",
    "textfield": "input",
    "text_edit": "input",
    "combobox": "select",
    "dropdown": "select",
    "radiobutton": "radio",
    "splitbutton": "button",
    "hyperlink": "link",
    "dataitem": "listitem",
    "datagrid": "table",
    "tabitem": "tab",
    "menubar": "menu",
    "menuitem": "menuitem",
    "tooltip": "text",
    # An editable document (Notepad's RichEditD2DPT editor, Word docs in
    # UIA) exposes a Value pattern and takes text: it is an input, not a
    # text label. Disambiguated in normalize_role below.
    "document": "document",
    "header": "row",
    "headeritem": "row",
    "appbar": "toolbar",
    "titlebar": "pane",
    "thumb": "slider",
    "spinner": "input",
    "calendar": "group",
    "custom": "groupless",  # sentinel, resolved below
    "semanticzoom": "group",
    "unknown": "unknown",
}

#: Native state spelling -> portable state.
STATE_ALIASES: dict[str, str] = {
    "unavailable": "disabled",
    "focusable": "focusable",
    "keyboardfocused": "focused",
    "focused": "focused",
    "invisible": "offscreen",
    "collapsed": "collapsed",
    "checked": "checked",
    "unchecked": "unchecked",
    "indeterminate": "mixed",
    "readonly": "readonly",
    "busy": "loading",
    "expanded": "expanded",
    "selected": "selected",
    "selectable": "selectable",
    "offscreen": "offscreen",
    "disabled": "disabled",
    "enabled": "enabled",
    "required": "required",
    "invalid": "invalid",
    "modal": "modal",
    "active": "active",
}

#: States that are never rendered (defaults, or purely internal).
SILENT_STATES: frozenset[str] = frozenset({"enabled", "focusable", "selectable"})

#: Which states are meaningful for which role (drives flag rendering).
ROLE_STATE_FLAGS: dict[str, frozenset[str]] = {
    "button": frozenset({"disabled", "focused", "active"}),
    "input": frozenset({"disabled", "focused", "readonly", "required", "invalid"}),
    "checkbox": frozenset({"checked", "unchecked", "mixed", "disabled", "focused"}),
    "radio": frozenset({"checked", "unchecked", "disabled", "focused"}),
    "switch": frozenset({"checked", "unchecked", "disabled", "focused"}),
    "listitem": frozenset({"selected", "disabled", "focused"}),
    "tab": frozenset({"selected", "disabled", "focused"}),
    "treeitem": frozenset({"expanded", "collapsed", "selected", "disabled", "focused"}),
    "menuitem": frozenset({"checked", "disabled", "focused"}),
    "slider": frozenset({"disabled", "focused"}),
    "link": frozenset({"disabled", "focused"}),
    "list": frozenset({"loading"}),
    "table": frozenset({"loading"}),
    "progressbar": frozenset({"loading"}),
    "select": frozenset({"disabled", "focused", "expanded", "collapsed"}),
    "group": frozenset({"expanded", "collapsed", "disabled"}),
    "pane": frozenset({"expanded", "collapsed"}),
}

#: Patterns whose absence forces the "unknown" role.
_INTERACTIVE_PATTERNS = frozenset(
    {"invoke", "toggle", "selectionitem", "value", "expandcollapse", "rangevalue"}
)


def normalize_role(raw_role: str, has_name: bool, patterns: frozenset[str]) -> str:
    """Map a provider role string to a portable role.

    ``custom``/``groupless`` nodes are disambiguated using their patterns: an
    unnamed custom control with an Invoke pattern is a button; one with a Value
    pattern is an input; otherwise it stays ``group`` if named else ``unknown``.
    """
    role = (raw_role or "unknown").lower()
    role = ROLE_ALIASES.get(role, role)
    if role == "groupless":
        if "invoke" in patterns:
            return "button"
        if "value" in patterns:
            return "input"
        if "selectionitem" in patterns:
            return "listitem"
        return "group" if has_name else "unknown"
    if role == "document":
        # Read-only documents (PDF views, help pages) are text; editable ones
        # (Notepad's RichEdit editor, word processors) are inputs.
        return "input" if "value" in patterns else "text"
    if role not in ROLES:
        return "group" if has_name else "unknown"
    return role


def normalize_states(states: frozenset[str]) -> frozenset[str]:
    out = set()
    for st in states:
        mapped = STATE_ALIASES.get(st.lower(), st.lower())
        if mapped in SILENT_STATES:
            continue
        out.add(mapped)
    if "disabled" in out:
        out.discard("enabled")
    return frozenset(out)


def is_interactive(role: str, patterns: frozenset[str]) -> bool:
    return bool(patterns & _INTERACTIVE_PATTERNS) or role in {
        "button",
        "input",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "select",
        "menuitem",
        "tab",
        "treeitem",
        "listitem",
        "link",
    }


def normalize(node: RawNode) -> RawNode:
    """Normalize one raw node in place and recurse."""
    role = normalize_role(node.role, bool(node.name), node.patterns)
    # Rich-text editors render as named groups (QQ's ProseMirror message box,
    # CodeMirror, generic "...editor" class names). They take text input and
    # are the primary target of set_text, so expose them as inputs - the class
    # name never leaves the runtime, so this cannot leak to the LLM.
    if role == "group" and _looks_like_editor(node):
        role = "input"
    node.role = role
    node.states = normalize_states(node.states)
    for child in node.children:
        normalize(child)
    return node


_EDITOR_CLASS_RE = re.compile(
    r"prosemirror|codemirror|monaco|textarea|text-editor|editor\b|"
    r"contenteditable|rich-?text|ace_editor|xterm|terminal",
    re.IGNORECASE,
)

#: Roles that become ``input`` when they are really a typing surface.
_EDITOR_CANDIDATE_ROLES: frozenset[str] = frozenset(
    {"group", "pane", "document", "unknown"}
)


def _looks_like_editor(node: RawNode) -> bool:
    """Whether a non-``input`` node is actually a text editor.

    A self-drawn editor (Chromium/Electron rich text, CodeMirror, Monaco, a
    terminal) is exposed by the platform as a plain ``group``/``pane`` with a
    text interface but no Value pattern, so role alone cannot identify it. Two
    signals are combined, class name first (cheap and exact for the well-known
    editors), then structure: a text interface on a leaf-ish focusable node that
    is not a labelled container.
    """
    cls = node.native.class_name or ""
    if cls and _EDITOR_CLASS_RE.search(cls):
        return True
    if node.role not in _EDITOR_CANDIDATE_ROLES:
        return False
    if "text" not in node.patterns or "value" in node.patterns:
        return False
    # A container with named interactive children is a toolbar/panel, not an
    # editor; an editor presents itself as a single typing surface.
    if any(
        c.role in ("button", "input", "menuitem", "tab", "link")
        for c in node.children
    ):
        return False
    return True
