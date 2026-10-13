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


def primitive_capabilities(node: RawNode) -> list[Capability]:
    """Return the primitive capabilities implied by ``node``'s role + patterns."""
    caps: dict[str, Capability] = {}
    patterns = node.patterns
    role = node.role

    if "invoke" in patterns or role in ("button", "link", "menuitem"):
        caps.setdefault("activate", _cap("activate"))

    # set_text is granted to text-entry surfaces. A read-only Value pattern
    # (labels, status bars, tree items) must not advertise it, or CALL would
    # fail at native execution (dev doc section 11.2: no capability without a
    # pattern). Besides the `input` role, a Document or an editable custom
    # control also exposes a writable Value pattern; gating on the pattern (and
    # the absence of `readonly`) keeps this generic instead of per-app.
    writable_value = "value" in patterns and "readonly" not in node.states
    if writable_value and role in ("input", "text", "pane", "slider", "select"):
        cap = _cap("set_text", {"text": ParamSpec(type="string", required=True)})
        cap.via = [PrimitiveStep(primitive="set_text", target="$self", value="$text")]
        caps.setdefault("set_text", cap)
    elif role == "input" and "value" not in patterns:
        # Rich-text editors (QQ's ProseMirror box, CodeMirror panes) expose no
        # Value pattern but accept keyboard input. Route set_text through
        # focus + unicode typing so generic mode can still write to them
        # (found live: QQ's message box was unfillable without this).
        cap = _cap("set_text", {"text": ParamSpec(type="string", required=True)})
        cap.via = [
            PrimitiveStep(primitive="focus", target="$self"),
            PrimitiveStep(primitive="type_text", target="$self", value="$text"),
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
