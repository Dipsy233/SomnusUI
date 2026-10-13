"""Projection rules: keep / flatten / prune / collapse (dev doc section 7.1).

A :class:`Projection` is a *view* of the semantic tree produced by the engine.
It annotates nodes with a :class:`Disposition`; the serializer then renders
accordingly. Rules are evaluated parent-first so a `prune` on an ancestor wins
over a `keep` on a descendant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from somnus.compiler.pack_schema import ProjectionRule
from somnus.ir.node import SemanticNode


class Rule(str, Enum):
    KEEP = "keep"
    FLATTEN = "flatten"  # drop the node itself, promote children
    PRUNE = "prune"  # drop the whole subtree
    COLLAPSE = "collapse"  # keep node, hide children behind count=

    @classmethod
    def parse(cls, value: str) -> "Rule":
        try:
            return cls(value.lower())
        except ValueError:
            return cls.KEEP


@dataclass(slots=True)
class Selector:
    """A tiny selector language: ``role=x``, ``name=y``, ``entity=z``, ``ref=r``.

    Multiple comma-separated clauses are ANDed; a clause value may be a regex
    when it is wrapped in ``/.../``.
    """

    role: str | None = None
    name: str | None = None
    entity: str | None = None
    ref: str | None = None
    layer: str | None = None
    has_cap: str | None = None

    @classmethod
    def parse(cls, expr: str) -> "Selector":
        sel = cls()
        for clause in expr.split(","):
            clause = clause.strip()
            if not clause or "=" not in clause:
                continue
            key, _, value = clause.partition("=")
            key = key.strip().lower()
            value = value.strip().strip('"')
            if key in ("role", "name", "entity", "ref", "layer"):
                setattr(sel, key, value)
            elif key in ("cap", "has"):
                sel.has_cap = value
        return sel

    def matches(self, node: SemanticNode) -> bool:
        if self.role and node.role != self.role:
            return False
        if self.layer and node.layer != self.layer:
            return False
        if self.entity and node.entity != self.entity:
            return False
        if self.ref and node.ref != self.ref:
            return False
        if self.name is not None and not _match_name(self.name, node.name):
            return False
        if self.has_cap and self.has_cap not in node.cap_names():
            return False
        return True


def _match_name(pattern: str, name: str | None) -> bool:
    if name is None:
        return False
    if pattern.startswith("/") and pattern.endswith("/"):
        try:
            return re.search(pattern[1:-1], name, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.casefold() in name.casefold()


@dataclass(slots=True)
class Projection:
    """Result of applying rules to a tree."""

    #: id(node) -> Rule
    dispositions: dict[int, Rule] = field(default_factory=dict)
    #: id(node) -> max visible children (collapse), None = no limit.
    limits: dict[int, int | None] = field(default_factory=dict)
    pruned_count: int = 0

    def rule_for(self, node: SemanticNode) -> Rule:
        return self.dispositions.get(id(node), Rule.KEEP)


def apply_projection(
    root: SemanticNode,
    pack_rules: list[ProjectionRule] | None = None,
    *,
    default_prune: bool = False,
) -> Projection:
    """Compute dispositions for each node.

    Order of precedence (highest first):

    1. Explicit pack rules in declaration order (later rules override earlier).
    2. Generic heuristics (offscreen non-collection nodes, anonymous leaves).
    3. ``default_prune`` for anonymous containers.
    """
    proj = Projection()
    compiled = [(Selector.parse(r.select), Rule.parse(r.rule)) for r in (pack_rules or [])]

    def walk(node: SemanticNode, parent_rule: Rule, depth: int) -> None:
        rule = parent_rule
        # Start from KEEP unless the parent already pruned/flattened us.
        for sel, r in compiled:
            if sel.matches(node):
                rule = r
        # Generic heuristics (only when no explicit rule fired).
        if rule is Rule.KEEP:
            rule = _generic_rule(node, depth, default_prune)

        proj.dispositions[id(node)] = rule
        if rule is Rule.PRUNE:
            proj.pruned_count += _subtree_size(node)
            return
        if rule is Rule.COLLAPSE:
            return  # children are not visited; rendered via count=

        for child in node.children:
            walk(child, Rule.KEEP, depth + 1)

    walk(root, Rule.KEEP, 0)
    return proj


def _generic_rule(node: SemanticNode, depth: int, default_prune: bool) -> Rule:
    # An anonymous structural container is transparent: promote its children.
    # `focus`/`read` are present on every node, so they must not count as
    # "capable" here or nothing would ever flatten (dev doc section 7.1).
    meaningful_caps = node.cap_names() - {"focus", "read"}
    if (
        node.role in ("group", "pane", "region")
        and not node.name
        and not node.value
        and not node.entity
        and not meaningful_caps
    ):
        return Rule.FLATTEN
    if default_prune and depth > 0 and node.role in ("group", "pane") and not node.name:
        return Rule.PRUNE
    return Rule.KEEP


def _subtree_size(node: SemanticNode) -> int:
    return sum(1 for _ in node.walk()) - 1
