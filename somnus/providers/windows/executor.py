"""Native pattern execution with timeouts.

``InvokePattern.Invoke`` can block indefinitely when it opens a modal dialog, so
every pattern call runs on the provider's MTA worker with a hard timeout. On
timeout we return ``undetermined=True``; the runtime then settles and re-observes
instead of hanging.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from somnus.ir.action import ExecResult
from somnus.providers.windows.clipboard import paste_text
from somnus.providers.windows.keyboard import send_unicode_text, send_vk

#: Pattern id per primitive capability (resolved lazily from the UIA module).
PATTERN_FOR_PRIMITIVE = {
    "activate": "UIA_InvokePatternId",
    "set_text": "UIA_ValuePatternId",
    # Also not a UIA pattern: the clipboard channel (focus, Ctrl+V, restore).
    # Mapped to the same sentinel as set_text - the real gate is the
    # `clipboard` condition checked in ``run_primitive``.
    "paste_text": "UIA_ValuePatternId",
    "toggle": "UIA_TogglePatternId",
    "select": "UIA_SelectionItemPatternId",
    "expand": "UIA_ExpandCollapsePatternId",
    "collapse": "UIA_ExpandCollapsePatternId",
    "increment": "UIA_RangeValuePatternId",
    "decrement": "UIA_RangeValuePatternId",
    "scroll": "UIA_ScrollPatternId",
    "open": "UIA_InvokePatternId",
    "close": "UIA_WindowPatternId",
}

#: Capabilities that need focus first because they lack a direct pattern.
FOCUS_FIRST_FALLBACK = frozenset()


def run_primitive(
    element: Any,
    primitive: str,
    value: Any,
    UIA: Any,
) -> ExecResult:
    """Invoke one primitive against a live UIA element.

    Raises nothing; converts COM failures into ``ExecResult`` codes.
    """
    try:
        if primitive == "focus":
            element.SetFocus()
        elif primitive == "read":
            return ExecResult(ok=True)
        elif primitive == "activate":
            _pattern(element, UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern).Invoke()
        elif primitive == "set_text":
            _pattern(element, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern).SetValue(
                str(value)
            )
        elif primitive == "toggle":
            _pattern(element, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern).Toggle()
        elif primitive == "select":
            _pattern(
                element,
                UIA.UIA_SelectionItemPatternId,
                UIA.IUIAutomationSelectionItemPattern,
            ).Select()
        elif primitive == "expand":
            _pattern(
                element,
                UIA.UIA_ExpandCollapsePatternId,
                UIA.IUIAutomationExpandCollapsePattern,
            ).Expand()
        elif primitive == "collapse":
            _pattern(
                element,
                UIA.UIA_ExpandCollapsePatternId,
                UIA.IUIAutomationExpandCollapsePattern,
            ).Collapse()
        elif primitive in ("increment", "decrement"):
            pat = _pattern(
                element, UIA.UIA_RangeValuePatternId, UIA.IUIAutomationRangeValuePattern
            )
            step = pat.CurrentSmallChange or 1
            pat.SetValue(
                pat.CurrentValue + (step if primitive == "increment" else -step)
            )
        elif primitive == "scroll":
            _scroll(
                _pattern(element, UIA.UIA_ScrollPatternId, UIA.IUIAutomationScrollPattern),
                value,
                UIA,
            )
        elif primitive == "paste_text":
            # Clipboard channel: focus, put the text on the clipboard, Ctrl+V,
            # then put the user's clipboard back. Reaches a control that
            # publishes nothing writable and maps no HWND (a self-drawn editor),
            # because the text is read by the application's own WM_PASTE handler
            # rather than pushed through the input queue - measured constant
            # ~37 ms from 10 to 3000 characters, where per-character SendInput
            # parks most of a fast burst (docs/text-input-channels.md 3.3).
            #
            # It needs the foreground and it disturbs the user's clipboard, so it
            # is gated on --allow-clipboard and ordered *after* the native value
            # route. Every route raises the target first; there is no focus-free
            # attempt. A refusal that costs nothing (the clipboard holds an image
            # we cannot restore) is reported as unsupported so the chain falls
            # through to typing rather than failing the call.
            fg = _bring_to_foreground(element)
            if not fg.ok:
                # The window could not be raised, so Ctrl+V would go to whatever
                # *is* in front. Report the route as unavailable so the chain
                # can try the next one rather than typing into a stranger.
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message=fg.message,
                    unsupported=True,
                )
            outcome = paste_text(str(value), verify=_text_landed_verifier(element, value))
            if outcome.ok:
                return ExecResult(ok=True)
            if outcome.error == "CLIPBOARD_UNAVAILABLE":
                # The environment refused (the clipboard could not be opened at
                # all - a locked session, or another process holding it). The
                # element may well accept a paste, so this is reported as an
                # unavailable *route* and the chain continues to typing.
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message=outcome.message,
                    unsupported=True,
                )
            return ExecResult(ok=False, error=outcome.error, message=outcome.message)
        elif primitive == "type_text":
            # Declarative keyboard fallback: focus the target, then type. Only
            # reached because a pack declared it as a conditional `via` step;
            # the LLM never sees this primitive.
            fg = _bring_to_foreground(element)
            if not fg.ok:
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message=fg.message,
                    unsupported=True,
                )
            # Pass a verifier so the sender can tell whether each character
            # actually landed. Without it the target can park queued input until
            # the next real keystroke, which shows up as "the text was entered but
            # nothing rendered, until I press a key". See keyboard.py.
            result = send_unicode_text(str(value), verify=_text_landed_verifier(element, value))
            if not result.ok:
                return result
        elif primitive == "scroll_into_view":
            _pattern(
                element,
                UIA.UIA_ScrollItemPatternId,
                UIA.IUIAutomationScrollItemPattern,
            ).ScrollIntoView()
        elif primitive == "context_menu":
            # Semantic layer, not input injection. ShowContextMenu is the UIA
            # verb for "open this element's context menu": it needs no
            # coordinates, moves no pointer, sends no keystroke, and works while
            # the window is in the background. Applications that only open on a
            # real double-click (QQ's conversation rows) expose their menu this
            # way, and the menu's items are ordinary UIA menu items whose Invoke
            # does work - which is what makes them operable without any
            # synthetic input at all.
            #
            # Element3 arrived in Windows 8.1. On an older provider the call
            # raises and the caller is told the action is unsupported rather
            # than silently doing nothing.
            try:
                element.QueryInterface(UIA.IUIAutomationElement3).ShowContextMenu()
            except Exception as exc:  # noqa: BLE001 - converted to a code
                if "Element3" in str(exc) or "AttributeError" in type(exc).__name__:
                    return ExecResult(
                        ok=False,
                        error="ACTION_NOT_SUPPORTED",
                        message="ShowContextMenu needs IUIAutomationElement3 (Windows 8.1+)",
                    )
                raise
            # ShowContextMenu ran but the element's handler may ignore it (some
            # shell/XAML hosts only react to a real keyboard menu gesture).
            # The standard keyboard gesture for "open this element's context
            # menu" is Shift+F10 (the Apps key without keyboard apps key).
            # This is the accessibility verb, not pointer simulation: no
            # coordinates, no cursor movement - the menu opens wherever the
            # element decides. It requires focus, so focus first.
            try:
                element.SetFocus()
                send_vk(0x2D, shift=True)  # VK_APPS-equivalent via Shift+F10
            except Exception:  # noqa: BLE001 - the first path already ran
                pass
        elif primitive == "close":
            _pattern(
                element, UIA.UIA_WindowPatternId, UIA.IUIAutomationWindowPattern
            ).Close()
        else:
            # Last-resort standard fallback.
            _pattern(
                element,
                UIA.UIA_LegacyIAccessiblePatternId,
                UIA.IUIAutomationLegacyIAccessiblePattern,
            ).DoDefaultAction()
    except _PatternMissing as exc:
        # The element does not offer this interface. That is a fact about the
        # control, not a failure of the call, so it is reported as unsupported
        # and the caller may try its next declared route.
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=str(exc), unsupported=True)
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return _classify(exc)
    return ExecResult(ok=True)


def _top_level_hwnd(element: Any) -> int:
    """The top-level window handle an element lives in, or 0.

    An element's own handle is 0 for the self-drawn controls the synthetic
    channels exist to serve, so the walk goes up the UIA tree to the first
    ancestor that has one (the enclosing window).
    """
    import ctypes

    user32 = ctypes.windll.user32
    try:
        hwnd = int(getattr(element, "CurrentNativeWindowHandle", 0) or 0)
    except Exception:
        hwnd = 0
    if hwnd:
        return int(user32.GetAncestor(hwnd, 2) or hwnd)  # 2 = GA_ROOT
    try:
        from comtypes.gen import UIAutomationClient as _UIA

        walker = _UIA.CUIAutomation().ControlViewWalker
        current = element
        for _ in range(64):
            parent = walker.GetParentElement(current)
            if parent is None:
                break
            parent_hwnd = int(getattr(parent, "CurrentNativeWindowHandle", 0) or 0)
            if parent_hwnd:
                return int(user32.GetAncestor(parent_hwnd, 2) or parent_hwnd)
            current = parent
    except Exception:
        return 0
    return 0


def _bring_to_foreground(element: Any) -> ExecResult:
    """Focus ``element`` and raise its top-level window, or say why not.

    A ``Ctrl+V`` (and typing) goes to the *foreground* window's focused control,
    so both halves are required: ``SetFocus`` chooses the control inside the
    window, ``SetForegroundWindow`` chooses the window. Focusing without raising
    is what let a synthetic write land on whatever else happened to be in front
    (observed live: a chat send went nowhere while another app held the
    foreground).

    ``SetForegroundWindow`` is refused while another process owns the foreground
    (Win32's foreground lock), so the raise goes through the same
    ``workspace.raise_window`` the provider uses (plain set, attach-thread, inert
    Alt tap). When even that fails the refusal is reported as
    ``FOREGROUND_DENIED``: synthetic input has no focus-free fallback, so the
    honest answer is that the window could not be fronted, not a keystroke that
    would land in somebody else's window.
    """
    try:
        element.SetFocus()
    except Exception:
        pass
    try:
        from somnus.providers.windows.workspace import raise_window

        hwnd = _top_level_hwnd(element)
        if not hwnd:
            return ExecResult(ok=True, message="no top-level window handle")
        if not raise_window(hwnd):
            return ExecResult(
                ok=False,
                error="FOREGROUND_DENIED",
                message=(
                    "the target window could not be raised (another application "
                    "holds the foreground); synthetic input would land elsewhere"
                ),
            )
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
    return ExecResult(ok=True)


def _text_landed_verifier(element: Any, expected: Any):
    """Build the callback that tells the sender when a character has arrived.

    Reads the element through UIA, which is the only channel that reports what a
    self-drawn control actually holds (a window-level ``WM_GETTEXT`` does not
    exist for one). Returns None when no readable surface exists at all, in
    which case the sender falls back to paced delivery and says so.

    **Two surfaces, tried in order.** The Value property is the obvious one, but
    it is *not* the one a rich-text editor uses. Measured live on QQ's
    ProseMirror editor (class ``ProseMirror ExEditor-qq-msg-editor``):

    ===========================  ==========================================
    surface                      what it reports while typing
    ===========================  ==========================================
    ``Value`` property (30045)   always ``''`` - present but dead
    ``Text`` pattern document    the live content, updated per character
    ===========================  ==========================================

    Reading only the Value property therefore produced a verifier that could
    never succeed: it returned ``len('') - 0 >= count`` -> ``False`` forever, so
    every character burned its full retry budget and the sender nudged the
    target with SHIFT keystrokes the whole time. Measured cost: 1.89 s for three
    characters, 3.41 s for six, and a hard provider timeout at ten. Reading the
    Text pattern where Value is empty fixes the signal rather than the timeout.

    Compares by *length*, not content: typing is incremental, the control may
    normalise what it stores (rich text, an IME composition), and the question is
    only "has the target caught up", not "is the stored text identical".
    """
    want = str(expected)
    surface = _read_surface(element)
    if surface is None:
        return None
    start = len(surface())

    def _verify(count: int) -> bool:
        current = surface()
        if current is None:
            # Cannot read: stop nudging, do not turn it into a failure.
            return True
        # Each character adds one unit; a control that reports something else
        # entirely should not spin the timeout.
        return len(current) - start >= count

    return _verify


#: UIA_TextPatternId, and the document range cap. A verifier only ever compares
#: lengths, so the cap only bounds the read cost on a very long document.
_UIA_TEXT_PATTERN_ID = 10014
_TEXT_READ_CAP = 65536


def _read_surface(element: Any):
    """Pick the property that actually reports this element's text.

    Returns a zero-argument callable yielding the current text, or ``None`` when
    neither surface works (meaning: no verifier is possible here).

    The Value property is preferred *only when it is alive*. An editor that
    exposes no Value pattern typically still answers the property read - with an
    empty string, not an error - so "it returned a string" is not evidence that
    it is usable. The Text pattern is the surface rich-text editors actually
    publish, and it is what the fallback reads.
    """

    def _value() -> str | None:
        try:
            current = element.GetCurrentPropertyValue(_UIA_VALUE_VALUE_PROPERTY)
        except Exception:  # noqa: BLE001 - unreadable element
            return None
        return current if isinstance(current, str) else None

    def _text() -> str | None:
        try:
            raw = element.GetCurrentPattern(_UIA_TEXT_PATTERN_ID)
            if raw is None:
                return None
            # ``GetCurrentPattern`` returns a bare IUnknown pointer; without the
            # cast its methods are not visible. A provider/test double that
            # already yields the interface is accepted as-is.
            query = getattr(raw, "QueryInterface", None)
            text_pattern = query(_text_pattern_interface()) if query else raw
            return text_pattern.DocumentRange.GetText(_TEXT_READ_CAP) or ""
        except Exception:  # noqa: BLE001 - no Text pattern, or it refused
            return None

    # Prefer Value when it reports something; otherwise use the Text pattern.
    value_now = _value()
    if value_now:
        return _value
    text_now = _text()
    if text_now is not None:
        return _text
    # A readable-but-empty Value is still better than nothing: it at least
    # reports "nothing arrived", which is the honest answer for a control that
    # stores elsewhere and cannot be observed.
    if value_now is not None:
        return _value
    return None


def _text_pattern_interface():
    """IUIAutomationTextPattern, imported on demand.

    Importing the generated UIA typelib at module scope would make the executor
    unusable wherever comtypes cannot generate it, and this is only reached for
    elements that actually answer the Text pattern.
    """
    from comtypes.gen import UIAutomationClient as _UIA

    return _UIA.IUIAutomationTextPattern


def _pattern(element: Any, pattern_id: int, interface: Any) -> Any:
    """Fetch a pattern and cast it to its COM interface.

    ``GetCurrentPattern`` returns a bare ``IUnknown`` pointer; without the cast
    the pattern methods are not visible.

    A pattern the element does not have raises :class:`_PatternMissing`, which
    the caller turns into an honest "unsupported" answer. This matters because
    UIA is unreliable about it: an element can report a pattern as available
    (``IsPatternAvailable`` true, or a non-null pointer that querys into a
    shell) and then fail when the method is called. Both shapes mean the same
    thing to a caller - the interface is not usable here - so both are reported
    the same way.
    """
    try:
        raw = element.GetCurrentPattern(pattern_id)
    except Exception as exc:  # noqa: BLE001 - converted to a routing signal
        raise _PatternMissing(str(exc)) from exc
    if raw is None:
        raise _PatternMissing("pattern unavailable")
    try:
        return raw.QueryInterface(interface)
    except Exception as exc:  # noqa: BLE001
        raise _PatternMissing(str(exc)) from exc


#: UIA_ValuePatternId / the Value.Value property, used to read back what a
#: control holds while typing into it (see `_text_landed_verifier`).
_UIA_VALUE_VALUE_PROPERTY = 30045


class _PatternMissing(Exception):
    """The element does not offer the requested native interface."""


def _scroll(pat: Any, value: Any, UIA: Any) -> None:
    if value == "up":
        pat.Scroll(UIA.ScrollAmount_NoAmount, UIA.ScrollAmount_SmallDecrement)
    elif value == "down":
        pat.Scroll(UIA.ScrollAmount_NoAmount, UIA.ScrollAmount_SmallIncrement)
    elif value == "left":
        pat.Scroll(UIA.ScrollAmount_SmallDecrement, UIA.ScrollAmount_NoAmount)
    elif value == "right":
        pat.Scroll(UIA.ScrollAmount_SmallIncrement, UIA.ScrollAmount_NoAmount)
    elif value == "top":
        pat.SetScrollPercent(pat.CurrentHorizontalScrollPercent, 0)
    elif value == "bottom":
        pat.SetScrollPercent(pat.CurrentHorizontalScrollPercent, 100)


def _classify(exc: Exception) -> ExecResult:
    """Turn a COM failure into an error the caller can act on.

    A raw HRESULT is useless to an agent ("(-2147220992, None, (None, None,
    None, 0, None))" says nothing about what to do next), so the codes UIA is
    known to return are named explicitly.
    """
    text = str(exc)
    name = type(exc).__name__
    # A NULL COM pointer means the element we held is gone: the UIA element was
    # released when the UI re-rendered. That is exactly the stale case, and
    # reporting it as such lets the caller re-find instead of retrying a dead
    # pointer.
    if "NULL COM pointer" in text:
        return ExecResult(ok=False, error="STALE_TARGET", message=text)
    if "ELEMENTNOTAVAILABLE" in text or "ElementNotAvailable" in name:
        return ExecResult(ok=False, error="STALE_TARGET", message=text)
    # UIA_E_ELEMENTNOTENABLED: the provider refuses the action because the
    # element is not enabled. Distinct from TARGET_DISABLED, which the runtime
    # decides from the observed state *before* acting; this one is the provider
    # refusing at execution time, and for a self-drawn control the two can
    # disagree (Chromium keeps the button's own IsEnabled false even while the
    # rendered button is live). Reporting it separately stops the failure from
    # looking like a generic native crash and tells the caller the control
    # needs a state change (or another route) rather than a retry.
    if "-2147220992" in text or "ELEMENTNOTENABLED" in text:
        return ExecResult(
            ok=False,
            error="TARGET_DISABLED",
            message="provider refused: element not enabled",
        )
    if "UIPI" in text or "AccessDenied" in name or "E_ACCESSDENIED" in text:
        return ExecResult(ok=False, error="PERMISSION_DENIED", message=text)
    return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=text)
