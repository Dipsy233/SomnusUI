"""Robustness regressions found driving real Windows apps (Steam/QQ/Chrome).

Each test encodes a fault observed live, so the generic (no-pack) path keeps
working on applications the packs were never written for.
"""

from __future__ import annotations

import re

from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


def _new_session(*trees):
    return Session(MockProvider(fixtures=list(trees) or [restaurant_demo_tree()]))


# ------------------------------------------------- default observe is L0


def test_default_observe_uses_l0_budget():
    """observe() with no level must return the tight L0 overview, not the
    unconstrained session budget (1500 tokens) - live Steam came out at 1109
    tokens because the default level fell through to `self.budget`."""
    session = _new_session(restaurant_demo_tree(), notepad_tree())
    obs = session.observe()
    assert obs.tokens <= ObservationBudget().for_level(0).hard_tokens


def test_l0_budget_accounts_for_window_header():
    """When the desktop header *is* requested, its tokens count against the
    level cap, or the total overshoots (live: Steam L0 = 365 > 300)."""
    # 8 helper windows make a fat header; the demo tree body is small enough
    # that an unshrunk budget would leave headroom the cap does not allow.
    trees = [restaurant_demo_tree()]
    for i in range(8):
        trees.append(notepad_tree())
        trees[-1].window.window_id = f"wx{i}"
        trees[-1].window.title = f"Helper Application Number {i} With A Long Title"
    session = _new_session(*trees)
    obs = session.observe(include_other_windows=True)
    assert obs.tokens <= 300


def test_observe_one_window_does_not_list_the_others():
    """Asking for one window's content must return that window, not a desktop
    dump the caller did not ask for and must pay tokens for (live: ten
    unrelated windows prefixed every observation, roughly doubling its cost)."""
    trees = [restaurant_demo_tree()]
    for i in range(8):
        trees.append(notepad_tree())
        trees[-1].window.window_id = f"wx{i}"
        trees[-1].window.title = f"Helper Application Number {i} With A Long Title"
    session = _new_session(*trees)
    scoped = session.observe()
    full = session.observe(include_other_windows=True)
    assert "Helper Application Number" not in scoped.text
    assert "Helper Application Number" in full.text
    assert scoped.tokens < full.tokens


def test_observe_grammar_selects_scope():
    """``OBSERVE w1`` is one window; ``OBSERVE w1 all`` also lists the rest;
    ``OBSERVE windows`` is the list alone."""
    from somnus.sui.parse import parse_action

    plain = parse_action("OBSERVE w1")
    assert plain.kind == "observe" and plain.window == "w1"
    assert plain.scope == "window"

    with_all = parse_action("OBSERVE w1 all")
    assert with_all.scope == "desktop" and with_all.window == "w1"

    bare_all = parse_action("OBSERVE all")
    assert bare_all.scope == "desktop"

    listing = parse_action("OBSERVE windows")
    assert listing.query == "windows"


# ------------------------------------------------------- FIND role filter


def test_find_by_primitive_role_reaches_entity_typed_nodes():
    """``FIND button "Send"`` must find a node a pack typed as an entity
    (qq pack types the send button as entity=send). The agent cannot be
    expected to know every pack's entity vocabulary."""
    session = _new_session()
    session.observe()
    # Restaurant pack types Add buttons? No - use a generic query against the
    # checkout button (entity=checkout in the restaurant pack).
    out = session.dispatch('FIND button "Checkout"')
    assert out.startswith("FOUND 1"), out
    out2 = session.dispatch('FIND checkout "Checkout"')
    assert "FOUND 1" in out2


# ------------------------------------------------- windows_only deltas


def test_windows_only_reports_appeared_window_and_bumps_rev():
    """OBSERVE windows used to refresh the baseline silently: a newly opened
    window changed the list but produced no delta line and no rev bump, so an
    agent polling OBSERVE windows never learned a dialog had appeared."""
    session = _new_session()
    first = session.dispatch("OBSERVE windows")
    assert "Notepad" not in first
    extra = notepad_tree()
    extra.window.window_id = "w9"
    session.provider.set_tree(extra)
    second = session.dispatch("OBSERVE windows")
    assert re.search(r'\+ WINDOW w\d+ "Untitled - Notepad"', second), second
    assert second.splitlines()[0] != first.splitlines()[0]  # rev bumped


def test_window_ids_stay_stable_when_window_set_changes():
    """Provider window ids keyed by list position renumber everything after a
    close, silently retargeting held refs (live: w3 flipped between Steam and
    QQ). The mock provider keys trees by window_id so this is structural;
    assert the session keeps ref->window bindings across a refresh where the
    provider is the same."""
    session = _new_session(restaurant_demo_tree(), notepad_tree())
    session.dispatch("OBSERVE windows")
    before = dict(session._window_refs_by_id)
    session.refresh_windows()
    assert session._window_refs_by_id == before
