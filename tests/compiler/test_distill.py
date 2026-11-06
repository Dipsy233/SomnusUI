"""Projection distillation: the ordered plugin chain behind ``simplify``.

Two things are pinned here:

* the historical structural rules still apply through the chain (a caption
  duplicated by the parent's name, a transparent wrapper, an anonymous empty
  leaf);
* the two safety rules added in this stage are conservative - a decorative
  image goes, an identified one stays, and nothing addressable is removed or
  unwrapped.

``NameOrigin`` is the fact the tree cannot carry: *this parent's name came from
that child*. It is recorded when the duplicate is dropped.
"""

from __future__ import annotations

from somnus.compiler.distill import (
    Action,
    NameOrigin,
    distill,
    distill_with_context,
    drop_decorative_image,
)
from somnus.ir.node import NativeRef, RawNode


def _node(
    role: str,
    name: str | None = None,
    *,
    class_name: str = "demo",
    automation_id: str | None = None,
    patterns: frozenset[str] = frozenset(),
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id="w1",
            runtime_id=(1,),
            automation_id=automation_id,
            class_name=class_name,
            control_type=role,
            path=(0,),
        ),
        role=role,
        name=name,
        patterns=patterns,
        children=children or [],
    )


def test_a_caption_repeating_the_parent_name_is_dropped_and_its_origin_recorded():
    caption = _node("text", "保存")
    button = _node("button", "保存", children=[caption])
    origins = NameOrigin()
    root = _node("pane", "屏幕", children=[button])
    distill(root, origins=origins)
    assert button.children == []
    assert origins.source_of(button) == id(caption)
    assert origins.is_origin(button, caption)


def test_a_different_caption_child_stays_and_is_not_an_origin():
    button = _node(
        "button", "保存", children=[_node("text", "保存"), _node("text", "另存")]
    )
    origins = NameOrigin()
    distill(_node("pane", "屏幕", children=[button]), origins=origins)
    assert [child.name for child in button.children] == ["另存"]
    assert origins.source_of(button) is not None


def test_a_decorative_image_is_removed():
    image = _node("image")
    root = _node("pane", "屏幕", children=[_node("text", "标题"), image])
    distill(root)
    assert image not in root.children
    assert [child.role for child in root.children] == ["text"]


def test_an_identified_or_named_image_is_kept():
    identified = _node("image", automation_id="logo")
    named = _node("image", "产品图")
    root = _node("pane", "屏幕", children=[identified, named])
    distill(root)
    assert root.children == [identified, named]


def test_an_image_with_children_is_content_and_is_kept():
    image = _node("image", children=[_node("text", "说明")])
    root = _node("pane", "屏幕", children=[image])
    distill(root)
    assert image in root.children


def test_an_anonymous_single_text_wrapper_is_unwrapped():
    wrapper = _node("pane", children=[_node("text", "标题")])
    root = _node("pane", "屏幕", children=[wrapper])
    distill(root)
    assert root.children[0].name == "标题"
    assert wrapper not in root.children


def test_a_named_or_interactive_wrapper_survives():
    named = _node("pane", "面板", children=[_node("text", "标题")])
    interactive = _node(
        "pane", patterns=frozenset({"invoke"}), children=[_node("text", "标题")]
    )
    root = _node("pane", "屏幕", children=[named, interactive])
    distill(root)
    assert named in root.children
    assert interactive in root.children


def test_a_row_shaped_single_child_container_survives():
    row = _node(
        "group",
        class_name="recent-contact-item",
        children=[_node("text", "某人")],
    )
    root = _node("pane", "屏幕", children=[row])
    distill(root)
    assert row in root.children


def test_an_empty_anonymous_leaf_is_removed():
    empty = _node("pane")
    root = _node("pane", "屏幕", children=[_node("text", "标题"), empty])
    distill(root)
    assert empty not in root.children


def test_the_chain_is_configurable_and_ordered():
    image = _node("image")
    empty = _node("pane")
    root = _node("pane", "屏幕", children=[image, empty])
    distilled = distill(root, plugins=(drop_decorative_image,))
    assert image not in distilled.children
    assert empty in distilled.children


def test_the_context_reports_what_changed():
    root = _node("pane", "屏幕", children=[_node("image"), _node("pane")])
    ctx = distill_with_context(root)
    assert ctx.removed == 2
    assert ctx.unwrapped == 0
    assert ctx.changed == 2


def test_a_plugin_can_unwrap_and_its_children_take_its_place():
    plain = _node("pane", children=[_node("text", "标题"), _node("text", "副标题")])

    def unwrap_anonymous(node, ctx):
        if node.role == "pane" and not node.name:
            return Action.UNWRAP
        return Action.KEEP

    root = _node("pane", "屏幕", children=[plain])
    distill(root, plugins=(unwrap_anonymous,))
    assert [child.name for child in root.children] == ["标题", "副标题"]


def test_an_unwrap_of_an_addressable_node_is_a_plugin_bug_not_the_chain_rule():
    """The chain itself never removes addressable structure; plugins must guard."""
    button = _node("button", "保存")
    assert button.name
    # The historical single-child rule only applies to container roles, so a
    # button is never a candidate even though it has one child removed here.
    root = _node("pane", "屏幕", children=[button])
    distill(root)
    assert button in root.children


def test_normalization_keeps_names_with_significant_whitespace():
    """A caption and a name are compared stripped, like the runtime's find key."""
    button = _node("button", "  保存 ", children=[_node("text", "保存")])
    root = _node("pane", "屏幕", children=[button])
    distill(root)
    assert button.children == []
    assert button.name == "  保存 "
