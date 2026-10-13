"""Generic pack: heuristics that work on any application.

This pack never produces domain entities with confidence 1.0 - it only encodes
widely-applicable *projection* rules and one soft entity family (``dialog``),
which is exactly the behaviour dev doc section 11.3 calls "Generic Mode".
"""

from __future__ import annotations

from somnus.compiler.pack_schema import Pack, ProjectionRule, register

GENERIC = register(
    Pack(
        id="generic",
        match_process=("*",),  # matches everything as a fallback
        projection=[
            ProjectionRule(select="role=toolbar", rule="collapse"),
            ProjectionRule(select="role=statusbar", rule="prune"),
            ProjectionRule(select="role=separator", rule="prune"),
            ProjectionRule(select="role=progressbar", rule="collapse"),
        ],
    )
)
