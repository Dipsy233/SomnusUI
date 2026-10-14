"""Primitive capability derivation.

Every portable capability that is *directly* supported by a native pattern is
created here with ``confidence == 1.0``. Heuristic upgrades (e.g. "this button
in a group probably means Add") happen later in :mod:`somnus.compiler.match`
and must lower confidence (dev doc section 11.2).
"""

from __future__ import annotations

from somnus.ir.capability import (
    SAFE_CAPABILITIES,
    Capability,
    ParamSpec,
    PrimitiveStep,
)
from somnus.ir.node import RawNode

#: role -> implicit capabilities granted purely by semantics (even without a
#: matching native pattern). These are what the SUI-Text default table hides.
ROLE_DEFAULT_CAPS: dict[str, tuple[str, ...]] = {
    "button": ("activate",),
    "link": ("activate",),
    "menuitem": ("activate",),
    "listitem": ("select",),
    "tab": ("select",),
    "treeitem": ("select", "expand"),
    "checkbox": ("toggle",),
    "switch": ("toggle",),
    "radio": ("select",),
    "input": ("set_text",),
    "slider": ("increment", "decrement"),
    "select": ("select", "expand"),
    "list": ("scroll",),
    "tree": ("scroll",),
    "table": ("scroll",),
    "pane": ("scroll",),
}


def _cap(name: str, params: dict[str, ParamSpec] | None = None) -> Capability:
    risk = "safe" if name in SAFE_CAPABILITIES else "normal"
    return Capability(
        name=name,
        level="primitive",
        params=params or {},
        via=[PrimitiveStep(primitive=name, target="$self")],
        risk=risk,  # type: ignore[arg-type]
    )


#: Roles that are inherently text-entry surfaces.
_TEXT_ENTRY_ROLES: frozenset[str] = frozenset({"input", "select", "slider"})


def _prefers_keyboard_entry(node: RawNode) -> bool:
    """Whether ``set_text`` should type rather than use the Value pattern.

    Rich-text and document editors advertise a Value pattern they do not honour
    (Windows 11 Notepad discards `SetValue`; Chromium editors have none but take
    keystrokes). For them the keyboard is the only reliable route. A plain form
    field keeps the native path, which is faster and does not depend on focus.
    """
    if node.role == "document":
        return True
    # The native class/control type still says "document" even after the role was
    # normalised to `input` (Notepad's RichEditD2DPT editor). A rich-text editor
    # advertises a Value pattern it does not honour, so it must be typed into.
    native_type = f"{node.native.class_name or ''} {node.native.control_type or ''}".lower()
    if "document" in native_type or "richedit" in native_type:
        return True
    cls = (node.native.class_name or "").lower()
    if any(
        token in cls
        for token in (
            "richedit",
            "prosemirror",
            "codemirror",
            "monaco",
            "editor",
            "contenteditable",
            "memo",
            "terminal",
            "xterm",
        )
    ):
        return True
    # A rich-text edit control is recognised by its multi-line/scrollable shape
    # plus a text interface; a one-line form field is not.
    return "text" in node.patterns and "scroll" in node.patterns and node.role == "input"


def _is_text_entry(node: RawNode) -> bool:
    """Whether ``node`` should advertise ``set_text``.

    True when the node is a known text-entry role, or when it is a focusable,
    enabled surface that either has a writable Value pattern or exposes a text
    interface it can be typed into. The three exclusions (readonly, disabled,
    offscreen) are the cases where typing would silently do nothing.
    """
    if "readonly" in node.states or "disabled" in node.states:
        return False
    if "offscreen" in node.states:
        return False
    if node.role in _TEXT_ENTRY_ROLES:
        return True
    # A writable Value pattern on a non-input role (an editable Document, an
    # editable custom control) still means "this takes text". A plain `text`
    # node is a *label* whose Value pattern is its displayed string, so it must
    # never be treated as an input.
    if "value" in node.patterns:
        return node.role in ("pane", "document", "group", "unknown")
    # No Value pattern, but a focusable text surface: a self-drawn editor
    # (Chromium/Electron rich text, terminal emulators) that accepts keyboard
    # input. A `text` label has no keyboard target, so it is excluded.
    if "text" in node.patterns and node.role in (
        "document",
        "group",
        "pane",
        "unknown",
    ):
        return True
    return False


