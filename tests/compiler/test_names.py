"""Name synthesis: the label is in the tree, so the node gets a name.

The risk this pass carries is ref stability, so the fingerprint tests matter as
much as the naming ones: a synthesised name must never become identity evidence.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.compiler.match import _default_identity
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

_BUTTON = frozenset({"invoke"})
_TEXT = frozenset({"text"})
_VALUE = frozenset({"value", "text"})


def _node(
    role: str,
    name: str | None = None,
    *,
    runtime_id: int,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    description: str | None = None,
    value: str | None = None,
    children: list[RawNode] | None = None,
) -> RawNode:
    return RawNode(
        native=NativeRef(window_id="w1", runtime_id=(runtime_id,)),
        role=role,
        name=name,
        value=value,
        description=description,
        states=states,
        patterns=patterns,
        children=children or [],
    )


def _tree(children: list[RawNode]) -> RawTree:
    root = _node("pane", "Main", runtime_id=1, children=children)
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="Main", process="app.exe", pid=1, active=True
        ),
        root=root,
    )


def _compile(children: list[RawNode], **kwargs):
    return Compiler(**kwargs).compile(_tree(children), pack=None)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def test_a_button_takes_the_text_it_contains_as_its_name():
    """A WPF button reports its caption as a child TextBlock, not as a Name."""
    result = _compile(
        [
            _node(
                "button",
                runtime_id=2,
                patterns=_BUTTON,
                children=[_node("text", "保存", runtime_id=3, patterns=_TEXT)],
            )
        ]
    )
    node = result.find("b1")
    assert node is not None
    assert node.name == "保存"
    assert node.name_synthetic is True
    # The caption child is the same concept as the name: it is dropped, the
    # same way simplify already drops a caption that repeats a real name.
    assert all(child.role != "text" for child in node.children)


def test_a_field_takes_the_label_immediately_before_it():
    """The Win32/WPF "(Label) (Edit)" layout, which `label_for` already reads."""
    result = _compile(
        [
            _node("text", "网络路径(N):", runtime_id=2, patterns=_TEXT),
            _node("input", runtime_id=3, patterns=_VALUE),
        ]
    )
    node = result.find("q1")
    assert node is not None
    assert node.name == "网络路径(N):"
    assert node.name_synthetic is True


def test_a_shortcut_hint_between_label_and_field_does_not_hide_the_label():
    result = _compile(
        [
            _node("text", "文件名:", runtime_id=2, patterns=_TEXT),
            _node("text", "必填", runtime_id=3, patterns=_TEXT),
            _node("input", runtime_id=4, patterns=_VALUE),
        ]
    )
    node = result.find("q1")
    assert node is not None
    assert node.name == "必填"
    assert node.name_synthetic is True


def test_a_placeholder_names_an_input_only_when_nothing_else_does():
    result = _compile(
        [_node("input", runtime_id=2, patterns=_VALUE, description="搜索联系人")]
    )
    node = result.find("q1")
    assert node is not None
    assert node.name == "搜索联系人"
    assert node.name_synthetic is True


def test_a_description_that_repeats_the_value_is_content_not_a_label():
    result = _compile(
        [
            _node(
                "input",
                runtime_id=2,
                patterns=_VALUE,
                value="张三",
                description="张三",
            )
        ]
    )
    node = result.find("q1")
    assert node is not None
    assert node.name is None


def test_a_leaf_text_behind_an_unnamed_wrapper_is_still_the_caption():
    result = _compile(
        [
            _node(
                "button",
                runtime_id=2,
                patterns=_BUTTON,
                children=[
                    _node(
                        "group",
                        runtime_id=3,
                        children=[_node("text", "确定", runtime_id=4, patterns=_TEXT)],
                    )
                ],
            )
        ]
    )
    node = result.find("b1")
    assert node is not None
    assert node.name == "确定"
    assert node.name_synthetic is True


# ---------------------------------------------------------------------------
# refusals (no name invented where the text is ambiguous or is content)
# ---------------------------------------------------------------------------


def test_two_different_texts_do_not_become_one_name():
    """Naming a container from one of two labels would be a guess."""
    result = _compile(
        [
            _node(
                "group",
                runtime_id=2,
                children=[
                    _node("text", "Big Mac", runtime_id=3, patterns=_TEXT),
                    _node("text", "$5.99", runtime_id=4, patterns=_TEXT),
                ],
            )
        ]
    )
    group = result.find("g1")
    assert group is not None
    assert group.name is None


def test_a_long_text_is_content_not_a_label():
    result = _compile(
        [
            _node(
                "group",
                runtime_id=2,
                # A selectable row survives simplification even with one child,
                # so the long text is really put to the synthesis rule.
                patterns=frozenset({"selectionitem"}),
                children=[
                    _node("text", "x" * 120, runtime_id=3, patterns=_TEXT),
                ],
            )
        ]
    )
    group = result.find("g1")
    assert group is not None
    assert group.name is None


def test_containers_are_never_named_from_their_children():
    """A list/pane would inherit the whole paragraph of its content."""
    result = _compile(
        [
            _node(
                "list",
                runtime_id=2,
                children=[_node("text", "唯一的一条", runtime_id=3, patterns=_TEXT)],
            )
        ]
    )
    node = result.find("l1")
    assert node is not None
    assert node.name is None


def test_a_node_that_already_has_a_name_is_not_touched():
    result = _compile(
        [
            _node(
                "button",
                "真实名称",
                runtime_id=2,
                patterns=_BUTTON,
                children=[_node("text", "另一段文本", runtime_id=3, patterns=_TEXT)],
            )
        ]
    )
    node = result.find("b1")
    assert node is not None
    assert node.name == "真实名称"
    assert node.name_synthetic is False


def test_the_pass_can_be_turned_off_for_an_ab_measurement():
    result = _compile(
        [
            _node(
                "button",
                runtime_id=2,
                patterns=_BUTTON,
                children=[_node("text", "保存", runtime_id=3, patterns=_TEXT)],
            )
        ],
        synthesize_names=False,
    )
    node = result.find("b1")
    assert node is not None
    assert node.name is None


# ---------------------------------------------------------------------------
# ref stability
# ---------------------------------------------------------------------------


def _button_tree(caption: str) -> RawTree:
    return _tree(
        [
            _node(
                "button",
                runtime_id=2,
                patterns=_BUTTON,
                children=[_node("text", caption, runtime_id=3, patterns=_TEXT)],
            )
        ]
    )


def test_a_synthetic_name_is_not_fingerprint_evidence():
    """A caption that changes (a counter, a price) must not re-mint the ref.

    The two trees differ only in the text; the native ids are identical, so the
    fingerprint has to be identical too. If the synthesised name leaked into
    identity, the second compilation would allocate a different fingerprint and
    the registry would report the element as replaced.
    """
    first = Compiler().compile(_button_tree("保存"), pack=None).find("b1")
    second = Compiler().compile(_button_tree("保存中…"), pack=None).find("b1")
    assert first is not None and second is not None
    assert first.name_synthetic and second.name_synthetic
    assert first.fingerprint == second.fingerprint


def test_a_real_name_is_still_fingerprint_evidence():
    """The exclusion above must apply to synthesised names only."""
    first = Compiler().compile(
        _tree([_node("button", "保存", runtime_id=2, patterns=_BUTTON)]), pack=None
    ).find("b1")
    second = Compiler().compile(
        _tree([_node("button", "取消", runtime_id=2, patterns=_BUTTON)]), pack=None
    ).find("b1")
    assert first is not None and second is not None
    assert not first.name_synthetic and not second.name_synthetic
    assert first.fingerprint != second.fingerprint


def test_a_pack_key_does_not_read_a_synthetic_name_as_identity():
    node = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(2,)),
        role="group",
        name="动态标题",
        name_synthetic=True,
    )
    assert _default_identity(node, {}) is None
    node.name_synthetic = False
    assert _default_identity(node, {}) == "动态标题"


def test_a_synthetic_name_does_not_turn_a_row_into_a_screen():
    """`layer=screen` is structural; a caption is not a screen title."""
    result = _compile(
        [
            _node(
                "group",
                runtime_id=2,
                patterns=_BUTTON,
                children=[_node("text", "Row", runtime_id=3, patterns=_TEXT)],
            )
        ]
    )
    node = result.find("g1")
    assert node is not None
    assert node.ref == "g1"
    assert node.layer is None
