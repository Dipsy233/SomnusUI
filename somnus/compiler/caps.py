"""Primitive capability derivation.

Every portable capability that is *directly* supported by a native pattern is
created here with ``confidence == 1.0``. Heuristic upgrades (e.g. "this button
in a group probably means Add") happen later in :mod:`somnus.compiler.match`
and must lower confidence.
"""

from __future__ import annotations

from somnus.ir.capability import (
    SAFE_CAPABILITIES,
    Capability,
    ParamSpec,
    PrimitiveStep,
)
from somnus.ir.node import RawNode

#: role -> implicit capabilities granted purely by semantics (even without a
#: matching native pattern). These are what the SUI-Text default table hides.
ROLE_DEFAULT_CAPS: dict[str, tuple[str, ...]] = {
    "button": ("activate",),
    "link": ("activate",),
    "menuitem": ("activate",),
    "listitem": ("select",),
    "tab": ("select",),
    "treeitem": ("select", "expand"),
    "checkbox": ("toggle",),
    "switch": ("toggle",),
    "radio": ("select",),
    "input": ("set_text",),
    "slider": ("increment", "decrement"),
    "select": ("select", "expand"),
    "list": ("scroll",),
    "tree": ("scroll",),
    "table": ("scroll",),
    "pane": ("scroll",),
}


def _cap(name: str, params: dict[str, ParamSpec] | None = None) -> Capability:
    risk = "safe" if name in SAFE_CAPABILITIES else "normal"
    return Capability(
        name=name,
        level="primitive",
        params=params or {},
        via=[PrimitiveStep(primitive=name, target="$self")],
        risk=risk,  # type: ignore[arg-type]
    )


#: Roles that are inherently text-entry surfaces.
_TEXT_ENTRY_ROLES: frozenset[str] = frozenset({"input", "select", "slider"})


#: Patterns that mean "this element is something the user interacts with".
#: The absence of all of them means the element is inert (a label, an image,
#: a layout container) and must not be offered any action.
_INTERACTIVE_PATTERNS: frozenset[str] = frozenset(
    {
        "invoke",
        "toggle",
        "selectionitem",
        "griditem",
        "value",
        "rangevalue",
        "expandcollapse",
        "scrollitem",
    }
)


#: Patterns that prove the element has a **default action** the platform can
#: perform: ``Invoke`` is the real one, and ``LegacyIAccessible`` is the
#: fallback bridge the executor uses (`DoDefaultAction`) for older or
#: self-drawn controls that never implemented Invoke. A control with neither
#: has no default action to run, no matter what its role says.
_ACTIVATION_PATTERNS: frozenset[str] = frozenset({"invoke", "legacyiaccessible"})

#: Patterns that prove the element has something to *operate* - a verb, a
#: value, a selection, an expansion. Used to decide whether a context menu is
#: even conceivable. Note this is deliberately narrower than "any interaction
#: pattern": `scrollitem`/`scroll` mean the element can be scrolled to or
#: scrolled, which says nothing about it answering a right click.
_CONTEXT_MENU_PATTERNS: frozenset[str] = frozenset(
    {
        "invoke",
        "legacyiaccessible",
        "toggle",
        "selectionitem",
        "griditem",
        "value",
        "rangevalue",
        "expandcollapse",
    }
)


def _has_default_action(node: RawNode) -> bool:
    """Whether the platform offers a **default action** for ``node``.

    This is the only honest source for ``activate``. Deriving it from the role
    (`role == "button"`) is a guess: a Win32 title-bar button, a `PANE`
    pretending to be one, or a control that only exposes `ExpandCollapse` all
    report `role=button` while exposing no `Invoke` at all. Advertising
    `activate` there sends the agent into a call that the provider refuses with
    `ACTION_NOT_SUPPORTED` - and worse, it hides the gesture that *does* work
    (`expand`/`collapse`).

    The check is on patterns the provider read from the platform, so a control
    whose Invoke exists but is not honoured (QQ's conversation rows) still
    advertises `activate`: the platform does offer the action, and whether the
    application reacts is the application's business, reported through the
    delta - that is the caller's decision, not the compiler's.

    A text-entry surface is the one exception, and it is a measured one: WPF's
    ``RichTextBox`` (and every native ``EDIT``) reports ``legacyiaccessible``,
    so treating that pattern as a default action would put ``+activate`` on a
    document. The element's default action there is to take text, which is
    already expressed by ``set_text``; offering ``activate`` alongside it would
    advertise a gesture the application does not perform.
    """
    if node.role in _TEXT_ENTRY_ROLES or _is_text_entry(node):
        # `invoke` on a text-entry node is taken seriously (some editors do
        # publish it as "open"), but the blanket `legacyiaccessible` bridge is
        # not - every Win32 edit control carries it.
        return "invoke" in node.patterns
    return bool(node.patterns & _ACTIVATION_PATTERNS)


