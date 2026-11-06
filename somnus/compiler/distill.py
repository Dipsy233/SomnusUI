"""Projection distillation: one tree walk, an ordered plugin chain.

The structural rules used to live as a set of ``if`` blocks spread through
:mod:`somnus.compiler.simplify`. They are the same rules, expressed as small
plugins with an explicit contract, because that is what makes a rule testable
in isolation and makes the order auditable.

The shape follows the distiller in Playwright's aria snapshot: a plugin sees a
node (children already settled) and answers one of three things:

* ``KEEP`` - keep the node as it is (it may have edited its own children);
* ``REMOVE`` - drop the node and its subtree;
* ``UNWRAP`` - drop the node, but keep its children in its place.

Every rule runs once per node, in :data:`DEFAULT_PLUGINS` order, after its
children have been distilled. That is a post-order walk: a parent decides about
a caption after the caption itself has been simplified.

The hard invariant, which every removal-shaped plugin must respect: a node that
carries a name, a value, an automation id, an interaction pattern or a
row-shaped class name is **addressable** and is never removed or unwrapped. A
ref is allocated to addressable structure after this pass, so dropping one
would make a real control unreachable. Plugins that must be more conservative
than the historical rule say so inline.

:class:`NameOrigin` records the one fact a later rule cannot recover from the
tree alone: *this parent's name came from that child*. With it, dropping the
duplicate child is safe (the name stays), and keeping the child because it is
content does not silently leave two copies of the same caption.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from somnus.compiler.normalize import is_interactive
from somnus.ir.node import RawNode

#: Roles considered structural containers. An unnamed, valueless node of one of
#: these roles carries no semantics and is folded into its child, which both
#: UIA produces in quantity (wrapper panes).
CONTAINER_ROLES = frozenset({"group", "pane", "unknown", "text"})

#: Minimum number of same-role siblings that triggers automatic collapse.
AUTO_COLLAPSE_THRESHOLD = 20

#: A class name that marks an element as a *row* in a list the user interacts
#: with. Such an element is addressable structure and must survive
#: distillation even when it wraps a single child.
_ROW_CLASS_RE = re.compile(
    r"(^|[-_\s])(item|row|entry|cell|card|result|option|node)([-_\s]|$)",
    re.IGNORECASE,
)

#: Roles where a child text that merely repeats the node's own name is an
#: accessibility artifact (WPF exposes a button's caption as a child TextBlock).
#: Row-shaped roles are deliberately absent: entity matching reads those child
#: texts, and the caption there is data, not a duplicate.
_CAPTION_ROLES = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "tab",
        "listitem",
        "group",
        "text",
        "heading",
        "checkbox",
        "radio",
        "treeitem",
    }
)


class Action(str, Enum):
    """What a plugin decided for one node."""

    KEEP = "keep"
    REMOVE = "remove"
    UNWRAP = "unwrap"


@dataclass(slots=True)
class NameOrigin:
    """``id(parent) -> id(child)`` for a name that a child supplied.

    Kept beside the tree rather than on a node: it is a fact about one pass,
    not a property of the element, and it must not travel into the semantic
    tree (where it would look like another piece of identity evidence).
    """

    sources: dict[int, int] = field(default_factory=dict)

    def record(self, parent: RawNode, child: RawNode) -> None:
        self.sources[id(parent)] = id(child)

    def source_of(self, parent: RawNode) -> int | None:
        return self.sources.get(id(parent))

    def is_origin(self, parent: RawNode, child: RawNode) -> bool:
        return self.sources.get(id(parent)) == id(child)

    def __len__(self) -> int:  # pragma: no cover - diagnostics only
        return len(self.sources)


@dataclass(slots=True)
class DistillContext:
    """State a plugin may read or update during one pass."""

    origins: NameOrigin = field(default_factory=NameOrigin)
    removed: int = 0
    unwrapped: int = 0

    @property
    def changed(self) -> int:
        return self.removed + self.unwrapped


Plugin = Callable[[RawNode, DistillContext], Action]


def _looks_like_row(node: RawNode) -> bool:
    cls = node.native.class_name or ""
    return bool(cls and _ROW_CLASS_RE.search(cls))


def _addressable(node: RawNode) -> bool:
    """Never drop this node: it can be named, focused, identified or clicked."""
    return bool(
        node.name
        or node.value
        or node.native.automation_id
        or is_interactive(node.role, node.patterns)
        or _looks_like_row(node)
    )


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------


def drop_duplicate_caption(node: RawNode, ctx: DistillContext) -> Action:
    """Drop a child text that only repeats the node's own name."""
    if not (node.children and node.name and node.role in _CAPTION_ROLES):
        return Action.KEEP
    name = node.name.strip()
    kept: list[RawNode] = []
    for child in node.children:
        if (
            child.role == "text"
            and not child.children
            and child.name
            and child.name.strip() == name
        ):
            # The name and the caption are one concept; the parent already
            # renders it. Remember which child supplied it, so a later rule
            # never has to guess.
            ctx.origins.record(node, child)
            continue
        kept.append(child)
    if len(kept) != len(node.children):
        node.children = kept
    return Action.KEEP


