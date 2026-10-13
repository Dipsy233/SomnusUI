"""Mouse support for the one interaction UIA has no pattern for: double-click.

Every other capability maps onto a native pattern (Invoke, Toggle, Value, ...),
which is why the runtime prefers them - they are semantic, they do not depend on
coordinates, and they work even when the element is scrolled or overlapped.

A few applications require a genuine double-click to open something (for example
QQ opens a chat window only on double-click). UIA exposes no DoubleClick pattern,
so this is the single place synthetic input is used, and it is reached only
through an explicit ``via`` step a pack declares - never as a capability the LLM
can call on its own (dev doc section 12.2).
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from somnus.ir.action import ExecResult

# ---------------------------------------------------------------------------
# SendInput structures
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    # The keyboard variant is needed for the union to be the right size; only
    # the mouse fields are used here.
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # INPUT is 40 bytes on x64: DWORD type + 4 bytes padding + a 32-byte union.
    # The union must be sized by its largest member (MOUSEINPUT / KEYBDINPUT),
    # so all variants are declared; a short union makes SendInput fail with
    # ERROR_INVALID_PARAMETER (87).
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def double_click(x: int, y: int, *, interval_s: float = 0.05) -> ExecResult:
    """Left double-click at absolute screen coordinates."""
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message="not windows")
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError as exc:  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=str(exc))

    vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN

    def abs_coords(px: int, py: int) -> tuple[int, int]:
        # SendInput with ABSOLUTE|VIRTUALDESK uses a 0..65535 range over the
        # whole virtual desktop.
        nx = int((px - vx) * 65535 / max(1, vw - 1))
        ny = int((py - vy) * 65535 / max(1, vh - 1))
        return nx, ny

    nx, ny = abs_coords(x, y)
    events = [
        _mouse(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK),
        _mouse(nx, ny, MOUSEEVENTF_LEFTDOWN),
        _mouse(nx, ny, MOUSEEVENTF_LEFTUP),
    ]
    for _ in range(2):  # the second press/release pair is the "double"
        events.append(_mouse(nx, ny, MOUSEEVENTF_LEFTDOWN))
        events.append(_mouse(nx, ny, MOUSEEVENTF_LEFTUP))
        events.append(_mouse(nx, ny, MOUSEEVENTF_MOVE))

    # Send the move + first click, pause, then the second click, so the shell
    # recognises it as a double-click rather than two separate clicks.
    first = events[:3]
    second = events[3:]
    if not _send(user32, first):
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message="SendInput failed")
    time.sleep(interval_s)
    if not _send(user32, second):
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message="SendInput failed")
    return ExecResult(ok=True)


def _mouse(nx: int, ny: int, flags: int) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(dx=nx, dy=ny, mouseData=0, dwFlags=flags, time=0,
                           dwExtraInfo=None)
    return inp


def _send(user32, events: list[_INPUT]) -> bool:
    array = (_INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_INPUT))
    return sent == len(events)
