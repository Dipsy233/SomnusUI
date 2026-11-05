"""Viewport capture: collection row selection, fallbacks, and the session wiring.

The provider-side walker is exercised with fake cached elements (no desktop),
covering the cases that decide whether a row is shown:
all visible / none visible / one visible / unreadable rects / a peek that fails.
The session side is covered with a replay provider: the contract attributes,
the full-read fallback, the FIND retry, and the `OBSERVE ... expand` ref
resolution.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.providers.windows import uia
from somnus.runtime import session as session_module
from somnus.runtime.session import Session
from tests.fixtures.builders import _n

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_a_rectangle_inside_the_window_is_visible():
    window = (100, 100, 500, 400)
    assert uia._rect_visible((120, 130, 100, 20), window) is True
    assert uia._rect_visible((120, 620, 100, 20), window) is False  # below
    assert uia._rect_visible((50, 130, 100, 20), window) is True  # straddles left
    assert uia._rect_visible((650, 130, 100, 20), window) is False  # right of it


def test_a_zero_or_unknown_rectangle_is_not_visible():
    window = (0, 0, 400, 300)
    assert uia._rect_visible(None, window) is False
    assert uia._rect_visible((10, 10, 0, 20), window) is False
    assert uia._rect_visible((10, 10, 100, 20), None) is False


def test_rect_parsing_accepts_uia_shapes_and_rejects_junk():
    assert uia._parse_rect((1.0, 2.0, 3.0, 4.0)) == (1.0, 2.0, 3.0, 4.0)
    assert uia._parse_rect([1, 2, 3, 4]) == (1.0, 2.0, 3.0, 4.0)
    assert uia._parse_rect(None) is None
    assert uia._parse_rect((1, 2, 3)) is None
    assert uia._parse_rect("junk") is None


def test_row_container_detection_uses_composition():
    rows = [_n("w1", (i,), "listitem", f"Item {i}") for i in range(10)]
    container = _n("w1", (), "pane", "rows", children=rows)
    assert uia._looks_like_row_container(container) is True

    structural = _n(
        "w1", (), "pane", "form",
        children=[_n("w1", (i,), "text", f"Field {i}") for i in range(10)],
    )
    assert uia._looks_like_row_container(structural) is False

    short = _n(
        "w1", (), "pane", "list",
        children=[_n("w1", (i,), "listitem", f"Item {i}") for i in range(3)],
    )
    assert uia._looks_like_row_container(short) is False


# ---------------------------------------------------------------------------
# fake cached elements for the provider-side walker
# ---------------------------------------------------------------------------

_ROLE_IDS = {role: cid for cid, role in uia.UIA_CONTROL_TYPES.items()}
_PID_NAMES = {pid: name for name, pid in uia._PATTERN_IDS.items()}


class FakeArray:
    def __init__(self, items):
        self._items = items
        self.Length = len(items)

    def GetElement(self, index):
        return self._items[index]


class FakeCached:
    """A cached UIA element: the members ``_build_node`` reads."""

    def __init__(self, node: RawNode, rect=None, children=None):
        self.node = node
        self.rect = rect
        self._children = [
            child if isinstance(child, FakeCached) else FakeCached(child)
            for child in (children or [])
        ]
        self.CachedControlType = _ROLE_IDS.get(node.role, 50025)
        self.CachedName = node.name
        self.CachedIsEnabled = "disabled" not in node.states
        self.CachedIsOffscreen = False
        self.CachedHasKeyboardFocus = False
        self.CachedAutomationId = node.native.automation_id
        self.CachedClassName = node.native.class_name
        self.CachedDescription = None

    def GetCachedChildren(self):
        return FakeArray(self._children) if self._children else None

    def GetCachedPropertyValue(self, prop_id):
        if prop_id == 30001:
            return self.rect
        if prop_id == 30045:
            return self.node.value
        if prop_id == 30010:
            return True
        if prop_id == 30022:
            return False
        if prop_id == 30008:
            return False
        if prop_id == 30020:
            return 0
        raise KeyError(prop_id)

    def GetCachedPattern(self, pattern_id):
        name = _PID_NAMES.get(pattern_id)
        if name and name in self.node.patterns:
            return object()
        raise KeyError(pattern_id)

    def GetRuntimeId(self):
        return self.node.native.runtime_id

    def BuildUpdatedCache(self, _request):
        return FakeCached(self.node, rect=self.rect, children=self.node.children)


class FakeElement(FakeCached):
    """A live element handle: ``BuildUpdatedCache`` returns the cached state."""

    def __init__(self, node, rect=None, fail_peek=False):
        super().__init__(node, rect=rect)
        self.fail_peek = fail_peek

    def BuildUpdatedCache(self, request):
        if getattr(request, "fail", False):
            raise KeyError("peek failed")
        rects = getattr(request, "rects", None)
        children = []
        given = getattr(request, "rows", None) or self.node.children
        for index, child in enumerate(given):
            child_rect = None if rects is None else rects.get(index)
            children.append(FakeCached(child, rect=child_rect, children=child.children))
        return FakeCached(self.node, rect=getattr(request, "rect", None), children=children)

    def GetRuntimeId(self):
        return self.node.native.runtime_id


class _Request:
    def __init__(self, *, rect=None, rows=None, rects=None, fail=False):
        self.rect = rect
        self.rows = rows
        self.rects = rects
        self.fail = fail


def _fake_provider(max_depth=20):
    provider = object.__new__(uia.UiaProvider)
    provider._current_window_id = "w1"
    provider.max_depth = max_depth
    provider.viewport_meta = {}
    provider._element_cache = {}
    provider._UIA = SimpleNamespace(
        UIA_SelectionPatternId=10001,
        UIA_ScrollPatternId=10004,
        UIA_AutomationIdPropertyId=30011,
        UIA_RuntimeIdPropertyId=30001,
        IUIAutomationSelectionPattern=object,
        IUIAutomationScrollPattern=object,
    )
    return provider


def _rows(count, rects=None):
    nodes = [
        _n(
            "w1",
            (i,),
            "listitem",
            f"Item {i}",
            children=[_n("w1", (i, 0), "text", f"Item {i} text")],
        )
        for i in range(count)
    ]
    return nodes


def _container(rows, role="list", name="goods"):
    return _n("w1", (), role, name, patterns=frozenset({"scroll"}), children=rows)


def _capture(container, *, rects, window_rect, peek_fails=False, expand=False):
    provider = _fake_provider()
    provider._cache = _Request()
    provider._peek_children_cache = _Request(rows=container.children, rects=rects, fail=peek_fails)
    provider._element_cache_request = _Request(rect=window_rect)
    node = provider._collection_node(
        FakeElement(container),
        1,
        (0,),
        window_rect,
        expand=expand,
    )
    return provider, node


# ---------------------------------------------------------------------------
# row selection: all / none / one visible, and the fallbacks
# ---------------------------------------------------------------------------


def test_all_visible_rows_are_read_completely():
    rows = _rows(3)
    rects = {0: (0, 0, 100, 20), 1: (0, 20, 100, 20), 2: (0, 40, 100, 20)}
    provider, node = _capture(_container(rows), rects=rects, window_rect=(0, 0, 300, 200))

    assert [row.name for row in node.children] == ["Item 0", "Item 1", "Item 2"]
    # "减冗余不减信息": a visible row keeps its children.
    assert all(row.children and row.children[0].role == "text" for row in node.children)
    meta = provider.viewport_meta["w1"][(0,)]
    assert meta["items_total"] == 3 and meta["items_visible"] == 3
    assert meta["items_truncated"] is False
    assert node.truncated is False


def test_only_the_on_screen_rows_are_read():
    rows = _rows(5)
    rects = {0: (0, 0, 100, 20), 1: (0, 20, 100, 20),
             2: (0, 60, 100, 20), 3: (0, 80, 100, 20), 4: (0, 100, 100, 20)}
    provider, node = _capture(_container(rows), rects=rects, window_rect=(0, 0, 300, 50))

    assert [row.name for row in node.children] == ["Item 0", "Item 1"]
    meta = provider.viewport_meta["w1"][(0,)]
    assert meta["items_total"] == 5 and meta["items_visible"] == 2
    assert meta["items_truncated"] is True


def test_one_visible_row_is_the_boundary_case():
    rows = _rows(4)
    rects = {0: (0, 500, 100, 20), 1: (0, 10, 100, 20), 2: (0, 600, 100, 20), 3: (0, 700, 100, 20)}
    provider, node = _capture(_container(rows), rects=rects, window_rect=(0, 0, 300, 100))

    assert [row.name for row in node.children] == ["Item 1"]
    assert provider.viewport_meta["w1"][(0,)]["items_visible"] == 1


def test_zero_visible_rows_keeps_the_summary_only():
    rows = _rows(4)
    rects = {i: (0, 900, 100, 20) for i in range(4)}
    provider, node = _capture(_container(rows), rects=rects, window_rect=(0, 0, 300, 100))

    assert node.children == []
    meta = provider.viewport_meta["w1"][(0,)]
    assert meta["items_total"] == 4 and meta["items_visible"] == 0
    assert meta["items_truncated"] is True


def test_an_empty_collection_is_not_truncated():
    provider, node = _capture(_container([]), rects={}, window_rect=(0, 0, 300, 100))
    assert node.children == []
    meta = provider.viewport_meta["w1"][(0,)]
    assert meta["items_total"] == 0 and meta["items_truncated"] is False


def test_unreadable_row_rectangles_fall_back_to_the_full_read():
    """No rect at all is 'cannot tell', never 'nothing is visible'."""
    rows = _rows(4)
    provider, node = _capture(
        _container(rows), rects={}, window_rect=(0, 0, 300, 100)
    )
    # Full read keeps every row (all 4), and no viewport metadata is claimed.
    assert len(node.children) == 4
    assert (0,) not in provider.viewport_meta.get("w1", {})


def test_a_failed_peek_falls_back_to_the_full_read():
    rows = _rows(4)
    provider, node = _capture(
        _container(rows),
        rects={0: (0, 0, 100, 20)},
        window_rect=(0, 0, 300, 100),
        peek_fails=True,
    )
    assert len(node.children) == 4
    assert node.children[0].children, "the fallback must keep full row content"


def test_expanding_a_container_reads_a_page_after_the_visible_window():
    rows = _rows(12)
    rects = {i: (0, i * 20, 100, 20) for i in range(12)}
    provider, node = _capture(
        _container(rows), rects=rects, window_rect=(0, 0, 300, 60), expand=True
    )
    # Window shows rows 0-2; expand continues past them up to the page cap.
    assert [row.name for row in node.children[:3]] == ["Item 0", "Item 1", "Item 2"]
    assert len(node.children) > 3


# ---------------------------------------------------------------------------
# session wiring: contract attributes, fallback, FIND retry, expand
# ---------------------------------------------------------------------------


def _list_tree(rows=5, row_names=None):
    names = row_names or [f"Item {i}" for i in range(rows)]
    nodes = [
        _n("w1", (0, i), "listitem", name,
           children=[_n("w1", (0, i, 0), "text", name)])
        for i, name in enumerate(names)
    ]
    root = _n(
        "w1", (), "pane", "form",
        children=[_n("w1", (0,), "list", "goods",
                     patterns=frozenset({"scroll"}), children=nodes)],
    )
    return RawTree(
        window=WindowInfo(window_id="w1", title="form", process="demo.exe", pid=7, active=True),
        root=root,
        raw_revision=1,
    )


class ViewportMock(MockProvider):
    """A replay provider with viewport capture: partial tree + meta."""

    def __init__(self, full_tree, partial_tree, meta):
        super().__init__(fixtures=[full_tree])
        self.full_tree = full_tree
        self.partial_tree = partial_tree
        self.viewport_meta = {}
        self.expanded_seen = []
        self._meta = meta

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.expanded_seen.append(set(expanded_paths or ()))
        self.viewport_meta = {"w1": self._meta}
        return copy.deepcopy(self.partial_tree)


def _partial_list_tree():
    """Two of five rows, one padded so the attrs test has a container."""
    return _list_tree(rows=2, row_names=["Item 0", "Item 1"])


def test_the_contract_fields_land_on_the_compiled_container():
    meta = {(0,): {"items_total": 5, "items_visible": 2, "items_truncated": True, "scroll_pct": 25}}
    provider = ViewportMock(_list_tree(5), _partial_list_tree(), meta)
    session = Session(provider)
    obs = session.observe()

    list_node = next(n for n in session.result.root.walk() if n.role == "list")
    assert list_node.attrs["items_total"] == 5
    assert list_node.attrs["items_visible"] == 2
    assert list_node.attrs["items_truncated"] is True
    assert list_node.attrs["scroll_pct"] == 25
    # The captured rows' own content is present in the observation (the
    # provider read them completely; the compiler may render the label as a
    # relation rather than a child, which is why this asserts on the text).
    assert "Item 0" in obs.text and "Item 1" in obs.text


def test_a_provider_without_viewport_capture_observes_the_full_tree():
    session = Session(MockProvider(fixtures=[_list_tree(5)]))
    session.observe()
    list_node = next(n for n in session.result.root.walk() if n.role == "list")
    assert len(list_node.children) == 5
    assert "items_total" not in list_node.attrs


def test_find_retries_with_a_full_read_when_the_hit_is_outside_the_viewport():
    provider = ViewportMock(
        _list_tree(5),
        _partial_list_tree(),
        {(0,): {"items_total": 5, "items_visible": 2, "items_truncated": True}},
    )
    session = Session(provider)
    session.observe()
    # "Item 4" was not captured: the first pass misses, the retry reads fully.
    out = session.dispatch('FIND "Item 4"')
    assert out.startswith("FOUND 1"), out
    assert session._viewport_limited is False


def test_observe_expand_passes_the_native_path_and_resolves_the_new_ref(monkeypatch):
    provider = ViewportMock(
        _list_tree(5),
        _partial_list_tree(),
        {(0,): {"items_total": 5, "items_visible": 2, "items_truncated": True}},
    )
    session = Session(provider)
    session.observe()

    seen_expanded: list = []
    original_plan = session_module.ProjectionEngine.plan

    def spy(self, root, projection, *, full=False, expanded=None, desktop_summary=None):
        seen_expanded.append(expanded)
        return original_plan(
            self,
            root,
            projection,
            full=full,
            expanded=expanded,
            desktop_summary=desktop_summary,
        )

    monkeypatch.setattr(session_module.ProjectionEngine, "plan", spy)

    list_ref = next(n.ref for n in session.result.root.walk() if n.role == "list")
    session.dispatch(f"OBSERVE w1 expand {list_ref}")

    # The provider was told which native path to page in...
    assert provider.expanded_seen[-1] == {(0,)}
    # ...and the projection received the id of the node in the *new* result,
    # not an id resolved against the pre-expand tree.
    assert seen_expanded[-1] is not None
    assert seen_expanded[-1] == {id(session.result.find(list_ref))}


# ---------------------------------------------------------------------------
# layered probe: watched-set projection and the full-read red line
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now += ms / 1000


def _all_natives(tree):
    """Identity keys of every node - what ``_watch_signature`` compares against."""
    from somnus.runtime.session import _native_key

    return [_native_key(node.native) for node in tree.walk()]


def test_the_watched_signature_ignores_nodes_outside_the_watch_set():
    """A change outside the watched set is not a change settle can act on."""
    from somnus.runtime.session import _watch_signature

    tree = _list_tree(3)
    watch = _all_natives(tree)[:3]
    changed_unwatched = copy.deepcopy(tree)
    for node in changed_unwatched.root.walk():
        if node.role == "text":
            node.states = frozenset({"disabled"})
    assert _watch_signature(tree, watch) == _watch_signature(changed_unwatched, watch)


def test_the_watched_signature_sees_a_deep_state_flip_inside_a_visible_row():
    from somnus.runtime.session import _watch_signature

    tree = _list_tree(2)
    watch = _all_natives(tree)
    flipped = copy.deepcopy(tree)
    target = next(n for n in flipped.root.walk() if n.role == "text")
    target.states = frozenset({"disabled"})
    assert _watch_signature(tree, watch) != _watch_signature(flipped, watch)


def test_a_missing_watched_node_makes_the_signature_unknown():
    from somnus.runtime.session import _watch_signature

    tree = _list_tree(2)
    partial = _list_tree(2)
    partial.root.children[0].children = partial.root.children[0].children[:1]
    assert _watch_signature(partial, _all_natives(tree)) is None


def test_a_touched_ref_with_no_native_identity_forces_full_reads():
    """The red line: the call's target must be in the probe's field of view."""
    provider = ViewportMock(
        _list_tree(5),
        _partial_list_tree(),
        {(0,): {"items_total": 5, "items_visible": 2, "items_truncated": True}},
    )
    session = Session(provider)
    session.observe()

    # A ref that is in the current compilation is covered (plus its ancestors).
    assert session._watch_for_call(["li1"])
    # A ref the compilation cannot place means the probe could miss the very
    # change this call caused - so no probe at all.
    assert session._watch_for_call(["no-such-ref"]) == []


def test_a_flip_outside_the_watch_set_does_not_force_a_full_read(monkeypatch):
    """The mirror image of the red line: unseen changes are not acted upon."""
    from somnus.runtime.session import _watch_signature
    from somnus.runtime.settle import Settler

    clock = _Clock()
    monkeypatch.setattr(session_module.time, "sleep", lambda s: clock.advance_ms(s * 1000))
    previous = _list_tree(3)
    watch = _all_natives(previous)[:3]  # root, list, first row - not the rows below
    changed = copy.deepcopy(previous)
    outside = changed.root.children[0].children[-1]
    outside.states = frozenset({"disabled"})
    calls = {"probes": 0, "fetches": 0}
    probes = [copy.deepcopy(changed), copy.deepcopy(changed)]

    def probe():
        calls["probes"] += 1
        return probes.pop(0)

    def fetch():  # pragma: no cover - must not be called
        calls["fetches"] += 1
        return copy.deepcopy(changed)

    settler = Settler(quiet_ms=20, poll_interval_ms=5, timeout_s=2.0, clock=clock)
    _tree, result = settler.wait(
        fetch,
        previous=previous,
        previous_signature=_watch_signature(previous, watch),
        probe=probe,
        signature=lambda t: _watch_signature(t, watch),
    )
    assert result.settled is True
    assert calls == {"probes": 1, "fetches": 0}, (
        "a change outside the captured layers must stay invisible to settle"
    )


def test_an_unchanged_viewport_costs_one_probe_and_no_snapshot(monkeypatch):
    from somnus.runtime.session import _watch_signature
    from somnus.runtime.settle import Settler

    tree = _list_tree(2)
    watch = _all_natives(tree)
    clock = _Clock()
    # Sleeping advances the fake clock; the probe itself is a prepared tree.
    monkeypatch.setattr(session_module.time, "sleep", lambda s: clock.advance_ms(s * 1000))
    calls = {"probes": 0, "fetches": 0}
    probes = [copy.deepcopy(tree)]

    def probe():
        calls["probes"] += 1
        return probes.pop(0)

    def fetch():
        calls["fetches"] += 1
        return copy.deepcopy(tree)

    settler = Settler(quiet_ms=20, poll_interval_ms=5, timeout_s=2.0, clock=clock)
    settled_tree, result = settler.wait(
        fetch,
        previous=tree,
        previous_signature=_watch_signature(tree, watch),
        probe=probe,
        signature=lambda t: _watch_signature(t, watch),
    )
    assert result.settled is True
    assert calls == {"probes": 1, "fetches": 0}


def test_a_watched_change_costs_exactly_one_full_snapshot(monkeypatch):
    from somnus.runtime.session import _watch_signature
    from somnus.runtime.settle import Settler

    clock = _Clock()
    monkeypatch.setattr(session_module.time, "sleep", lambda s: clock.advance_ms(s * 1000))
    previous = _list_tree(2)
    watch = _all_natives(previous)
    changed = copy.deepcopy(previous)
    target = next(n for n in changed.root.walk() if n.role == "text")
    target.states = frozenset({"disabled"})
    calls = {"probes": 0, "fetches": 0}
    probes = [copy.deepcopy(changed), copy.deepcopy(previous)]

    def probe():
        calls["probes"] += 1
        return probes.pop(0)

    def fetch():
        calls["fetches"] += 1
        return copy.deepcopy(previous)

    settler = Settler(quiet_ms=20, poll_interval_ms=5, timeout_s=2.0, clock=clock)
    tree, result = settler.wait(
        fetch,
        previous=previous,
        previous_signature=_watch_signature(previous, watch),
        probe=probe,
        signature=lambda t: _watch_signature(t, watch),
    )
    assert result.settled is True
    assert calls["fetches"] == 1
    assert calls["probes"] == 2


def test_a_probe_that_cannot_cover_the_watch_set_forces_full_reads(monkeypatch):
    """The hard red line: no coverage -> full snapshot, never 'unchanged'."""
    from somnus.runtime.session import _watch_signature
    from somnus.runtime.settle import Settler

    clock = _Clock()
    monkeypatch.setattr(session_module.time, "sleep", lambda s: clock.advance_ms(s * 1000))
    previous = _list_tree(3)
    watch = _all_natives(previous)
    # The probe lost one row (scrolled away): its watched native is absent.
    partial = _list_tree(3)
    partial.root.children[0].children = partial.root.children[0].children[:2]
    calls = {"probes": 0, "fetches": 0}

    def probe():
        calls["probes"] += 1
        return copy.deepcopy(partial)

    def fetch():
        calls["fetches"] += 1
        return copy.deepcopy(previous)

    settler = Settler(quiet_ms=20, poll_interval_ms=5, timeout_s=2.0, clock=clock)
    tree, result = settler.wait(
        fetch,
        previous=previous,
        previous_signature=_watch_signature(previous, watch),
        probe=probe,
        signature=lambda t: _watch_signature(t, watch),
    )
    assert result.settled is True
    assert calls["fetches"] >= 1, "an uncoverable probe must not settle from itself"


def test_a_scroll_action_drops_the_watch_set_and_says_to_re_observe():
    tree = _list_tree(5, row_names=["Item 0", "Item 1", "Item 2", "Item 3", "Item 4"])
    first_row = next(n for n in tree.root.walk() if n.role == "listitem")
    first_row.patterns = frozenset({"scrollitem"})
    partial = copy.deepcopy(tree)
    partial.root.children[0].children = partial.root.children[0].children[:2]
    provider = ViewportMock(
        tree,
        partial,
        {(0,): {"items_total": 5, "items_visible": 2, "items_truncated": True}},
    )
    session = Session(provider)
    session.observe()
    assert session._watch_natives

    ref = session.result.find("li1").ref
    out = session.dispatch(f"CALL {ref}.scroll_into_view()")

    assert "~ viewport changed; OBSERVE again" in out, out
    assert session._watch_natives == []


def test_the_provider_probe_keeps_visible_rows_complete_and_skips_the_rest():
    rows = _rows(4)
    for row in rows:
        row.children[0].states = frozenset({"disabled"})
    rects = {0: (0, 0, 100, 20), 1: (0, 20, 100, 20),
             2: (0, 300, 100, 20), 3: (0, 320, 100, 20)}
    provider = _fake_provider()
    provider._probe_children_cache = _Request()
    provider._probe_element_cache = _Request(rect=(0, 0, 300, 100))
    provider._probe_subtree_cache = _Request()
    provider._rect_peek_cache = _Request(rows=rows, rects=rects)
    node = provider._collection_node(
        FakeElement(_container(rows)), 1, (0,), (0, 0, 300, 100), expand=False, probe=True
    )
    assert [row.name for row in node.children] == ["Item 0", "Item 1"]
    # Deep state inside a visible row is present in the probe tree.
    assert "disabled" in node.children[0].children[0].states
    assert all(row.name != "Item 2" for row in node.children)
