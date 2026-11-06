"""Structural simplification.

Removes nodes that carry no semantic information *before* matching, so packs
and heuristics see a clean tree. This mirrors the projection ``flatten`` rule
but runs earlier - the difference is that structural simplify is irreversible
for matching purposes while projection flatten is an output policy.

The rules themselves live in :mod:`somnus.compiler.distill`, as an ordered
plugin chain with an explicit ``keep`` / ``remove`` / ``unwrap`` contract. This
module keeps the historical entry point (``simplify``) and the collapse helper
the projection engine reads, so callers and tests do not need to know about the
chain.

Rules, in the order the chain applies them:

* a parent's name duplicated by one of its own caption texts loses the child;
* an unnamed, childless, inert image is removed (decoration, not content);
* an anonymous container whose only child is a text leaf is unwrapped;
* an unnamed, valueless single-child container is replaced by that child
  (transparent wrapper, except a row-shaped element);
* a container with no name, no value and no capabilities whose children are all
  anonymous ``group``/``pane`` nodes is dropped level by level;
* empty leaves (no name, no value, no capability, no pattern) are removed.
"""

from __future__ import annotations

from somnus.compiler.distill import (
    AUTO_COLLAPSE_THRESHOLD,
    CONTAINER_ROLES,
    NameOrigin,
    distill,
)
from somnus.ir.node import RawNode

__all__ = [
    "AUTO_COLLAPSE_THRESHOLD",
    "CONTAINER_ROLES",
    "NameOrigin",
    "auto_collapse_groups",
    "simplify",
]


def simplify(root: RawNode) -> RawNode:
    """Return a structurally reduced copy of ``root`` (in place).

    Deliberately conservative: it removes empty leaves and transparent
    single-child wrappers, but keeps named or otherwise *addressable* structure
    intact, because pack matching runs right after and needs to see it. The
    aggressive flattening of anonymous containers is a projection concern,
    applied when the tree is rendered.
    """
    return distill(root)


def auto_collapse_groups(
    children: list[RawNode], threshold: int = AUTO_COLLAPSE_THRESHOLD
):
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
        if (
            child is not None
            and role == run_role
            and _same_shape(children[i], children[run_start])
        ):
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
