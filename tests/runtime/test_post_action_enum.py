"""Post-action window enumeration is skipped only when the desktop is unchanged.

The signature is a cheap Win32 `(hwnd, title)` set in the real provider; the
mock here exposes the same contract over its fixtures. A skip must report the
same empty window delta a refresh would, and every real desktop change (new /
closed / renamed window) must still take the full enumeration.
"""

from __future__ import annotations

import copy

from somnus.ir.node import WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n, notepad_tree


class _SigMock(MockProvider):
    """A replay provider with a cheap desktop signature and call counters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enum_calls = 0
        self.sig_calls = 0
        self.fail_sig = False

    def list_windows(self):
        self.enum_calls += 1
        # The real provider returns fresh metadata per enumeration. Returning
        # the live window objects would let a tree mutation leak into the
        # session's cached list, which would hide the very regression tested
        # here (a title that changes after the cached list was taken).
        return [copy.deepcopy(w) for w in super().list_windows()]

    def peek(self, window_id):
        # The real provider builds fresh metadata per peek; returning the live
        # window object would let a later tree mutation change the entry the
        # session already holds (and hide the delayed-title regression).
        win = super().peek(window_id)
        return copy.deepcopy(win) if win is not None else None

    def top_level_signature(self):
        self.sig_calls += 1
        if self.fail_sig:
            raise RuntimeError("signature read failed")
        return tuple(
            sorted((t.window.window_id, t.window.title) for t in self._trees.values())
        )


def _second_window(window_id="w2", title="Notes"):
    tree = notepad_tree()
    tree.window = WindowInfo(
        window_id=window_id, title=title, process="notepad.exe", pid=2
    )
    tree.root = _n(window_id, (), "pane", title)
    return tree


def _setup():
    provider = _SigMock(fixtures=[notepad_tree()])
    session = Session(provider)
    session.observe()
    editor = next(
        ref
        for ref, src in session.result.sources.items()
        if src.native.automation_id == "editor"
    )
    return provider, session, editor


def _write(session, editor):
    return session.dispatch(f'CALL {editor}.set_text("hello")')


def test_an_unchanged_desktop_skips_the_uia_enumeration():
    provider, session, editor = _setup()
    before = provider.enum_calls
    prev = dict(session._prev_windows)

    out = _write(session, editor)

    assert out.startswith("OK"), out
    assert provider.enum_calls == before, "the desktop did not change; no UIA walk"
    # The baseline must not be polluted: a skipped call reports exactly what a
    # no-op refresh would have.
    assert session._prev_windows == prev
    assert "WINDOW" not in out


class _DelayedTitleMock(_SigMock):
    """A write renames the window a moment later - inside the settle's read.

    The action only schedules the rename; the settle snapshot applies it. A
    signature checked *before* settle therefore still sees the old title (the
    old implementation's bug); one checked after settle sees the new one.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_title: str | None = None

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        result = super().execute(ref, primitive, value, timeout_s)
        if primitive in ("set_text", "paste_text", "type_text") and value is not None:
            self.pending_title = f"*{value} - Notepad"
        return result

    def snapshot(self, window_id, scope=None, max_depth=12):
        if self.pending_title is not None:
            self._trees[window_id].window.title = self.pending_title
            self.pending_title = None
        return super().snapshot(window_id, scope, max_depth)


def test_a_title_that_flips_during_settle_is_reported_in_the_same_call():
    """The regression: the rename used to be reported one CALL late.

    Live: Notepad's title gained its "*" marker ~8.2ms after the write, while
    the old code checked the signature 3-4ms in and skipped the enumeration -
    the response had no `~ WINDOW` and `session.windows` kept the old title.
    """
    provider = _DelayedTitleMock(fixtures=[notepad_tree()])
    session = Session(provider)
    session.observe()
    editor = next(
        ref
        for ref, src in session.result.sources.items()
        if src.native.automation_id == "editor"
    )
    before = provider.enum_calls

    out = _write(session, editor)

    assert out.startswith("OK"), out
    assert "~ WINDOW" in out and "→" in out, out
    assert provider.enum_calls == before + 1, "a changed title must enumerate"
    titles = [w.title or "" for w in session.windows]
    assert any("*hello - Notepad" in title for title in titles), titles


def test_a_new_window_forces_a_full_refresh():
    provider, session, editor = _setup()
    before = provider.enum_calls
    provider.set_tree(_second_window())

    out = _write(session, editor)

    assert provider.enum_calls == before + 1
    assert "WINDOW" in out, out


def test_a_closed_window_forces_a_full_refresh():
    provider, session, editor = _setup()
    provider.set_tree(_second_window())
    session.observe()
    before = provider.enum_calls
    provider._trees.pop("w2")

    out = _write(session, editor)

    assert provider.enum_calls == before + 1
    assert "WINDOW" in out, out


def test_a_renamed_window_forces_a_full_refresh():
    provider, session, editor = _setup()
    before = provider.enum_calls
    provider._trees["w1"].window.title = "Untitled - renamed"

    out = _write(session, editor)

    assert provider.enum_calls == before + 1
    assert "WINDOW" in out and "→" in out, out


def test_post_action_enum_off_is_untouched():
    provider, session, editor = _setup()
    session.post_action_enum = False
    provider.set_tree(_second_window())
    before = provider.enum_calls

    out = _write(session, editor)

    assert provider.enum_calls == before, "enum is off; the signature must not matter"
    assert "WINDOW" not in out


def test_a_signature_failure_falls_back_to_full_enumeration():
    provider, session, editor = _setup()
    provider.fail_sig = True
    before = provider.enum_calls

    out = _write(session, editor)

    assert out.startswith("OK"), out
    assert provider.enum_calls == before + 1, "unknown signature must enumerate"
    # And the baseline becomes unknown, so the next call still enumerates.
    assert session._top_level_sig is None


def test_observe_always_does_the_full_refresh():
    provider, session, editor = _setup()
    before = provider.enum_calls
    session.observe()
    assert provider.enum_calls == before + 1


def test_a_provider_without_a_signature_still_works():
    provider = MockProvider(fixtures=[notepad_tree()])
    session = Session(provider)
    session.observe()
    editor = next(
        ref
        for ref, src in session.result.sources.items()
        if src.native.automation_id == "editor"
    )
    out = _write(session, editor)
    assert out.startswith("OK"), out


def test_no_stale_window_delta_after_a_skipped_call():
    """A skipped call must not advance ``_prev_windows`` to a stale set."""
    provider, session, editor = _setup()
    _write(session, editor)  # skipped
    provider.set_tree(_second_window())
    session.observe()  # full refresh: baseline becomes the two-window desktop
    before = dict(session._prev_windows)
    _write(session, editor)  # skipped again
    assert session._prev_windows == before