def _is_control(node: RawNode) -> bool:
    """Whether ``node`` is an operable control rather than inert content.

    A control is defined by the platform, not guessed: it exposes at least one
    interaction pattern. That keeps the capability honest - nothing is offered
    on a text label or a wrapping pane, and nothing is withheld from a control
    because of what application it belongs to.
    """
    if node.patterns & _INTERACTIVE_PATTERNS:
        return True
    # A disabled control still exists and still has an area; the executor
    # refuses it with TARGET_DISABLED, which is clearer than the capability
    # silently disappearing from the observation.
    return node.role in ("button", "listitem", "tab", "treeitem", "menuitem")


def _prefers_keyboard_entry(node: RawNode) -> bool:
    """Whether ``set_text`` would have to type, because no write interface exists.

    Kept as a *predicate*, not as the routing decision: routing now lives in the
    ordered ``via`` chain in :func:`primitive_capabilities`, which tries
    ValuePattern, then the element's own HWND, then typing. This helper answers
    the single question "does this node have nothing but the keyboard?", which
    is what tests and diagnostics want.

    Note the answer is now slightly wider than before: a node that maps a native
    HWND (``native_window_handle != 0``) also has a non-keyboard write route, so
    it is *not* keyboard-only either. That is the correction from
    docs/text-input-channels.md 3.2 - the HWND route was hidden by the
    report's "does the process own an Edit child window?" criterion, which asked
    about the process instead of the element.

    ``ValuePattern::SetValue`` remains the first choice when present: the
    application performs it on its own thread, so it is atomic, does not race
    the message pump and does not need keyboard focus.

    It used to send every RichEdit/document control down the keyboard path on
    the belief that they advertise a Value pattern they do not honour
    ("Windows 11 Notepad discards SetValue"). Measured live on Windows 11
    24H2 that belief is false: Notepad's RichEditD2DPT reports ``value``,
    ``IsReadOnly == 0``, and ``SetValue`` lands correctly in 20/20 runs including
    CJK and long strings. The keyboard route it substituted is not merely slower:
    ``SendInput`` queues keystrokes into the target's input queue and returns
    before they are processed, so a fast burst is dropped by the message pump
    (measured: 0/20 complete for a 10-character string, always a correct
    *prefix*). Typing is the last resort it was always documented to be, never a
    preference based on a control's class name.
    """
    return "value" not in node.patterns and not node.native.native_window_handle


def _is_text_entry(node: RawNode) -> bool:
    """Whether ``node`` should advertise ``set_text``.

    True when the node is a known text-entry role, or when it publishes a
    **Value pattern** on a role that can carry text. Both are evidence the
    platform supplied.

    There is deliberately **no** structural fallback here. A node that merely
    has a Text interface was previously granted `set_text` on the strength of
    its role (any `group`/`pane`), which handed the capability to controls that
    cannot accept text at all - a labelled panel, a read-only document - and
    made the observation promise a write that could only fail. Self-drawn
    editors are identified in the compiler's normalize pass (by the toolkit's
    class name) and arrive here already carrying the `input` role, so nothing is
    lost by removing the guess: what is left is exactly what the platform
    proved.

    The three exclusions (readonly, disabled, offscreen) are the cases where
    typing would silently do nothing.
    """
    if "readonly" in node.states or "disabled" in node.states:
        return False
    if "offscreen" in node.states:
        return False
    if node.role in _TEXT_ENTRY_ROLES:
        return True
    # A writable Value pattern on a non-input role (an editable Document, an
    # editable custom control) still means "this takes text". A plain `text`
    # node is a *label* whose Value pattern is its displayed string, so it must
    # never be treated as an input.
    if "value" in node.patterns:
        return node.role in ("pane", "document", "group", "unknown")
    return False


