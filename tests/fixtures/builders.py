"""Fixture builders shared by tests and the CLI --demo mode."""

from __future__ import annotations

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo


def _n(
    wid: str,
    path: tuple[int, ...],
    role: str,
    name: str | None = None,
    value: str | None = None,
    *,
    states: frozenset[str] = frozenset(),
    patterns: frozenset[str] = frozenset(),
    automation_id: str | None = None,
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id=wid,
            runtime_id=tuple(1000 + i for i in path),
            automation_id=automation_id,
            class_name="demo",
            control_type=role,
            path=path,
        ),
        role=role,
        name=name,
        value=value,
        states=states,
        patterns=patterns,
        children=children or [],
    )


_BUTTON = frozenset({"invoke"})
_TEXT = frozenset({"text"})
_SElection = frozenset({"selectionitem", "invoke"})
_LIST = frozenset({"scroll", "selection"})


def restaurant_demo_tree(
    cart_count: int = 0,
    cart_total: float = 0.0,
    catalog: list[tuple[str, float]] | None = None,
) -> RawTree:
    """Build the Restaurant Demo accessibility tree (mirrors the WPF app).

    Product groups each contain a title text, a price text and an Add button -
    exactly what the restaurant pack matches. Pass ``catalog`` to build a
    larger menu (used by the compression benchmark).
    """
    if catalog is None:
        catalog = [("Big Mac", 5.99), ("Fries", 2.49), ("Cola", 1.99)]
    wid = "w1"

    def product(idx: int, title: str, price: float, aid: str) -> RawNode:
        return _n(
            wid,
            (1, idx),
            "group",
            name=None,
            automation_id=aid,
            children=[
                _n(wid, (1, idx, 0), "text", name=title, patterns=_TEXT,
                   automation_id=f"{aid}_title"),
                _n(wid, (1, idx, 1), "text", name=f"${price:.2f}", patterns=_TEXT,
                   automation_id=f"{aid}_price"),
                _n(wid, (1, idx, 2), "button", name="Add", patterns=_BUTTON,
                   automation_id=f"{aid}_add"),
            ],
        )

    products = _n(
        wid,
        (1,),
        "list",
        name="Products",
        automation_id="products_list",
        patterns=_LIST,
        children=[
            product(i, title, price, f"product_{i}")
            for i, (title, price) in enumerate(catalog)
        ],
    )

    cart = _n(
        wid,
        (2,),
        "group",
        name="Cart",
        automation_id="cart_group",
        children=[
            _n(wid, (2, 0), "text", name=f"{cart_count} items ${cart_total:.2f}",
               patterns=_TEXT, automation_id="cart_count"),
            _n(wid, (2, 1), "button", name="Open Cart", patterns=_BUTTON,
               automation_id="open_cart"),
        ],
    )

    checkout = _n(wid, (3,), "button", name="Checkout", patterns=_BUTTON,
                  automation_id="checkout_btn")

    screen = _n(wid, (), "pane", name="menu", automation_id="screen_menu",
                children=[products, cart, checkout])

    window = WindowInfo(
        window_id=wid,
        title="Restaurant Demo",
        process="RestaurantDemo.exe",
        pid=4242,
        active=True,
    )
    return RawTree(window=window, root=screen, raw_revision=1)


#: Mock action scripts for the restaurant demo (activate an Add button).
#: Keyed by ``<button name>:activate`` so only the Add buttons mutate the cart.
DEMO_SCRIPTS = {
    "Add:activate": [{"op": "cart_add"}],
    "\u52a0\u5165:activate": [{"op": "cart_add"}],
    "\u6dfb\u52a0:activate": [{"op": "cart_add"}],
}


def demo_scripts() -> dict:
    """Scripted mutations for the restaurant demo mock provider."""
    return {k: [dict(m) for m in v] for k, v in DEMO_SCRIPTS.items()}


def notepad_tree(text: str = "", loading: bool = False) -> RawTree:
    """A minimal Notepad-like tree for generic-mode tests."""
    wid = "w1"
    states = frozenset({"loading"}) if loading else frozenset()
    root = _n(
        wid,
        (),
        "pane",
        name="Untitled - Notepad",
        automation_id="notepad_root",
        children=[
            _n(wid, (0,), "menu", name="File", patterns=frozenset({"expandcollapse"})),
            _n(wid, (1,), "input", name="Text Editor", value=text,
               patterns=frozenset({"value", "text"}), automation_id="editor",
               states=states),
            _n(wid, (2,), "statusbar", name="Ln 1, Col 1"),
        ],
    )
    window = WindowInfo(
        window_id=wid,
        title="Untitled - Notepad",
        process="notepad.exe",
        pid=1234,
        active=True,
    )
    return RawTree(window=window, root=root, raw_revision=1)
