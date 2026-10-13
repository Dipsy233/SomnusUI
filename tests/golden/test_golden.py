"""Golden tests: fixture -> SUI-Text output compared verbatim.

Any token change requires an explicit golden update (``--update-golden``), which
keeps the LLM-facing surface stable across refactors (dev doc section 14).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from somnus.compiler.compile import Compiler
from somnus.packs import get_pack
from somnus.projection.budget import ObservationBudget
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import apply_projection
from somnus.sui.serialize import render_document
from tests.fixtures.builders import restaurant_demo_tree

GOLDEN_DIR = Path(__file__).parent


def _observe_text(tree, pack_id):
    result = Compiler().compile(tree, pack=get_pack(pack_id))
    proj = apply_projection(result.root, [])
    # The default observation budget reproduces the design-doc L0 example.
    plan = ProjectionEngine(ObservationBudget()).plan(result.root, proj)
    return render_document(plan.lines, rev=1)


@pytest.mark.parametrize(
    "name,builder,pack_id",
    [
        ("restaurant_l0", lambda: restaurant_demo_tree(), "restaurant-demo"),
    ],
)
def test_golden(name, builder, pack_id, request):
    tree = builder()
    text = _observe_text(tree, pack_id)
    path = GOLDEN_DIR / f"{name}.txt"
    if request.config.getoption("--update-golden", default=False) or not path.exists():
        path.write_text(text + "\n", encoding="utf-8")
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    assert text == expected
