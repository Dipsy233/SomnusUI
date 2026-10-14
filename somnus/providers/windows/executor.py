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
from somnus.providers.windows.keyboard import send_unicode_text
from somnus.providers.windows.mouse import double_click

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
        elif primitive == "scroll_into_view":
            _pattern(
                element,
                UIA.UIA_ScrollItemPatternId,
                UIA.IUIAutomationScrollItemPattern,
            ).ScrollIntoView()
        elif primitive == "double_click":
            # UIA has no DoubleClick pattern; QQ and a few others only open on a
            # real double-click. This is the single place synthetic input is
            # used, and a pack must declare it explicitly (dev doc section 12.2).
            result = _double_click_element(element, UIA)
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


def _double_click_element(element: Any, UIA: Any) -> ExecResult:
    """Double-click an element at its centre, safely.

    Synthetic clicks are coordinate based, so unlike every other primitive they
    can land on the wrong thing. Two guards keep that from happening:

    * the rectangle is read fresh, and a degenerate one is refused;
    * the element is moved into view and the pointer is only clicked once
      ``ElementFromPoint`` confirms our element is actually on top there. Without
      that check a click can hit whatever else happens to be over the element
      (a tooltip, an overlay, another window), which is how a stray click ends
      up closing something.
    """
    try:
        element.SetFocus()
    except Exception:
        pass
    # A minimised window still reports UIA rectangles, but they are the *old*
    # layout positions, not where the window is on screen - so a synthetic click
    # would land on the desktop or on whatever occupies those pixels (live: QQ
    # minimised to (-32000,-32000) while its rows still reported y~200, and the
    # click hit explorer). Restore and raise the window before touching the
    # mouse; if that cannot be done, refuse rather than click blind.
    restored = _ensure_visible(element, UIA)
    if not restored.ok:
        return restored
    # A target inside a scrolled region may be clipped or logically offscreen;
    # bring it into view first so its rectangle is where it claims to be.
    try:
        _pattern(
            element, UIA.UIA_ScrollItemPatternId, UIA.IUIAutomationScrollItemPattern
        ).ScrollIntoView()
    except Exception:
        pass
    try:
        rect = element.CurrentBoundingRectangle
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except Exception as exc:  # noqa: BLE001 - reported as a code
        return ExecResult(
            ok=False, error="ACTION_NOT_SUPPORTED", message=f"no bounds: {exc}"
        )
    # Require a real area. `x <= 0 and y <= 0` was wrong twice over: it accepted
    # an element at y=0 (a valid top-edge target) and accepted a zero-size rect
    # as long as its centre was positive. A degenerate rect means there is
    # nothing to click, and clicking the desktop origin is exactly the kind of
    # stray input that can hit an unrelated window.
    if right <= left or bottom <= top:
        return ExecResult(
            ok=False,
            error="ACTION_NOT_SUPPORTED",
            message=f"element has no area ({left},{top},{right},{bottom})",
        )
    x = (left + right) // 2
    y = (top + bottom) // 2

    # Hit-test: something must actually be there. If the point is on another
    # window (the app is not foreground), do not click blindly - report it so
    # the caller can focus the window and retry instead of clicking into
    # whatever is on screen. `WindowFromPoint` is used rather than a UIA
    # ElementFromPoint because it is always available and answers exactly the
    # question asked: which window owns this screen pixel.
    #
    # This guard fails *closed*: if the hit-test cannot be performed at all, the
    # click is refused. Proceeding without knowing what is under the point is
    # exactly the situation the guard exists to prevent.
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        hit_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(hit_pid))
        hit_pid = int(hit_pid.value)
        own_pid = int(element.CurrentProcessId)
    except Exception as exc:  # noqa: BLE001 - reported as a code
        return ExecResult(
            ok=False,
            error="TARGET_NOT_FOREGROUND",
            message=f"cannot verify the click point: {exc}",
        )
    if hit_pid and hit_pid != own_pid:
        return ExecResult(
            ok=False,
            error="TARGET_NOT_FOREGROUND",
            message=f"point ({x},{y}) belongs to pid {hit_pid}, not {own_pid}",
        )
    return double_click(x, y)


def _ensure_visible(element: Any, UIA: Any) -> ExecResult:
    """Restore and raise the window owning ``element`` before a synthetic click.

    Returns a failed ``ExecResult`` when the window cannot be made visible, so
    the caller refuses to click a coordinate that may not belong to the target.
    """
    try:
        hwnd = int(element.CurrentNativeWindowHandle or 0)
        own_pid = int(element.CurrentProcessId)
    except Exception as exc:  # noqa: BLE001 - reported as a code
        return ExecResult(
            ok=False, error="TARGET_NOT_FOREGROUND", message=f"cannot read target: {exc}"
        )
    if not hwnd:
        # Rows inside a self-drawn surface have no handle of their own; fall back
        # to the top-level window owned by the same process.
        hwnd = _top_level_window_for_pid(own_pid)
    if not hwnd:
        return ExecResult(
            ok=False,
            error="TARGET_NOT_FOREGROUND",
            message="no native window handle for the target",
        )
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.25)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        return ExecResult(ok=True)
    except Exception as exc:  # noqa: BLE001 - reported as a code
        return ExecResult(
            ok=False, error="TARGET_NOT_FOREGROUND", message=f"cannot raise window: {exc}"
        )


def _top_level_window_for_pid(pid: int) -> int:
    """The handle of the first visible top-level window owned by ``pid``."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    found.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(enum_proc(_cb), 0)
    except Exception:  # noqa: BLE001 - best effort
        return 0
    return found[0] if found else 0


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
    # A NULL COM pointer means the element we held is gone: the UIA element was
    # released when the UI re-rendered. That is exactly the stale case, and
    # reporting it as such lets the caller re-find instead of retrying a dead
    # pointer (dev doc section 10).
    if "NULL COM pointer" in text:
        return ExecResult(ok=False, error="STALE_TARGET", message=text)
    if "ELEMENTNOTAVAILABLE" in text or "ElementNotAvailable" in name:
        return ExecResult(ok=False, error="STALE_TARGET", message=text)
    if "UIPI" in text or "AccessDenied" in name or "E_ACCESSDENIED" in text:
        return ExecResult(ok=False, error="PERMISSION_DENIED", message=text)
    return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=text)
