"""Projection and token-budget tests."""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.packs import get_pack
from somnus.projection.budget import ObservationBudget, count_tokens, estimate_tokens
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import Projection, Rule, Selector, apply_projection
from somnus.sui.serialize import render_document
from tests.fixtures.builders import restaurant_demo_tree


def _compiled():
    return Compiler().compile(restaurant_demo_tree(), pack=get_pack("restaurant-demo"))


def test_selector_parsing():
    sel = Selector.parse('role=button,name="/Add/"')
    assert sel.role == "button"
    assert sel.name == "/Add/"


def test_prune_hides_subtree():
    result = _compiled()
    proj = apply_projection(result.root, [], default_prune=False)
    # Force-prune the products list.
    products = next(n for n in result.root.walk() if n.attrs.get("entity") == "product")
    proj.dispositions[id(products)] = Rule.PRUNE
    plan = ProjectionEngine().plan(result.root, proj)
    text = render_document(plan.lines, rev=1)
    assert "PRODUCT" not in text


def test_flatten_promotes_children():
    result = _compiled()
    proj = Projection()
    cart = next(n for n in result.root.walk() if n.entity == "cart")
    proj.dispositions[id(cart)] = Rule.FLATTEN
    plan = ProjectionEngine().plan(result.root, proj)
    text = render_document(plan.lines)
    assert "CART" not in text
    assert 'BUTTON b4 "Open Cart"' in text


def test_auto_collapse_large_collection():
    catalog = [(f"Item {i}", 1.0 + i) for i in range(24)]
    tree = restaurant_demo_tree(catalog=catalog)
    result = Compiler().compile(tree, pack=get_pack("restaurant-demo"))
    proj = apply_projection(result.root, [])
    plan = ProjectionEngine().plan(result.root, proj)
    text = render_document(plan.lines)
    assert "count=24" in text
    assert "+24 more" in text
    # Children are hidden behind the collapse.
    assert text.count("PRODUCT") == 0


def test_budget_truncates_with_marker():
    tree = restaurant_demo_tree(catalog=[(f"Item {i}", 1.0) for i in range(30)])
    result = Compiler().compile(tree, pack=get_pack("restaurant-demo"))
    proj = apply_projection(result.root, [])
    budget = ObservationBudget(max_nodes=5, max_tokens=100)
    engine = ProjectionEngine(budget, auto_collapse_threshold=1000)
    plan = engine.plan(result.root, proj)
    assert plan.truncated
    text = render_document(plan.lines)
    assert "+" in text and "more" in text


def test_token_estimate_cjk():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcd") == 1


def test_l0_budget_under_200():
    result = _compiled()
    proj = apply_projection(result.root, [])
    engine = ProjectionEngine(ObservationBudget().for_level(0))
    plan = engine.plan(result.root, proj)
    text = render_document(plan.lines, rev=1)
    assert count_tokens(text) <= 200
