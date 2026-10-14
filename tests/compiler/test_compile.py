"""Compiler tests: normalization, capabilities, entity matching, fingerprints."""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.compiler.normalize import normalize_role, normalize_states
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.packs import get_pack, select_pack
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


# ---------------------------------------------------------------------------
# gesture semantics: select (click) vs activate (default action)
# ---------------------------------------------------------------------------


def _listitem_tree(patterns: frozenset[str]) -> RawTree:
    root = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(1,)),
        role="list",
        name="Files",
        children=[
            RawNode(
                native=NativeRef(window_id="w1", runtime_id=(2,)),
                role="listitem",
                name="WhaleBun.png",
                patterns=patterns,
            )
        ],
    )
    return RawTree(
        window=WindowInfo(window_id="w1", title="Files", process="explorer.exe", pid=1),
        root=root,
    )


def test_select_and_activate_are_separate_capabilities():
    """They mean different things and both must survive compilation.

    `select` is SelectionItem (a single click - highlight). `activate` is
    Invoke (the element's *default action* - for a file, "open"). Collapsing
    them would make "pick this file" indistinguishable from "launch this
    file", which is exactly how a stray file gets opened by accident.
    """
    tree = _listitem_tree(frozenset({"invoke", "selectionitem"}))
    result = Compiler().compile(tree, pack=None)
    node = result.find("li1")
    assert node is not None
    caps = node.cap_names()
    assert "select" in caps
    assert "activate" in caps


def test_select_step_targets_selectionitem_not_invoke():
    """Each capability resolves to its own native primitive"""
    tree = _listitem_tree(frozenset({"invoke", "selectionitem"}))
    result = Compiler().compile(tree, pack=None)
    node = result.find("li1")
    assert node is not None
    assert [s.primitive for s in node.get_cap("select").via] == ["select"]
    assert [s.primitive for s in node.get_cap("activate").via] == ["activate"]


def test_a_control_offers_the_context_menu():
    """The context menu is how an application refusing every native gesture is
    still driven, so it is reachable on an ordinary control."""
    tree = _listitem_tree(frozenset({"invoke", "selectionitem"}))
    result = Compiler().compile(tree, pack=None)
    node = result.find("li1")
    assert node is not None
    assert "context_menu" in node.cap_names()


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


def test_generic_mode_grants_capabilities_from_patterns_only():
    """Without any pack, capabilities come from native patterns alone.

    This is the compatibility story (dev doc sections 4.5, 11.3): a new app is
    usable the moment its controls expose standard UIA patterns.
    """
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    def n(role, name, patterns, class_name="", children=()):
        return RawNode(
            native=NativeRef("w1", (abs(hash((role, name))) % 9999,), class_name=class_name),
            role=role, name=name, patterns=frozenset(patterns), children=list(children),
        )

    root = n("window", "w", (), children=[
        n("button", "Save", ("invoke",)),
        n("button", "Bluetooth", ("toggle", "invoke"), class_name="ToggleSwitch"),
        n("edit", "Search", ("value", "text")),
        n("edit", "Read only", ("value", "text")),
        n("listitem", "Cell", ("selectionitem",)),
        n("listitem", "Far row", ("scrollitem",)),
    ])
    root.children[3].states = frozenset({"readonly"})
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1), root=root)

    result = Compiler().compile(tree, pack=None)
    by_name = {x.name: x for x in result.root.walk() if x.name}

    assert "activate" in by_name["Save"].cap_names()
    assert "toggle" in by_name["Bluetooth"].cap_names()
    assert "set_text" in by_name["Search"].cap_names()
    # A read-only Value pattern must not advertise set_text.
    assert "set_text" not in by_name["Read only"].cap_names()
    assert "select" in by_name["Cell"].cap_names()
    # ScrollItem is a real capability, not a guess.
    assert "scroll_into_view" in by_name["Far row"].cap_names()


def test_scroll_into_view_clears_offscreen_in_mock():
    from somnus.providers.mock import MockProvider
    from somnus.runtime.session import Session
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    row = RawNode(
        native=NativeRef("w1", (1,)),
        role="listitem", name="Far row",
        states=frozenset({"offscreen"}),
        patterns=frozenset({"scrollitem"}),
    )
    root = RawNode(
        native=NativeRef("w1", (0,)),
        role="window", name="w", patterns=frozenset(),
        children=[row],
    )
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1), root=root)
    session = Session(MockProvider(fixtures=[tree]))
    session.observe()
    ref = next(n.ref for n in session.result.root.walk() if n.name == "Far row")
    assert session.dispatch(f"CALL {ref}.scroll_into_view()").startswith("OK")


def test_duplicate_siblings_get_distinct_fingerprints():
    """Two identically named siblings must not share a fingerprint.

    QQ has several buttons named "更多"; without disambiguation they collide and
    the registry reports each as ambiguous, making the app inoperable
    (dev doc section 8).
    """
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    def btn(name):
        return RawNode(
            native=NativeRef("w1", (abs(hash(name)) % 9999,)),
            role="button", name=name, patterns=frozenset({"invoke"}),
        )

    root = RawNode(
        native=NativeRef("w1", (0,)), role="window", name="w",
        children=[btn("更多"), btn("保存"), btn("更多")],
    )
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1), root=root)
    result = Compiler().compile(tree)

    more = [n for n in result.root.walk() if n.name == "更多"]
    assert len(more) == 2
    assert more[0].fingerprint != more[1].fingerprint
    assert more[0].ref != more[1].ref


def test_unique_named_siblings_ignore_position():
    """A uniquely named node keeps a position-independent fingerprint."""
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    def build(order):
        return RawTree(
            window=WindowInfo("w1", "w", "app.exe", 1),
            root=RawNode(
                native=NativeRef("w1", (0,)), role="window", name="w",
                children=[
                    RawNode(native=NativeRef("w1", (i + 1,)), role="button",
                            name=name, patterns=frozenset({"invoke"}))
                    for i, name in enumerate(order)
                ],
            ),
        )

    a = Compiler().compile(build(["保存", "取消"]))
    b = Compiler().compile(build(["取消", "保存"]))
    fp_a = {n.name: n.fingerprint for n in a.root.walk() if n.name in ("保存", "取消")}
    fp_b = {n.name: n.fingerprint for n in b.root.walk() if n.name in ("保存", "取消")}
    assert fp_a == fp_b


def test_identical_unnamed_containers_are_distinct():
    """Anonymous wrappers must not collide; position disambiguates them."""
    from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

    def wrap(idx):
        return RawNode(
            native=NativeRef("w1", (idx,)), role="group",
            children=[RawNode(native=NativeRef("w1", (idx, 0)), role="text",
                              name=f"row{idx}")],
        )

    root = RawNode(
        native=NativeRef("w1", (0,)), role="window", name="w",
        children=[wrap(1), wrap(2), wrap(3)],
    )
    tree = RawTree(window=WindowInfo("w1", "w", "app.exe", 1), root=root)
    result = Compiler().compile(tree)
    groups = [n for n in result.root.walk() if n.role == "group"]
    assert len({g.fingerprint for g in groups}) == len(groups)
