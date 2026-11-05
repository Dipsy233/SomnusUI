"""FIND's waiting contract and the desktop summary handed to the projection.

The waiting half is measured in fake time: the point is *what* the wait does
(search, re-read the viewport, search again) and what it costs in reads, not
how long a wall clock takes. The desktop half checks the structure the session
passes to ``ProjectionEngine.plan`` - the projection renders it, the session
never assembles text.
"""

from __future__ import annotations

import copy
import re

from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime import session as session_module
from somnus.runtime.session import Session
from tests.fixtures.builders import _n


def _tree(title: str = "Form", window_id: str = "w1", extra: tuple[str, ...] = ()):
    children = [
        _n(window_id, (0,), "button", "提交", patterns=frozenset({"invoke"}))
    ]
    children += [
        _n(window_id, (i + 1,), "button", name, patterns=frozenset({"invoke"}))
        for i, name in enumerate(extra)
    ]
    return RawTree(
        window=WindowInfo(
            window_id=window_id,
            title=title,
            process="demo.exe",
            pid=1,
            active=True,
        ),
        root=_n(window_id, (), "pane", "form", children=children),
        raw_revision=1,
    )


class _FakeTime:
    """A clock the test drives; ``sleep`` advances it (no wall-clock waiting)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedViewport(MockProvider):
    """A replay provider whose viewport read serves a scripted sequence.

    Each call serves the next tree; the last one repeats, so a waiting search
    keeps seeing the final state.
    """

    def __init__(self, trees: list[RawTree]) -> None:
        super().__init__(fixtures=[trees[0]])
        self._sequence = list(trees)
        self.reads = 0

    def _next(self):
        tree = self._sequence[min(self.reads, len(self._sequence) - 1)]
        self.reads += 1
        return copy.deepcopy(tree)

    def snapshot_viewport(self, window_id, expanded_paths=None):
        return self._next()

    def snapshot(self, window_id, scope=None, max_depth=12):
        # The small-window adaptation reads the window in full instead of
        # walking it; both entry points must serve the same scripted UI.
        return self._next()


# ---------------------------------------------------------------------------
# FIND without a timeout: exactly today's one in-memory search
# ---------------------------------------------------------------------------


def test_find_without_a_timeout_does_not_read_the_window_again():
    provider = ScriptedViewport([_tree(extra=("Target",))])
    session = Session(provider)
    session.observe()
    reads = provider.reads

    assert session.find("Target").startswith("FOUND 1")
    assert provider.reads == reads, "a hit must not re-read"
    # A miss on a viewport-limited result deliberately takes one full read
    # before reporting "not found" (the rows off screen were not captured).
    assert session.find("Nothing").startswith("FOUND 0")


def test_a_zero_timeout_is_the_immediate_path_too():
    provider = ScriptedViewport([_tree(extra=("Target",))])
    session = Session(provider)
    session.observe()
    reads = provider.reads
    assert session.find("Target", timeout_s=0).startswith("FOUND 1")
    assert provider.reads == reads


# ---------------------------------------------------------------------------
# FIND while waiting
# ---------------------------------------------------------------------------


def test_a_target_that_appears_later_is_found_while_waiting(monkeypatch):
    clock = _FakeTime()
    monkeypatch.setattr(session_module, "time", clock)
    trees = [
        _tree(),                       # the observe
        _tree(),                       # first poll: not there yet
        _tree(extra=("Target",)),      # second poll: it is there
    ]
    provider = ScriptedViewport(trees)
    session = Session(provider)
    session.observe()

    out = session.find("Target", timeout_s=1.0)
    assert out.startswith("FOUND 1"), out
    # observe + one miss + one hit; the wait stops on the hit, not on timeout.
    assert provider.reads == 3
    assert clock.now < 1.0


def test_a_target_that_never_appears_reports_the_deadline(monkeypatch):
    clock = _FakeTime()
    monkeypatch.setattr(session_module, "time", clock)
    provider = ScriptedViewport([_tree()])
    session = Session(provider)
    session.observe()

    out = session.find("Never", timeout_s=0.5)
    assert out.startswith("ERR TARGET_NOT_FOUND after=0.5s"), out
    assert "hint=observe" in out
    assert "viewport reads" in out
    # The wait stops at the deadline; it does not keep polling afterwards.
    assert 0.45 <= clock.now <= 0.55
    # Every poll is one viewport read (~one per poll floor in fake time).
    assert provider.reads >= 20


def test_the_wait_does_not_pay_a_full_read_for_an_uncaptured_miss(monkeypatch):
    """Waiting must stay inside its deadline: a truncated collection is not
    re-read in full, and the reply says which rows were not searched."""
    clock = _FakeTime()
    monkeypatch.setattr(session_module, "time", clock)

    class Truncated(ScriptedViewport):
        def snapshot_viewport(self, window_id, expanded_paths=None):
            self.viewport_meta = {
                "w1": {(0,): {"items_total": 9, "items_visible": 1, "items_truncated": True}}
            }
            return super().snapshot_viewport(window_id, expanded_paths)

    provider = Truncated([_tree()])
    session = Session(provider)
    session.observe()
    assert session._viewport_limited is True

    out = session.find("Never", timeout_s=0.5)
    assert out.startswith("ERR TARGET_NOT_FOUND"), out
    assert "rows outside the captured viewport were not searched" in out


def test_the_string_contract_accepts_a_window_argument():
    provider = MockProvider(
        fixtures=[
            _tree(title="First", window_id="w1", extra=("Target",)),
            _tree(title="Second", window_id="w2"),
        ]
    )
    session = Session(provider)
    session.observe()

    # `find(pattern, window=None, timeout_s=None)` - `window` is the same
    # scope as `FIND "..." in wN`.
    out = session.find("Target", window="w1")
    assert out.startswith("FOUND 1"), out


# ---------------------------------------------------------------------------
# desktop summary: the structure the projection renders
# ---------------------------------------------------------------------------


def _capture_plan(monkeypatch):
    original = session_module.ProjectionEngine.plan
    seen: list[object] = []

    def spy(
        self,
        root,
        projection=None,
        *,
        focus=None,
        depth_limit=None,
        full=False,
        expanded=None,
        desktop_summary=None,
    ):
        seen.append(desktop_summary)
        return original(
            self,
            root,
            projection,
            focus=focus,
            depth_limit=depth_limit,
            full=full,
            expanded=expanded,
            desktop_summary=desktop_summary,
        )

    monkeypatch.setattr(session_module.ProjectionEngine, "plan", spy)
    return seen


def test_observe_passes_the_desktop_summary_as_data(monkeypatch):
    seen = _capture_plan(monkeypatch)
    provider = MockProvider(
        fixtures=[
            _tree(title="商店", window_id="w1"),
            _tree(title="设置", window_id="w2"),
        ]
    )
    session = Session(provider)
    session.observe()

    summary = seen[-1]
    assert isinstance(summary, list)
    assert [entry["title"] for entry in summary] == ["商店", "设置"]
    # Only the three promised fields; `ops` needs a per-window shallow read.
    assert all(set(entry) == {"window_id", "title", "active"} for entry in summary)
    assert all(entry["window_id"].startswith("w") for entry in summary)
    assert [entry["active"] for entry in summary] == [True, True]


def test_the_summary_names_the_same_refs_the_header_uses(monkeypatch):
    """The line the projection renders must be `CALL <ref>.focus()`-able."""
    seen = _capture_plan(monkeypatch)
    provider = MockProvider(
        fixtures=[
            _tree(title="商店", window_id="w1"),
            _tree(title="设置", window_id="w2"),
        ]
    )
    session = Session(provider)
    session.observe()

    summary = seen[-1]
    for entry in summary:
        assert session._window_by_ref(entry["window_id"]) is not None
        assert session.dispatch(f"CALL {entry['window_id']}.focus()").startswith("OK")


_SUMMARY_REF_RE = re.compile(r'(w\d+) "([^"]*)"')


def _summary_refs(text: str) -> dict[str, str]:
    """title -> ref from the observation's `WINDOWS ...` line."""
    for line in text.splitlines():
        if line.startswith("WINDOWS "):
            return {title: ref for ref, title in _SUMMARY_REF_RE.findall(line)}
    return {}


