"""Scenario S6: cross-cutting contracts, checked on every scenario screen.

The per-screen files check what each screen *does*. This file checks the things
that must hold on **all** of them, because they are the promises the project is
built on rather than features of one app:

* **information isolation** - no AutomationId, handle, runtime id, coordinate or
  process id ever reaches the LLM-facing text;
* **zero vision** - the runtime has no screenshot/coordinate capability, so the
  scan is structural, but it is asserted per screen anyway;
* **tool contract** - every observation parses as SUI-Text and every capability
  named on a line is really callable;
* **window switching** - a two-screen workflow works from the window list.

A leak that only appeared on the chat screen, or a capability that only the
form advertised but could not run, is exactly the kind of defect a per-screen
test misses.
"""

from __future__ import annotations

import re

import pytest

from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures import scenarios as S

#: The screens, and a session factory for each. Kept as data so a new scenario
#: is covered by every contract below the moment it is added here.
SCREENS = [
    ("store", lambda: S.store_tree(), S.scenario_scripts),
    ("form", lambda: S.form_tree(), S.scenario_scripts),
    ("chat", lambda: S.chat_tree(), S.chat_scripts),
    ("console", lambda: S.console_tree(), S.console_scripts),
    ("tree", lambda: S.tree_tree(), S.scenario_scripts),
]

#: Patterns that must never appear in LLM-facing text.
LEAK_PATTERNS = [
    re.compile(r"\bAutomationId\b", re.IGNORECASE),
    re.compile(r"\bRuntimeId\b", re.IGNORECASE),
    re.compile(r"0x[0-9a-fA-F]{4,}"),  # COM pointer / HWND
    re.compile(r"\bpid=\d+"),
    re.compile(r"class_name="),
    re.compile(r"\(\s*\d{2,5}\s*,\s*\d{2,5}\s*\)"),  # (x, y)
    re.compile(r"native_window_handle"),
    # The automation ids this suite uses are `store_*`, `form_*`, `chat_*`,
    # `console_*`, `tree_*`; none may appear in the text.
    re.compile(r"\b(store|form|chat|console|tree)_[a-z0-9_]+\b"),
]


def _session(builder, scripts_factory) -> Session:
    return Session(
        MockProvider(
            fixtures=[builder()],
            scripts=scripts_factory(),
            callbacks=S.callbacks(),
        )
    )


@pytest.mark.parametrize("name,builder,scripts", SCREENS, ids=[s[0] for s in SCREENS])
def test_observe_leaks_nothing(name, builder, scripts):
    session = _session(builder, scripts)
    text = session.observe().text
    for pattern in LEAK_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"{name}: leaked {match.group(0)!r} in:\n{text}"


@pytest.mark.parametrize("name,builder,scripts", SCREENS, ids=[s[0] for s in SCREENS])
def test_observe_is_parseable_sui_text(name, builder, scripts):
    """Every line must be a node line, a header or a marker."""
    session = _session(builder, scripts)
    text = session.observe().text
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        assert first.startswith("@rev") or first.isupper() or stripped.startswith("…"), (
            f"{name}: unparseable line {line!r}"
        )


@pytest.mark.parametrize("name,builder,scripts", SCREENS, ids=[s[0] for s in SCREENS])
def test_every_advertised_primitive_capability_resolves(name, builder, scripts):
    """A capability printed on a line must have a `via` chain behind it.

    The failure this catches is a capability advertised with nothing to run:
    the agent calls it and gets ACTION_NOT_SUPPORTED for a gesture the
    observation promised.
    """
    session = _session(builder, scripts)
    session.observe()
    for node in session.result.root.walk():
        for cap in node.caps:
            assert getattr(cap, "via", None), (
                f"{name}: {node.ref}.{cap.name} is advertised with no via chain"
            )


@pytest.mark.parametrize("name,builder,scripts", SCREENS, ids=[s[0] for s in SCREENS])
def test_inspect_matches_the_observation(name, builder, scripts):
    """INSPECT of a node found in the observation must render that node."""
    session = _session(builder, scripts)
    obs = session.observe().text
    ref = None
    for line in obs.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isupper() and re.fullmatch(r"[a-z]+\d+", parts[1]):
            ref = parts[1]
            break
    assert ref, f"{name}: no addressable node in the overview"
    detail = session.inspect_direct(ref)
    assert ref in detail


@pytest.mark.parametrize("name,builder,scripts", SCREENS, ids=[s[0] for s in SCREENS])
def test_a_wrong_capability_is_refused_with_a_hint(name, builder, scripts):
    """Calling a capability the node does not have fails loudly, never silently."""
    session = _session(builder, scripts)
    obs = session.observe().text
    ref = None
    for line in obs.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "BUTTON":
            ref = parts[1]
            break
    assert ref, f"{name}: no button in the overview"
    out = session.dispatch(f"CALL {ref}.definitely_not_a_capability()")
    assert out.startswith("ERR"), out
    assert "hint=" in out


# ---------------------------------------------------------------------------
# a two-screen workflow: the window list is the entry point
# ---------------------------------------------------------------------------


def test_window_switching_between_two_scenarios():
    """Two screens in one session: list, switch, act, switch back.

    This is the cross-window workflow the docs describe, and it is the reason
    window refs and node refs must not collide.
    """
    store = S.store_tree(window_id="w1")
    chat = S.chat_tree(window_id="w2")
    session = Session(
        MockProvider(
            fixtures=[store, chat],
            scripts=S.chat_scripts(),
            callbacks=S.callbacks(),
        )
    )
    listed = session.dispatch("OBSERVE windows")
    assert "WINDOWS count=2" in listed
    assert "store" in listed and "chat" in listed

    # Switch to the chat window and act there.
    chat_ref = _window_ref(listed, "chat")
    assert session.dispatch(f"CALL {chat_ref}.focus()").startswith("OK")
    session.observe()
    composer = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "编辑器" in (n.name or "")
    )
    assert "set_text" in composer.cap_names()

    # Switch back: the store is still there and still operable.
    listed2 = session.dispatch("OBSERVE windows")
    store_ref = _window_ref(listed2, "store")
    assert session.dispatch(f"CALL {store_ref}.focus()").startswith("OK")
    session.observe()
    assert any(
        "商品" in (n.name or "") for n in session.result.root.walk()
    )


def _window_ref(listing: str, needle: str) -> str:
    for line in listing.splitlines():
        if "WINDOW" in line and needle in line:
            parts = line.split()
            return parts[1]
    raise AssertionError(f"no window matching {needle!r} in:\n{listing}")
