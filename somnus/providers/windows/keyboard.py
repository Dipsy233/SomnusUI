"""Declarative keyboard fallback (dev doc section 12.2).

Keyboard input is **never** exposed to the LLM. It exists only as an explicitly
authorized ``via`` step inside a capability, used when the target has no
suitable native pattern - the documented case is ``set_text`` on a control that
lacks a Value pattern: focus it, then type. A control that *has* a Value pattern
is written through it instead; typing is the last resort, never a preference.

Delivery is ``SendInput`` with ``KEYEVENTF_UNICODE``, which is layout
independent (it types the character, not the physical key). The runtime performs
the focus step separately, so this module only sends keystrokes.

Caveat worth knowing: ``SendInput`` only *queues* events and returns before the
target processes them. A fast burst is therefore droppable by the target's
message pump, and the caller cannot tell from the return value alone. This route
is used only where no write interface exists, and the caller verifies the result
rather than trusting ``ok``.
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
VK_SHIFT = 0x10
VK_F10 = 0x79


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


def send_vk(vk: int, *, shift: bool = False) -> None:
    """Send one virtual key (optionally with Shift held).

    Used only by the context-menu keyboard fallback (Shift+F10): the standard
    accessibility gesture for "open this element's context menu". Focus is
    performed by the caller.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    downs: list[_INPUT] = []
    ups: list[_INPUT] = []
    if shift:
        downs.append(_mk_key(VK_SHIFT, 0))
    downs.append(_mk_key(vk, 0))
    ups.append(_mk_key(vk, KEYEVENTF_KEYUP))
    if shift:
        ups.append(_mk_key(VK_SHIFT, KEYEVENTF_KEYUP))
    for batch in (downs, ups):
        arr = (_INPUT * len(batch))(*batch)
        sent = user32.SendInput(len(arr), ctypes.byref(arr), ctypes.sizeof(_INPUT))
        if sent != len(batch):
            raise OSError(f"SendInput sent {sent}/{len(batch)} (err {ctypes.get_last_error()})")
        time.sleep(0.01)


def _mk_key(vk: int, flags: int) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


def _mk_unicode(ch: str, flags: int) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


#: How long to keep nudging before giving up on one character. The target's
#: pump hands over queued input in small bursts when woken, so the useful signal
#: is "did it appear yet", not "how long has it been" - a fixed delay is a guess
#: that is either too short (loss) or needlessly slow (every other target).
_PER_CHAR_TIMEOUT_S = 0.5
#: Pause between wake-up nudges.
_NUDGE_INTERVAL_S = 0.01


def send_unicode_text(text: str, *, verify=None) -> ExecResult:
    """Type ``text`` into whatever currently has keyboard focus.

    **Why this is not a single batch of ``SendInput`` events.**

    ``SendInput`` only *queues* events into the target thread's input queue and
    returns; it does not wait for the target to process them. Measured live on
    Windows 11 24H2 against Notepad's RichEdit, with no extra input afterwards:

    ==============================  =========  ==========================
    delivery                        reported   characters actually in the box
    ==============================  =========  ==========================
    one burst (the original version)  20/20     ``'AB'``        (2 of 10)
    chunks of 8 + 10 ms               20/20     ``'CDEFGHI'``   (7 of 10)
    chunks of 1 + 5 ms                20/20     ``'JABCDEFGHIJ'`` (leftover + 10)
    chunks of 4 + 30 ms               20/20     ``'ABCDEF'``    (6 of 10)
    ==============================  =========  ==========================

    ``SendInput`` reports complete success in every row. The count it returns
    says how many events were *accepted*, never how many were *processed*, so it
    is not evidence that the text landed.

    Worse, the unprocessed characters are not lost - they stay parked, and the
    target only drains them when new input wakes its pump. That is why a write
    can appear to do nothing and then "type itself" after the user presses a
    key. Measured, idle after sending 10 characters, then pressing SHIFT (which
    types nothing):

    ======  =========  ==================
    stage   length     content
    ======  =========  ==================
    idle   2          ``'AB'``
    key 1  4          ``'ABCD'``
    key 2  6          ``'ABCDEF'``
    key 3  8          ``'ABCDEFGH'``
    key 4  10         ``'ABCDEFGHIJ'``
    ======  =========  ==================

    Each keypress releases exactly two more characters. The text was never
    dropped; it was parked, and only a new input event made the target consume
    it. That is the reported "the text is entered but not rendered, then appears
    as you keep typing" symptom, and it is a queue-stall rather than a repaint
    lag: the model itself (``WM_GETTEXT``) contained 2 of 10 characters.

    **The fix is to stop guessing and observe.** Each character is sent, then
    the caller's ``verify`` callback says whether it actually landed; while it
    has not, a harmless wake-up (a bare SHIFT, which types nothing) is sent to
    make the target drain its queue. Measured 3/3 exact with no keypress needed,
    against 0/3 for the batched version.

    ``verify`` receives the number of characters expected so far and returns
    True once the target shows them. Without it there is nothing to observe, so
    the function falls back to paced delivery, which is a guess and is treated
    as one.
    """
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message="not windows")
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError as exc:  # pragma: no cover - platform
        return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED", message=str(exc))

    if not text:
        return ExecResult(ok=True)

    def _send(events: list[_INPUT]) -> int:
        if not events:
            return 0
        array = (_INPUT * len(events))(*events)
        return int(user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_INPUT)))

    def _nudge() -> None:
        """Wake the target's input pump without typing anything.

        A bare SHIFT press types no character, so it cannot corrupt the text;
        it only gives the target's pump something to process, which is what
        makes it drain the characters still parked behind it.
        """
        _send([_mk_key(VK_SHIFT, 0)])
        _send([_mk_key(VK_SHIFT, KEYEVENTF_KEYUP)])

    try:
        if verify is None:
            # No way to observe the result: paced delivery is the best available
            # guess. Documented as a guess because it is one.
            for ch in text:
                _send([_mk_unicode(ch, KEYEVENTF_UNICODE)])
                _send([_mk_unicode(ch, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
                time.sleep(_NUDGE_INTERVAL_S)
            return ExecResult(ok=True)

        for index, ch in enumerate(text, start=1):
            _send([_mk_unicode(ch, KEYEVENTF_UNICODE)])
            _send([_mk_unicode(ch, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
            # The target may hold this character back until its pump runs again.
            # Nudge until it shows up, rather than sleeping a fixed amount and
            # hoping: the amount a single wake-up releases depends on the
            # target, and measured as low as one character.
            deadline = time.monotonic() + _PER_CHAR_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    if verify(index):
                        break
                except Exception:  # noqa: BLE001 - a failed check is not a lost write
                    break
                _nudge()
                time.sleep(_NUDGE_INTERVAL_S)
        return ExecResult(ok=True)
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
