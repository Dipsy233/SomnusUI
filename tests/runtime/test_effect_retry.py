"""P2-7 (second half): EFFECT_NOT_CONFIRMED retries the next route once.

A route can report success and leave the declared effect absent - a Chromium
editor's Value setter is the measured case: it returns success and writes
nothing. The next untried route on the same element is the honest second
attempt. One only, never a loop.
"""

from __future__ import annotations

from somnus.ir.capability import Capability, Effect, PrimitiveStep
from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider, raw_node_to_dict
from somnus.runtime.session import Session
from tests.fixtures.builders import _n


def _tree():
    row = _n("w1", (0, 0), "listitem", "Row 0")
    rows = _n(
        "w1",
        (0,),
        "list",
        "Rows",
        patterns=frozenset({"scroll", "value"}),
        children=[row],
    )
    root = _n("w1", (), "window", "App", children=[rows])
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="App", process="demo.exe", pid=1, active=True
        ),
        root=root,
    )


_NEW_ROW = _n("w1", (0, 1), "listitem", "Row 1")


def _provider(*, mutating: tuple[str, ...] = ("type_text",)) -> MockProvider:
    """Every route succeeds; only ``mutating`` actually changes the tree."""
    scripts = {
        name: [{"op": "set_name", "to": "Rows"}]  # a successful no-op
        for name in ("paste_text", "type_text", "set_text")
    }
    for name in mutating:
        scripts[name] = [{"op": "add_child", "child": raw_node_to_dict(_NEW_ROW)}]
    return MockProvider(fixtures=[_tree()], scripts=scripts)


def _install(session: Session, steps: list[str]) -> str:
    ref = next(n.ref for n in session.result.root.walk() if n.role == "list")
    node = session.result.find(ref)
    node.caps.append(
        Capability(
            name="add_row",
            level="semantic",
            via=[
                PrimitiveStep(primitive=name, target="$self", value="x")
                for name in steps
            ],
            effects=[
                Effect(target="$self", field="count", op="+=", value=1, raw="count += 1")
            ],
        )
    )
    return ref


def test_an_unconfirmed_effect_retries_the_next_route_and_succeeds():
    provider = _provider()
    session = Session(provider, allow_packs=False)
    session.observe()
    ref = _install(session, ["paste_text", "type_text"])

    out = session.dispatch(f"CALL {ref}.add_row()")
    assert out.startswith("OK"), out
    assert "~ retried via type_text" in out, out
    assert [primitive for _r, primitive, _v in provider.executed] == [
        "paste_text",
        "type_text",
    ]


def test_only_one_retry_happens_even_when_a_third_route_exists():
    # Only the third route would change the tree; the retry stops at the second.
    provider = _provider(mutating=("set_text",))
    session = Session(provider, allow_packs=False)
    session.observe()
    ref = _install(session, ["paste_text", "type_text", "set_text"])

    out = session.dispatch(f"CALL {ref}.add_row()")
    assert out.startswith("ERR EFFECT_NOT_CONFIRMED"), out
    # The second route did not confirm either; the third is never reached.
    assert [primitive for _r, primitive, _v in provider.executed] == [
        "paste_text",
        "type_text",
    ]
    rows = next(n for n in session.provider._trees["w1"].root.walk() if n.role == "list")
    assert len(rows.children) == 1, "the third route must not have run"


def test_a_capability_with_no_second_route_keeps_the_original_error():
    provider = _provider(mutating=())
    session = Session(provider, allow_packs=False)
    session.observe()
    ref = _install(session, ["paste_text"])

    out = session.dispatch(f"CALL {ref}.add_row()")
    assert out.startswith("ERR EFFECT_NOT_CONFIRMED"), out
    assert [primitive for _r, primitive, _v in provider.executed] == ["paste_text"]
