"""The Notepad test-hygiene path: WM_CLOSE, discard, bounded fallback.

A forced kill leaves unsaved buffers in Notepad's session cache; the next
launch restores them as extra windows and pollutes every measurement. These
tests use a mock desktop so the decision logic is exercised without a real app.
"""

from __future__ import annotations

import pytest

from somnus.ir.action import ExecResult
from somnus.ir.node import RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.e2e.conftest import (
    assert_clean_notepad_launch,
    close_notepad_windows,
)
from tests.fixtures.builders import _n


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _PromptProvider(MockProvider):
    """A Notepad window that prompts on close and answers the chosen button."""

    def __init__(self, *, discard_closes: bool = True):
        super().__init__(fixtures=[])
        self.window = WindowInfo(
            window_id="w1",
            title="记事本",
            process="notepad.exe",
            pid=42,
            hwnd=0x42,
        )
        self.discard_closes = discard_closes
        self.prompted = False
        self.closed = False

    def list_windows(self):
        return [] if self.closed else [self.window]

    def peek(self, window_id):
        return None if self.closed else self.window

    def _root(self) -> RawNode:
        if not self.prompted:
            return _n("w1", (), "pane", name="记事本")
        buttons = [_n("w1", (1,), "button", name="保存", patterns=frozenset({"invoke"}))]
        if self.discard_closes:
            buttons.append(
                _n(
                    "w1",
                    (2,),
                    "button",
                    name="不保存",
                    patterns=frozenset({"invoke"}),
                )
            )
        return _n("w1", (), "dialog", name="记事本", children=buttons)

    def snapshot(self, window_id, scope=None, max_depth=12):
        return RawTree(window=self.window, root=self._root(), raw_revision=1)

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        if primitive == "activate" and ref.path == (2,) and self.discard_closes:
            self.closed = True
            return ExecResult(ok=True)
        return super().execute(ref, primitive, value, timeout_s)


def _session(provider) -> Session:
    return Session(provider, allow_packs=False)


def test_wm_close_prompt_discards_and_the_window_disappears():
    provider = _PromptProvider()
    session = _session(provider)
    clock = _Clock()
    logs: list[str] = []
    killed: list[int] = []

    def post_close(hwnd):
        provider.prompted = True
        return True

    leftover = close_notepad_windows(
        session,
        timeout_s=5,
        poll_interval=0.5,
        clock=clock.monotonic,
        sleep=clock.sleep,
        post_close=post_close,
        taskkill=killed.append,
        log=logs.append,
    )

    assert leftover == []
    assert provider.closed
    assert any("chose '不保存'" in line for line in logs), logs
    assert not any("taskkill" in line for line in logs), logs


def test_a_close_that_never_completes_falls_back_to_taskkill():
    provider = _PromptProvider(discard_closes=False)
    session = _session(provider)
    clock = _Clock()
    logs: list[str] = []
    killed: list[int] = []

    def post_close(hwnd):
        provider.prompted = True
        return True

    leftover = close_notepad_windows(
        session,
        timeout_s=5,
        poll_interval=0.5,
        clock=clock.monotonic,
        sleep=clock.sleep,
        post_close=post_close,
        taskkill=killed.append,
        log=logs.append,
    )

    assert leftover == [42]
    assert killed == [42]
    assert any(
        "fell back to taskkill pid=42 after 5s" in line for line in logs
    ), logs


def test_a_clean_launch_is_one_window():
    assert_clean_notepad_launch('OK pid=1\n+ WINDOW w1 "记事本"')


def test_extra_restored_windows_are_a_dirty_baseline():
    reply = (
        'OK pid=1\n'
        '+ WINDOW w1 "记事本"\n'
        '+ WINDOW w2 "记事本"\n'
        "~ 2 windows from one launch"
    )
    with pytest.raises(AssertionError, match="DIRTY_BASELINE"):
        assert_clean_notepad_launch(reply)