def primitive_capabilities(node: RawNode) -> list[Capability]:
    """Return the primitive capabilities implied by ``node``'s role + patterns."""
    caps: dict[str, Capability] = {}
    patterns = node.patterns
    role = node.role

    if "invoke" in patterns or role in ("button", "link", "menuitem"):
        caps.setdefault("activate", _cap("activate"))

    # set_text is granted to *every* text-entry surface. The route it takes is
    # decided at execution time by the resolver, via mutually exclusive steps:
    #
    # * a plain form field writes through its Value pattern;
    # * a rich-text/document editor types through the keyboard instead.
    #
    # The keyboard path matters because a Value pattern is not proof that text
    # will stick: Windows 11 Notepad's RichEdit editor advertises `value` yet
    # accepts `SetValue` and silently discards it (verified live). Rich-text and
    # document editors are therefore always typed into, which is also what makes
    # a self-drawn Chromium editor (QQ's ProseMirror box, CodeMirror, Monaco)
    # writable without a hand-written pack.
    if _is_text_entry(node):
        cap = _cap("set_text", {"text": ParamSpec(type="string", required=True)})
        if _prefers_keyboard_entry(node):
            cap.via = [
                PrimitiveStep(primitive="focus", target="$self"),
                PrimitiveStep(primitive="type_text", target="$self", value="$text"),
            ]
        else:
            cap.via = [
                PrimitiveStep(
                    primitive="set_text",
                    target="$self",
                    value="$text",
                    condition="has:value",
                ),
                PrimitiveStep(
                    primitive="focus", target="$self", condition="missing:value"
                ),
                PrimitiveStep(
                    primitive="type_text",
                    target="$self",
                    value="$text",
                    condition="missing:value",
                ),
            ]
        caps.setdefault("set_text", cap)

    if "toggle" in patterns or role in ("checkbox", "switch"):
        caps.setdefault("toggle", _cap("toggle"))

    # GridItem cells are selectable, like list items (Excel, WPF DataGrid).
    if "selectionitem" in patterns or "griditem" in patterns or role in (
        "listitem",
        "tab",
        "radio",
        "row",
        "treeitem",
    ):
        caps.setdefault("select", _cap("select"))

    if "expandcollapse" in patterns or role in ("treeitem", "select", "group", "pane"):
        if "collapsed" in node.states or "expandcollapse" in patterns:
            caps.setdefault("expand", _cap("expand"))
            caps.setdefault("collapse", _cap("collapse"))

    if "rangevalue" in patterns or role == "slider":
        caps.setdefault("increment", _cap("increment"))
        caps.setdefault("decrement", _cap("decrement"))

    if "scroll" in patterns or "scrollitem" in patterns or role in (
        "list",
        "tree",
        "table",
        "pane",
    ):
        caps.setdefault(
            "scroll",
            _cap(
                "scroll",
                {
                    "to": ParamSpec(
                        type="enum", enum=("up", "down", "top", "bottom"), required=False
                    )
                },
            ),
        )

    # A ScrollItem (a row in a virtualised list) can be brought into view; this
    # is how an off-screen item is reached without scrolling blindly (dev doc
    # section 13). It is a real native capability, not a heuristic.
    if "scrollitem" in patterns:
        caps.setdefault(
            "scroll_into_view",
            Capability(
                name="scroll_into_view",
                level="primitive",
                via=[PrimitiveStep(primitive="scroll_into_view", target="$self")],
                risk="safe",
            ),
        )

    if "window" in patterns and role in ("window", "dialog"):
        caps.setdefault("close", _cap("close"))

    # Every readable node can be focused and read.
    caps.setdefault("focus", _cap("focus"))
    if node.name or node.value:
        caps.setdefault("read", _cap("read"))

    return list(caps.values())
