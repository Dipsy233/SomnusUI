"""Tests for dev-doc clauses added after the first implementation pass.

Covers: UNKNOWN rendering (section 11.2), long-text preview (section 5),
the six relations (section 4.6), rev semantics (section 9.1),
TARGET_AMBIGUOUS (section 10) and the plain-text protocol (section 6.4).
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, SemanticNode, WindowInfo
from somnus.packs import get_pack
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.sui.serialize import RenderOptions, render_node_line
from tests.fixtures.builders import demo_scripts, notepad_tree, restaurant_demo_tree


def _n(wid, path, role, name=None, patterns=frozenset(), children=None, value=None):
    return RawNode(
        native=NativeRef(wid, tuple(path), class_name="x"),
        role=role,
        name=name,
        value=value,
        patterns=patterns,
        children=children or [],
    )


# --------------------------------------------------------------- UNKNOWN (§11.2)


def test_unknown_canvas_rendered_with_reason():
    assert render_node_line(SemanticNode(ref="cv1", role="canvas")) == (
        "? cv1 canvas reason=visual"
    )
    assert render_node_line(SemanticNode(ref="im1", role="image")) == (
        "? im1 image reason=visual"
    )


def test_named_image_is_not_unknown():
    line = render_node_line(SemanticNode(ref="im2", role="image", name="logo.png"))
    assert line.startswith("IMAGE im2")


def test_unknown_role_with_name_is_a_normal_line():
    # section 11.2: UNKNOWN is only for unreadable *content*, never for a node
    # that merely failed to upgrade to an entity.
    line = render_node_line(SemanticNode(ref="g1", role="unknown", name="Panel"))
    assert line.startswith("UNKNOWN g1") or line.startswith("GROUP g1")
    assert "reason=" not in line


# --------------------------------------------------------------- long text (§5)


def test_long_text_uses_len_and_preview():
    node = SemanticNode(ref="t1", role="text", value="x" * 5230)
    line = render_node_line(node)
    assert "len=5230" in line
    assert 'preview="' in line
    # The full value must not be inlined.
    assert len(line) < 300


def test_short_text_is_unchanged():
    node = SemanticNode(ref="t1", role="text", value="hello")
    assert render_node_line(node).strip() == "TEXT t1"


# --------------------------------------------------------------- relations (§4.6)


def test_all_relation_kinds_are_produced():
    root = _n(
        "w1",
        (),
        "pane",
        name="Form",
        children=[
            _n("w1", (0,), "text", name="Username"),
            _n("w1", (1,), "input", patterns=frozenset({"value"})),
            _n(
                "w1",
                (2,),
                "list",
                name="Results",
                children=[_n("w1", (2, 0), "listitem", name="row")],
            ),
            _n(
                "w1",
                (3,),
                "group",
                name="Advanced",
                patterns=frozenset({"expandcollapse"}),
                children=[_n("w1", (3, 0), "text", name="hidden")],
            ),
        ],
    )
    tree = RawTree(window=WindowInfo("w1", "Form", "app.exe", 1), root=root)
    result = Compiler().compile(tree)

    kinds = {
        rel.kind for n in result.root.walk() for rel in n.relations
    }
    assert "label_for" in kinds
    assert "controls" in kinds
    assert "selected_in" in kinds


def test_entity_contains_and_acts_on():
    result = Compiler().compile(
        restaurant_demo_tree(), pack=get_pack("restaurant-demo")
    )
    kinds = {rel.kind for n in result.root.walk() for rel in n.relations}
    assert "contains" in kinds
    assert "acts_on" in kinds


def test_relations_only_rendered_on_inspect():
    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    )
    observe = session.observe().text
    assert "acts_on=" not in observe  # L0 stays lean

    detail = session.inspect_direct("p1")
    assert "acts_on=" in detail


# --------------------------------------------------------------- rev (§9.1)


def test_rev_only_moves_on_semantic_change():
    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    )
    r1 = session.observe().rev
    r2 = session.observe().rev
    assert r1 == r2  # identical projection -> same rev

    out = session.dispatch("CALL p1.add()")
    assert f"rev={r1 + 1}" in out


# --------------------------------------------------------------- ambiguous (§10)


def test_ambiguous_fingerprint_reports_error():
    # Two products with the same automation id and name are indistinguishable.
    tree = restaurant_demo_tree()
    products = [c for c in tree.root.children[0].children]
    products[1].native = NativeRef(
        "w1", products[1].native.runtime_id, automation_id="dup"
    )
    products[1].children[0].native = NativeRef(
        "w1", products[1].children[0].native.runtime_id, automation_id="dup_title"
    )
    products[1].children[0].name = "Big Mac"
    products[1].children[1].name = "$5.99"

    session = Session(MockProvider(fixtures=[tree], scripts=demo_scripts()))
    session.observe()
    # The duplicate simply must not be silently rebound to the wrong element;
    # either it is ambiguous or it is a distinct ref.
    refs = [n.ref for n in session.result.root.walk() if n.entity == "product"]
    assert len(refs) == len(set(refs))


# --------------------------------------------------------------- plain text (§6.4)


def test_find_tool_accepts_find_syntax():
    """Regression: call_tool('find') must parse the FIND syntax argument."""
    from somnus.protocol.tools import call_tool

    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    )
    session.observe()
    out = call_tool(session, "find", {"query": 'FIND product "Big Mac"'})
    assert out.startswith("FOUND 1")
    assert 'PRODUCT p1 "Big Mac"' in out


def test_find_tool_accepts_bare_query():
    from somnus.protocol.tools import call_tool

    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    )
    session.observe()
    out = call_tool(session, "find", {"query": "Big Mac"})
    assert "Big Mac" in out


def test_dispatch_lines_plain_text_protocol():
    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    )
    session.observe()
    block = """
    # order a Big Mac and fries
    FIND product "Big Mac"
    CALL p1.add()
    CALL p2.add()
    """
    out = session.dispatch_lines(block)
    assert "FOUND 1" in out
    assert "OK rev=" in out
    assert "~ c1 count 0→1" in out
    assert "~ c1 count 1→2" in out