def test_the_summary_and_the_body_name_a_window_the_same_way():
    """Regression: the desktop line said w2 while the tree under it said w1.

    One window (one native id) carried two refs because the summary read the
    reverse map while the body used the compiled root's ref - and only the
    forward map had been written for the target.
    """
    provider = MockProvider(
        fixtures=[
            _tree(title="商店", window_id="w1"),
            _tree(title="设置", window_id="w2"),
        ]
    )
    session = Session(provider)
    obs = session.observe().text
    refs = _summary_refs(obs)
    assert set(refs) == {"商店", "设置"}, obs
    target_title = session._window_by_ref(session.target_window).title
    target_ref = refs[target_title]
    # The body prints the target under the same ref the summary named.
    assert target_ref == session.result.root.ref
    assert f'WINDOW {target_ref} "{target_title}"' in obs, obs
    # Different windows have different refs.
    assert len(set(refs.values())) == len(refs)

    # ...and the refs do not drift over repeated observations.
    again = session.observe().text
    assert _summary_refs(again) == refs


def test_every_summary_ref_observes_the_window_it_names():
    provider = MockProvider(
        fixtures=[
            _tree(title="商店", window_id="w1"),
            _tree(title="设置", window_id="w2"),
        ]
    )
    session = Session(provider)
    refs = _summary_refs(session.observe().text)

    for title, ref in refs.items():
        window = session._window_by_ref(ref)
        assert window is not None and window.title == title
        out = session.dispatch(f"OBSERVE {ref}")
        # The observation switched to that window and prints it under the ref
        # the summary gave (not a freshly minted one).
        assert f'WINDOW {ref} "{title}"' in out, out
        assert session.target_window == window.window_id
        # Switching back and forth must not renumber the windows.
        assert _summary_refs(session.observe().text) == refs

    # The long-form header uses the same refs as the summary.
    header = session.observe(include_other_windows=True).text
    target_title = session._window_by_ref(session.target_window).title
    for title, ref in refs.items():
        if title != target_title:
            assert f'WINDOW {ref} "{title}"' in header, header


def test_a_desktop_that_cannot_be_read_passes_none(monkeypatch):
    seen = _capture_plan(monkeypatch)
    provider = MockProvider(fixtures=[_tree()])
    session = Session(provider)
    session.observe()
    assert seen[-1] and len(seen[-1]) == 1, "one window is a one-entry summary"

    # Nothing enumerated -> nothing to say -> the projection keeps today's
    # output instead of receiving a half-built list.
    session.windows = []
    assert session._desktop_summary(session.result) is None


def test_the_long_form_header_suppresses_the_summary(monkeypatch):
    seen = _capture_plan(monkeypatch)
    provider = MockProvider(
        fixtures=[
            _tree(title="商店", window_id="w1"),
            _tree(title="设置", window_id="w2"),
        ]
    )
    session = Session(provider)
    session.observe(include_other_windows=True)
    assert seen[-1] is None
    assert "WINDOW w" in session.observe(include_other_windows=True).text
