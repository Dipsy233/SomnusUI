"""P2-4: a settled action that visibly changed the UI is `OK`, not UNVERIFIED.

The old rule only flattened ``OK_UNVERIFIED`` to ``OK`` when the delta was
*empty*; a change without declared effects stayed UNVERIFIED. That is the
"semantic inflation" behind agents retrying writes and toggles that had already
succeeded: the UI moved, but the status word told them it did not.

The evidence available at the verifier is the delta (the settled transition).
These pin:
* non-empty delta -> OK;
* empty delta -> OK_UNVERIFIED (still honest: "ran and nothing moved").
"""

from __future__ import annotations

from somnus.providers.mock import MockProvider
from somnus.runtime.delta import Delta
from somnus.runtime.session import Session
from somnus.runtime.verifier import Verifier
from tests.fixtures.builders import notepad_tree


def test_no_effects_but_a_visible_change_is_ok():
    verifier = Verifier(by_ref={}, by_entity={})
    delta = Delta(changed=[("q1", "value", "", "hello")])
    assert verifier.verify([], "q1", delta).status == "OK"


def test_no_effects_and_no_change_stays_unverified():
    verifier = Verifier(by_ref={}, by_entity={})
    assert verifier.verify([], "q1", Delta()).status == "OK_UNVERIFIED"


def test_a_declared_effect_that_cannot_be_evaluated_falls_back_to_the_delta():
    verifier = Verifier(by_ref={}, by_entity={})
    from somnus.ir.capability import Effect

    effect = Effect(target="ghost", field="value", op="=", value="x", raw="ghost.value = x")
    assert verifier.verify([effect], "q1", Delta()).status == "OK_UNVERIFIED"
    delta = Delta(changed=[("q1", "value", "", "hello")])
    assert verifier.verify([effect], "q1", delta).status == "OK"


def test_a_write_that_changed_the_value_is_reported_as_plain_ok():
    """End-to-end: set_text has no declared effects, but the value moved."""
    session = Session(MockProvider(fixtures=[notepad_tree()]))
    session.observe()
    box = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "set_text" in n.cap_names()
    )
    out = session.dispatch(f'CALL {box.ref}.set_text("hello")')
    assert out.startswith("OK "), out
    assert "OK_UNVERIFIED" not in out
    assert "value" in out, "the changed value must still be shown in the delta"


def test_a_settled_no_op_write_stays_ok_through_the_session_rule():
    """Writing the same value changes nothing; the session's own rule still
    reports OK (settled + empty delta), and this change must not disturb it."""
    session = Session(MockProvider(fixtures=[notepad_tree(text="same")]))
    session.observe()
    box = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "set_text" in n.cap_names()
    )
    out = session.dispatch(f'CALL {box.ref}.set_text("same")')
    assert out.startswith("OK"), out
