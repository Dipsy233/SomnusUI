"""Compiler tests: normalization, capabilities, entity matching, fingerprints."""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.compiler.normalize import normalize_role, normalize_states
from somnus.packs import get_pack, select_pack
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


def test_normalize_role_aliases():
    assert normalize_role("edit", True, frozenset()) == "input"
    assert normalize_role("combobox", True, frozenset()) == "select"
    assert normalize_role("radiobutton", True, frozenset()) == "radio"


def test_normalize_custom_disambiguated_by_pattern():
    assert normalize_role("custom", False, frozenset({"invoke"})) == "button"
    assert normalize_role("custom", False, frozenset({"value"})) == "input"
    assert normalize_role("custom", False, frozenset()) == "unknown"


def test_normalize_states_drops_defaults():
    states = normalize_states(frozenset({"enabled", "focusable", "disabled"}))
    assert "enabled" not in states
    assert "focusable" not in states
    assert "disabled" in states


def test_select_pack_by_process():
    pack = select_pack("RestaurantDemo.exe")
    assert pack is not None and pack.id == "restaurant-demo"
    assert select_pack("notepad.exe").id == "notepad"
    assert select_pack("some-unknown-app.exe") is None


def test_compile_demo_entities():
    tree = restaurant_demo_tree()
    result = Compiler().compile(tree, pack=get_pack("restaurant-demo"))

    products = [n for n in result.root.walk() if n.entity == "product"]
    assert len(products) == 3
    big_mac = products[0]
    assert big_mac.name == "Big Mac"
    assert big_mac.attrs["price"] == 5.99
    assert "add" in big_mac.cap_names()

    cart = next(n for n in result.root.walk() if n.entity == "cart")
    assert cart.attrs["count"] == 0
    assert cart.attrs["total"] == 0.0

    checkout = next(n for n in result.root.walk() if n.entity == "checkout")
    assert "submit" in checkout.cap_names()
    submit = checkout.get_cap("submit")
    assert submit.requires_confirmation  # critical -> needs confirmation


def test_collection_annotation():
    tree = restaurant_demo_tree()
    result = Compiler().compile(tree, pack=get_pack("restaurant-demo"))
    products_list = next(n for n in result.root.walk() if n.attrs.get("entity") == "product")
    assert products_list.attrs["count"] == 3
    assert "find" in products_list.cap_names()


def test_generic_mode_no_pack():
    tree = notepad_tree(text="hello")
    result = Compiler().compile(tree, pack=None)
    # No entities; primitives only.
    assert not [n for n in result.root.walk() if n.entity]
    editor = next(n for n in result.root.walk() if n.role == "input")
    assert editor.value == "hello"
    assert "set_text" in editor.cap_names()


def test_fingerprint_stable_across_name_change():
    from somnus.ir.node import compute_fingerprint

    fp1 = compute_fingerprint(
        entity="cart", role="group", identity="cart_group", name="0 items"
    )
    fp2 = compute_fingerprint(
        entity="cart", role="group", identity="cart_group", name="1 items $5.99"
    )
    assert fp1 == fp2  # stable identity ignores volatile name


def test_fingerprint_uses_name_without_identity():
    from somnus.ir.node import compute_fingerprint

    fp1 = compute_fingerprint(entity=None, role="button", identity=None, name="Save")
    fp2 = compute_fingerprint(entity=None, role="button", identity=None, name="Save")
    fp3 = compute_fingerprint(entity=None, role="button", identity=None, name="Cancel")
    assert fp1 == fp2
    assert fp1 != fp3


def test_disabled_state_blocks_capability():
    tree = restaurant_demo_tree()
    # Mark the checkout button disabled and recompile.
    for node in tree.walk():
        if node.native.automation_id == "checkout_btn":
            node.states = frozenset({"disabled"})
    result = Compiler().compile(tree, pack=get_pack("restaurant-demo"))
    checkout = next(n for n in result.root.walk() if n.entity == "checkout")
    assert "disabled" in checkout.states


def test_virtualised_collection_count_is_a_lower_bound():
    """A virtualising list only realises some rows: count is `count>=N`."""
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
    from somnus.sui.serialize import render_node_line

    def n(role, name=None, children=(), patterns=(), vc=None):
        x = RawNode(
            native=NativeRef("w1", (abs(hash((role, name))) % 9999,), class_name="x"),
            role=role, name=name, patterns=frozenset(patterns), children=list(children),
        )
        x.virtual_count = vc
        return x

    rows = [n("listitem", name=f"row{i}") for i in range(3)]
    virt = n("list", name="Items", children=rows,
             patterns=("selection", "itemcontainer"), vc=-1)
    empty = n("list", name="Empty", children=[],
              patterns=("selection", "itemcontainer"), vc=-1)
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1),
                   root=n("window", children=[virt, empty]))
    result = Compiler().compile(tree)

    lines = {x.name: render_node_line(x) for x in result.root.walk() if x.role == "list"}
    assert "count>=3" in lines["Items"]
    assert "count=?" in lines["Empty"]


def test_non_virtualised_collection_count_is_exact():
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
    from somnus.sui.serialize import render_node_line

    def n(role, name=None, children=(), patterns=()):
        return RawNode(
            native=NativeRef("w1", (abs(hash((role, name))) % 9999,), class_name="x"),
            role=role, name=name, patterns=frozenset(patterns), children=list(children),
        )

    rows = [n("listitem", name=f"row{i}") for i in range(3)]
    plain = n("list", name="Items", children=rows, patterns=("selection",))
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1),
                   root=n("window", children=[plain]))
    result = Compiler().compile(tree)
    line = next(render_node_line(x) for x in result.root.walk() if x.role == "list")
    assert "count=3" in line and ">=" not in line
