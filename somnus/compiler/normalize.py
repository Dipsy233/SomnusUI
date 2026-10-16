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


def normalize_role(
    raw_role: str,
    has_name: bool,
    patterns: frozenset[str],
) -> str:
    """Map a provider role string to a portable role.

    ``custom``/``groupless`` nodes are disambiguated using their patterns: an
    unnamed custom control with an Invoke pattern is a button; one with a Value
    pattern is an input; otherwise it stays ``group`` if named else ``unknown``.

    A ``document`` is mapped to ``text`` unless it publishes a Value pattern.
    Whether a document that lacks one is nevertheless editable is decided by
    **structure** in :func:`_looks_like_editor`, not here: a class name would be
    a naming convention rather than a capability, and this compiler only accepts
    a capability the platform actually published.
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
        # A Value pattern is proof; without one the node is provisionally text
        # and `normalize()` re-examines it as a typing surface.
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
    raw_role = node.role
    role = normalize_role(node.role, bool(node.name), node.patterns)
    # A self-drawn typing surface is promoted to `input` so it can be written.
    # The evidence is the control's **class name**, and that is deliberate: a
    # self-drawn editor (QQ/豆包's ProseMirror composer, CodeMirror, Monaco, a
    # terminal) publishes *nothing* the platform can use - that is the whole
    # problem - so the only thing left to recognise it by is what the toolkit
    # calls itself. Role and patterns cannot tell it from a read-only document
    # or a toolbar, both of which are also leaf-ish nodes with a Text interface.
    #
    # Falling back to structure instead would be a guess, and the guess fails in
    # both directions: a PDF viewer and a `RichTextBox` look like editors and
    # are not always writable, while a toolbar is a container that is not a
    # typing surface at all. This project's rule is that a capability must be
    # proof-able; class names are the one honest signal here, they never leave
    # the runtime, and an editor with an unknown class name is simply not
    # recognised - which is reported, not guessed at.
    if (
        raw_role.lower() in _EDITOR_CANDIDATE_ROLES
        and _looks_like_editor(node)
    ):
        role = "input"
    node.role = role
    node.states = normalize_states(node.states)
    for child in node.children:
        normalize(child)
    return node


#: Class names of editors whose *name* is a well-known giveaway. This is how a
#: self-drawn typing surface is recognised at all: it publishes no usable
#: interface (that is what makes it self-drawn), so its class name is the only
#: evidence left. Rich-text toolkits (ProseMirror, CodeMirror, Monaco, tiptap,
#: xterm) and the platform editors that behave the same way for our purposes
#: (``RichEdit``/``RichEditD2DPT``) are listed together.
#:
#: A class name is a naming convention rather than a platform guarantee, so it
#: is used for exactly one thing - granting ``set_text`` - and the routes that
#: capability resolves to are the ones the element really has. An editor whose
#: class name is not here is left as a ``group``/``text`` and treated as
#: unwritable, because the runtime genuinely cannot tell what it is.
_EDITOR_CLASS_RE = re.compile(
    r"prosemirror|codemirror|monaco|tiptap|textarea|text-editor|editor\b|"
    r"contenteditable|rich-?text|richedit|ace_editor|xterm|terminal",
    re.IGNORECASE,
)

#: Roles that become ``input`` when they are really a typing surface.
_EDITOR_CANDIDATE_ROLES: frozenset[str] = frozenset(
    {"group", "pane", "document", "unknown"}
)


def _looks_like_editor(node: RawNode) -> bool:
    """Whether a node the platform did not call ``input`` is really an editor.

    Recognised by **class name**. That is the only honest signal available: a
    self-drawn editor is exactly a control that publishes no usable interface
    (otherwise it would already be an ``input``), so there is no pattern to
    consult. Role cannot do it either - a read-only document and a writable
    rich-text box are both leaf-ish nodes with a Text interface - and structure
    cannot do it, because a toolbar is also a container with a Text interface.

    The class name is a naming convention, which is weaker evidence than a
    platform pattern, so it is used for exactly this one promotion and nothing
    else: it grants ``set_text`` to a control whose *only* remaining write
    routes are the clipboard and the keyboard, which are off by default and
    reported when refused. An editor whose class name is not recognised stays a
    ``group``/``text`` and is treated as unwritable - the honest answer, since
    the runtime genuinely cannot tell what it is.
    """
    cls = node.native.class_name or ""
    if not cls:
        return False
    if node.role not in _EDITOR_CANDIDATE_ROLES:
        return False
    # A container with named interactive children is a toolbar/panel, not an
    # editor; an editor presents itself as a single typing surface.
    #
    # Children are still *raw* here (this runs before `normalize` recurses), so
    # the check accepts both spellings of "interactive": the raw role and the
    # role it will normalise to. Testing only the raw spelling would let a
    # toolbar containing an `edit` slip through as an editor.
    if any(
        c.role in _INTERACTIVE_CHILD_ROLES
        or ROLE_ALIASES.get(c.role.lower(), c.role.lower()) in _INTERACTIVE_CHILD_ROLES
        for c in node.children
    ):
        return False
    return _EDITOR_CLASS_RE.search(cls) is not None


#: Child roles that prove a container is a panel/toolbar rather than a single
#: typing surface. Listed in portable spelling; raw spellings are mapped through
#: ``ROLE_ALIASES`` at the call site.
_INTERACTIVE_CHILD_ROLES: frozenset[str] = frozenset(
    {"button", "input", "menuitem", "tab", "link", "checkbox", "radio", "switch"}
)
