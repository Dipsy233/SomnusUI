"""Settle frame counts, the signature probe, and the pacing settings.

The settler is the runtime's fixed tax: every CALL paid for one or more *full*
window snapshots just to decide that the UI had stopped moving. This file pins
the numbers the optimisation plan quotes, so a change that quietly makes a call
more expensive cannot pass as "still works":

* baseline frame counts (small window 3 frames, large window 2) at the old
  150/50 pacing - the formula is ``F = 1 + ceil(quiet / (poll + snapshot))``;
* with a signature probe: no change = **zero full snapshots, one probe**;
  a change = **one full snapshot** (+ the probe that found it);
* the probe must see a state-only flip (disabled -> enabled), because that flip
  is exactly what settle exists for (QQ's 发送 button, the form's gated submit);
* defaults and ``Session.apply_settings`` (all-or-nothing, reset, persist hook).

The frame tests use a fake clock and a fake ``time.sleep``: the *number* of
frames is what matters, and actually waiting 2.9s per assertion would make this
file too slow to run on every change.
"""

from __future__ import annotations

import copy

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.node import RawTree
from somnus.providers.mock import MockProvider
from somnus.providers.windows import cache as cache_module
from somnus.runtime import settle as settle_module
from somnus.runtime.session import Session
from somnus.runtime.settle import SIGNATURE_FIELDS, Settler, _sig
from tests.fixtures.builders import restaurant_demo_tree

# ---------------------------------------------------------------------------
# fake time + fake provider
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the test advances explicitly (no real sleeping)."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000


class FrameProvider:
    """A non-deterministic provider whose one snapshot costs ``frame_ms``."""

    name = "fake"
    is_deterministic = False

    def __init__(self, tree: RawTree, clock: FakeClock, frame_ms: float) -> None:
        self.tree = tree
        self.clock = clock
        self.frame_ms = frame_ms
        self.snapshots = 0
        self.probes = 0

    def list_windows(self):
        return [self.tree.window]

    def peek(self, window_id):
        return self.tree.window if window_id == self.tree.window.window_id else None

    def snapshot(self, window_id, scope=None, max_depth=20):
        self.snapshots += 1
        self.clock.advance_ms(self.frame_ms)
        return copy.deepcopy(self.tree)

    def snapshot_signature(self, window_id):
        self.probes += 1
        self.clock.advance_ms(self.frame_ms / 10)
        return copy.deepcopy(self.tree)

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        from somnus.ir.action import ExecResult

        return ExecResult(ok=True)

    def close(self):
        return None


@pytest.fixture
def instant(monkeypatch):
    """Sleeping advances the fake clock instead of real time.

    The settler measures quiet in ``clock()`` units and sleeps between rounds;
    with a frozen fake clock a real no-op sleep would leave the quiet window
    unreachable. Returning a factory keeps each test's own clock in charge.
    """

    def install(clock: FakeClock) -> FakeClock:
        def fake_sleep(seconds: float) -> None:
            clock.advance_ms(seconds * 1000)

        monkeypatch.setattr(settle_module.time, "sleep", fake_sleep)
        return clock

    return install


# ---------------------------------------------------------------------------
# baseline: the numbers the plan quotes (old 150/50 pacing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frame_ms", "expected_frames"),
    [(90, 3), (1450, 2)],
    ids=["small-window-3-frames", "large-tree-2-frames"],
)
def test_baseline_frame_count_matches_the_cost_model(instant, frame_ms, expected_frames):
    """Without a probe, the old pacing really costs this many full snapshots."""
    clock = instant(FakeClock())
    provider = FrameProvider(restaurant_demo_tree(), clock, frame_ms)
    settler = Settler(quiet_ms=150, poll_interval_ms=50, timeout_s=30.0, clock=clock)
    wid = provider.tree.window.window_id
    tree, result = settler.wait(lambda: provider.snapshot(wid), previous=None)

    assert result.settled is True
    assert result.snapshots == expected_frames
    assert result.probes == 0
    assert provider.snapshots == expected_frames
    assert _sig(tree.root) == _sig(provider.tree.root)


def test_defaults_are_the_documented_values():
    """Settle pacing defaults: 40ms quiet, 10ms poll, 3s timeout."""
    settler = Settler()
    assert settler.quiet_ms == 40
    assert settler.poll_interval_ms == 10
    assert settler.timeout_s == 3.0


# ---------------------------------------------------------------------------
# signature probe: zero full snapshots when nothing changed
# ---------------------------------------------------------------------------


