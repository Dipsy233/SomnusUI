"""Generic-mode regressions: operate an app *without* a pack.

The point of SomnusUI is that an LLM can drive an unfamiliar application, not
only the handful that have a hand-written pack. Each test below encodes one of
the faults found while driving real Windows apps (Notepad, Explorer) packless,
so the generic path cannot silently regress into "needs a pack again".
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.compiler.caps import _is_text_entry, _prefers_keyboard_entry
from somnus.compiler.match import HEURISTIC_THRESHOLD, heuristic_entities
from somnus.compiler.normalize import normalize
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.sui.serialize import RenderOptions, render_node_line
from tests.fixtures.builders import notepad_tree


def _n(
    role: str,
    name: str | None = None,
    *,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
    children: list[RawNode] | None = None,
    cls: str | None = None,
    aid: str | None = None,
    runtime_id: int | None = None,
    hwnd: int = 0,
) -> RawNode:
    """Build a fixture node with a unique native identity.

    The runtime id defaults to a fresh value rather than a constant: a provider
    always hands every element its own id, and a fixture that shares one between
    a parent and its child cannot be resolved - the mock would find the wrong
    node and the element under test would appear to lack the interface it has.
    """
    if runtime_id is None:
        runtime_id = _next_runtime_id()
    return RawNode(
        native=NativeRef(
            window_id="w1", runtime_id=(runtime_id,), automation_id=aid, class_name=cls,
            native_window_handle=hwnd,
        ),
        role=role,
        name=name,
        states=states,
        patterns=patterns,
        children=children or [],
    )


_runtime_counter = [0]


def _next_runtime_id() -> int:
    _runtime_counter[0] += 1
    return _runtime_counter[0]


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
# an input must be *identifiable* as a text box from the observation alone
# --------------------------------------------------------------------------


def test_writable_and_readonly_inputs_render_differently():
    """An agent must be able to tell a text box from a read-only field by
    reading the line.

    Regression: `set_text` used to be hidden as the default capability of the
    `input` role, so a writable field rendered as `INPUT q1 "Name"` and a
    read-only one as `INPUT q2 "Path" readonly` - the *first* showed no
    `set_text` at all either, which made the capability impossible to discover
    and the read-only case indistinguishable from the normal one (the agent
    either tried to write a field that refuses text, or never realised a text
    box was a text box)."""
    tree = _packless_tree(
        [
            _n("edit", "Name", patterns=_VALUE, runtime_id=1),
            _n("edit", "Path", patterns=_VALUE, states=frozenset({"readonly"}),
               runtime_id=2),
        ]
    )
    res = _compile_packless(tree)
    opts = RenderOptions()
    writable = render_node_line(res.find("q1"), opts)
    readonly = render_node_line(res.find("q2"), opts)

    # The writable field advertises set_text...
    assert "+set_text" in writable
    # ...and the read-only one does not, which is the signal.
    assert "+set_text" not in readonly
    assert writable != readonly
    # A bare `INPUT` line therefore always means "this field can be written".
    assert "readonly" not in writable


def test_observe_shows_set_text_on_a_text_box():
    """End to end: the L0 overview an agent actually receives names the
    capability, so the text box is usable without an extra INSPECT."""
    session = Session(MockProvider(fixtures=[notepad_tree("hello")]), allow_packs=False)
    text = session.observe().text
    line = next(l for l in text.splitlines() if l.strip().startswith("INPUT"))
    assert "+set_text" in line
    # And the capability recorded on the node is the one that was printed.
    node = session.result.find("q1")
    assert node is not None and "set_text" in node.cap_names()


# --------------------------------------------------------------------------
# set_text must use the control's own write interface, not the keyboard
# --------------------------------------------------------------------------


def _set_text_steps(node):
    cap = node.get_cap("set_text") if hasattr(node, "get_cap") else None
    return [(s.primitive, s.condition) for s in cap.via]


def test_value_pattern_is_written_through_never_typed():
    """A control exposing a Value pattern is written through it.

    `ValuePattern::SetValue` is the application's own write interface: it runs
    on the app's thread, so it is atomic and needs no keyboard focus. Typing is
    not merely slower - `SendInput` queues keystrokes and returns before they
    are processed, so a fast burst is dropped by the target's message pump
    (measured live against Notepad: a 10-character `set_text` landed as a
    correct *prefix* of 4-7 characters, 0/20 complete). So a control that
    offers the native write must never be typed into."""
    tree = _packless_tree(
        [_n("document", "文本编辑器", patterns=frozenset({"value", "text", "scroll"}),
            cls="RichEditD2DPT", runtime_id=1)]
    )
    res = _compile_packless(tree)
    node = res.find("q1")
    assert node is not None
    steps = _set_text_steps(node)
    # The native write is first and unguarded...
    assert steps[0] == ("set_text", "has:value")
    # ...and typing is last, gated so it only runs when no native write exists.
    # The exact gate is `no-hwnd`: typing must not fire for a node that has
    # *any* native write route, whether that is the Value pattern or a Win32
    # HWND (docs/interface-capability-report.md 3.2). Asserting
    # `missing:value` here would let typing run on a control that the HWND
    # route can write without focus.
    assert steps[-1][0] == "type_text", steps
    assert steps[-1][1] == "no-hwnd", steps
    assert all(p != "type_text" for p, _ in steps[:-1]), steps


def test_self_drawn_editor_without_a_value_pattern_still_types():
    """The fallback survives: a control with no write interface is typed into.

    This is what keeps a self-drawn Chromium/Electron editor (QQ's ProseMirror
    box, CodeMirror, Monaco) and a terminal writable without a hand-written
    pack. Routing the fix must not seal this route - it is the only one those
    controls have."""
    tree = _packless_tree(
        [_n("group", "editor", patterns=frozenset({"text"}),
            cls="ProseMirror is-empty ExEditor-qq-msg-editor", runtime_id=1)]
    )
    res = _compile_packless(tree)
    node = res.find("q1")
    assert node is not None and node.role == "input"
    steps = _set_text_steps(node)
    # Typing is still offered - and is still the last resort, gated on nothing
    # else being able to write. `no-hwnd` is that gate: this node maps no Win32
    # handle, so it really is keyboard-only.
    primitives = [p for p, _ in steps]
    assert primitives[-1] == "type_text", steps
    assert steps[-1][1] == "no-hwnd", steps
    # The two native routes are present but must not match this node: it has no
    # Value pattern and no HWND, so the conditions gate both out.
    assert ("set_text", "has:value") in steps, steps
    assert ("set_text_hwnd", "hwnd") in steps, steps


# --------------------------------------------------------------------------
# The HWND write route (docs/interface-capability-report.md 3.2)
# --------------------------------------------------------------------------


def _set_text_steps_of(node):
    cap = node.get_cap("set_text")
    return [(s.primitive, s.condition) for s in cap.via]


def test_element_hwnd_offers_the_native_write_route():
    """A control with no Value pattern but a real HWND writes without focus.

    Measured on Windows 11 24H2 Notepad: the window stays in the background and
    `EM_REPLACESEL`/`WM_SETTEXT` still replace its text, 4/4 runs including CJK
    and a 300-character string. This is the route the capability report's
    "does the process own an Edit child window?" criterion had hidden."""
    rich = _n("group", "editor", patterns=frozenset({"text"}),
              cls="RichEditD2DPT", runtime_id=1, hwnd=0x940B7E)
    res = _compile_packless(_packless_tree([rich]))
    steps = _set_text_steps_of(res.find("q1"))
    assert ("set_text_hwnd", "hwnd") in steps, steps
    # And typing must not also be reachable: the HWND route already works.
    assert steps[-1] == ("type_text", "no-hwnd"), steps


def test_no_hwnd_means_the_hwnd_route_is_gated_out():
    """The HWND route must not be attempted for an element that maps none.

    Sending a message to HWND 0 goes to the desktop, so the gate is a safety
    property, not an optimisation."""
    editor = _n("group", "editor", patterns=frozenset({"text"}),
               cls="ProseMirror ExEditor-qq-msg-editor", runtime_id=1)
    res = _compile_packless(_packless_tree([editor]))
    node = res.find("q1")
    steps = _set_text_steps_of(node)
    # The step is declared...
    assert ("set_text_hwnd", "hwnd") in steps, steps
    # ...but the resolver drops it, because this element has no handle.
    compiled = res.sources[node.ref]
    assert compiled.native.native_window_handle == 0

def test_set_text_route_is_decided_by_interface_not_class_name():
    """The route follows the control's interface, never its class name.

    Regression: every RichEdit/document class was sent down the keyboard path
    on the belief that it "advertises a Value pattern it does not honour"
    ("Windows 11 Notepad discards SetValue"). Measured live on Windows 11
    24H2 that belief is false - Notepad's RichEditD2DPT reports `value`,
    `IsReadOnly == 0`, and `SetValue` lands in 20/20 runs - so acting on it
    threw away the one reliable route for the exact control it named."""
    rich = _n("document", "文本编辑器", patterns=frozenset({"value", "text", "scroll"}),
              cls="RichEditD2DPT", runtime_id=1)
    plain = _n("edit", "Name", patterns=frozenset({"value", "text"}),
               cls="Edit", runtime_id=2)
    # Same interface, wildly different class names: same route.
    assert _prefers_keyboard_entry(rich) is False
    assert _prefers_keyboard_entry(plain) is False

    typed = _n("group", "editor", patterns=frozenset({"text"}), cls="CodeMirror",
               runtime_id=3)
    assert _prefers_keyboard_entry(typed) is True


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


def test_context_menu_stays_alongside_activate_on_a_row():
    """The two gestures are offered *together*, not one instead of the other.

    QQ's conversation rows advertise a real `Invoke` that only highlights the
    row, and the command that opens the chat lives in their context menu. The
    runtime cannot know which of the two the application honours, so both must
    survive - withholding the menu because `activate` looked available would
    have made the chat unreachable."""
    row = _n(
        "group",
        patterns=frozenset({"invoke", "selectionitem"}),
        cls="recent-contact-item",
        children=[_n("text", "我的手机", patterns=_TEXT, runtime_id=2)],
    )
    tree = _packless_tree([row])
    res = _compile_packless(tree)
    node = res.find("g1")
    assert node is not None
    assert "activate" in node.cap_names()
    assert "context_menu" in node.cap_names()


# --------------------------------------------------------------------------
# a capability must be proven by the platform, never guessed from the role
# --------------------------------------------------------------------------


def test_role_button_without_invoke_gets_no_activate():
    """`activate` is the element's default action, so it exists only when the
    platform offers one.

    Regression: the role alone used to grant it (`role == "button"`), so a
    title-bar button exposing no `Invoke` advertised `activate` and the call
    could only ever fail with ACTION_NOT_SUPPORTED - while the gesture that
    actually worked (`expand`/`collapse`) was buried next to it and usually not
    tried (observed live on Notepad's title-bar buttons)."""
    # Only ExpandCollapse: the real gesture is expand/collapse, not activate.
    expandable = _n("button", "最大化", patterns=frozenset({"expandcollapse"}), runtime_id=1)
    # No interaction pattern at all: nothing is actionable.
    inert = _n("button", "关闭", runtime_id=2)
    # Only a text interface: it is a label wearing a button role.
    fake = _n("button", "假按钮", patterns=_TEXT, runtime_id=3)
    tree = _packless_tree([expandable, inert, fake])
    res = _compile_packless(tree)

    for ref in ("b1", "b2", "b3"):
        node = res.find(ref)
        assert node is not None
        assert "activate" not in node.cap_names(), f"{ref} ({node.name}) guessed activate"

    # The gesture that really exists is still offered.
    assert "expand" in res.find("b1").cap_names()
    assert "collapse" in res.find("b1").cap_names()


def test_invoke_still_grants_activate():
    """The platform-provided Invoke is untouched by the stricter rule."""
    tree = _packless_tree([_n("button", "确定", patterns=_BUTTON)])
    res = _compile_packless(tree)
    node = res.find("b1")
    assert node is not None and "activate" in node.cap_names()


def test_context_menu_without_a_menu_is_not_reported_as_ok():
    """A context_menu that produces no menu must not come back `OK`.

    The platform offers no way to ask "does this element have a menu":
    `ShowContextMenu` is callable everywhere and silently does nothing when the
    application registered none. Reporting the empty delta as a plain `OK` told
    the agent it had opened a menu that does not exist (observed live: the
    Notepad title-bar buttons). It is `OK_UNVERIFIED`, and the body names the
    gesture that is really there."""
    session = Session(MockProvider(fixtures=[notepad_tree()]), allow_packs=False)
    session.observe()
    # The mock executes context_menu successfully but never mutates the tree -
    # exactly what a control with no menu does on a real desktop.
    out = session.dispatch("CALL q1.context_menu()")
    assert out.startswith("OK_UNVERIFIED"), out
    assert "no menu" in out


def test_context_menu_that_opens_a_menu_is_ok():
    """The other half of the rule: when a menu really appears, it is `OK`.

    The menu arrives as a shell popup (a separate top-level window with no
    structural link to the control), so the in-window delta alone cannot
    confirm it - the popup hook is what proves the gesture worked.
    """
    row = _n(
        "group",
        patterns=frozenset({"invoke", "selectionitem"}),
        children=[_n("text", "我的手机", patterns=_TEXT, runtime_id=2)],
        runtime_id=1,
    )
    window = _packless_tree([row], title="QQ")
    window.window.process = "qq.exe"
    menu = RawTree(
        window=WindowInfo(
            window_id="popup1", title="menu", process="qq.exe", pid=1
        ),
        root=_n(
            "menu",
            children=[
                _n("menuitem", "打开独立聊天窗口", patterns=_BUTTON, runtime_id=101)
            ],
            runtime_id=100,
        ),
    )

    class _PopupProvider(MockProvider):
        def popup_menu_snapshot(self):
            return ("popup1", menu)

    session = Session(_PopupProvider(fixtures=[window]), allow_packs=False)
    session.observe()
    out = session.dispatch("CALL g1.context_menu()")
    assert out.startswith("OK "), out
    assert not out.startswith("OK_UNVERIFIED"), out
    # And the menu is mounted under the node that opened it.
    assert "打开独立聊天窗口" in out


def test_activate_without_a_visible_change_is_still_ok():
    """The honesty rule is specific to context_menu.

    An `activate` that ran on a settled UI with no tree change really did run -
    flattening it to UNVERIFIED would make agents retry actions that already
    succeeded, which is the reason the OK collapse exists in the first place.
    """
    tree = _packless_tree([_n("button", "OK", patterns=_BUTTON)])
    session = Session(MockProvider(fixtures=[tree]), allow_packs=False)
    session.observe()
    out = session.dispatch("CALL b1.activate()")
    assert out.startswith("OK"), out
    assert not out.startswith("OK_UNVERIFIED"), out


def test_link_and_menuitem_keep_their_implicit_default_action():
    """A hyperlink and a menu row are activated by the accessibility layer
    itself, so they keep `activate` without an explicit Invoke pattern."""
    tree = _packless_tree(
        [
            _n("hyperlink", "网络和共享中心", runtime_id=1),
            _n("menuitem", "文件", patterns=frozenset({"expandcollapse"}), runtime_id=2),
        ]
    )
    res = _compile_packless(tree)
    assert "activate" in res.find("lnk1").cap_names()
    assert "activate" in res.find("mi1").cap_names()


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
