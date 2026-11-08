"""Golden tests: fixture -> SUI-Text output compared verbatim.

Any token change requires an explicit golden update (``--update-golden``), which
keeps the LLM-facing surface stable across refactors.

``test_compile_idempotent`` is the determinism acceptance: the same interface,
captured twice, must serialize byte-identically (same tree twice, and a fresh
identical tree).
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
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree

GOLDEN_DIR = Path(__file__).parent


def _observe_text(tree, pack):
    result = Compiler().compile(tree, pack=pack)
    proj = apply_projection(result.root, [])
    # The default observation budget reproduces the design-doc L0 example.
    plan = ProjectionEngine(ObservationBudget()).plan(result.root, proj)
    return render_document(plan.lines, rev=1)


#: (golden name, tree builder, pack id or None for generic mode)
_CASES = [
    ("restaurant_l0", lambda: restaurant_demo_tree(), "restaurant-demo"),
    (
        "restaurant_fold",
        lambda: restaurant_demo_tree(
            catalog=[(f"Item {i:02d}", 1.0 + i) for i in range(30)]
        ),
        "restaurant-demo",
    ),
    ("notepad_generic", lambda: notepad_tree(), None),
    ("notepad_long_value", lambda: notepad_tree(text="x" * 200), None),
]


def _pack(pack_id: str | None):
    return get_pack(pack_id) if pack_id else None


@pytest.mark.parametrize("name,builder,pack_id", _CASES)
def test_golden(name, builder, pack_id, request):
    tree = builder()
    text = _observe_text(tree, _pack(pack_id))
    path = GOLDEN_DIR / f"{name}.txt"
    if request.config.getoption("--update-golden", default=False) or not path.exists():
        path.write_text(text + "\n", encoding="utf-8")
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    assert text == expected


@pytest.mark.parametrize("name,builder,pack_id", _CASES)
def test_compile_idempotent(name, builder, pack_id):
    pack = _pack(pack_id)
    tree = builder()
    first = _observe_text(tree, pack)
    # Same tree compiled a second time: the pipeline must not mutate its input.
    assert _observe_text(tree, pack) == first
    # A freshly built identical tree serializes byte-identically.
    assert _observe_text(builder(), pack) == first