def test_unchanged_ui_costs_one_probe_and_no_snapshot(instant):
    """The headline of P1-1: an already-settled call never re-reads the tree."""
    clock = instant(FakeClock())
    provider = FrameProvider(restaurant_demo_tree(), clock, frame_ms=90)
    previous = copy.deepcopy(provider.tree)
    settler = Settler(quiet_ms=40, poll_interval_ms=10, timeout_s=3.0, clock=clock)
    wid = provider.tree.window.window_id

    tree, result = settler.wait(
        lambda: provider.snapshot(wid),
        previous=previous,
        probe=lambda: provider.snapshot_signature(wid),
    )

    assert result.settled is True
    assert result.snapshots == 0
    assert result.probes == 1
    assert provider.snapshots == 0
    assert tree is previous


def test_a_real_change_costs_exactly_one_full_snapshot(instant):
    """A change: the probe that finds it, then one full read, then confirm."""
    clock = instant(FakeClock())
    provider = FrameProvider(restaurant_demo_tree(), clock, frame_ms=90)
    previous = copy.deepcopy(provider.tree)
    # Mutate the provider's tree *after* taking the baseline, as an action does.
    provider.tree.root.children[0].name = "Products (updated)"
    settler = Settler(quiet_ms=40, poll_interval_ms=10, timeout_s=3.0, clock=clock)
    wid = provider.tree.window.window_id

    tree, result = settler.wait(
        lambda: provider.snapshot(wid),
        previous=previous,
        probe=lambda: provider.snapshot_signature(wid),
    )

    assert result.settled is True
    assert result.snapshots == 1
    assert provider.snapshots == 1
    # One probe finds the change, one confirms the quiet window afterwards.
    assert result.probes == 2
    assert _sig(tree.root) == _sig(provider.tree.root)


def test_stored_tree_is_compared_by_its_pre_compile_signature(instant):
    """Compilation normalises the tree in place; the probe must still match.

    ``normalize`` rewrites ``edit`` -> ``input`` on the stored frame, so the
    stored tree's own signature differs from a fresh read of the *same* UI. A
    settler that compared the probe against the tree would take a full
    snapshot on every call (this was a real defect while wiring P1-1).
    """
    from somnus.compiler.normalize import normalize
    from somnus.runtime.settle import signature_of
    from tests.fixtures.scenarios import form_tree

    clock = instant(FakeClock())
    provider = FrameProvider(form_tree(), clock, frame_ms=90)
    wid = provider.tree.window.window_id
    stored = copy.deepcopy(provider.tree)
    as_read = signature_of(stored)
    normalize(stored.root)  # exactly what compile does to the session's frame
    assert signature_of(stored) != as_read, "fixture must contain a role the compiler rewrites"
    settler = Settler(quiet_ms=40, poll_interval_ms=10, timeout_s=3.0, clock=clock)

    tree, result = settler.wait(
        lambda: provider.snapshot(wid),
        previous=stored,
        previous_signature=as_read,
        probe=lambda: provider.snapshot_signature(wid),
    )

    assert result.settled is True
    assert result.snapshots == 0, "a normalised frame must not force a snapshot"
    assert tree is stored


def test_the_probe_sees_a_state_only_flip():
    """disabled -> enabled changes no role/name/value/count; it must still count.

    This is the failure mode the state field exists for: after ``set_text`` a
    self-drawn send button becomes clickable without changing any structural
    field. A signature that skipped states would settle early and the next
    compilation would carry a stale ``disabled``.
    """
    clock = FakeClock()
    provider = FrameProvider(restaurant_demo_tree(), clock, frame_ms=90)
    previous = copy.deepcopy(provider.tree)

    button = next(n for n in provider.tree.root.walk() if n.role == "button")
    button.states = frozenset(set(button.states) | {"disabled"})

    assert _sig(previous.root) != _sig(provider.tree.root)


def test_signature_fields_document_the_shared_contract():
    """The probe's field list is the one settle compares (guards drift)."""
    assert SIGNATURE_FIELDS == ("role", "name", "value", "child_count", "states")


# ---------------------------------------------------------------------------
# cache tiers (P0-1) - the probe tier must cover every state it compares
# ---------------------------------------------------------------------------


def test_signature_tier_covers_every_state_property():
    """Every state in the signature is backed by a property the sig tier asks for."""
    missing = {
        state: pid
        for state, pid in cache_module.SIGNATURE_STATE_PROPERTIES.items()
        if pid not in cache_module.SIGNATURE_PROPERTIES
    }
    assert not missing, missing


