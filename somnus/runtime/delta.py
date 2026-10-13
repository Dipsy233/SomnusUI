"""Delta computation between two compiled revisions (dev doc section 9.4).

Three line kinds: ``~`` changed, ``+`` added, ``-`` removed. Identity is the
fingerprint, so a node that merely got re-referenced does not show as add/remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from somnus.ir.node import SemanticNode

#: Fields compared between structurally identical nodes.
WATCHED_ATTRS = ("count", "total", "qty", "price", "value", "len")


@dataclass(slots=True)
class Delta:
    changed: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    added: list[SemanticNode] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def render(self) -> str:
        from somnus.sui.serialize import render_delta

        return render_delta(self.changed, self.added, self.removed)


def compute_delta(before: dict[str, SemanticNode], after: dict[str, SemanticNode]) -> Delta:
    """Compute the delta between two ref->node maps.

    Both maps are keyed by *fingerprint* here so re-references are invisible.
    """
    delta = Delta()
    for fp, new_node in after.items():
        old_node = before.get(fp)
        if old_node is None:
            if _worth_reporting(new_node):
                delta.added.append(new_node)
            continue
        delta.changed.extend(_diff_node(old_node, new_node))
        for child in new_node.children:
            delta.added.extend([])  # children handled by full walk below
    # Children of added nodes are represented implicitly; only report added
    # standalone nodes that have a ref different from their parent.
    for fp, old_node in before.items():
        if fp not in after:
            delta.removed.append(old_node.ref)
    return delta


def diff_trees(
    before: dict[str, SemanticNode],
    after: dict[str, SemanticNode],
) -> Delta:
    """Flat comparison over all nodes (preferred entry point)."""
    delta = Delta()
    for fp, new_node in after.items():
        old_node = before.get(fp)
        if old_node is None:
            if _worth_reporting(new_node):
                delta.added.append(new_node)
            continue
        delta.changed.extend(_diff_node(old_node, new_node))
    for fp, old_node in before.items():
        if fp not in after:
            delta.removed.append(old_node.ref)
    return delta


def _diff_node(old: SemanticNode, new: SemanticNode) -> list[tuple[str, str, Any, Any]]:
    out: list[tuple[str, str, Any, Any]] = []
    for key in WATCHED_ATTRS:
        ov = old.attrs.get(key)
        nv = new.attrs.get(key)
        if ov != nv and nv is not None:
            out.append((new.ref, key, ov if ov is not None else "?", nv))
    # value changes only for leaves / inputs (avoid noise from entity groups).
    if old.role in ("input", "slider", "text", "statusbar") and old.value != new.value:
        if new.value is not None:
            out.append((new.ref, "value", old.value or "", new.value))
    return out


def _worth_reporting(node: SemanticNode) -> bool:
    """Filter structural noise: only entities and top-level nodes."""
    return node.entity is not None or node.layer in ("screen", "dialog") or bool(node.name)


def index_by_fingerprint(root: SemanticNode) -> dict[str, SemanticNode]:
    out: dict[str, SemanticNode] = {}
    for node in root.walk():
        out[node.fingerprint] = node
    return out
