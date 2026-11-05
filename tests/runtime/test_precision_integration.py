"""P2 integration points that live in ``Session`` and the Windows provider.

The precision work itself (caps/twins/verifier/compile) landed in its own files;
these are the five pieces that can only be done here, so they are pinned here:

* ``TARGET_AMBIGUOUS`` is *deferred*: the named ref is tried first and its twin
  is the fallback, instead of refusing both refs outright;
* P2-4's ``settled`` half: a visible change on a tree that never settled is
  evidence, not confirmation;
* the snapshot's ``SelectionPattern.CurrentSelection`` fallback (container-only
  selection providers);
* corroborating an AutomationId fallback hit before acting on it;
* the select read-back message only on total failure.

The last two are pure helpers exercised with fakes; the first three run through
a real ``Session`` against the replay provider.
"""

from __future__ import annotations

from somnus.ir.action import ExecResult
from somnus.ir.node import NativeRef
from somnus.providers.mock import MockProvider
from somnus.providers.windows import uia
from somnus.runtime.session import Session
from somnus.runtime.settle import Settler
from tests.fixtures.builders import _n, notepad_tree
from tests.fixtures import scenarios as S


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _twin_tree():
    """One window with two identically named buttons (a real twin shape)."""
    from somnus.ir.node import RawTree, WindowInfo

    root = _n(
        "w1",
        (),
        "pane",
        name="twins",
        children=[
            _n("w1", (0,), "button", name="Twin", patterns=frozenset({"invoke"}),
               automation_id="twin_a"),
            _n("w1", (1,), "button", name="Twin", patterns=frozenset({"invoke"}),
               automation_id="twin_b"),
        ],
    )
    return RawTree(
        window=WindowInfo(window_id="w1", title="twins", process="demo.exe", pid=7, active=True),
        root=root,
        raw_revision=1,
    )


class _RefusingProvider(MockProvider):
    """Refuses ``activate`` on one element, the way a ghost twin does."""

    def __init__(self, tree, refuse_runtime_id):
        super().__init__(fixtures=[tree])
        self.refuse_runtime_id = tuple(refuse_runtime_id)

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        if primitive == "activate" and tuple(ref.runtime_id) == self.refuse_runtime_id:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="ghost copy refused Invoke",
                unsupported=True,
            )
        return super().execute(ref, primitive, value, timeout_s)


def _collide(session, first: str, second: str) -> None:
    """Make the runtime see `first`/`second` as one fingerprint's two refs.

    Two nodes only share a fingerprint when the compiler's disambiguation
    evidence runs out (the same native element seen twice, or a provider that
    reports one identity for two nodes). The compiler now prevents most of
    those, so the *collision report* itself is simulated here - it is the input
    that used to become a blanket ``TARGET_AMBIGUOUS`` refusal, and it is what
    ``registry.candidates_for`` answers from.
    """
    shared = session.result.fingerprints[first]
    session.result.fingerprints[second] = shared
    record = session.registry.records[second]
    record.fingerprint = shared
    session.registry.by_fingerprint[(session.target_window, shared)] = [first, second]
    session._ambiguous_refs = {first, second}


def _twins(session):
    nodes = [n for n in session.result.root.walk() if n.name == "Twin"]
    assert len(nodes) == 2, [n.ref for n in nodes]
    return nodes[0].ref, nodes[1].ref


# ---------------------------------------------------------------------------
# 1. deferred ambiguity + twin retry
# ---------------------------------------------------------------------------


def test_both_twin_refs_are_actionable_and_ambiguity_is_not_fatal():
    """Neither ref is refused up front; the first one works."""
    provider = MockProvider(fixtures=[_twin_tree()])
    session = Session(provider)
    session.observe()
    first, second = _twins(session)
    _collide(session, first, second)
    assert session._ambiguous_refs, "the fixture must produce a collision"

    out = session.dispatch(f"CALL {first}.activate()")
    assert not out.startswith("ERR"), out
    assert "TARGET_AMBIGUOUS" not in out
    assert "healed" not in out


