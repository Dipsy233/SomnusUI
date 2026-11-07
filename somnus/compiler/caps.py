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


def _cap(name: str, params: dict[str, ParamSpec] | None = None) -> Capability:
    risk = "safe" if name in SAFE_CAPABILITIES else "normal"
    return Capability(
        name=name,
        level="primitive",
        params=params or {},
        via=[PrimitiveStep(primitive=name, target="$self")],
        risk=risk,  # type: ignore[arg-type]
    )


#: Whether the default-action capability is exposed under the **pattern's own
#: name** (``invoke``, no alias) instead of the intent verb ``activate``.
#: Off by default — the alias is the shipped behaviour. The benchmark's
#: naming round flips this per scheme to measure what the alias costs or
#: saves; everything downstream (executor, provider scripts) speaks
#: primitives, so only this naming decision changes.
NATIVE_CAP_NAMES = False


def set_native_cap_names(enabled: bool) -> None:
    """Switch the default-action capability name (process-global)."""
    global NATIVE_CAP_NAMES
    NATIVE_CAP_NAMES = bool(enabled)


def _default_action_cap() -> str:
    return "invoke" if NATIVE_CAP_NAMES else "activate"


#: Roles that are inherently text-entry surfaces.
#:
#: ``select`` is deliberately **absent**: a combo box is a *selection* control,
#: not a free-text field. It used to be listed here, which advertised ``set_text``
#: on every combo box - a write the control cannot perform (the value is chosen
#: from a list, never typed), so the capability promised a gesture whose only
#: possible outcome was a refusal. What actually moves a combo box is
#: ``select`` on one of its items, plus ``expand``/``collapse`` on the box.
_TEXT_ENTRY_ROLES: frozenset[str] = frozenset({"input", "slider"})

#: Roles that are selectable by definition. They are granted ``select`` even when
#: the provider did not report the pattern, exactly as before: for a list item /
#: tab / radio the platform semantic *is* "this can be picked", and the executor
#: tries the SelectionItem interface and reports honestly when the element
#: declines it.
#:
#: ``select`` (the portable role of a combo box) is here because its drop-down
#: rows are what actually get picked; the box itself carries the Selection
#: pattern, which proves its children are selectable but has no ``Select`` verb
#: of its own (UIA's ``SelectionPattern`` only reads). Granting ``select`` to a
#: *list container* was considered and rejected for that reason: it advertised
#: an action with no native primitive behind it, which is exactly the false
#: promise this change removes.
_SELECTABLE_ROLES: frozenset[str] = frozenset(
    {"listitem", "tab", "radio", "row", "treeitem", "select"}
)


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
    if (
        node.role in _TEXT_ENTRY_ROLES
        or node.role == "select"
        # A container carrying a Value pattern is still judged here by the old
        # text-surface rule even though `_is_text_entry` now refuses it as an
        # input (`P2-5`): the LegacyIAccessible bridge on such a node does not
        # mean "this has a default action", it means "this aggregates text".
        or ("value" in node.patterns and node.role in ("pane", "document", "group", "unknown"))
        or _is_text_entry(node)
    ):
        # `invoke` on a text-entry node is taken seriously (some editors do
        # publish it as "open"), but the blanket `legacyiaccessible` bridge is
        # not - every Win32 edit control carries it.
        #
        # ``select`` is included here for the same reason even though it is no
        # longer a *text-entry* role: a combo box commonly publishes
        # LegacyIAccessible, and its default action is "open the drop-down",
        # not "activate". Advertising `activate` on it would send the agent into
        # a call the element does not honour. Its real gestures are `expand`
        # and picking an item's `select`.
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
    docs/reference/text-input-channels.md 3.2 - the HWND route was hidden by the
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


def _can_read(node: RawNode) -> bool:
    """Whether ``read`` can actually return this node's content.

    Proof, not role guessing:

    * ``value`` - the application's own readable value interface (every field
      and text control that can be read back publishes it);
    * ``text`` - the Text pattern, which is the readable surface of documents
      and rich/self-drawn editors;
    * for the pure text roles, the accessible **Name** *is* the content, so a
      named text/status/heading node is readable even without either pattern
      (its label is already what observe prints).
    """
    if "value" in node.patterns or "text" in node.patterns:
        return True
    return node.role in ("text", "statusbar", "heading", "paragraph") and bool(node.name)


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
    #
    # A pane/group **that has children** is a container, not an input surface.
    # Its Value pattern is the accessibility bridge reporting the aggregate text
    # of what it contains (a window root carrying its title, a panel carrying
    # its labels), and advertising `set_text` there offered a write that could
    # only land on the container's own name - the "phantom `pn1 +set_text`" on
    # every window root. A leaf with a writable Value pattern is still an
    # editable surface, so the check is on structure, not on the role alone.
    if "value" in node.patterns:
        return node.role in ("pane", "document", "group", "unknown") and not node.children
    return False


