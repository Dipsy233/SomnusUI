"""Restaurant Demo pack - the v0.1 acceptance pack (dev doc sections 15, 19.1).

The demo app is a WPF order screen with this accessibility shape::

    SCREEN "menu"
      GROUP "Products"                      <- product collection
        GROUP                                       (one PRODUCT)
          TEXT   "Big Mac"
          TEXT   "$5.99"
          BUTTON "Add"
        ...
      GROUP "Cart"                          <- CART
        TEXT   "0 items"
        TEXT   "$0.00"
        BUTTON "Open Cart"
      BUTTON "Checkout"

The pack matches ``product`` groups structurally (a named text + a numeric price
text + an Add button), which is robust to the exact WPF layout.
"""

from __future__ import annotations

from somnus.compiler.pack_schema import (
    EntityCapDef,
    EntityDef,
    MatchRule,
    Pack,
    ProjectionRule,
    register,
)
from somnus.ir.capability import ParamSpec

_ADD_BUTTON = MatchRule(role="button", name=r"^(add|加入|添加)$")
_TITLE_TEXT = MatchRule(role="text", has_name=True)

PRODUCT = EntityDef(
    name="product",
    match=MatchRule(
        role=("group", "listitem", "row"),
        has=[
            MatchRule(role="text", has_name=True, alias="title"),
            MatchRule(role="text", name=r"\d+[.,]\d{2}", alias="price_text"),
            MatchRule(role="button", name=r"^(add|加入|添加)$", alias="add_btn"),
        ],
    ),
    # fingerprint key: prefer automation id of the title, fall back to its name
    key="$title.automation_id",
    attrs={
        "name": "$title.name",
        "price": {"from": "$price_text.name", "regex": r"(\d+[.,]\d{2})", "cast": "number"},
    },
    caps={
        "add": EntityCapDef(
            name="add",
            via=["$add_btn.activate"],
            effects=["cart.count += 1"],
            risk="normal",
        ),
        "customize": EntityCapDef(
            name="customize",
            via=["$add_btn.activate"],
            params={"size": ParamSpec(type="enum", enum=("small", "medium", "large"))},
            risk="normal",
        ),
    },
    aliases={},
)

CART = EntityDef(
    name="cart",
    match=MatchRule(
        role="group",
        has=[
            MatchRule(role="text", name=r"(items?|件|项)", alias="count_text"),
            MatchRule(role="button", name=r"(open|view|查看|购物车)", alias="open_btn"),
        ],
    ),
    key="$open_btn.automation_id",
    attrs={
        "count": {"from": "$count_text.name", "regex": r"(\d+)", "cast": "int", "default": 0},
        "total": {"from": "$count_text.name", "regex": r"(\d+[.,]\d{2})", "cast": "number", "default": 0.0},
    },
    caps={
        "open": EntityCapDef(
            name="open",
            via=["$open_btn.activate"],
            effects=["cart.opened = true"],
            risk="safe",
        ),
    },
)

CHECKOUT = EntityDef(
    name="checkout",
    match=MatchRule(role="button", name=r"^(checkout|结账|去结算)$"),
    attrs={},
    caps={
        "submit": EntityCapDef(
            name="submit",
            via=["$self.activate"],
            effects=[],
            risk="critical",
        ),
    },
)

RESTAURANT_DEMO = register(
    Pack(
        id="restaurant-demo",
        match_process=("restaurantdemo.exe", "restaurant demo", "restaurant_demo"),
        projection=[
            ProjectionRule(select="name=Navigation", rule="prune"),
            ProjectionRule(select="role=toolbar", rule="collapse"),
        ],
        entities=[PRODUCT, CART, CHECKOUT],
        named_refs={
            "cart": CART.match,
            "products": MatchRule(role=("group", "list", "listitem"), name=r"(products?|menu|菜单)"),
        },
    )
)
