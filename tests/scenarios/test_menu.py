"""Scenario S7: menu — a menu bar and a context menu as navigation.

A menu is not a list of always-present buttons; its items are *realized when the
menu opens*. An agent therefore has to activate the bar entry, read the delta to
find the item, and only then choose it - which is exactly the loop this screen
exists to pin. The context menu covers the other half: the runtime's
``context_menu`` gesture mounting a menu under the node that owns it.
"""

from __future__ import annotations


def test_menu_items_are_absent_until_the_menu_opens(menu_session):
    """The bar entries are there; their items are not.

    This is what makes a menu navigation rather than a lookup: guessing an item's
    ref before opening the menu must be impossible."""
    obs = menu_session.observe().text
    assert "文件" in obs
    # No item is in the tree while every menu is closed.
    assert "新建" not in obs, obs
    assert "撤销" not in obs, obs


def test_opening_a_menu_realizes_its_items(menu_session):
    menu_session.observe()
    ref = _ref(menu_session, "menu_bar_文件")
    out = menu_session.dispatch(f"CALL {ref}.activate()")
    assert not out.startswith("ERR"), out
    # The items arrive as deltas, so they are readable from the reply itself.
    assert "新建" in out and "保存" in out, out

    menu_session.observe()
    assert _ref(menu_session, "menu_item_new") is not None


def test_choosing_an_item_is_recorded_and_closes_the_menu(menu_session):
    """The action is observable, and the menu is gone again afterwards."""
    menu_session.observe()
    menu_session.dispatch(f"CALL {_ref(menu_session, 'menu_bar_文件')}.activate()")
    out = menu_session.dispatch(f"CALL {_ref(menu_session, 'menu_item_save')}.activate()")
    assert not out.startswith("ERR"), out

    menu_session.observe()
    assert _name(menu_session, "menu_status") == "已执行: 保存"
    # The menu closed: its items are no longer in the tree.
    assert _find(menu_session, "menu_item_save") is None


def test_only_the_opened_menu_shows_its_items(menu_session):
    """Opening one menu does not leave another's items behind."""
    menu_session.observe()
    menu_session.dispatch(f"CALL {_ref(menu_session, 'menu_bar_文件')}.activate()")
    menu_session.dispatch(f"CALL {_ref(menu_session, 'menu_bar_编辑')}.activate()")
    menu_session.observe()
    assert _ref(menu_session, "menu_item_undo") is not None
    assert _find(menu_session, "menu_item_save") is None


def test_context_menu_mounts_the_menu_under_the_row(menu_session):
    """``context_menu`` on the row produces a menu whose items are reachable."""
    menu_session.observe()
    ref = _ref(menu_session, "menu_row")
    out = menu_session.dispatch(f"CALL {ref}.context_menu()")
    # A menu really appeared, so this is a plain OK rather than UNVERIFIED.
    assert out.startswith("OK"), out
    assert "重命名" in out and "删除" in out, out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref(session, automation_id: str) -> str:
    node = _find(session, automation_id)
    assert node is not None, f"no node {automation_id!r}"
    return node.ref


def _find(session, automation_id: str):
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node
    return None


def _name(session, automation_id: str) -> str:
    node = _find(session, automation_id)
    assert node is not None, f"no node {automation_id!r}"
    return node.name or ""