def primitive_capabilities(node: RawNode) -> list[Capability]:
    """Return the primitive capabilities implied by ``node``'s role + patterns."""
    caps: dict[str, Capability] = {}
    patterns = node.patterns
    role = node.role

    # The default-action capability exists exactly when the platform offers
    # one: an `Invoke` pattern, or the `LegacyIAccessible` bridge the executor
    # falls back to. The role is *not* evidence - a `role=button` element with
    # no Invoke (a title-bar button, a self-drawn fake, a control that only
    # exposes ExpandCollapse) would otherwise be handed a capability whose only
    # possible outcome is ACTION_NOT_SUPPORTED, while the gesture that really
    # works went unmentioned.
    #
    # `link` and `menuitem` keep an implicit default action: a hyperlink and a
    # menu row are activated by the accessibility layer itself even without an
    # explicit Invoke, which is what makes them navigable at all.
    if _has_default_action(node) or role in ("link", "menuitem"):
        name = _default_action_cap()
        cap = _cap(name)
        if name != "activate":
            # Native naming (`invoke`) aliases nothing: the capability carries
            # the pattern's own name, but the executor's verb for the default
            # action is still `activate`, so the route names that primitive.
            cap.via = [PrimitiveStep(primitive="activate", target="$self")]
        caps.setdefault(name, cap)

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

    # `select` is "pick this one". It is offered when the platform proves the
    # element is a selection surface - SelectionItem / GridItem - or when the
    # role itself is selectable by definition (a list item, a tab, a radio,
    # a combo box).
    #
    # The combo-box case is why this is not just a pattern check: WPF's expanded
    # drop-down publishes each item's name on a copy of the item as well as on
    # the real row, and the copy may refuse `invoke` while the real row answers
    # `SelectionItemPattern.Select`. The old rule granted `activate` (from
    # `invoke`) and no `select` at all, so the one gesture that works was never
    # offered. The route chain is now explicit and ordered:
    #
    #   1. `scroll_into_view` - a virtualised row may not be realised/onscreen;
    #      this is a real UIA verb, not a scroll guess.
    #   2. `select`           - SelectionItemPattern.Select, with the executor
    #      reading `CurrentIsSelected` back and reporting it unsupported when the
    #      element silently ignored it (that is how a ghost copy behaves).
    #   3. `activate`         - Invoke, the next UIA-reachable gesture when
    #      SelectionItem is absent or did not take effect.
    #
    # No coordinate click is ever added: everything here is a UIA pattern.
    if (
        "selectionitem" in patterns
        or "griditem" in patterns
        or role in _SELECTABLE_ROLES
    ):
        steps = []
        if "scrollitem" in patterns and ("selectionitem" in patterns or "invoke" in patterns):
            # Prepare only when a real selection route follows; a lone
            # ScrollIntoView would report success without selecting anything.
            steps.append(PrimitiveStep(primitive="scroll_into_view", target="$self"))
        steps.append(PrimitiveStep(primitive="select", target="$self"))
        if "invoke" in patterns:
            # Escalation after select: the element may implement its
            # "pick" as Invoke (a combo box item that selects on click).
            steps.append(PrimitiveStep(primitive="activate", target="$self"))
        cap = _cap("select")
        cap.via = steps
        caps.setdefault("select", cap)

    if "expandcollapse" in patterns or role in ("treeitem", "select", "group", "pane"):
        if "collapsed" in node.states or "expandcollapse" in patterns:
            caps.setdefault("expand", _cap("expand"))
            caps.setdefault("collapse", _cap("collapse"))

    if "rangevalue" in patterns or role == "slider":
        caps.setdefault("increment", _cap("increment"))
        caps.setdefault("decrement", _cap("decrement"))

    if "scroll" in patterns or "scrollitem" in patterns or role in (
        "list",
        "tree",
        "table",
        "pane",
    ):
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
    # `read` is a *content* capability: it must return the text the platform
    # exposes, so it is only offered where a readable surface was proven -
    # the Value pattern (a field's own readable value) or the Text pattern
    # (documents and self-drawn editors), plus the pure text/status roles whose
    # content *is* their accessible Name. A button used to advertise `read`
    # merely because it had a name, and `CALL b1.read()` then answered OK with
    # nothing - the same false promise as a combo box's `set_text`.
    if _can_read(node):
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
    # The platform exposes **no** query for "does this element have a context
    # menu": `ShowContextMenu` is callable on any element and simply does
    # nothing when the application registered no menu. So this can only be an
    # *offer*, never a promise. What it did is reported honestly instead: a
    # call that produces no menu comes back `OK_UNVERIFIED` with a body that
    # says the element registers none, rather than a bare `OK` that leaves the
    # agent believing a menu is open somewhere (observed live: the Notepad
    # title-bar buttons).
    #
    # An element with no operable patterns at all is excluded, as is a menu
    # item and a menu itself: there is nothing for a menu to act on, and a menu
    # is what `context_menu` produces - offering "open a menu on the menu" is
    # meaningless. `_is_control` keeps a disabled button (or a self-drawn
    # control that reports no pattern) in scope, which is what lets the menu
    # remain the escape hatch for exactly those elements.
    if (
        _is_control(node)
        and node.patterns & _CONTEXT_MENU_PATTERNS
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
