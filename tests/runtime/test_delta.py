"""Tree diff: what the delta is allowed to notice.

The delta is the whole reason a call is cheap - it says what changed instead
of re-sending the tree. It is also the only evidence the runtime has for the
``no effect`` verdict, so a change it cannot see is reported to the agent as
"this did nothing", which is the kind of wrong answer that makes an agent
retry an action that already worked.
"""

from __future__ import annotations

from somnus.ir.node import SemanticNode
from somnus.runtime.delta import diff_trees, index_by_fingerprint


def leaf(ref: str, role: str, name: str, *, fp: str | None = None) -> SemanticNode:
    return SemanticNode(
        ref=ref,
        role=role,
        name=name,
        fingerprint=fp or f"fp-{ref}",
    )


def test_name_change_on_a_text_leaf_is_reported():
    """A status line's text *is* its state: `0 items` -> `1 item`."""
    before = index_by_fingerprint(leaf("t1", "text", "0 items"))
    after = index_by_fingerprint(leaf("t1", "text", "1 item"))
    delta = diff_trees(before, after)
    assert not delta.empty
    assert delta.changed[0][1] == "name"
    assert delta.changed[0][2] == "0 items"
    assert delta.changed[0][3] == "1 item"


def test_name_change_on_a_radio_is_reported():
    """A control whose current value lives in its accessible name.

    Regression, found live in Paint: the colour swatch reads
    ``颜色 1: 红色`` and then ``颜色 1: 深红色``, and ``radio`` was not among
    the roles whose name was compared. Every click therefore produced an empty
    delta, the runtime concluded the action had done nothing, and the agent was
    told ``~ no effect; ... the command may be in the menu`` - for a click that
    had in fact changed the colour.
    """
    before = index_by_fingerprint(leaf("r3", "radio", "颜色 1: 红色"))
    after = index_by_fingerprint(leaf("r3", "radio", "颜色 1: 深红色"))
    delta = diff_trees(before, after)
    assert not delta.empty
    assert delta.changed == [
        ("r3", "name", "颜色 1: 红色", "颜色 1: 深红色")
    ]


def test_stateful_roles_cover_the_toggle_family():
    """checkbox / toggle / switch report state the same way, so they are all
    in the set - missing one of them is the same bug in a different app."""
    for role in ("radio", "checkbox", "check", "toggle", "switch", "option"):
        before = index_by_fingerprint(leaf("c1", role, "On"))
        after = index_by_fingerprint(leaf("c1", role, "Off"))
        assert not diff_trees(before, after).empty, role


def test_button_name_change_is_not_reported():
    """A button's label is not its state.

    Deliberately excluded: buttons re-title themselves for reasons that are
    not state (a tab showing the document name it now holds), and a delta that
    reports those is noise the agent has to read past.
    """
    before = index_by_fingerprint(leaf("b1", "button", "Save"))
    after = index_by_fingerprint(leaf("b1", "button", "Save as"))
    assert diff_trees(before, after).empty


def test_group_name_change_is_not_reported():
    """A container's name is a label, and containers carry attrs instead."""
    before = index_by_fingerprint(leaf("g1", "group", "Colors"))
    after = index_by_fingerprint(leaf("g1", "group", "Colours"))
    assert diff_trees(before, after).empty


def test_unchanged_node_produces_no_delta():
    """Sanity: nothing changed really does mean an empty delta.

    This is the case the `no effect` verdict must keep reporting as such - the
    fix above widens what counts as a change, and it must not turn every call
    into a delta line.
    """
    before = index_by_fingerprint(leaf("r3", "radio", "颜色 1: 红色"))
    after = index_by_fingerprint(leaf("r3", "radio", "颜色 1: 红色"))
    assert diff_trees(before, after).empty


def test_a_parent_whose_child_changed_is_not_double_reported():
    """The name comparison is for leaves.

    A container's name often summarises its children ("3 selected"), so
    reporting it changes every line into two. ProseMirror-style composers
    rename themselves after their content, which is why the leaf condition is
    kept alongside the role condition.
    """
    parent = SemanticNode(ref="g1", role="radio", name="Group A", fingerprint="fp-g")
    parent.children.append(leaf("r3", "radio", "颜色 1: 红色"))
    before = index_by_fingerprint(parent)

    parent2 = SemanticNode(ref="g1", role="radio", name="Group A", fingerprint="fp-g")
    child2 = leaf("r3", "radio", "颜色 1: 蓝色")
    parent2.children.append(child2)
    after = index_by_fingerprint(parent2)

    delta = diff_trees(before, after)
    # Only the child reports; the parent's name did not change anyway, and the
    # leaf condition means a renamed parent with children stays silent.
    assert [c[0] for c in delta.changed] == ["r3"]


def test_a_container_that_renames_itself_with_children_stays_silent():
    """The leaf condition, tested directly.

    `radio` is in the stateful set, but a `radio` that *has children* is a
    container. ProseMirror's message editor renames itself after the
    conversation it is showing, and reporting that on every keystroke would
    bury the line that matters.
    """
    parent = SemanticNode(ref="r9", role="radio", name="Chat with Ada", fingerprint="fp-r9")
    parent.children.append(leaf("t1", "text", "hello"))
    before = index_by_fingerprint(parent)

    parent2 = SemanticNode(ref="r9", role="radio", name="Chat with Bob", fingerprint="fp-r9")
    parent2.children.append(leaf("t1", "text", "hello"))
    after = index_by_fingerprint(parent2)

    assert diff_trees(before, after).empty
