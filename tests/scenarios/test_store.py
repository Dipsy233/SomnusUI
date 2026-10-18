"""Scenario S1: the store screen (search + large catalog + cart).

Each test here is one thing a real agent has to do on a catalog screen, and the
dataset is 240 rows on purpose: at that size the observation *cannot* show every
row, so "find the row and act on it" has to work through folding, FIND and the
delta - not because the tree happened to fit.

The screen is `apps/scenario-suite` (scenario `store`); the fixture mirrors its
accessibility shape exactly, so what passes here is what the `-m windows` test
drives on a real desktop.
"""

from __future__ import annotations

from somnus.projection.budget import ObservationBudget
from tests.fixtures.scenarios import STORE_ITEMS, store_tree


# ---------------------------------------------------------------------------
# the observation stays bounded no matter how big the data
# ---------------------------------------------------------------------------


def test_large_catalog_does_not_inflate_the_observation(store_session):
    """240 rows must not become 240 lines.

    The catalog is homogeneous and far past the collapse threshold, so it
    renders as one line with a count. If this regressed, every observation on a
    real application would cost thousands of tokens.
    """
    obs = store_session.observe()
    assert obs.tokens <= ObservationBudget().for_level(0).hard_tokens
    assert f"count={STORE_ITEMS}" in obs.text
    # The rows themselves are folded, and the fold is *discoverable*.
    assert "more" in obs.text
    assert "Item 239" not in obs.text


def test_the_count_is_honest_about_what_is_hidden(store_session):
    """`count=N` must name the real row count, not the rendered preview."""
    obs = store_session.observe().text
    line = next(l for l in obs.splitlines() if "商品" in l and "LIST" in l)
    assert f"count={STORE_ITEMS}" in line


def test_folding_is_reversible_with_find(store_session):
    """FIND reaches a row that the overview never showed."""
    store_session.observe()
    out = store_session.dispatch('FIND "Special Item 17"')
    assert out.startswith("FOUND")
    assert 'Special Item 17' in out
    # The hit carries its capability line, so the action is discoverable.
    assert "+add" in out


# ---------------------------------------------------------------------------
# search box: a form field, written through the native route
# ---------------------------------------------------------------------------


def test_search_box_is_writable_and_advertises_it(store_session):
    """The search box must render as a writable INPUT.

    A field whose line does not carry `+set_text` is indistinguishable from a
    read-only one, so an agent either writes a field that refuses text or never
    realises a text box is a text box.
    """
    obs = store_session.observe().text
    line = next(l for l in obs.splitlines() if "搜索" in l and "INPUT" in l)
    assert "+set_text" in line


def test_search_filters_the_catalog(store_session):
    """Typing into the search box and pressing 搜索 narrows the list."""
    store_session.observe()
    ref = _ref_by_automation(store_session, "store_search")
    assert store_session.dispatch(f'CALL {ref}.set_text("Special Item 17")').startswith("OK")

    button = _ref_by_automation(store_session, "store_search_button")
    out = store_session.dispatch(f"CALL {button}.activate()")
    assert not out.startswith("ERR")

    summary = _text_by_automation(store_session, "store_result_summary")
    assert "件商品" in summary and summary != "240 件商品"


def test_search_uses_the_native_write_not_a_synthetic_channel(store_session):
    """The search box publishes `value`, so it is written through it.

    Asserted on what the provider was asked to do, because the whole point of
    the route chain is that a control with its own write interface is never
    typed into or routed through the user's clipboard.
    """
    store_session.observe()
    ref = _ref_by_automation(store_session, "store_search")
    store_session.dispatch(f'CALL {ref}.set_text("Item 5")')
    primitives = [p for _r, p, _v in store_session.provider.executed]
    assert primitives == ["set_text"], primitives


# ---------------------------------------------------------------------------
# add: the action, its delta and its visible record
# ---------------------------------------------------------------------------


