"""Scenario S8: dialog — a modal confirmation that appears on demand.

A modal dialog is not part of the tree until it opens, and its buttons are not
addressable until then. An agent must therefore press Delete, read the delta to
discover the dialog and its buttons, and only then confirm or cancel - the loop
this screen pins. The dialog also exercises the compiler's dialog layer (it is
prefixed ``d`` and reported as a dialog, not a screen).
"""

from __future__ import annotations


def test_the_dialog_is_absent_until_it_opens(dialog_session):
    obs = dialog_session.observe().text
    assert "删除" in obs
    # No dialog and none of its buttons while it is closed.
    assert "确定要删除" not in obs, obs
    assert "D dialog" not in obs, obs


def test_pressing_delete_mounts_the_dialog(dialog_session):
    dialog_session.observe()
    out = dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_delete')}.activate()")
    assert not out.startswith("ERR"), out
    # The dialog arrives as deltas, so its message and buttons are readable from
    # the reply that opened it.
    assert "确认删除" in out, out
    assert "确定" in out and "取消" in out, out


def test_confirming_deletes_the_row_and_closes_the_dialog(dialog_session):
    dialog_session.observe()
    assert len(_rows(dialog_session)) == 3
    dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_delete')}.activate()")
    out = dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_confirm')}.activate()")
    assert not out.startswith("ERR"), out

    dialog_session.observe()
    assert len(_rows(dialog_session)) == 2, "confirming must remove one row"
    assert _name(dialog_session, "dialog_status") == "已删除"
    # The dialog is gone: its buttons are not in the tree any more.
    assert _find(dialog_session, "dialog_confirm") is None


def test_cancelling_leaves_everything_alone(dialog_session):
    """Cancel closes the dialog and changes nothing else."""
    dialog_session.observe()
    dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_delete')}.activate()")
    out = dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_cancel')}.activate()")
    assert not out.startswith("ERR"), out

    dialog_session.observe()
    assert len(_rows(dialog_session)) == 3, "cancel must not delete a row"
    assert _name(dialog_session, "dialog_status") == "已取消"
    assert _find(dialog_session, "dialog_confirm") is None


def test_the_open_dialog_is_reported_as_a_dialog_layer(dialog_session):
    """The compiler treats it as a dialog, so its ref is prefixed `d`.

    That prefix is how a model tells "a modal is up" from "a screen changed"."""
    dialog_session.observe()
    dialog_session.dispatch(f"CALL {_ref(dialog_session, 'dialog_delete')}.activate()")
    dialog_session.observe()
    dialog = _find(dialog_session, "dialog_confirm")
    assert dialog is not None
    # The dialog node itself carries the `d` prefix.
    for node in dialog_session.result.root.walk():
        if node.role == "dialog":
            assert node.ref.startswith("d"), node.ref
            break
    else:
        raise AssertionError("no dialog node in the tree")


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


def _rows(session) -> list:
    node = _find(session, "dialog_rows")
    assert node is not None, "rows list not found"
    return node.children