def test_tiers_are_the_documented_sets():
    full_props = set(cache_module.properties_for("full"))
    full_patterns = set(cache_module.patterns_for("full"))
    lean_patterns = set(cache_module.patterns_for("lean"))
    sig_patterns = set(cache_module.patterns_for("sig"))
    peek_props = set(cache_module.properties_for("peek"))

    # 14 consumed fields + BoundingRectangle (30001), added for the viewport
    # capture's visibility decision (docs/archive/performance-2026-11.md).
    assert len(full_props) == 15 and 30001 in full_props
    assert len(full_patterns) == 16
    assert full_patterns - lean_patterns == cache_module.LEAN_DROPPED_PATTERNS
    assert len(sig_patterns) == 1, "the probe asks for the value pattern only"
    assert set(cache_module.properties_for("sig")) == set(
        cache_module.SIGNATURE_PROPERTIES
    )
    # The row peek: name/type/enabled/rect, no patterns.
    assert peek_props == {30005, 30003, 30010, 30001}
    assert cache_module.patterns_for("peek") == {}


def test_cache_tier_env_is_parsed_and_validated(monkeypatch):
    monkeypatch.delenv(cache_module.CACHE_TIER_ENV, raising=False)
    assert cache_module.cache_tier_from_env() == "full"
    monkeypatch.setenv(cache_module.CACHE_TIER_ENV, "lean")
    assert cache_module.cache_tier_from_env() == "lean"
    monkeypatch.setenv(cache_module.CACHE_TIER_ENV, "nonsense")
    assert cache_module.cache_tier_from_env() == "full"


# ---------------------------------------------------------------------------
# session wiring: the probe is used inside a real call
# ---------------------------------------------------------------------------


class ProbedMock(MockProvider):
    """The scenario mock, made non-deterministic and given a signature probe.

    Snapshots are copied: the real provider hands back a fresh tree per read,
    and the mock's in-place mutation would otherwise make "previous" and "now"
    the same object (which would hide the change the test is looking for).
    """

    is_deterministic = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.snapshot_calls = 0
        self.probe_calls = 0
        self.enum_calls = 0

    def list_windows(self):
        self.enum_calls += 1
        return super().list_windows()

    def snapshot(self, window_id, scope=None, max_depth=12):
        self.snapshot_calls += 1
        return copy.deepcopy(super().snapshot(window_id, scope, max_depth))

    def snapshot_signature(self, window_id):
        self.probe_calls += 1
        return copy.deepcopy(super().snapshot(window_id))


def _form_session(**settings):
    from tests.fixtures import scenarios as S

    provider = ProbedMock(
        fixtures=[S.form_tree()],
        scripts=S.scenario_scripts(),
        callbacks=S.callbacks(),
    )
    settler = Settler(quiet_ms=15, poll_interval_ms=5, skip_when_deterministic=False)
    session = Session(provider, settler=settler)
    session.observe()
    anchors = S.resolve_anchors(
        session, {"name": "form_name", "agree": "form_agree", "submit": "form_submit"}
    )
    return provider, session, anchors


def test_call_uses_the_probe_instead_of_a_second_full_snapshot():
    """An action that changes the tree: one full snapshot in settle, not two."""
    provider, session, anchors = _form_session()
    before = provider.snapshot_calls

    out = session.dispatch(f'CALL {anchors["name"]}.set_text("张三")')

    assert out.startswith("OK"), out
    assert "settled=false" not in out
    assert provider.probe_calls >= 1
    # Exactly one full snapshot for the settle; the old path took two.
    assert provider.snapshot_calls - before == 1, out


def test_no_op_call_takes_no_full_snapshot():
    """Writing the same text again changes nothing: the settle reuses the tree."""
    provider, session, anchors = _form_session()
    session.dispatch(f'CALL {anchors["name"]}.set_text("张三")')
    before = provider.snapshot_calls

    out = session.dispatch(f'CALL {anchors["name"]}.set_text("张三")')

    assert out.startswith("OK"), out
    assert provider.snapshot_calls == before, out


def test_state_flip_is_seen_by_the_probe_and_settles_true():
    """The gating checkbox flips state only; the probe must catch it."""
    provider, session, anchors = _form_session()
    before = provider.snapshot_calls

    out = session.dispatch(f'CALL {anchors["agree"]}.toggle()')

    assert out.startswith("OK"), out
    assert "settled=false" not in out
    assert provider.snapshot_calls - before >= 1


