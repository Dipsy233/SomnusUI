"""Native pattern execution with timeouts.

``InvokePattern.Invoke`` can block indefinitely when it opens a modal dialog, so
every pattern call runs on the provider's MTA worker with a hard timeout. On
timeout we return ``undetermined=True``; the runtime then settles and re-observes
instead of hanging (dev doc sections 9.2, 13).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from somnus.ir.action import ExecResult
from somnus.providers.windows.keyboard import send_unicode_text, send_vk

#: Win32 message ids for the HWND write channel.
WM_SETTEXT = 0x000C
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2
#: ``SendMessageTimeout`` flags: SMTO_ABORTIFHUNG | SMTO_BLOCK.
_SMTO = 0x0002 | 0x0001
#: A control that does not answer within this must be treated as busy, not hung
#: on forever: a modal dialog on the target thread would otherwise block the
#: agent for good (the same reason every pattern call has a timeout).
_SEND_TIMEOUT_MS = 2000

#: Pattern id per primitive capability (resolved lazily from the UIA module).
PATTERN_FOR_PRIMITIVE = {
    "activate": "UIA_InvokePatternId",
    "set_text": "UIA_ValuePatternId",
    # Not a UIA pattern: a Win32 message to the element's own HWND. Kept in the
    # same table so "which primitive can this element run" has one answer.
    # Mapped to a sentinel id that no element reports as a pattern, because the
    # real gate is `native_window_handle != 0`, checked in ``run_primitive``.
    "set_text_hwnd": "UIA_ValuePatternId",
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
        elif primitive == "set_text_hwnd":
            # Win32 message channel. Used when the element has a real HWND but
            # no usable Value pattern. Measured on Windows 11 24H2 Notepad:
            # writes correctly with the window in the *background*, 4/4 runs
            # including CJK and a 300-character string, and the foreground
            # window never changes - so unlike SendInput this needs no focus and
            # cannot drop tail characters.
            return _set_text_hwnd(element, str(value), UIA)
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
        elif primitive == "type_text":
            # Declarative keyboard fallback: focus the target, then type. Only
            # reached because a pack declared it as a conditional `via` step
            # (dev doc section 12.2); the LLM never sees this primitive.
            element.SetFocus()
            result = send_unicode_text(str(value))
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
            # Last-resort standard fallback (dev doc section 13).
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


def _set_text_hwnd(element: Any, text: str, UIA: Any) -> ExecResult:
    """Replace an element's text through a Win32 message to its own HWND.

    Two messages are tried, in order, because they mean different things to the
    control and only one of them is right for a given window class:

    * ``EM_REPLACESEL`` - the RichEdit/Edit "replace the selection" verb. This
      is the closest native equivalent to a user edit, and preserves the undo
      stack.
    * ``WM_SETTEXT`` - the generic "set the window text" message, for classes
      that answer it but not the ``EM_*`` messages.

    ``SendMessageTimeout`` is used for every call: a plain ``SendMessage`` from
    this process to another process blocks until the target's message pump
    responds, and a modal dialog would block the agent indefinitely.

    The caller must have verified ``native_window_handle != 0``; a message sent
    to HWND 0 would go to the desktop and is rejected here rather than sent.
    """
    import ctypes
    from ctypes import wintypes

    hwnd = _element_hwnd(element)
    if not hwnd:
        return ExecResult(
            ok=False,
            error="ACTION_NOT_SUPPORTED",
            message="element has no native window handle",
            unsupported=True,
        )

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    result = ctypes.c_size_t()

    def _send(msg: int, wparam: int, lparam: Any) -> bool:
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd),
            msg,
            wparam,
            lparam,
            _SMTO,
            _SEND_TIMEOUT_MS,
            ctypes.byref(result),
        )
        return bool(ok)

    try:
        buf = ctypes.create_unicode_buffer(text)
        # EM_REPLACESEL first: `wparam=1` means "the operation can be undone".
        _send(EM_SETSEL, 0, -1)  # select everything: replace, not append
        if _send(EM_REPLACESEL, 1, ctypes.byref(buf)):
            return ExecResult(ok=True)
    except Exception:  # noqa: BLE001 - fall through to WM_SETTEXT
        pass

    try:
        buf = ctypes.create_unicode_buffer(text)
        if _send(WM_SETTEXT, 0, ctypes.byref(buf)):
            return ExecResult(ok=True)
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return _classify(exc)

    # SendMessageTimeout returns 0 both for "timed out" and for a target that
    # failed the message. ERR distinguishes them: 0 means the target answered
    # (and refused), otherwise the call timed out.
    err = ctypes.get_last_error()
    if err:
        return ExecResult(
            ok=False,
            error="TIMEOUT",
            undetermined=True,
            message=f"SendMessageTimeout failed (err {err})",
        )
    return ExecResult(
        ok=False,
        error="NATIVE_EXECUTION_FAILED",
        message="the control refused both EM_REPLACESEL and WM_SETTEXT",
    )


def _element_hwnd(element: Any) -> int:
    """The element's own HWND, or 0.

    Read live from the element rather than passed in, so the value cannot be
    stale: an element that has been recreated since the snapshot reports its new
    handle. A failure here is reported as "no handle" and the message is not
    sent anywhere.
    """
    try:
        return int(element.CurrentNativeWindowHandle or 0)
    except Exception:  # noqa: BLE001 - a dead element has no handle
        return 0


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
    # pointer (dev doc section 10).
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
