"""SUI-Text serialization (dev doc section 6.1).

Grammar::

    doc     := header line*
    header  := "@rev" INT
    line    := INDENT* node
    node    := TYPE REF [STRING] (attr | flag)* cap*
    attr    := KEY "=" (NUMBER | STRING | BARE | "?")
    flag    := disabled | selected | checked | unchecked | expanded | collapsed
             | focused | readonly | loading | active
    cap     := "+" NAME ["(" PARAMS ")"] ["!"]
    unknown := "?" REF ROLE "reason=" REASON
    more    := "…" "+" INT "more" [hint]

Key savings vs v0.1:

* capabilities live on the same line as the node;
* default capabilities are omitted (the system prompt carries the table);
* ``enabled`` and default states are never emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from somnus.ir.node import SemanticNode

INDENT = "  "

#: Role -> default capability that need not be printed.
DEFAULT_CAP = {
    "button": "activate",
    "link": "activate",
    "menuitem": "activate",
    "checkbox": "toggle",
    "switch": "toggle",
    "radio": "select",
    "input": "set_text",
    "listitem": "select",
    "tab": "select",
}

#: Generic capabilities that are implied for every node and never printed
#: unless the node is inspected directly (L2). This is the biggest token win.
UBIQUITOUS_CAPS = frozenset({"focus", "read"})
#: Container capabilities implied by the role (list/tree/pane scroll).
IMPLIED_CONTAINER_CAPS = frozenset({"scroll"})

#: States rendered as bare flags, in a fixed order for stable goldens.
FLAG_ORDER = (
    "disabled",
    "readonly",
    "required",
    "invalid",
    "selected",
    "checked",
    "unchecked",
    "mixed",
    "expanded",
    "collapsed",
    "focused",
    "loading",
    "active",
)

#: Attributes that are rendered positionally/numerically, not quoted.
NUMERIC_ATTRS = frozenset({"price", "total", "count", "qty", "len", "amount"})
#: Attributes rendered as bare words (enum-ish identifiers).
BARE_ATTRS = frozenset({"entity", "role", "cur", "type", "kind", "state"})


@dataclass(slots=True)
class RenderOptions:
    """Knobs controlling serialization."""

    include_default_caps: bool = False
    show_layers: bool = True
    max_string: int = 80
    compact: bool = False  # single space separators (default already compact)
    #: Emit semantic relations (dev doc section 4.6). Off for L0/L1 to save
    #: tokens, on for INSPECT (L2) where the caller wants the full picture.
    show_relations: bool = False


def render_document(
    lines: list["Any"],
    rev: int | None = None,
    *,
    options: RenderOptions | None = None,
    extra_header: list[str] | None = None,
) -> str:
    """Render a full observation document."""
    options = options or RenderOptions()
    out: list[str] = []
    if rev is not None:
        out.append(f"@rev {rev}")
    for h in extra_header or ():
        out.append(h)
    for line in lines:
        out.append(render_line(line, options))
    return "\n".join(out)


def render_line(line: "Any", options: RenderOptions | None = None) -> str:
    """Render one projection :class:`~somnus.projection.engine.Line`."""
    options = options or RenderOptions()
    indent = "" if options.compact else INDENT * line.depth
    if getattr(line, "marker", False):
        return f"{indent}{line.marker_text}"
    text = render_node_line(
        line.node, options, collapsed=getattr(line, "collapsed", False),
        hidden_count=getattr(line, "hidden_count", 0),
    )
    return f"{indent}{text}"


def render_node_line(
    node: SemanticNode,
    options: RenderOptions | None = None,
    *,
    collapsed: bool = False,
    hidden_count: int = 0,
) -> str:
    """Render a single semantic node (no indentation)."""
    options = options or RenderOptions()
    parts: list[str] = []

    # 1. TYPE + REF
    parts.append(node.display_type)
    parts.append(node.ref)

    # 2. Name (quoted) or UNKNOWN marker.
    #    A node whose *content* cannot be read without vision is emitted as
    #    `? ref role reason=visual` (dev doc sections 6.1 and 11.2): unnamed
    #    canvas / image nodes and explicit `unknown` roles. An `unknown` role
    #    that nevertheless carries a name stays a normal primitive line.
    if _is_unreadable(node):
        reason = "visual" if node.role in ("canvas", "image") else "unreadable"
        return f"? {node.ref} {node.role} reason={reason}"

    if node.name:
        parts.append(_quote(node.name, options.max_string))

    # 2b. Long text: emit `len=N preview="…"` instead of the full value so a
    #     5000-char document does not blow the budget; read it in slices with
    #     `INSPECT t1 range=0:2000` (dev doc section 5).
    if _is_long_text_node(node):
        value = str(node.value)
        parts.append(f"len={len(value)}")
        preview = value[: options.max_string]
        parts.append(f"preview={_quote(preview, options.max_string)}")

    # 3. Attributes (entity attrs + generic numeric ones).
    for key, value in _ordered_attrs(node):
        if key == "count_min":
            # A virtualised collection only realises part of its rows, so the
            # count is a lower bound (dev doc section 13): `count>=N`.
            parts.append(f"count>={value}")
            continue
        parts.append(f"{key}={_render_value(value, key, options)}")

    # 4. Flags (deviations from default only).
    for flag in _flags(node):
        parts.append(flag)

    # 5. Capabilities.
    for cap in _ordered_caps(node):
        if not options.include_default_caps and _is_implicit(node, cap):
            continue
        parts.append(cap.render())

    # 5b. Relations (only when requested: L2 INSPECT).
    if options.show_relations:
        for rel in node.relations:
            if rel.kind == "contains":
                continue  # containment is already the tree structure
            parts.append(rel.render())

    # 6. Collapse / truncation marker.
    if collapsed and hidden_count:
        parts.append(f"+{hidden_count} more")

    return " ".join(parts)


def render_delta(
    changed: Iterable[tuple[str, str, Any, Any]] = (),
    added: Iterable[SemanticNode] = (),
    removed: Iterable[str] = (),
    *,
    options: RenderOptions | None = None,
) -> str:
    """Render the three delta line kinds: ``~``, ``+``, ``-``.

    ``changed`` is a list of ``(ref, field, old, new)`` tuples.
    """
    options = options or RenderOptions()
    out: list[str] = []
    for ref, field_name, old, new in changed:
        out.append(f"~ {ref} {field_name} {_brief(old)}→{_brief(new)}")
    for node in added:
        out.append("+ " + render_node_line(node, options))
    for ref in removed:
        out.append(f"- {ref}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


#: Values longer than this are rendered as `len=` + `preview=` (section 5).
LONG_TEXT_THRESHOLD = 160


def _is_long_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) > LONG_TEXT_THRESHOLD


def _is_long_text_node(node: SemanticNode) -> bool:
    return node.role in ("text", "document", "input") and _is_long_text(node.value)


def _is_unreadable(node: SemanticNode) -> bool:
    """True when the node's content is only knowable through vision.

    Per dev doc section 11.2, UNKNOWN is reserved for content that is itself
    unreadable (canvas, unnamed image, custom-drawn control) - never for a node
    we merely failed to upgrade to an entity.
    """
    if node.name or node.value is not None or node.children:
        return False
    if node.entity:
        return False
    return node.role in ("canvas", "image", "unknown")


def _quote(text: str, limit: int) -> str:
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f'"{text}"'


def _render_value(value: Any, key: str, options: RenderOptions) -> str:
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        if key in NUMERIC_ATTRS and _looks_numeric(value):
            return value
        if key in BARE_ATTRS and _looks_bare(value):
            return value
        return _quote(value, options.max_string)
    return _quote(str(value), options.max_string)


def _looks_numeric(text: str) -> bool:
    return all(c.isdigit() or c in ".-" for c in text) and text


def _looks_bare(text: str) -> bool:
    return bool(text) and all(c.isalnum() or c in "_-" for c in text)


def _ordered_attrs(node: SemanticNode) -> list[tuple[str, Any]]:
    # Deterministic order: entity attrs sorted, stable across runs.
    items = sorted(node.attrs.items(), key=lambda kv: kv[0])
    return items


def _flags(node: SemanticNode) -> list[str]:
    flags = [f for f in FLAG_ORDER if f in node.states]
    # `readonly` is only meaningful on inputs; on other roles it is noise (every
    # Text/StatusBar element exposes a read-only Value pattern).
    if "readonly" in flags and node.role not in ("input", "select", "slider"):
        flags.remove("readonly")
    # `checked`/`unchecked` are mutually exclusive; both never appear.
    if "checked" in flags and "unchecked" in flags:
        flags.remove("unchecked")
    return flags


def _ordered_caps(node: SemanticNode) -> list[Any]:
    # Semantic caps first (they are the domain API), then primitives by name.
    return sorted(node.caps, key=lambda c: (0 if c.level == "semantic" else 1, c.name))


def _is_implicit(node: SemanticNode, cap: Any) -> bool:
    """True when a capability is implied by the role and may be omitted.

    Semantic (entity) capabilities are always printed. Primitive capabilities
    that follow from the role (activate on a button, set_text on an input) plus
    the ubiquitous focus/read pair are hidden; the system prompt carries the
    table so the LLM still knows they exist.
    """
    if cap.level == "semantic":
        return False
    if cap.name in UBIQUITOUS_CAPS:
        return True
    if cap.name in IMPLIED_CONTAINER_CAPS and node.role in (
        "list",
        "tree",
        "table",
        "pane",
        "screen",
        "window",
    ):
        return True
    return DEFAULT_CAP.get(node.role) == cap.name


def _brief(value: Any) -> str:
    if value is None:
        return "?"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
