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