# ---------------------------------------------------------------------------
# post-action window enumeration (P1-3)
# ---------------------------------------------------------------------------


def test_post_action_enum_is_on_by_default():
    _, session, _ = _form_session()
    assert session.post_action_enum is True


def test_post_action_enum_can_be_switched_off():
    """Off: a call re-enumerates no windows at all (the declared saving)."""
    provider, session, anchors = _form_session()
    assert session.apply_settings(post_action_enum=False)["post_action_enum"] is False
    before = provider.enum_calls

    out = session.dispatch(f'CALL {anchors["name"]}.set_text("张三")')

    assert out.startswith("OK"), out
    assert provider.enum_calls == before, "post_action_enum=false must not enumerate"


# ---------------------------------------------------------------------------
# apply_settings (P1-2 session side)
# ---------------------------------------------------------------------------


def test_settings_are_echoed_with_scope():
    _, session, _ = _form_session()
    current = session.apply_settings()
    assert current == {
        "settle_quiet_ms": 15,
        "settle_poll_ms": 5,
        "settle_timeout_s": 3.0,
        "post_action_enum": True,
        "scope": "session",
    }


def test_a_change_applies_to_the_next_call_only_to_given_keys():
    _, session, _ = _form_session()
    session.apply_settings(quiet_ms=60)

    current = session.apply_settings()
    assert current["settle_quiet_ms"] == 60
    assert current["settle_poll_ms"] == 5
    assert session.settler.quiet_ms == 60


@pytest.mark.parametrize(
    ("kwargs", "mentioned"),
    [
        ({"quiet_ms": 5}, "key=settle_quiet_ms expected int in 10..1000"),
        ({"quiet_ms": 5000}, "key=settle_quiet_ms expected int in 10..1000"),
        ({"poll_ms": 0}, "key=settle_poll_ms expected int in 1..100"),
        ({"timeout_s": 0.1}, "key=settle_timeout_s expected number in 0.5..30.0"),
        ({"quiet_ms": "40"}, "key=settle_quiet_ms expected int in 10..1000"),
        ({"quiet_ms": True}, "key=settle_quiet_ms expected int in 10..1000"),
        ({"post_action_enum": 1}, "key=post_action_enum expected bool"),
    ],
)
def test_an_invalid_value_changes_nothing(kwargs, mentioned):
    """All-or-nothing: the error names the key/range, and no value moved."""
    _, session, _ = _form_session()
    before = session.apply_settings()

    with pytest.raises(SomnusError) as err:
        session.apply_settings(**kwargs)

    assert err.value.code == ErrorCode.INVALID_ARGUMENT
    assert mentioned in err.value.message
    assert "nothing changed" in err.value.message
    assert session.apply_settings() == before


def test_reset_returns_to_the_session_start_values():
    _, session, _ = _form_session()
    session.apply_settings(quiet_ms=100, poll_ms=50, post_action_enum=False)

    current = session.apply_settings(reset=True)

    assert current["settle_quiet_ms"] == 15
    assert current["settle_poll_ms"] == 5
    assert current["post_action_enum"] is True
    assert session.post_action_enum is True


def test_reset_and_explicit_keys_apply_together():
    _, session, _ = _form_session()
    session.apply_settings(quiet_ms=100, post_action_enum=False)

    current = session.apply_settings(reset=True, quiet_ms=80)

    assert current["settle_quiet_ms"] == 80
    assert current["post_action_enum"] is True


def test_persist_uses_the_config_layer_hook_when_present(monkeypatch):
    from somnus import config as config_module

    seen: dict = {}

    def fake_persist(**values):
        seen.update(values)
        return "C:/tmp/config.toml"

    monkeypatch.setattr(config_module, "persist_session_settings", fake_persist, raising=False)
    _, session, _ = _form_session()

    current = session.apply_settings(quiet_ms=30, persist=True)

    assert seen["settle_quiet_ms"] == 30
    assert current["persisted"] == "C:/tmp/config.toml"
    assert session.settler.quiet_ms == 30


def test_persist_without_the_config_hook_changes_nothing(monkeypatch):
    """The config work owns persistence; if that entry disappears, nothing is half-applied."""
    from somnus import config as config_module

    monkeypatch.delattr(config_module, "persist_session_settings", raising=False)
    _, session, _ = _form_session()
    before = session.apply_settings()

    with pytest.raises(NotImplementedError) as err:
        session.apply_settings(quiet_ms=30, persist=True)

    assert "persist_session_settings" in str(err.value)
    assert session.apply_settings() == before
