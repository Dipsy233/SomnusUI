"""The keyboard fallback: event construction and the verify-driven delivery.

Typing is the last-resort route, and the one it is easiest to get subtly wrong:
`SendInput` reports how many events it *accepted*, never how many the target
*processed*, so a batched send looks like total success while most characters
sit parked in the target's input queue (measured 2 of 10 landed). These tests
pin the delivery logic that makes it correct - send, then nudge until the
caller's verify says the character arrived - without needing a live window: the
SendInput calls are captured through a fake `user32`.

The `_INPUT` structure size (a wrong union makes SendInput fail outright) is
pinned in tests/e2e/test_windows_windows.py, where the real platform is present.
"""

from __future__ import annotations

import sys

import pytest

from somnus.providers.windows import keyboard

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="keyboard module is Windows-only"
)


class _FakeUser32:
    """Records the batches SendInput is given, and can fail on demand."""

    def __init__(self, *, accept_all: bool = True) -> None:
        self.batches: list[list] = []
        self._accept_all = accept_all

    def SendInput(self, count, _ptr, _size):
        # The pointer target is not read here; the count is what the caller
        # checks. `_mk_*` construction is asserted separately.
        self.batches.append([count])
        return count if self._accept_all else count - 1


@pytest.fixture
def fake_user32(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(
        keyboard.ctypes, "WinDLL", lambda *a, **k: fake
    )
    return fake


# ---------------------------------------------------------------------------
# event construction
# ---------------------------------------------------------------------------


def test_a_unicode_event_carries_the_character_in_wscan():
    """KEYEVENTF_UNICODE types the character, not a physical key.

    That is what makes the route layout-independent: `wVk` is 0 and the codepoint
    travels in `wScan`."""
    event = keyboard._mk_unicode("A", keyboard.KEYEVENTF_UNICODE)
    assert event.type == keyboard.INPUT_KEYBOARD
    assert event.u.ki.wVk == 0
    assert event.u.ki.wScan == ord("A")
    assert event.u.ki.dwFlags & keyboard.KEYEVENTF_UNICODE


def test_a_unicode_event_holds_any_codepoint():
    """A CJK character is not a virtual key, so it must survive construction."""
    event = keyboard._mk_unicode("中", keyboard.KEYEVENTF_UNICODE)
    assert event.u.ki.wScan == ord("中")


def test_a_keyup_event_sets_the_keyup_flag():
    event = keyboard._mk_key(keyboard.VK_SHIFT, keyboard.KEYEVENTF_KEYUP)
    assert event.u.ki.wVk == keyboard.VK_SHIFT
    assert event.u.ki.dwFlags & keyboard.KEYEVENTF_KEYUP


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------


def test_empty_text_sends_nothing(fake_user32):
    result = keyboard.send_unicode_text("")
    assert result.ok
    assert fake_user32.batches == []


def test_delivery_sends_a_down_and_an_up_per_character(fake_user32):
    """Each character is one SendInput for the down and one for the up.

    `verify` returns True immediately, so no nudge is needed and the batch count
    is exactly twice the length."""
    result = keyboard.send_unicode_text("ab", verify=lambda _n: True)
    assert result.ok
    assert len(fake_user32.batches) == 4, fake_user32.batches


def test_a_character_that_has_not_landed_is_nudged_until_it_does(fake_user32):
    """The fix for the queue stall: nudge (Shift) until verify says it arrived.

    `verify` fails twice, then succeeds, so two nudges are sent - each a SHIFT
    down/up pair - on top of the character's own down/up."""
    calls = {"n": 0}

    def verify(_count: int) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    result = keyboard.send_unicode_text("x", verify=verify)
    assert result.ok
    # 1 char down/up + 2 nudges (each SHIFT down/up) = 2 + 4 SendInput calls.
    assert len(fake_user32.batches) == 6, fake_user32.batches


def test_verify_is_called_once_per_character_in_order(fake_user32):
    seen: list[int] = []
    keyboard.send_unicode_text("abc", verify=lambda n: (seen.append(n), True)[1])
    assert seen == [1, 2, 3]


def test_without_a_verify_it_falls_back_to_paced_delivery(fake_user32):
    """No readable surface means no signal, so it sends down/up per char and
    says so by not pretending to verify."""
    result = keyboard.send_unicode_text("abc")
    assert result.ok
    assert len(fake_user32.batches) == 6, fake_user32.batches


def test_a_raising_verify_stops_nudging_but_does_not_fail_the_write(fake_user32):
    """An unreadable target is not a lost write: stop nudging, report success."""

    def verify(_n: int) -> bool:
        raise OSError("element died")

    result = keyboard.send_unicode_text("x", verify=verify)
    assert result.ok
    # 1 char down/up; the raising verify prevents any nudge.
    assert len(fake_user32.batches) == 2, fake_user32.batches


def test_a_rejected_sendinput_event_is_reported(monkeypatch):
    """If SendInput accepts fewer events than given, that is a failure to say."""
    fake = _FakeUser32(accept_all=False)
    monkeypatch.setattr(keyboard.ctypes, "WinDLL", lambda *a, **k: fake)
    result = keyboard.send_unicode_text("a", verify=lambda _n: True)
    assert not result.ok
    assert result.error == "NATIVE_EXECUTION_FAILED"


# ---------------------------------------------------------------------------
# the context-menu gesture
# ---------------------------------------------------------------------------


def test_send_vk_sends_the_key_with_an_optional_shift(fake_user32):
    """Shift+F10 is the accessibility gesture for "open the context menu"."""
    keyboard.send_vk(0x79, shift=True)  # VK_F10 with Shift
    # Shift down, F10 down, F10 up, Shift up = two batches of two.
    assert len(fake_user32.batches) == 2, fake_user32.batches