def primitive_capabilities(node: RawNode) -> list[Capability]:
    """Return the primitive capabilities implied by ``node``'s role + patterns."""
    caps: dict[str, Capability] = {}
    patterns = node.patterns
    role = node.role

    # `activate` runs the element's default action, so it exists exactly when
    # the platform offers one: an `Invoke` pattern, or the `LegacyIAccessible`
    # bridge the executor falls back to. The role is *not* evidence - a
    # `role=button` element with no Invoke (a title-bar button, a self-drawn
    # fake, a control that only exposes ExpandCollapse) would otherwise be
    # handed a capability whose only possible outcome is ACTION_NOT_SUPPORTED,
    # while the gesture that really works went unmentioned.
    #
    # `link` and `menuitem` keep an implicit default action: a hyperlink and a
    # menu row are activated by the accessibility layer itself even without an
    # explicit Invoke, which is what makes them navigable at all.
    if _has_default_action(node) or role in ("link", "menuitem"):
        caps.setdefault("activate", _cap("activate"))
    elif (node.native.class_name or "").startswith("android.") and _is_control(node):
        # Android gesture fallback (SomnusDroid doc §7.1): some ROM surfaces
        # render as controls but publish no CLICK to the accessibility tree
        # (measured: MIUI settings' search bar). A synthetic tap at the node's
        # bounds centre is the only route. Offered as `tap` - not `activate`,
        # because a tap is synthesized from coordinates the model never sees
        # and comes back OK_UNVERIFIED unless the delta proves it - and the
        # provider's own double opt-in (host --allow-gesture + Portal config)
        # is what decides whether the route exists at all. A provider without
        # the channel refuses with ACTION_NOT_SUPPORTED, which the agent sees
        # as an honest refusal rather than a missing capability.
        tap = _cap("tap")
        tap.via = [PrimitiveStep(primitive="gesture", target="$self", value="tap")]
        caps.setdefault("tap", tap)

    # set_text is granted to *every* text-entry surface. The route it takes is
    # decided by the control's own interface, not by its class name or role.
    # Routes are tried in order of how much they cost the user, and each is a
    # conditional `via` step so the resolver picks from the live element:
    #
    #   1. `set_text`   - ValuePattern::SetValue. The application's own write
    #                     interface: atomic, no focus, no keystrokes. Every
    #                     control that can be written natively exposes it -
    #                     measured on WPF, WinForms, RichEdit and Notepad's
    #                     RichEditD2DPT alike - so this is the normal path.
    #   2. `paste_text` - put the text on the clipboard and send Ctrl+V.
    #                     Needs the foreground and borrows the user's
    #                     clipboard, but reaches a control that publishes no
    #                     Value interface, because the application's own
    #                     WM_PASTE handler reads the text. Measured: a constant
    #                     ~37 ms from 10 to 3000 characters, since the text never
    #                     enters the input queue (report 3.1). Gated on
    #                     `--allow-clipboard`.
    #   3. `type_text`  - focus and type. Last resort, gated on
    #                     `--allow-keyboard`, because it steals focus and
    #                     provably loses characters.
    #
    # There used to be a `set_text_hwnd` route between 1 and 2, writing through
    # EM_REPLACESEL / WM_SETTEXT to the element's own HWND. It was removed: its
    # gate was "the element maps an HWND", and a control with an HWND that can
    # hold editable text always also publishes a Value pattern (measured on a
    # raw Win32 EDIT, a WinForms TextBox, a multiline one and a RichTextBox), so
    # route 1 always won and route 2 was never reached. Constructing a control
    # with an HWND and no Value interface is possible only by hiding the text
    # surface from UIA entirely - a shape no real application has. The route was
    # therefore unreachable in practice; what it was believed to cover (an
    # element UIA can see but not write) is served by routes 2 and 3.
    #
    # The ordering is the whole point: a self-drawn editor with no Value pattern
    # (QQ's ProseMirror box) falls through to 2 and 3 and is refused unless a
    # host explicitly enabled one.
    #
    # `paste_text` sits *before* `type_text` because the report measures it as
    # strictly better where both apply: no queue stall, length-independent cost,
    # and a real delivery signal when the element can be read back. Both raise
    # the target window first (see executor._bring_to_foreground); the runtime
    # does not attempt a focus-free write, so a window that cannot be raised
    # yields an honest refusal rather than a keystroke sent to a stranger.
    if _is_text_entry(node):
        cap = _cap("set_text", {"text": ParamSpec(type="string", required=True)})
        cap.via = [
            PrimitiveStep(
                primitive="set_text",
                target="$self",
                value="$text",
                condition="has:value",
            ),
            PrimitiveStep(
                primitive="paste_text",
                target="$self",
                value="$text",
            ),
            PrimitiveStep(
                primitive="type_text",
                target="$self",
                value="$text",
            ),
        ]
        caps.setdefault("set_text", cap)

    if "toggle" in patterns or role in ("checkbox", "switch"):
        caps.setdefault("toggle", _cap("toggle"))

    # GridItem cells are selectable, like list items (Excel, WPF DataGrid).
    # Leaf *content* roles are excluded: a TextView / IMAGE / unknown surface
    # is not a selection unit - what looks like selection evidence there is
    # the toolkit's blanket ACTION_SELECT (MIUI paints it on every launcher
    # label), and the agent that taps `select` on a label gets a highlight at
    # best. A selectable text surface is an input, which keeps its caps.
    if (
        "selectionitem" in patterns
        or "griditem" in patterns
        or role in ("listitem", "tab", "radio", "row", "treeitem")
    ) and role not in ("text", "image", "unknown"):
        caps.setdefault("select", _cap("select"))

    if "expandcollapse" in patterns or role in ("treeitem", "select", "group", "pane"):
        if "collapsed" in node.states or "expandcollapse" in patterns:
            caps.setdefault("expand", _cap("expand"))
            caps.setdefault("collapse", _cap("collapse"))

    if "rangevalue" in patterns or role == "slider":
        caps.setdefault("increment", _cap("increment"))
        caps.setdefault("decrement", _cap("decrement"))

    # `scroll` is granted on the element's own Scroll evidence only: a
    # SCROLL_FORWARD/BACKWARD action on the node itself. `scrollitem`
    # (Android's SCROLL_TO_POSITION / SHOW_ON_SCREEN, UIA's ScrollItem) says
    # "this element can be *brought into view*" - a property of list rows and
    # of leaf icons alike - and used to be read as "this element can scroll",
    # which painted `+scroll(to:up|down|top|bottom)` on every TextView and
    # button a MIUI launcher publishes (found live: TIM's search BUTTON and
    # the launcher's app labels advertised a scroll they cannot perform).
    # The role table is likewise not evidence: an element that reports no
    # scroll interface has nothing for the provider to run.
    if "scroll" in patterns:
        caps.setdefault(
            "scroll",
            _cap(
                "scroll",
                {
                    "to": ParamSpec(
                        type="enum", enum=("up", "down", "top", "bottom"), required=False
                    )
                },
            ),
        )

    # A ScrollItem (a row in a virtualised list) can be brought into view; this
    # is how an off-screen item is reached without scrolling blindly. It is a
    # real native capability, not a heuristic.
    if "scrollitem" in patterns:
        caps.setdefault(
            "scroll_into_view",
            Capability(
                name="scroll_into_view",
                level="primitive",
                via=[PrimitiveStep(primitive="scroll_into_view", target="$self")],
                risk="safe",
            ),
        )

    if "window" in patterns and role in ("window", "dialog"):
        caps.setdefault("close", _cap("close"))

    # Every readable node can be focused and read.
    caps.setdefault("focus", _cap("focus"))
    if node.name or node.value:
        caps.setdefault("read", _cap("read"))

    # A context menu is offered where one is *plausible*: the element exposes
    # something to operate on (`invoke` / a value / a selection / an expansion)
    # and is not itself a menu. It is the gesture that reaches an application
    # refusing everything else - QQ's conversation rows answer Invoke by merely
    # highlighting, so neither `activate` nor `select` opens a chat, but the
    # context menu they expose carries "open this chat" as an ordinary UIA menu
    # item and invoking that item works. That row *does* advertise `activate`
    # (the platform really offers Invoke), so the menu must be offered
    # alongside it rather than instead of it: the runtime cannot know which of
    # the two the application honours, and that choice is the caller's.
    #
    # On Android the context menu IS the long press, and the Portal routes the
    # primitive to ACTION_LONG_CLICK - so there the menu is offered only when
    # the element itself advertises LONG_CLICK. The Windows plausibility rule
    # painted `+context_menu` on every clickable MIUI label (they all say
    # CLICK), and the call then died with ACTION_NOT_SUPPORTED because no
    # long-press interface existed behind it (found live: TIM's launcher
    # icons). An element that says LONG_CLICK can be long-pressed; one that
    # does not, cannot.
    _android = (node.native.class_name or "").startswith("android.")
    _menu_plausible = (
        "long_click" in patterns if _android else bool(node.patterns & _CONTEXT_MENU_PATTERNS)
    )
    if (
        _is_control(node)
        and _menu_plausible
        and node.role not in ("menuitem", "menu")
    ):
        caps.setdefault(
            "context_menu",
            Capability(
                name="context_menu",
                level="primitive",
                via=[PrimitiveStep(primitive="context_menu", target="$self")],
                risk="safe",
            ),
        )

    return list(caps.values())
