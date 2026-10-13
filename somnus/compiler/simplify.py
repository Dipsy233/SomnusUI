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

#: Roles considered structural containers.
CONTAINER_ROLES = frozenset({"group", "pane", "unknown"})

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
    """Return a structurally reduced copy of ``root`` (in place)."""
    root.children = [_simplify_child(c) for c in root.children]
    root.children = [c for c in root.children if c is not None]
    return root


def _simplify_child(node: RawNode) -> RawNode | None:
    node.children = [c for c in (_simplify_child(c) for c in node.children) if c]

    # A button/link/text pair where the child text merely repeats the parent
    # name is a UIA artifact (WPF exposes the button's caption as a child
    # TextBlock). Drop it so the entity tree stays clean.
    if node.children and node.name and node.role in (
        "button",
        "link",
        "menuitem",
        "tab",
        "listitem",
        "group",
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
