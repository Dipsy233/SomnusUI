"""Projection engine: turn a compiled tree + rules + budget into a render plan.

The engine does not serialize; it produces a list of :class:`Line` records that
the SUI serializer formats. Keeping rendering separate is what makes golden
tests stable and lets us compute token budgets incrementally (we stop adding
lines once the budget is exhausted, emitting a ``… +N more`` marker).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from somnus.ir.node import SemanticNode
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.rules import Projection, Rule


@dataclass(slots=True)
class Line:
    """One renderable node line plus its indent depth."""

    node: SemanticNode
    depth: int
    #: When set, the node itself is hidden and only this summary is rendered.
    collapsed: bool = False
    #: Number of children hidden behind a collapse.
    hidden_count: int = 0
    #: True when this line is a truncation marker.
    marker: bool = False
    marker_text: str = ""
    #: Name/numeric attribute overrides (entity attrs already on node).
    extra_flags: tuple[str, ...] = ()


@dataclass(slots=True)
class RenderPlan:
    lines: list[Line] = field(default_factory=list)
    tokens: int = 0
    truncated: bool = False
    hidden_total: int = 0


class ProjectionEngine:
    def __init__(
        self,
        budget: ObservationBudget | None = None,
        *,
        auto_collapse_threshold: int = 8,
    ) -> None:
        self.budget = budget or ObservationBudget()
        self.auto_collapse_threshold = auto_collapse_threshold

    def plan(
        self,
        root: SemanticNode,
        projection: Projection | None = None,
        *,
        focus: SemanticNode | None = None,
        depth_limit: int | None = None,
    ) -> RenderPlan:
        projection = projection or Projection()
        max_depth = depth_limit if depth_limit is not None else self.budget.max_depth
        plan = RenderPlan()
        counter = {"nodes": 0, "tokens": 0}
        self._walk(root, 0, max_depth, projection, plan, counter, top=True)

        # Depth/entity focus: if a focus node is given, also plan its subtree
        # (used by INSPECT which bypasses the L0 collapse).
        if focus is not None and focus is not root:
            self._walk(focus, 0, max_depth, projection, plan, counter, top=True)
        plan.tokens = counter["tokens"]
        if plan.truncated and plan.hidden_total:
            plan.lines.append(
                Line(
                    node=root,
                    depth=0,
                    marker=True,
                    marker_text=f"… +{plan.hidden_total} more",
                )
            )
        return plan

    # -- walking ------------------------------------------------------------

    def _walk(
        self,
        node: SemanticNode,
        depth: int,
        max_depth: int,
        projection: Projection,
        plan: RenderPlan,
        counter: dict[str, int],
        *,
        top: bool = False,
    ) -> None:
        if counter["nodes"] >= self.budget.max_nodes:
            plan.truncated = True
            plan.hidden_total += 1
            return
        rule = projection.rule_for(node) if not top else Rule.KEEP
        if rule is Rule.PRUNE:
            return
        if rule is Rule.FLATTEN:
            for child in node.children:
                self._walk(child, depth, max_depth, projection, plan, counter)
            return

        # Budget by depth.
        if depth > max_depth:
            plan.truncated = True
            plan.hidden_total += self._count(node)
            return

        collapsed = rule is Rule.COLLAPSE or self._auto_collapse(node)
        line = Line(node=node, depth=depth, collapsed=collapsed)
        if collapsed:
            line.hidden_count = self._count(node)

        if not self._emit(line, plan, counter):
            return

        if collapsed:
            return
        for child in node.children:
            self._walk(child, depth + 1, max_depth, projection, plan, counter)

    def _emit(self, line: Line, plan: RenderPlan, counter: dict[str, int]) -> bool:
        # Pre-render through the shared formatter to measure tokens.
        from somnus.sui.serialize import render_line

        text = render_line(line)
        cost = count_tokens(text)
        if counter["tokens"] + cost > self.budget.max_tokens and counter["nodes"] > 0:
            plan.truncated = True
            plan.hidden_total += 1
            return False
        plan.lines.append(line)
        counter["tokens"] += cost
        counter["nodes"] += 1
        return True

    def _auto_collapse(self, node: SemanticNode) -> bool:
        # Homogeneous collections over the threshold collapse automatically.
        if node.layer in ("screen", "window"):
            return False
        if len(node.children) >= self.auto_collapse_threshold:
            roles = {c.role for c in node.children}
            if len(roles) <= 2:
                return True
        return False

    @staticmethod
    def _count(node: SemanticNode) -> int:
        return len(node.children)
