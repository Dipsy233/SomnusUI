"""Preflight: state checks cost nothing, and the wait only runs when blocked."""

from __future__ import annotations

import pytest

from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime import preflight
from somnus.runtime.session import Session

_BUTTON = frozenset({"invoke"})
_VALUE = frozenset({"value", "text"})


def _node(
    role: str,
    *,
    runtime_id: int,
    name: str | None = None,
    value: str | None = None,
    patterns: frozenset[str] = frozenset(),
    states: frozenset[str] = frozenset(),
) -> RawNode:
    return RawNode(
        native=NativeRef(window_id="w1", runtime_id=(runtime_id,)),
        role=role,
        name=name,
        value=value,
        patterns=patterns,
        states=states,
    )


def _tree(*children: RawNode) -> RawTree:
    root = RawNode(
        native=NativeRef(window_id="w1", runtime_id=(1,)),
        role="pane",
        name="Main",
        children=list(children),
    )
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="Main", process="app.exe", pid=1, active=True
        ),
        root=root,
    )


def _compiled(*children: RawNode):
    provider = MockProvider(fixtures=[_tree(*children)])
    session = Session(provider)
    session.observe()
    return session


# ---------------------------------------------------------------------------
# the pure check
# ---------------------------------------------------------------------------


def test_a_disabled_control_is_blocked_and_waitable():
    session = _compiled(_node("button", runtime_id=2, patterns=_BUTTON, states=frozenset({"disabled"})))
    node = session.result.find("b1")
    result = preflight.check(node, "activate")
    assert not result.ok
    assert result.state == "disabled"
    assert result.waitable


def test_read_and_focus_are_never_blocked_by_a_state():
    session = _compiled(
        _node("input", runtime_id=2, patterns=_VALUE, value="x", states=frozenset({"disabled"}))
    )
    node = session.result.find("q1")
    for capability in preflight.NON_ACTING_CAPS:
        assert preflight.check(node, capability).ok, capability


def test_a_capability_the_node_does_not_declare_is_left_to_the_resolver():
    session = _compiled(_node("button", runtime_id=2, patterns=_BUTTON, states=frozenset({"disabled"})))
    node = session.result.find("b1")
    # `add` is not on this node; the resolver's CAPABILITY_NOT_FOUND hint is
    # the useful answer, not a state error.
    assert preflight.check(node, "add").ok


def test_a_readonly_field_refuses_only_a_write():
    """The read-only case is a pack-granted write; the compiled INPUT has no
    ``set_text`` at all, so this pins the preflight matrix itself."""
    from somnus.ir.capability import Capability
    from somnus.ir.node import SemanticNode

    node = SemanticNode(
        ref="q1",
        role="input",
        states=frozenset({"readonly"}),
        caps=[
            Capability(name="set_text", level="primitive"),
            Capability(name="focus", level="primitive"),
        ],
    )
    assert preflight.check(node, "focus").ok
    blocked = preflight.check(node, "set_text")
    assert not blocked.ok
    assert blocked.state == "readonly"
    assert not blocked.waitable


# ---------------------------------------------------------------------------
# the session path: historical refusal, bounded wait, structured failure
# ---------------------------------------------------------------------------


def test_a_disabled_control_without_a_signature_probe_is_refused_at_once():
    """The replay provider cannot observe a change, so it keeps TARGET_DISABLED."""
    session = _compiled(
        _node("button", runtime_id=2, patterns=_BUTTON, states=frozenset({"disabled"}))
    )
    out = session.dispatch("CALL b1.activate()")
    assert out.startswith("ERR TARGET_DISABLED"), out


class _Transient(MockProvider):
    """A provider whose cheap probe clears the disabled state on its first call."""

    is_deterministic = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.probes = 0

    def snapshot_signature(self, window_id: str) -> RawTree:
        self.probes += 1
        tree = self._trees[window_id]
        for node in tree.root.walk():
            if "disabled" in node.states:
                node.states = frozenset(s for s in node.states if s != "disabled")
        return tree


def test_a_briefly_disabled_control_is_waited_for_and_then_works():
    tree = _tree(_node("button", runtime_id=2, name="提交", patterns=_BUTTON, states=frozenset({"disabled"})))
    provider = _Transient(fixtures=[tree])
    session = Session(provider)
    session.actionable_timeout_s = 0.2
    session.observe()
    out = session.dispatch("CALL b1.activate()")
    assert out.startswith("OK"), out
    assert provider.probes >= 1


class _NeverClears(MockProvider):
    """Signature never changes, so the state never clears."""

    is_deterministic = False

    def snapshot_signature(self, window_id: str) -> RawTree:
        return self._trees[window_id]


def test_a_state_that_does_not_clear_becomes_not_actionable():
    tree = _tree(_node("button", runtime_id=2, patterns=_BUTTON, states=frozenset({"disabled"})))
    session = Session(_NeverClears(fixtures=[tree]))
    session.actionable_timeout_s = 0.08
    session.settler.poll_interval_ms = 5
    session.observe()
    out = session.dispatch("CALL b1.activate()")
    assert out.startswith("ERR NOT_ACTIONABLE"), out
    assert "state=disabled" in out
    assert "waited=" in out


def test_the_happy_path_never_probes():
    """The check reads compiled states; an actionable control costs no read.

    Asserted on the preflight entry itself, because a call's settle *does*
    legitimately use the signature probe afterwards.
    """

    class _Counting(MockProvider):
        is_deterministic = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.probes = 0

        def snapshot_signature(self, window_id: str) -> RawTree:
            self.probes += 1
            return self._trees[window_id]

    from somnus.sui.parse import parse_action

    tree = _tree(_node("button", runtime_id=2, name="可用", patterns=_BUTTON))
    provider = _Counting(fixtures=[tree])
    session = Session(provider)
    session.observe()
    session._preflight(parse_action("CALL b1.activate()"))
    assert provider.probes == 0


def test_a_batch_refuses_a_disabled_step_before_anything_runs():
    """Batch state checks are free: nothing has run, so no wait is spent."""
    from somnus.errors import ErrorCode, SomnusError
    from somnus.sui.parse import parse_call_block

    tree = _tree(
        _node("button", runtime_id=2, name="可用", patterns=_BUTTON),
        _node(
            "button",
            runtime_id=3,
            name="禁用",
            patterns=_BUTTON,
            states=frozenset({"disabled"}),
        ),
    )
    provider = MockProvider(fixtures=[tree])
    session = Session(provider)
    session.observe()
    with pytest.raises(SomnusError) as exc:
        session.call_batch(parse_call_block("CALL b1.activate()\nCALL b2.activate()"))
    assert exc.value.code == ErrorCode.TARGET_DISABLED
    assert provider.executed == []


def test_read_still_answers_on_a_disabled_field():
    session = _compiled(
        _node("input", runtime_id=2, patterns=_VALUE, value="只读内容", states=frozenset({"disabled"}))
    )
    out = session.dispatch("CALL q1.read()")
    assert out.startswith("OK"), out
