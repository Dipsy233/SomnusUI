"""Structural simplification.

Removes nodes that carry no semantic information *before* matching, so packs
and heuristics see a clean tree. This mirrors the projection ``flatten`` rule
but runs earlier - the difference is that structural simplify is irreversible
for matching purposes while projection flatten is an output policy.

Rules:

* A container with a single child and no name/value/capability is replaced by
  that child (transparent wrapper).
* A container with no name, no value and no capabilities whose children are all
  anonymous ``group``/``pane`` nodes is dropped level by level.
* Empty leaves (no name, no value, no capability, no pattern) are removed.
"""

from __future__ import annotations

from somnus.compiler.normalize import is_interactive
from somnus.ir.node import RawNode

#: Roles considered structural containers. An unnamed, valueless node of one
#: of these roles carries no semantics and is folded into its child, which both
#: UIA and CDP produce in quantity (wrapper panes, ``paragraph > StaticText``).
CONTAINER_ROLES = frozenset({"group", "pane", "unknown", "text"})

#: Minimum number of same-role siblings that triggers automatic collapse.
AUTO_COLLAPSE_THRESHOLD = 20


def _meaningful(node: RawNode) -> bool:
    if node.name or node.value:
        return True
    if is_interactive(node.role, node.patterns):
        return True
    if node.role not in CONTAINER_ROLES:
        return True
    return False


def simplify(root: RawNode) -> RawNode:
    """Return a structurally reduced copy of ``root`` (in place).

    Deliberately conservative: it removes empty leaves and transparent
    single-child wrappers, but keeps named or otherwise *addressable* structure
    intact, because pack matching runs right after and needs to see it. The
    aggressive flattening of anonymous containers is a projection concern
    (dev doc section 7.1), applied when the tree is rendered.
    """
    root.children = _promote_children(root.children, inline_anonymous=False)
    return root


def _promote_children(
    children: list[RawNode], *, inline_anonymous: bool = False
) -> list[RawNode]:
    """Simplify each child; optionally inline anonymous containers.

    ``inline_anonymous`` is the aggressive mode used by the projection stage:
    an unnamed container with no identity of its own is replaced by its children,
    so many identical unnamed wrappers cannot hash to one fingerprint and make a
    subtree ambiguous (dev doc sections 7.1, 8).
    """
    out: list[RawNode] = []
    for child in children:
        simplified = _simplify_child(child)
        if simplified is None:
            continue
        if (
            inline_anonymous
            and simplified.role in CONTAINER_ROLES
            and not simplified.name
            and not simplified.value
            and not simplified.native.automation_id
            and not is_interactive(simplified.role, simplified.patterns)
            and simplified.children
        ):
            out.extend(simplified.children)
        else:
            out.append(simplified)
    return out


def _simplify_child(node: RawNode) -> RawNode | None:
    node.children = _promote_children(node.children)

    # A node that carries a child text which merely repeats its own name is an
    # accessibility artifact (WPF exposes a button's caption as a child
    # TextBlock; CDP wraps a heading's text in a StaticText). Drop it so the
    # tree has one line per concept.
    if node.children and node.name and node.role in (
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
    ):
        node.children = [
            c
            for c in node.children
            if not (
                c.role == "text"
                and not c.children
                and c.name
                and c.name.strip() == (node.name or "").strip()
            )
        ]

    # Transparent single-child wrapper.
    if (
        node.role in CONTAINER_ROLES
        and not node.name
        and not node.value
        and len(node.children) == 1
        and not is_interactive(node.role, node.patterns)
    ):
        return node.children[0]

    # Anonymous container whose children are all anonymous containers -> drop it,
    # promoting the grandchildren (keeps depth low for WPF/VCL layouts).
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
        node.children = [gc for c in node.children for gc in c.children]

    # Anonymous empty leaf.
    if (
        not node.children
        and node.role in CONTAINER_ROLES
        and not node.name
        and not node.value
        and node.role != "unknown"
    ):
        return None
    return node


def auto_collapse_groups(children: list[RawNode], threshold: int = AUTO_COLLAPSE_THRESHOLD):
    """Mark homogeneous sibling groups for collapse.

    Returns a dict ``id(first_sibling) -> count``. The projection engine uses
    this to emit ``count=N`` plus a find hint instead of hundreds of rows.
    """
    out: dict[int, int] = {}
    run_start = 0
    run_role: str | None = None
    count = 0
    for i, child in enumerate(children + [None]):  # type: ignore[list-item]
        role = child.role if child is not None else None
        if child is not None and role == run_role and _same_shape(children[i], children[run_start]):
            count += 1
            continue
        if run_role is not None and count >= threshold:
            out[id(children[run_start])] = count
        run_start = i
        run_role = role
        count = 1
    return out


def _same_shape(a: RawNode, b: RawNode) -> bool:
    return a.role == b.role
