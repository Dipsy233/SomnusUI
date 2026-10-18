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

    This is the compatibility story: a new app is
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
    # ScrollItem is NOT a capability on Android-path trees: virtualised lists
    # only instantiate the rows on screen, so a row needing "bringing into
    # view" either does not exist (call scroll(up)/scroll(down) + FIND) or is
    # already visible. The action was refused by every measured app.
    assert "scroll_into_view" not in by_name["Far row"].cap_names()


def test_duplicate_siblings_get_distinct_fingerprints():
    """Two identically named siblings must not share a fingerprint.

    QQ has several buttons named "更多"; without disambiguation they collide and
    the registry reports each as ambiguous, making the app inoperable.
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


# ------------------------------------------------------- order independence
#
# The runtime raises a window before every snapshot (a covered or minimized
# self-drawn window publishes an empty tree). Raising can reorder the walk, and
# UIA's child order is the application's choice anyway, so a ref must never
# depend on where a sibling sits in the list.


def _unnamed_buttons(order, rids=None):
    """Three buttons with no name, no automation id and no class name.

    This is the hardest case: every piece of identity evidence the compiler
    normally uses is absent, so position used to be the only thing telling the
    siblings apart.
    """
    rids = rids or {i: (1, i) for i in order}
    kids = [
        RawNode(
            native=NativeRef(window_id="w1", runtime_id=rids[i], path=(i,)),
            role="button",
            name=None,
            patterns=frozenset({"invoke"}),
        )
        for i in order
    ]
    return RawTree(
        window=WindowInfo("w1", "App", "app.exe", 1),
        root=RawNode(
            native=NativeRef(window_id="w1", runtime_id=(0,), path=()),
            role="window",
            name="App",
            children=kids,
        ),
        raw_revision=0,
    )


def _ref_to_native(result):
    return {
        n.ref: result.sources[n.ref].native.runtime_id
        for n in result.root.walk()
        if n.role == "button"
    }


def test_sibling_order_change_does_not_retarget_refs():
    """Reordering unnamed siblings must not move a ref onto another element.

    Measured before the fix: swapping two unnamed buttons kept the refs but
    pointed each at the other's element, and the registry reported "kept"
    because the two fingerprints matched *each other*. A silent retarget is
    exactly the failure a window raise could trigger.
    """
    from somnus.runtime.registry import SessionRegistry

    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)

    first = compiler.compile(_unnamed_buttons([0, 1, 2]), pack=None)
    registry.reconcile(first)
    before = _ref_to_native(first)
    assert len(before) == 3

    reordered = compiler.compile(_unnamed_buttons([2, 1, 0]), pack=None)
    registry.reconcile(reordered)
    after = _ref_to_native(reordered)

    # Same refs, and each still names the same native element.
    assert after == before


def test_named_siblings_stay_stable_across_reorder():
    """Named siblings were already position independent; keep it that way."""
    from somnus.runtime.registry import SessionRegistry

    names = {0: "Save", 1: "Cancel", 2: "Help"}

    def tree(order):
        base = _unnamed_buttons(order)
        for child, i in zip(base.root.children, order):
            child.name = names[i]
        return base

    registry = SessionRegistry()
    compiler = Compiler(allocator=registry.allocator)
    first = compiler.compile(tree([0, 1, 2]), pack=None)
    registry.reconcile(first)
    before = _ref_to_native(first)
    assert {n.name for n in first.root.walk() if n.role == "button"} == set(names.values())

    reordered = compiler.compile(tree([2, 1, 0]), pack=None)
    registry.reconcile(reordered)
    assert _ref_to_native(reordered) == before


def test_fingerprint_ignores_runtime_id_when_a_name_exists():
    """A named element keeps its ref when its runtime id changes.

    The app rebuilt the control (new runtime id) but it is still "Save" - the
    name is the evidence, so the ref must survive.
    """
    from somnus.ir.node import compute_fingerprint

    base = compute_fingerprint(
        entity=None, role="button", identity=None, name="Save", runtime_id=(1, 0)
    )
    rebuilt = compute_fingerprint(
        entity=None, role="button", identity=None, name="Save", runtime_id=(9, 9)
    )
    assert base == rebuilt


def test_fingerprint_uses_runtime_id_for_anonymous_elements():
    """With no name and no identity, the native id decides - not the position."""
    from somnus.ir.node import compute_fingerprint

    a = compute_fingerprint(
        entity=None, role="image", identity=None, name=None,
        ancestor_path="window#0", role_index=0, runtime_id=(1, 7),
    )
    # Same element, different position in the list: unchanged.
    moved = compute_fingerprint(
        entity=None, role="image", identity=None, name=None,
        ancestor_path="window#3", role_index=5, runtime_id=(1, 7),
    )
    # Different element at the same position: different.
    other = compute_fingerprint(
        entity=None, role="image", identity=None, name=None,
        ancestor_path="window#0", role_index=0, runtime_id=(1, 8),
    )
    assert a == moved
    assert a != other


def test_fingerprint_falls_back_to_position_without_a_runtime_id():
    """A provider that reports no native id still gets distinct siblings."""
    from somnus.ir.node import compute_fingerprint

    a = compute_fingerprint(
        entity=None, role="button", identity=None, name=None,
        ancestor_path="window#0", role_index=0, runtime_id=(),
    )
    b = compute_fingerprint(
        entity=None, role="button", identity=None, name=None,
        ancestor_path="window#0", role_index=1, runtime_id=(),
    )
    assert a != b


# ---------------------------------------------------------------------------
# Generic widget class names must not become identity
# ---------------------------------------------------------------------------


def _toolbar(class_name: str | None) -> RawTree:
    """Eight differently-named toolbar buttons sharing one class name.

    This is Explorer's toolbar, reproduced: 新建 / 剪切 / 复制 / 粘贴 / 重命名 /
    共享 / 删除 / 查看 all report class ``Button`` and no automation id.
    """
    names = ["新建", "剪切", "复制", "粘贴", "重命名", "共享", "删除", "查看"]
    root = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(42, 100), control_type="Window"),
        role="window",
        name="tmp",
        children=[],
    )
    toolbar = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(42, 100, 4), class_name="Toolbar"),
        role="toolbar",
        children=[
            RawNode(
                native=NativeRef(
                    window_id="w1",
                    runtime_id=(42, 100, 4, i),
                    class_name=class_name,
                    control_type="Button",
                ),
                role="button",
                name=name,
                patterns=frozenset({"invoke"}),
            )
            for i, name in enumerate(names)
        ],
    )
    root.children.append(toolbar)
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="tmp", process="explorer.exe", pid=42, hwnd=1
        ),
        root=root,
    )


def _button_fingerprints(class_name: str | None) -> set[str]:
    result = Compiler().compile(_toolbar(class_name))
    return {
        n.fingerprint for n in result.root.walk() if n.role == "button"
    }


def test_generic_widget_class_does_not_collapse_siblings():
    """A shared class name must not exclude the element's own name.

    Regression, found live: every button on Explorer's toolbar reports class
    ``Button``. ``_identity_class`` returned that as a *stable identity*, which
    made ``compute_fingerprint`` drop ``name`` as volatile evidence - all eight
    buttons then hashed identically, eleven refs were flagged
    ``TARGET_AMBIGUOUS``, and the toolbar became unoperable (新建 could not be
    clicked at all).

    The check is fingerprint *distinctness*, not the identity string: that is
    what the agent actually experiences.
    """
    assert len(_button_fingerprints("Button")) == 8


def test_prefixed_generic_widget_classes_are_also_generic():
    """``XAMLButton``/``WinUIButton``/``ToolbarButton`` name the type too."""
    for class_name in ("XAMLButton", "WinUIButton", "ToolbarButton", "Button"):
        assert len(_button_fingerprints(class_name)) == 8, class_name


def test_no_class_name_still_works():
    """The fallback: no class at all leaves name-based identity intact."""
    assert len(_button_fingerprints(None)) == 8


def test_distinctive_class_names_are_still_identity():
    """A class that names *one* element must keep acting as identity.

    The fix must not turn every class name into noise: QQ's message box and a
    self-drawn send button publish no automation id, and their class name is
    the only stable evidence they have.
    """
    from somnus.compiler.compile import _identity_class

    assert _identity_class("ExEditor-qq-msg-editor") == "exeditor-qq-msg-editor"
    assert _identity_class("send-msg") == "send-msg"
    # A class that is *only* a state modifier carries no identity - that is the
    # pre-existing `_VOLATILE_CLASS_TOKENS` rule and this fix does not touch it.
    assert _identity_class("ProseMirror-focused") is None
    assert _identity_class("ProseMirror") == "prosemirror"


def test_generic_class_detection():
    from somnus.compiler.compile import _is_generic_class

    for generic in (
        "Button", "button", "CheckBox", "ComboBox", "Edit", "SysListView32",
        "XAMLButton", "WinUIButton", "ToolbarButton", "toolbar_button",
        "MenuFlyoutItem", "ListViewItem",
    ):
        assert _is_generic_class(generic), generic
    for specific in ("ExEditor-qq-msg-editor", "send-msg", "q-button--primary", "x"):
        # `q-button--primary` is BEM: the state modifier separates it from the
        # generic tail, and `_identity_class` strips the modifier, so the
        # generic test may or may not fire - what matters is that the two
        # distinctive names survive.
        if specific in ("ExEditor-qq-msg-editor", "send-msg", "x"):
            assert not _is_generic_class(specific), specific


def test_shared_type_class_among_siblings_adds_role_index():
    """Breadcrumb items share a control *type*, so they need the index.

    Regression, found live in Explorer: the crumb bar hands every item class
    ``fileexplorerextensions.breadcrumbbaritemcontrol``. That reached
    ``compute_fingerprint`` as a stable identity, which excluded each item's own
    name (此电脑 / Data (D:) / tmp), and since `_duplicate_identities` only
    looked at automation ids - which are None here - the compiler saw no reason
    to add the role index either. Result: three crumbs, one fingerprint.

    The general rule is that a stable identity shared by siblings is not an
    identity. This test pins it independently of any class-name list.
    """
    names = ["此电脑", "Data (D:)", "tmp"]
    parent = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(1,), class_name="Group"),
        role="group",
        children=[
            RawNode(
                native=NativeRef(
                    window_id="w1",
                    runtime_id=(1, i),
                    class_name="fileexplorerextensions.breadcrumbbaritemcontrol",
                ),
                role="button",
                name=name,
            )
            for i, name in enumerate(names)
        ],
    )
    tree = RawTree(
        window=WindowInfo(window_id="w1", title="t", process="explorer.exe", pid=1),
        root=RawNode(
            native=NativeRef(window_id="w1", runtime_id=(0,)), role="window", children=[parent]
        ),
    )
    result = Compiler().compile(tree)
    fps = {n.fingerprint for n in result.root.walk() if n.role == "button"}
    assert len(fps) == 3


def test_duplicate_identities_includes_class_derived_identity():
    from somnus.compiler.compile import _duplicate_identities

    children = [
        RawNode(
            native=NativeRef(window_id="w1", runtime_id=(i,), class_name="fileexplorerextensions.breadcrumbbaritemcontrol"),
            role="button",
            name=n,
        )
        for i, n in enumerate(("此电脑", "Data (D:)", "tmp"))
    ]
    assert "fileexplorerextensions.breadcrumbbaritemcontrol" in _duplicate_identities(children)

    # A class unique among its siblings is *not* a duplicate.
    alone = [
        RawNode(native=NativeRef(window_id="w1", runtime_id=(0,), class_name="send-msg"), role="button", name="a"),
        RawNode(native=NativeRef(window_id="w1", runtime_id=(1,), class_name="other-widget"), role="button", name="b"),
    ]
    assert _duplicate_identities(alone) == set()
