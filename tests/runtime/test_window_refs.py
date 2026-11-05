"""The window list and the overview must agree on a window's ref.

A ref the agent was just handed has to keep meaning that window. The compiler
minted a fresh ``wN`` for the window node on every pass while the window list
kept its own allocation, so one window answered to two names - the list said
``w2``, the overview that followed said ``w12`` - and ``observe w2`` stopped
resolving to the window the agent had been told about. Found while testing
whether the child session can be opened from a single tool call: the switch
reply listed ``w1`` and the observation of ``w1`` came back as ``w3``.

These run on the mock provider, so they hold on any platform.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from somnus.providers.mock import MockProvider
from somnus.runtime.registry import SessionRegistry
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree
from tests.runtime.test_window_switching import _second_window


@pytest.fixture
def session():
    provider = MockProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
    )
    return Session(provider)


def _header_ref(text: str) -> str:
    """The first ``WINDOW wN`` line of an observation."""
    return next(
        line.split()[1] for line in text.splitlines() if line.startswith("WINDOW ")
    )


def test_observing_a_listed_ref_keeps_that_ref(session):
    """OBSERVE windows says w2; observe w2 must answer with w2."""
    session.observe(windows_only=True)
    session.refresh_windows()
    ref = session._window_refs_by_id[session.target_window]

    assert _header_ref(session.observe(ref).text) == ref


def test_a_ref_keeps_resolving_after_repeated_observations(session):
    """The name must not drift on the second and third observation either."""
    session.observe(windows_only=True)
    session.refresh_windows()
    ref = session._window_refs_by_id[session.target_window]

    seen = {_header_ref(session.observe(ref).text) for _ in range(3)}
    assert seen == {ref}, seen


def test_pinning_does_not_collide_with_a_new_window():
    """A pinned ref is reserved: the next window node cannot take it."""
    first = restaurant_demo_tree()
    second = _second_window(wid="w2", title="Notepad")
    session = Session(MockProvider(fixtures=[first, second]))
    session.observe(windows_only=True)
    session.refresh_windows()

    refs = dict(session._window_refs_by_id)
    assert len(refs) == 2, refs
    assert len(set(refs.values())) == 2, refs
    # Every ref maps back to exactly one window id.
    for ref, window_id in refs.items():
        assert session._window_refs[ref] == window_id

    # Observing either window keeps its own name.
    for ref in refs.values():
        assert _header_ref(session.observe(ref).text) == ref


def test_the_allocator_stays_ahead_of_a_pinned_ref():
    """Reusing w7 must not let the following allocation hand out w7 again."""
    registry = SessionRegistry()
    registry.allocator.observe_existing("w7")
    assert registry.allocator.next("w") == "w8"


class _DuplicateHandleProvider(MockProvider):
    """A UWP-shaped enumeration: one HWND reported twice."""

    def list_windows(self):
        base = super().list_windows()
        first = replace(base[0], hwnd=0x1234)
        return [first, replace(first, window_id="w99", title="Notepad")]


def test_one_native_window_gets_one_ref():
    session = Session(_DuplicateHandleProvider(fixtures=[restaurant_demo_tree()]))
    session.observe(windows_only=True)

    assert len(session.windows) == 1, session.windows
    lines = [ln for ln in session.observe(windows_only=True).text.splitlines() if "WINDOW w" in ln]
    assert len(lines) == 1, lines
    assert "w99" not in "\n".join(lines)


def test_every_listed_ref_can_be_inspected_and_focused():
    first = restaurant_demo_tree()
    second = _second_window(wid="w2", title="Notepad")
    session = Session(MockProvider(fixtures=[first, second]))

    text = session.observe(windows_only=True).text
    refs = [ln.split()[1] for ln in text.splitlines() if ln.startswith("  WINDOW ")]
    assert len(refs) == 2, text

    for ref in refs:
        inspected = session.inspect_direct(ref)
        assert inspected.startswith("@rev"), (ref, inspected)
        assert session.result is not None and session.result.find(ref) is not None
        out = session.dispatch(f"CALL {ref}.focus()")
        assert out.startswith("OK"), (ref, out)


def test_a_uwp_frame_and_content_are_labelled():
    first = restaurant_demo_tree()
    second = _second_window(wid="w2", title=first.window.title)
    second.window = replace(second.window, frame=True)
    session = Session(MockProvider(fixtures=[first, second]))

    text = session.observe(windows_only=True).text
    lines = [ln for ln in text.splitlines() if ln.startswith("  WINDOW ")]
    assert any("frame" in ln for ln in lines), lines
    assert any("content" in ln for ln in lines), lines

    refs = [ln.split()[1] for ln in lines]
    for ref in refs:
        session.inspect_direct(ref)


def test_a_lone_uwp_frame_is_not_labelled():
    tree = restaurant_demo_tree()
    tree.window = replace(tree.window, frame=True)
    session = Session(MockProvider(fixtures=[tree]))

    text = session.observe(windows_only=True).text
    line = next(ln for ln in text.splitlines() if ln.startswith("  WINDOW "))
    assert "frame" not in line and "content" not in line, line


def test_two_uwp_instances_are_not_a_frame_content_pair():
    first = restaurant_demo_tree()
    first.window = replace(first.window, frame=True)
    second = _second_window(wid="w2", title=first.window.title)
    second.window = replace(second.window, frame=True)
    session = Session(MockProvider(fixtures=[first, second]))

    text = session.observe(windows_only=True).text
    lines = [ln for ln in text.splitlines() if ln.startswith("  WINDOW ")]
    assert len(lines) == 2, lines
    assert all("frame" not in ln and "content" not in ln for ln in lines), lines
