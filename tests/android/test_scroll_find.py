"""FIND scroll: walking a virtualised list (SomnusDroid doc §7.3)."""

from __future__ import annotations

from somnus.providers.android.mock import AndroidMockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from somnus.sui.parse import parse_action
from tests.fixtures import android_scenarios as A


def make_session(rows: int = 500) -> Session:
    return Session(
        AndroidMockProvider(snapshots=[A.store_tree(rows=rows)]), policy=Policy()
    )


def test_scroll_find_walks_the_list_and_finds():
    """'Special Item 17' sits at offset 17; the realised window holds 20 rows
    so it is found at once or within a couple of scrolls."""
    session = make_session()
    session.observe()
    out = session.dispatch('FIND "Special Item 17" in l1 scroll')
    assert out.startswith("FOUND"), out
    assert "Special Item 17" in out


def test_scroll_find_reports_exhaustion_without_inventing():
    """A query that matches nothing must come back FOUND 0, never a guess."""
    session = make_session()
    session.observe()
    out = session.dispatch('FIND "Item 99999" in l1 scroll')
    assert out.startswith("FOUND 0"), out
    assert "exhausted" in out


def test_plain_find_still_works_without_scroll():
    session = make_session()
    session.observe()
    out = session.dispatch('FIND "Item 0"')
    assert out.startswith("FOUND"), out


def test_scroll_find_without_scope_is_plain_find():
    session = make_session()
    session.observe()
    out = session.dispatch('FIND "Special Item 17" scroll')
    # No scope -> no scrollable -> plain semantics, still honest.
    assert out.startswith("FOUND"), out


def test_parser_peels_the_scroll_suffix():
    action = parse_action('FIND "x" in l1 scroll')
    assert action.kind == "find"
    assert action.scope == "l1"
    assert action.query == "x"
    assert action.scope_scroll is True

    plain = parse_action('FIND "x" in l1')
    assert plain.scope_scroll is False


def test_scrolling_reaches_the_bottom_without_hanging():
    """The loop must terminate at the list end, not spin for its budget."""
    import time as _time

    session = make_session(rows=25)  # 25 rows, 20 realised: 6 scrolls to bottom
    session.observe()
    start = _time.monotonic()
    out = session.dispatch('FIND "Item 24" in l1 scroll')
    elapsed = _time.monotonic() - start
    assert out.startswith("FOUND") or "exhausted" in out
    assert elapsed < 10.0