def test_a_ghost_twin_is_retried_once_and_the_response_says_so():
    """The named element refuses the interface; the twin answers."""
    tree = _twin_tree()
    nodes = [n for n in tree.root.walk() if n.name == "Twin"]
    provider = _RefusingProvider(tree, nodes[0].native.runtime_id)
    session = Session(provider)
    session.observe()
    first, second = _twins(session)
    _collide(session, first, second)

    out = session.dispatch(f"CALL {first}.activate()")

    assert out.startswith("OK"), out
    assert f"~ healed {first}→{second}" in out, out
    # The twin (and only it) really executed.
    assert [ref for ref, _p, _v in provider.executed] and all(
        tuple(r.runtime_id) == nodes[1].native.runtime_id for r, _p, _v in provider.executed
    )


def test_a_different_failure_is_not_routed_to_the_twin():
    """TARGET_DISABLED is a real answer about this element; no sibling retry."""
    tree = _twin_tree()
    nodes = [n for n in tree.root.walk() if n.name == "Twin"]

    class Disabled(MockProvider):
        def execute(self, ref, primitive, value=None, timeout_s=3.0):
            if tuple(ref.runtime_id) == nodes[0].native.runtime_id:
                return ExecResult(ok=False, error="TARGET_DISABLED")
            return super().execute(ref, primitive, value, timeout_s)

    provider = Disabled(fixtures=[tree])
    session = Session(provider)
    session.observe()
    first, second = _twins(session)
    _collide(session, first, second)

    out = session.dispatch(f"CALL {first}.activate()")
    assert out.startswith("ERR TARGET_DISABLED"), out
    assert "healed" not in out


def test_batch_reports_the_healed_step():
    tree = _twin_tree()
    nodes = [n for n in tree.root.walk() if n.name == "Twin"]
    provider = _RefusingProvider(tree, nodes[0].native.runtime_id)
    session = Session(provider)
    session.observe()
    first, second = _twins(session)
    _collide(session, first, second)

    from somnus.sui.parse import parse_call_block

    actions = parse_call_block(f"CALL {first}.activate()\nCALL {first}.activate()")
    out = session.call_batch(actions).text
    assert out.startswith("BATCH OK"), out
    assert f"~ step 1 healed {first}→{second}" in out, out


# ---------------------------------------------------------------------------
# 2. P2-4, the settled half
# ---------------------------------------------------------------------------


def test_a_visible_change_on_an_unsettled_tree_is_not_reported_as_ok():
    """The delta is evidence, not confirmation, while the UI is still moving."""
    # quiet can never be reached before the timeout, so settle returns false.
    session = Session(
        MockProvider(fixtures=[notepad_tree()]),
        settler=Settler(quiet_ms=1000, poll_interval_ms=5, timeout_s=0.2),
    )
    session.observe()
    box = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "set_text" in n.cap_names()
    )

    out = session.dispatch(f'CALL {box.ref}.set_text("hello")')

    assert out.startswith("OK_UNVERIFIED"), out
    assert "settled=false" in out, out
    assert "hello" in out, "the visible change must still be reported"


def test_a_settled_visible_change_still_reads_ok():
    """The other side of the boundary: settling completed, the change counts."""
    session = Session(MockProvider(fixtures=[notepad_tree()]))
    session.observe()
    box = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "set_text" in n.cap_names()
    )
    out = session.dispatch(f'CALL {box.ref}.set_text("hello")')
    assert out.startswith("OK "), out


# ---------------------------------------------------------------------------
# 3. container SelectionPattern fallback (pure helper)
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self, name):
        self.CurrentName = name


class _Selection:
    def __init__(self, names):
        self.Length = len(names)
        self._names = names

    def GetElement(self, index):
        return _Item(self._names[index])


class _Pattern:
    def __init__(self, names):
        self.CurrentSelection = _Selection(names)


class _CachedPatternHost:
    def __init__(self, pattern=None):
        self._pattern = pattern

    def GetCachedPattern(self, _pid):
        if self._pattern is None:
            raise KeyError(_pid)
        return self._pattern


class _UIA:
    UIA_SelectionPatternId = 10001


class _TypedSelection:
    """What a SelectionPattern looks like after QueryInterface."""

    def __init__(self, names):
        self._names = names

    def GetCachedSelection(self):
        return _Selection(self._names)

    def GetCurrentSelection(self):
        return _Selection(self._names)


class _BarePattern:
    """A raw IUnknown pointer: no members until it is cast."""

    def __init__(self, names):
        self._names = names

    def QueryInterface(self, _interface):
        return _TypedSelection(self._names)


