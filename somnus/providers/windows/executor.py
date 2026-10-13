"""Native pattern execution with timeouts.

``InvokePattern.Invoke`` can block indefinitely when it opens a modal dialog, so
every pattern call runs on the provider's MTA worker with a hard timeout. On
timeout we return ``undetermined=True``; the runtime then settles and re-observes
instead of hanging (dev doc sections 9.2, 13).
"""

from __future__ import annotations

from typing import Any

from somnus.ir.action import ExecResult
from somnus.providers.windows.keyboard import send_unicode_text

#: Pattern id per primitive capability (resolved lazily from the UIA module).
PATTERN_FOR_PRIMITIVE = {
    "activate": "UIA_InvokePatternId",
    "set_text": "UIA_ValuePatternId",
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
        elif primitive == "type_text":
            # Declarative keyboard fallback: focus the target, then type. Only
            # reached because a pack declared it as a conditional `via` step
            # (dev doc section 12.2); the LLM never sees this primitive.
            element.SetFocus()
            result = send_unicode_text(str(value))
            if not result.ok:
                return result
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
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return _classify(exc)
    return ExecResult(ok=True)


def _pattern(element: Any, pattern_id: int, interface: Any) -> Any:
    """Fetch a pattern and cast it to its COM interface.

    ``GetCurrentPattern`` returns a bare ``IUnknown`` pointer; without the cast
    the pattern methods are not visible.
    """
    raw = element.GetCurrentPattern(pattern_id)
    return raw.QueryInterface(interface)


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
    text = str(exc)
    name = type(exc).__name__
    if "ELEMENTNOTAVAILABLE" in text or "ElementNotAvailable" in name:
        return ExecResult(ok=False, error="STALE_TARGET", message=text)
    if "UIPI" in text or "AccessDenied" in name or "E_ACCESSDENIED" in text:
        return ExecResult(ok=False, error="PERMISSION_DENIED", message=text)
    return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=text)
