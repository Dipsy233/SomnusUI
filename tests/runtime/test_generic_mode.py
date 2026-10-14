"""Generic-mode regressions: operate an app *without* a pack.

The point of SomnusUI is that an LLM can drive an unfamiliar application, not
only the handful that have a hand-written pack. Each test below encodes one of
the faults found while driving real Windows apps (Notepad, Explorer) packless,
so the generic path cannot silently regress into "needs a pack again".
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.compiler.caps import _is_text_entry
from somnus.compiler.match import HEURISTIC_THRESHOLD, heuristic_entities
from somnus.compiler.normalize import normalize
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session


def _n(
    role: str,
    name: str | None = None,
    *,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    children: list[RawNode] | None = None,
    cls: str | None = None,
    aid: str | None = None,
    runtime_id: int = 1,
) -> RawNode:
    return RawNode(
        native=NativeRef(
            window_id="w1", runtime_id=(runtime_id,), automation_id=aid, class_name=cls
        ),
        role=role,
        name=name,
        states=states,
        patterns=patterns,
        children=children or [],
    )


class _Seq:
    """A tiny counter so sibling nodes get distinct runtime ids."""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


_BUTTON = frozenset({"invoke"})
_TEXT = frozenset({"text"})
_VALUE = frozenset({"value", "text"})
#: A selectable row (a chat/mail/file entry that is one of many).
_SEL = frozenset({"selectionitem"})


def _packless_tree(children: list[RawNode], title: str = "Generic App") -> RawTree:
    root = _n("window", title, children=children)
    return RawTree(
        window=WindowInfo(
            window_id="w1", title=title, process="generic.exe", pid=1
        ),
        root=root,
    )


def _compile_packless(tree: RawTree):
    return Compiler().compile(tree, pack=None)


# --------------------------------------------------------------------------
# set_text must be available generically, not only via a pack
# --------------------------------------------------------------------------


def test_plain_text_field_gets_set_text():
    """A Win32/WPF text field (Value pattern) offers set_text with no pack."""
    tree = _packless_tree([_n("edit", "Name", patterns=_VALUE)])
    res = _compile_packless(tree)
    node = res.find("q1")
    assert node is not None and "set_text" in node.cap_names()


def test_self_drawn_editor_is_an_input_and_gets_set_text():
    """A Chromium/Electron rich-text box exposes no Value pattern but accepts
    keyboard input; it must still be writable. This is the QQ message-box case,
    which previously needed a hand-written pack."""
    editor = _n(
        "group",
        "editor",
        patterns=frozenset({"text"}),
        cls="ProseMirror is-empty ExEditor-qq-msg-editor",
    )
    tree = _packless_tree([editor])
    res = _compile_packless(tree)
    node = res.find("q1")
    assert node is not None
    assert node.role == "input"
    assert "set_text" in node.cap_names()


def test_readonly_field_does_not_get_set_text():
    """A read-only Value pattern cannot accept text, so it must not advertise
    set_text (or CALL would fail at execution)."""
    tree = _packless_tree(
        [_n("edit", "Path", patterns=_VALUE, states=frozenset({"readonly"}))]
    )
    res = _compile_packless(tree)
    node = res.find("q1")
    assert node is not None and "set_text" not in node.cap_names()


def test_static_label_never_becomes_an_input():
    """A `text` node is a label whose Value pattern is its own caption; it must
    never be offered as an editable field (regression: golden L0 grew a bogus
    `+set_text` on every product name and price)."""
    assert not _is_text_entry(_n("text", "Big Mac", patterns=_VALUE))
    tree = _packless_tree([_n("text", "Big Mac", patterns=_VALUE)])
    res = _compile_packless(tree)
    label = res.find("tx1")
    assert label is not None and "set_text" not in label.cap_names()


# --------------------------------------------------------------------------
# generic entity detection (the packless path)
# --------------------------------------------------------------------------


def test_action_row_becomes_an_item_without_a_pack():
    """A titled row with a verb button is an item (restaurant-demo shape),
    detected structurally so any shopping/cart UI works packless."""
    row = _n(
        "group",
        children=[
            _n("text", "Big Mac", patterns=_TEXT, runtime_id=2),
            _n("button", "加入", patterns=_BUTTON, runtime_id=3),
        ],
    )
    tree = _packless_tree([row])
    res = _compile_packless(tree)
    entity = [n for n in res.root.walk() if n.entity == "item"]
    assert len(entity) == 1
    assert entity[0].name == "Big Mac"
    assert "add" in entity[0].cap_names()


def test_repeated_titled_rows_become_listitems_without_a_pack():
    """Three or more same-shaped titled siblings is a list (chat list, mail
    list, file list); each row becomes a selectable entity with no pack."""
    rows = [
        _n(
            "group",
            patterns=_SEL,
            children=[
                _n("text", f"Friend {i}", patterns=_TEXT, runtime_id=10 + i * 2),
                _n("text", "11:39", patterns=_TEXT, runtime_id=11 + i * 2),
            ],
            runtime_id=100 + i,
        )
        for i in range(4)
    ]
    tree = _packless_tree([_n("list", "Contacts", patterns=frozenset({"scroll"}), children=rows)])
    res = _compile_packless(tree)
    items = [n for n in res.root.walk() if n.entity == "listitem"]
    assert len(items) == 4
    assert {n.name for n in items} == {f"Friend {i}" for i in range(4)}
    assert all("open" in n.cap_names() for n in items)


def test_two_rows_are_not_a_list():
    """Below three siblings there is no repetition signal, so nothing is
    promoted - the heuristic must not guess."""
    rows = [
        _n(
            "group",
            patterns=_SEL,
            children=[_n("text", f"Row {i}", patterns=_TEXT, runtime_id=20 + i)],
            runtime_id=200 + i,
        )
        for i in range(2)
    ]
    tree = _packless_tree([_n("list", "Things", patterns=frozenset({"scroll"}), children=rows)])
    res = _compile_packless(tree)
    assert not [n for n in res.root.walk() if n.entity == "listitem"]


def test_heuristic_threshold_is_not_bypassed():
    """A bare titled group with no action and no repetition must stay
    primitive: the structural score has to clear the threshold."""
    lonely = _n("group", children=[_n("text", "Just A Label", patterns=_TEXT, runtime_id=2)])
    tree = _packless_tree([lonely])
    res = _compile_packless(tree)
    assert not [n for n in res.root.walk() if n.entity]
    assert HEURISTIC_THRESHOLD > 0


# --------------------------------------------------------------------------
# gesture choice belongs to the caller, not the runtime
# --------------------------------------------------------------------------


def test_open_uses_activate_when_the_control_reports_invoke():
    """`open` is the conventional activation, not a guess about which gesture
    the application *really* wants. A row that advertises Invoke gets activate
    even when it is self-drawn (live: QQ's conversation rows report an Invoke
    that only highlights the row; deciding up front to double-click instead
    would be the runtime overruling the caller)."""
    row = _n(
        "group",
        patterns=frozenset({"invoke", "selectionitem"}),
        cls="recent-contact-item recent-contact-item--top",
        children=[_n("text", "我的手机", patterns=_TEXT, runtime_id=2)],
    )
    tree = _packless_tree([
        _n("list", "Chats", patterns=frozenset({"scroll"}), runtime_id=0, children=[
            row,
            _n("group", patterns=frozenset({"invoke", "selectionitem"}),
               children=[_n("text", "B", patterns=_TEXT, runtime_id=3)], runtime_id=9),
            _n("group", patterns=frozenset({"invoke", "selectionitem"}),
               children=[_n("text", "C", patterns=_TEXT, runtime_id=4)], runtime_id=10),
        ])
    ])
    res = _compile_packless(tree)
    item = next(n for n in res.root.walk() if n.entity == "listitem" and n.name == "我的手机")
    cap = item.get_cap("open")
    assert cap is not None
    assert [s.primitive for s in cap.via] == ["activate"]


def test_context_menu_is_offered_on_a_control_but_not_on_a_label():
    """The menu is available as its own capability, so the caller can reach for
    it when the native gesture does nothing. It is granted only to real controls
    - an element with an interaction pattern - never to a text label."""
    row = _n(
        "group",
        patterns=_SEL,
        children=[_n("text", "Row", patterns=_TEXT, runtime_id=2)],
    )
    label = _n("text", "Just a label", patterns=_TEXT, runtime_id=30)
    tree = _packless_tree([row, label])
    res = _compile_packless(tree)

    control = res.find("g1")
    assert control is not None
    assert "context_menu" in control.cap_names()

    text_node = next(
        n for n in res.root.walk() if n.role == "text" and n.name == "Just a label"
    )
    assert "context_menu" not in text_node.cap_names()


def test_inert_node_gets_no_action_capabilities():
    """Nothing is offered on a node the platform does not mark as operable."""
    label = _n("text", "Static", patterns=_TEXT)
    image = _n("image")
    tree = _packless_tree([label, image])
    res = _compile_packless(tree)
    for node in res.root.walk():
        if node.role in ("text", "image"):
            assert "activate" not in node.cap_names()
            assert "set_text" not in node.cap_names()
            assert "context_menu" not in node.cap_names()
            assert "toggle" not in node.cap_names()


# --------------------------------------------------------------------------
# the permission check must use the compiled window's process
# --------------------------------------------------------------------------


def test_policy_uses_compiled_process_not_a_renumbered_window():
    """A window id is only valid for the enumeration that produced it. The
    allow-list must be checked against the process the *current compilation*
    belongs to, never a fresh lookup that may now name another window (live bug:
    a Notepad call was denied against `explorer.exe`)."""
    tree = _packless_tree([_n("button", "OK", patterns=_BUTTON)])
    tree.window.process = "notepad.exe"
    session = Session(
        MockProvider(fixtures=[tree]),
        policy=__import__("somnus.runtime.policy", fromlist=["Policy"]).Policy(
            allow_processes=("notepad.exe",)
        ),
    )
    session.observe()
    assert session._target_process() == "notepad.exe"
    # A packless button still activates through the generic path.
    out = session.dispatch("CALL b1.activate()")
    assert out.startswith("OK")