class _TypedUIA(_UIA):
    IUIAutomationSelectionPattern = object


class _CachedBareHost:
    def __init__(self, names):
        self._pattern = _BarePattern(names)

    def GetCachedPattern(self, _pid):
        return self._pattern


def test_a_bare_pattern_pointer_is_cast_before_reading():
    """GetCachedPattern returns an IUnknown; members appear only after the cast."""
    label = uia._current_selection_label(
        _Element(), _CachedBareHost(["低"]), _TypedUIA
    )
    assert label == "低"


class _Element:
    def __init__(self, pattern=None):
        self._pattern = pattern

    def GetCurrentPattern(self, _pid):
        if self._pattern is None:
            raise KeyError(_pid)
        return self._pattern


def test_the_current_selection_label_comes_from_the_container():
    label = uia._current_selection_label(
        _Element(), _CachedPatternHost(_Pattern(["中"])), _UIA
    )
    assert label == "中"


def test_the_probe_path_falls_back_to_the_live_pattern():
    """The signature cache does not request the selection pattern."""
    label = uia._current_selection_label(
        _Element(_Pattern(["高"])), _CachedPatternHost(None), _UIA
    )
    assert label == "高"


def test_an_unreadable_selection_is_not_a_failure():
    assert uia._current_selection_label(_Element(), _CachedPatternHost(None), _UIA) is None


def test_a_selection_container_is_recognised_by_role_or_type():
    class Cached:
        CachedControlType = 50003

    assert uia._is_selection_container("combobox", Cached()) is True
    assert uia._is_selection_container("select", None) is True
    assert uia._is_selection_container("list", Cached()) is True
    assert uia._is_selection_container("text", None) is False


# ---------------------------------------------------------------------------
# 4. AutomationId fallback corroboration
# ---------------------------------------------------------------------------


class _Found:
    def __init__(self, control_type, class_name=None):
        self.CurrentControlType = control_type
        self.CurrentClassName = class_name


def test_an_automation_id_hit_with_the_wrong_control_type_is_not_trusted():
    ref = NativeRef(window_id="w1", automation_id="row", control_type="button")
    assert uia._ref_matches_element(_Found(50020), ref) is False  # text
    assert uia._ref_matches_element(_Found(50000), ref) is True   # button


def test_a_matching_class_name_corroborates_even_if_the_type_differs():
    ref = NativeRef(
        window_id="w1", automation_id="row", control_type="button", class_name="GoBtn"
    )
    assert uia._ref_matches_element(_Found(50020, "GoBtn"), ref) is True
    assert uia._ref_matches_element(_Found(50020, "Other"), ref) is False


def test_a_ref_without_comparable_fields_is_accepted():
    ref = NativeRef(window_id="w1", automation_id="row")
    assert uia._ref_matches_element(_Found(50020), ref) is True


# ---------------------------------------------------------------------------
# 5. select read-back message on total failure
# ---------------------------------------------------------------------------


def test_the_last_route_message_is_kept_on_total_failure():
    """A chain where every route is refused names what the last one answered."""
    from somnus.ir.node import RawTree, WindowInfo

    class SelectGhost(MockProvider):
        def execute(self, ref, primitive, value=None, timeout_s=3.0):
            if primitive in ("select", "activate"):
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message=(
                        "SelectionItemPattern.Select returned but the element "
                        "did not become selected"
                    ),
                    unsupported=True,
                )
            return super().execute(ref, primitive, value, timeout_s)

    combo = _n(
        "w1",
        (0,),
        "select",
        name="优先级",
        patterns=frozenset({"expandcollapse", "selection"}),
        children=[
            _n("w1", (0, i), "listitem", label,
               patterns=frozenset({"selectionitem", "invoke"}))
            for i, label in enumerate(("低", "中", "高"))
        ],
    )
    tree = RawTree(
        window=WindowInfo(window_id="w1", title="form", process="demo.exe", pid=7, active=True),
        root=_n("w1", (), "pane", name="form", children=[combo]),
        raw_revision=1,
    )
    session = Session(SelectGhost(fixtures=[tree]))
    session.observe()
    row = next(
        n for n in session.result.root.walk()
        if n.role == "listitem" and n.name == "中"
    )
    out = session.dispatch(f"CALL {row.ref}.select()")
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    assert "did not become selected" in out, out