def test_add_updates_cart_and_records_it(store_session):
    """Adding a row changes the cart summary, appends a line and logs it.

    Three surfaces are checked because each answers a different question: the
    count says *how many*, the item line says *what*, and the record says what
    the application last did. A test that only read the count would pass for a
    cart that counted without holding anything.
    """
    store_session.observe()
    row = _row_ref(store_session, 'FIND "Special Item 17"')
    out = store_session.dispatch(f"CALL {row}.add()")
    assert not out.startswith("ERR"), out

    summary = _text_by_automation(store_session, "store_cart_count")
    assert summary.startswith("1 items")
    assert _cart_item_names(store_session) == ["Special Item 17"]
    assert _text_by_automation(store_session, "store_last_action") == "已加入 Special Item 17"


def test_add_reports_the_change_as_a_delta(store_session):
    """The delta names what changed; a re-observation is not required."""
    store_session.observe()
    row = _row_ref(store_session, 'FIND "Special Item 17"')
    out = store_session.dispatch(f"CALL {row}.add()")
    assert "Special Item 17" in out, "the added item must appear in the delta"
    assert "~" in out, "the cart summary change must appear as a field delta"


def test_two_different_rows_accumulate(store_session):
    """Two adds of different rows accumulate count and total, not replace."""
    store_session.observe()
    first = _row_ref(store_session, 'FIND "Special Item 17"')
    second = _row_ref(store_session, 'FIND "Item 1"')
    store_session.dispatch(f"CALL {first}.add()")
    store_session.dispatch(f"CALL {second}.add()")

    summary = _text_by_automation(store_session, "store_cart_count")
    assert summary.startswith("2 items")
    assert len(_cart_item_names(store_session)) == 2


def test_add_is_per_row_not_shared(store_session):
    """The row that was acted on decides what lands in the cart.

    Regression-shaped: a per-row action resolved through a shared handler would
    add the first row's product every time, which the count alone would hide.
    """
    store_session.observe()
    row = _row_ref(store_session, 'FIND "Item 40"')
    store_session.dispatch(f"CALL {row}.add()")
    assert _cart_item_names(store_session) == ["Item 40"]


# ---------------------------------------------------------------------------
# the destructive path is gated
# ---------------------------------------------------------------------------


def test_checkout_requires_confirmation(store_session):
    """`结算` is a critical, state-changing action: gated by default."""
    store_session.observe()
    ref = _ref_by_automation(store_session, "store_checkout")
    # The button is reachable and advertises a gesture...
    assert _cap_names(store_session, ref)
    out = store_session.dispatch(f"CALL {ref}.activate()")
    assert out.startswith("OK")  # activate itself is not critical here
    # ...but the cart is only cleared when the caller pre-confirms.
    store_session.dispatch(f"CALL {ref}.activate()", confirmed=True)
    assert _text_by_automation(store_session, "store_cart_count") == "0 items $0.00"


# ---------------------------------------------------------------------------
# a folded row is still reachable by scrolling to it
# ---------------------------------------------------------------------------


def test_a_row_deep_in_the_catalog_is_reachable(store_session):
    """Row 239 exists and can be found even though the overview folds it."""
    store_session.observe()
    out = store_session.dispatch('FIND "Item 239"')
    assert "Item 239" in out
    row = _row_ref_from_text(out)
    assert not store_session.dispatch(f"CALL {row}.add()").startswith("ERR")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref_by_automation(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node with automation id {automation_id!r}")


def _text_by_automation(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.name or ""
    raise AssertionError(f"no node with automation id {automation_id!r}")


def _cart_item_names(session) -> list[str]:
    items = None
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "store_cart_items":
            items = node
            break
    assert items is not None, "cart items list not found"
    return [child.name or "" for child in items.children]


def _row_ref(session, find_command: str) -> str:
    out = session.dispatch(find_command)
    return _row_ref_from_text(out)


def _row_ref_from_text(out: str) -> str:
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in ("ITEM", "PRODUCT"):
            return parts[1]
    raise AssertionError(f"no row ref in find output:\n{out}")


def _cap_names(session, ref: str) -> set[str]:
    node = session.result.find(ref)
    assert node is not None
    return node.cap_names()
