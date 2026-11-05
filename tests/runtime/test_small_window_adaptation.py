"""Small windows read in full and probe with one signature read.

The viewport walker pays a cache round trip per node, so on a window whose
capture fit whole it is slower than one subtree request (measured: form 30
nodes 127ms vs 78ms, chat 58 nodes 192 vs 140). It only earns its cost when it
*cut* rows. These tests pin the switch and, importantly, that a truncated
capture keeps the walker.
"""

from __future__ import annotations

import copy

from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n


def _tree():
    row = _n("w1", (0, 0), "listitem", "Row 0")
    rows = _n("w1", (0,), "list", "Rows", children=[row])
    root = _n(
        "w1",
        (),
        "window",
        "Small App",
        children=[
            rows,
            _n("w1", (1,), "button", "Go", patterns=frozenset({"invoke"})),
        ],
    )
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="Small App", process="demo.exe", pid=1, active=True
        ),
        root=root,
    )


_META_SMALL = {(0,): {"items_total": 1, "items_visible": 1, "items_truncated": False}}
_META_TRUNCATED = {
    (0,): {"items_total": 40, "items_visible": 1, "items_truncated": True}
}


class _ProbeProvider(MockProvider):
    """Counts which read entry point each path uses.

    Declared non-deterministic like the real UIA provider, so the settle rules
    that depend on a provider finishing on its own do not apply.
    """

    is_deterministic = False

    def __init__(self, meta):
        super().__init__(fixtures=[_tree()])
        self.viewport_meta = {"w1": meta}
        self.last_viewport_partial = True
        self.viewport_calls = 0
        self.full_calls = 0
        self.sig_calls = 0
        self.layered_calls = 0

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.viewport_calls += 1
        return copy.deepcopy(self._trees[window_id])

    def snapshot(self, window_id, scope=None, max_depth=12):
        self.full_calls += 1
        return copy.deepcopy(self._trees[window_id])

    def snapshot_signature(self, window_id):
        self.sig_calls += 1
        return copy.deepcopy(self._trees[window_id])

    def snapshot_probe(self, window_id, max_depth=4):
        self.layered_calls += 1
        return copy.deepcopy(self._trees[window_id])


def test_a_second_observe_of_a_small_window_reads_it_in_full():
    provider = _ProbeProvider(_META_SMALL)
    session = Session(provider)
    session.observe()
    assert provider.viewport_calls == 1 and provider.full_calls == 0

    session.observe()
    assert provider.full_calls == 1, "the known-small window must be read in one request"
    assert provider.viewport_calls == 1
    assert session._last_capture_truncated is False


def test_a_truncated_capture_keeps_the_viewport_walker():
    """A long list must stay on the walker: full would read every row."""
    provider = _ProbeProvider(_META_TRUNCATED)
    session = Session(provider)
    session.observe()
    session.observe()
    assert provider.viewport_calls == 2
    assert provider.full_calls == 0
    assert session._last_capture_truncated is True


def test_a_small_window_settles_with_full_reads_not_probes():
    """A probe costs about a full read here, so probing is a net loss.

    Measured on the chat screen: sig probe 158ms against a 163ms full read.
    "probe, read, probe" is then three reads where the full loop takes two.
    """
    provider = _ProbeProvider(_META_SMALL)
    session = Session(provider)
    session.observe()
    ref = next(n.ref for n in session.result.root.walk() if n.role == "input" or n.role == "button")

    session.dispatch(f"CALL {ref}.focus()")
    session.dispatch(f"CALL {ref}.focus()")
    assert provider.sig_calls == 0
    assert provider.layered_calls == 0
    assert provider.full_calls >= 1, "settle must still read the frame in full"


class _TwoWindowProvider(_ProbeProvider):
    """One small window and one truncated (long-list) window."""

    def __init__(self):
        small = _tree()
        long_list = _tree()
        long_list.window.window_id = "w2"
        long_list.window.title = "Long List"
        long_list.root.name = "Long List"
        MockProvider.__init__(self, fixtures=[small, long_list])
        self.viewport_meta = {"w1": _META_SMALL, "w2": _META_TRUNCATED}
        self.last_viewport_partial = True
        self.viewport_calls = 0
        self.full_calls = 0
        self.sig_calls = 0
        self.layered_calls = 0
        self.viewport_ids: list[str] = []

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.viewport_calls += 1
        self.viewport_ids.append(window_id)
        return copy.deepcopy(self._trees[window_id])


def test_switching_windows_forgets_the_small_verdict():
    """Window sizes are not interchangeable; w2's list must stay on the walker."""
    provider = _TwoWindowProvider()
    session = Session(provider)
    session.observe()
    assert session._last_capture_window == "w1"

    session.dispatch("OBSERVE w2")
    assert provider.full_calls == 0, "a switch must not carry w1's small verdict"
    assert provider.viewport_ids[-1] == "w2"


def test_a_truncated_window_keeps_the_layered_probe():
    provider = _ProbeProvider(_META_TRUNCATED)
    session = Session(provider)
    session.observe()
    ref = next(n.ref for n in session.result.root.walk() if n.role == "input" or n.role == "button")

    session.dispatch(f"CALL {ref}.focus()")
    session.dispatch(f"CALL {ref}.focus()")
    assert provider.layered_calls >= 1
    assert provider.sig_calls == 0