def drop_decorative_image(node: RawNode, ctx: DistillContext) -> Action:
    """Remove an image that carries no name, no children and no interaction.

    An image with an automation id is kept: the id is how the application
    identifies it, and the renderer's ``? ref role reason=visual`` line is
    evidence about real content. A purely decorative separator has neither.
    """
    if node.role != "image":
        return Action.KEEP
    if _addressable(node) or node.children or node.patterns:
        return Action.KEEP
    return Action.REMOVE


def inline_single_text_wrapper(node: RawNode, ctx: DistillContext) -> Action:
    """Unwrap an anonymous container whose only child is a text leaf.

    This is the common WPF/VCL template shape: a border pane around one
    caption. The container adds a line and nothing else. The strict
    addressability guard keeps a container that has its own identity.
    """
    if node.role not in CONTAINER_ROLES or _addressable(node):
        return Action.KEEP
    if len(node.children) != 1 or node.children[0].role != "text":
        return Action.KEEP
    return Action.UNWRAP


def inline_single_child_container(node: RawNode, ctx: DistillContext) -> Action:
    """Unwrap a transparent single-child container (historical rule).

    Kept for every child role, not only text: UIA nests wrappers around
    buttons, combos and lists just as often. A row-shaped class name is kept
    even with one child: it *is* the row the user clicks, and flattening it
    hid rows from entity detection (live: QQ's pinned conversations vanished).
    """
    if (
        node.role in CONTAINER_ROLES
        and not node.name
        and not node.value
        and len(node.children) == 1
        and not is_interactive(node.role, node.patterns)
        and not _looks_like_row(node)
    ):
        return Action.UNWRAP
    return Action.KEEP


def flatten_anonymous_layers(node: RawNode, ctx: DistillContext) -> Action:
    """Drop an anonymous container whose children are all anonymous containers."""
    if (
        node.role in CONTAINER_ROLES
        and not node.name
        and not node.value
        and node.children
        and all(
            c.role in CONTAINER_ROLES and not c.name and not c.value
            for c in node.children
        )
    ):
        node.children = [gc for child in node.children for gc in child.children]
    return Action.KEEP


def drop_empty_leaf(node: RawNode, ctx: DistillContext) -> Action:
    """Remove an anonymous, valueless leaf with no capabilities."""
    if (
        not node.children
        and node.role in CONTAINER_ROLES
        and not node.name
        and not node.value
        and node.role != "unknown"
    ):
        return Action.REMOVE
    return Action.KEEP


DEFAULT_PLUGINS: tuple[Plugin, ...] = (
    drop_duplicate_caption,
    drop_decorative_image,
    inline_single_text_wrapper,
    inline_single_child_container,
    flatten_anonymous_layers,
    drop_empty_leaf,
)


# ---------------------------------------------------------------------------
# pass
# ---------------------------------------------------------------------------


def distill(
    root: RawNode,
    *,
    origins: NameOrigin | None = None,
    plugins: tuple[Plugin, ...] = DEFAULT_PLUGINS,
) -> RawNode:
    """Distil *root* in place and return it (the historical ``simplify`` API)."""
    distill_with_context(root, origins=origins, plugins=plugins)
    return root


def distill_with_context(
    root: RawNode,
    *,
    origins: NameOrigin | None = None,
    plugins: tuple[Plugin, ...] = DEFAULT_PLUGINS,
) -> DistillContext:
    """Distil *root* and return the pass context (counts and name origins)."""
    ctx = DistillContext(
        origins=origins if origins is not None else NameOrigin()
    )
    root.children = _promote(root.children, ctx, plugins)
    return ctx


def _promote(
    children: list[RawNode], ctx: DistillContext, plugins: tuple[Plugin, ...]
) -> list[RawNode]:
    out: list[RawNode] = []
    for child in children:
        # Post-order: the child's own rules run before this node's decide about
        # it, so a caption is simplified before it is compared to a parent name.
        child.children = _promote(child.children, ctx, plugins)
        action = _decide(child, ctx, plugins)
        if action is Action.REMOVE:
            ctx.removed += 1
            continue
        if action is Action.UNWRAP:
            ctx.unwrapped += 1
            out.extend(child.children)
            continue
        out.append(child)
    return out


def _decide(node: RawNode, ctx: DistillContext, plugins: tuple[Plugin, ...]) -> Action:
    for plugin in plugins:
        action = plugin(node, ctx)
        if action is not Action.KEEP:
            return action
    return Action.KEEP
