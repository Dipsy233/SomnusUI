"""P2-1 end to end: selecting a drop-down row really moves the combo box.

The mock mirrors what the live WPF combo box does: the row publishes
SelectionItem/Invoke, and selecting it clears the old row's ``selected`` state
and sets the new one. What this pins is the *observable* result:

* the combo box projects ``value="低"``;
* ``CALL li2.select()`` runs the SelectionItem route (not Invoke, not typing);
* the settled delta names the new value (``~ sel1 value 低→中``) instead of
  coming back empty - which is what makes the call verifiable at all.
"""

from __future__ import annotations

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session

WID = "w1"


def _n(
    path: tuple[int, ...],
    role: str,
    name: str | None = None,
    *,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    automation_id: str | None = None,
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id=WID,
            runtime_id=tuple(1000 + i for i in path),
            automation_id=automation_id,
            class_name="demo",
            control_type=role,
            path=path,
        ),
        role=role,
        name=name,
        states=states,
        patterns=patterns,
        children=children or [],
    )


def _combo_tree(selected: str = "低") -> RawTree:
    items = [
        _n(
            (0, i),
            "listitem",
            label,
            patterns=frozenset({"selectionitem", "invoke"}),
            states=frozenset({"selected"}) if label == selected else frozenset(),
        )
        for i, label in enumerate(("低", "中", "高"))
    ]
    combo = _n(
        (0,),
        "select",
        "优先级",
        patterns=frozenset({"expandcollapse", "selection"}),
        automation_id="form_priority",
        children=items,
    )
    root = _n((), "pane", "form", children=[combo])
    return RawTree(
        window=WindowInfo(
            window_id=WID, title="form", process="demo.exe", pid=1, active=True
        ),
        root=root,
        raw_revision=1,
    )


def _pick_selected(node: RawNode, root: RawNode) -> None:
    """Application behaviour: one row selected, every other row cleared."""
    for item in root.walk():
        if item.role == "listitem":
            item.states = frozenset(s for s in item.states if s != "selected")
    node.states = node.states | {"selected"}


def _session(tree: RawTree) -> Session:
    provider = MockProvider(
        fixtures=[tree],
        callbacks={"pick": _pick_selected},
        scripts={
            "中:select": [{"op": "callback", "fn": "pick"}],
            "高:select": [{"op": "callback", "fn": "pick"}],
            "高:activate": [{"op": "callback", "fn": "pick"}],
        },
    )
    return Session(provider)


def _combo(session: Session) -> RawNode:
    return next(n for n in session.result.root.walk() if n.role == "select")


def test_the_observation_names_the_current_selection():
    session = _session(_combo_tree(selected="低"))
    text = session.observe().text
    line = next(l for l in text.splitlines() if "优先级" in l)
    assert 'value="低"' in line, line
    assert "+set_text" not in line, line


def test_select_runs_the_selection_item_route_and_the_delta_shows_it():
    session = _session(_combo_tree(selected="低"))
    session.observe()
    row = next(
        n for n in session.result.root.walk()
        if n.role == "listitem" and n.name == "中"
    )
    out = session.dispatch(f"CALL {row.ref}.select()")
    assert not out.startswith("ERR"), out
    # The route that ran is SelectionItem, not Invoke (which is only a fallback).
    primitives = [entry["primitive"] for entry in session.audit_log()]
    assert primitives[-1] == "select", primitives
    # The settled delta names the new value, so the call is verifiable.
    assert "~ sel1 value 低→中" in out, out
    combo = _combo(session)
    assert combo.attrs.get("value") == "中"


def test_a_ghost_row_that_ignores_select_escalates_to_invoke():
    """The element accepts Select and stays unselected; Invoke must be next.

    The mock reports a successful ``select`` for a row that only publishes
    ``invoke`` after SelectionItem is refused; the runtime must not return
    ACTION_NOT_SUPPORTED while a declared route is still available.
    """
    tree = _combo_tree(selected="低")
    for item in tree.root.walk():
        if item.role == "listitem" and item.name == "高":
            # The ghost copy: no SelectionItem, only Invoke.
            item.patterns = frozenset({"invoke"})
    session = _session(tree)
    session.observe()
    row = next(
        n for n in session.result.root.walk()
        if n.role == "listitem" and n.name == "高"
    )
    out = session.dispatch(f"CALL {row.ref}.select()")
    assert not out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    primitives = [entry["primitive"] for entry in session.audit_log()]
    assert primitives == ["select", "activate"], primitives
    assert "~ sel1 value 低→高" in out, out
