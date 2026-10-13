"""Declarative keyboard fallback (dev doc section 12.2).

Keyboard input is **never** exposed to the LLM. It exists only as an explicitly
authorized ``via`` step inside a pack capability, used when the target has no
suitable native pattern - the documented case is ``set_text`` on a control that
lacks a Value pattern: focus it, then type.

Delivery is ``SendInput`` with ``KEYEVENTF_UNICODE``, which is layout
independent (it types the character, not the physical key). The runtime performs
the focus step separately, so this module only sends keystrokes.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from somnus.ir.action import ExecResult

# ---------------------------------------------------------------------------
# Win32 structures (SendInput)
# ---------------------------------------------------------------------------

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    # Declared so the union is sized by its largest member; only the keyboard
    # fields are used here. A short union makes SendInput fail with
    # ERROR_INVALID_PARAMETER (87).
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
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
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def send_unicode_text(text: str) -> ExecResult:
    """Type ``text`` into whatever currently has keyboard focus.

    Fails loud (returns a code) when the platform cannot deliver input, rather
    than silently pretending success.
    """
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message="not windows")
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError as exc:  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=str(exc))

    inputs: list[_INPUT] = []
    for ch in text:
        code = ord(ch)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = _INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki = _KEYBDINPUT(
                wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=None
            )
            inputs.append(inp)

    if not inputs:
        return ExecResult(ok=True)

    # Deliver in small chunks with a tiny pause: a single SendInput of dozens
    # of unicode events gets dropped by slower message pumps (live: Notepad
    # received only the first two characters of a 17-char string), and games/
    # editors with per-frame input loops lose whole bursts.
    chunk_size = 8
    total_sent = 0
    for i in range(0, len(inputs), chunk_size):
        chunk = inputs[i : i + chunk_size]
        array = (_INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), ctypes.byref(array), ctypes.sizeof(_INPUT))
        total_sent += sent
        if sent != len(chunk):
            err = ctypes.get_last_error()
            return ExecResult(
                ok=False,
                error="NATIVE_EXECUTION_FAILED",
                message=f"SendInput sent {total_sent}/{len(inputs)} (err {err})",
            )
        time.sleep(0.01)
    return ExecResult(ok=True)
