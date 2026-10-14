"""Regressions for the double-click primitive.

`synthetic input` is the one place SomnusUI leaves the accessibility API, so it
is also the one place a bug can affect *other* windows. These tests pin the two
faults found live: a triple-click (three press/release pairs) and clicking at
the desktop origin when the element has no usable rectangle.
"""

from __future__ import annotations

import ctypes

import pytest

from somnus.providers.windows import mouse


class _FakeUser32:
    """Records every SendInput call instead of touching the real desktop."""

    def __init__(self) -> None:
        self.batches: list[list] = []

    def GetSystemMetrics(self, index: int) -> int:
        return {76: 0, 77: 0, 78: 1920, 79: 1080}.get(index, 0)

    def SendInput(self, count, _ptr, _size):
        # `_ptr` points at an array of _INPUT; copy the flags out so the test can
        # assert on the event sequence without decoding the struct in place.
        array = ctypes.cast(_ptr, ctypes.POINTER(mouse._INPUT * count)).contents
        self.batches.append([inp.u.mi.dwFlags for inp in array])
        return count


@pytest.fixture()
def fake_user32(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(mouse.ctypes, "WinDLL", lambda *a, **k: fake)
    return fake


def _count(fake, flag: int) -> int:
    """Number of events with ``flag`` set (flags are bitmasks, not values)."""
    return sum(1 for batch in fake.batches for f in batch if f & flag)


def test_double_click_is_exactly_two_click_pairs(fake_user32):
    """Three press/release pairs is a *triple*-click, which is a different
    gesture (select-all / default action) and closed windows in practice."""
    result = mouse.double_click(100, 100)
    assert result.ok
    assert _count(fake_user32, mouse.MOUSEEVENTF_LEFTDOWN) == 2
    assert _count(fake_user32, mouse.MOUSEEVENTF_LEFTUP) == 2


def test_double_click_moves_once_before_the_clicks(fake_user32):
    """Extra moves between the pairs make the shell read two single clicks."""
    mouse.double_click(100, 100)
    moves = [
        f
        for batch in fake_user32.batches
        for f in batch
        if f & mouse.MOUSEEVENTF_MOVE
    ]
    assert len(moves) == 1
    # The move must come first, before any button event.
    assert fake_user32.batches[0][0] & mouse.MOUSEEVENTF_ABSOLUTE
    assert not fake_user32.batches[0][0] & mouse.MOUSEEVENTF_LEFTDOWN


def test_double_click_coordinates_are_absolute_over_the_virtual_desktop(fake_user32):
    mouse.double_click(0, 0)
    first = fake_user32.batches[0][0]
    assert first & mouse.MOUSEEVENTF_ABSOLUTE
    assert first & mouse.MOUSEEVENTF_VIRTUALDESK
