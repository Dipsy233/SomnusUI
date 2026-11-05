"""STALE_TARGET relocation: one structural retry, same window, then the old error.

Live evidence: switching the calculator from standard to scientific re-parents
the digit buttons, so the old fingerprint no longer matches and `CALL b59` said
STALE_TARGET even though the same `七` button is right there. The registry now
keeps the last-seen role/name/index/depth/ancestor evidence and can relocate
once, without a silent retarget and without looping through the twin/route
retries.
"""

from __future__ import annotations

import copy

from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n

_BUTTON = frozenset({"invoke"})


def _btn(wid: str, path, name):
    return _n(wid, path, "button", name, patterns=_BUTTON)


def _tree(*, variant: str, window_id: str = "w1") -> RawTree:
    """Three layouts in which the same named button sits somewhere else."""
    wid = window_id

    def button(path, name):
        return _btn(wid, path, name)

    def group(path, name, children):
        return _n(wid, path, "group", name, children=children)

    if variant == "standard":
        body = [
            group((0,), "标准运算符", [button((0, 0), "七"), button((0, 1), "乘以")]),
            group((1,), "标准函数", [button((1, 0), "清除")]),
        ]
    elif variant == "scientific":
        body = [
            _n(
                wid,
                (0,),
                "pane",
                "科学面板",
                children=[
                    group(
                        (0, 0),
                        "数字键盘",
                        [
                            button((0, 0, i), name)
                            for i, name in enumerate(
                                ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
                            )
                        ],
                    )
                ],
            ),
            group((1,), "科学函数", [button((1, 0), "指数")]),
        ]
    elif variant == "gone":
        body = [
            group((0,), "数字键盘", [button((0, 0), "八")]),
        ]
    else:  # ambiguous: two identical shells, each with one 七
        body = [
            group((0,), "数字键盘", [button((0, 0), "七")]),
            group((1,), "数字键盘", [button((1, 0), "七")]),
        ]
    root = _n(wid, (), "window", "计算器", children=body)
    return RawTree(
        window=WindowInfo(
            window_id=wid,
            title="计算器",
            process="CalculatorApp.exe",
            pid=1,
            active=True,
        ),
        root=root,
    )


def _session(tree: RawTree) -> Session:
    provider = MockProvider(fixtures=[tree])
    session = Session(provider, allow_packs=False)
    session.observe()
    return session


def _ref(session: Session, name: str) -> str:
    node = next(n for n in session.result.root.walk() if (n.name or "") == name)
    return node.ref


def test_a_stale_ref_is_relocated_and_the_action_runs():
    session = _session(_tree(variant="standard"))
    old_ref = _ref(session, "七")

    # The layout changed and was observed (the live mode-switch path). The old
    # ref is no longer in the result, but its record still carries the locator.
    session.provider._trees["w1"] = copy.deepcopy(_tree(variant="scientific"))
    session._recompile()
    assert session.result.find(old_ref) is None

    out = session.dispatch(f"CALL {old_ref}.activate()")
    assert out.startswith("OK"), out
    assert "~ healed " in out and "(via role=button" in out, out
    assert "name='七'" in out, out

    new_ref = _ref(session, "七")
    assert new_ref != old_ref, (old_ref, new_ref)
    assert session.provider.executed, "the action must run on the relocated element"
    assert session.provider.executed[-1][0] == session.result.sources[new_ref].native


def test_a_stale_ref_with_no_candidate_keeps_the_original_error():
    session = _session(_tree(variant="standard"))
    old_ref = _ref(session, "七")

    session.provider._trees["w1"] = copy.deepcopy(_tree(variant="gone"))
    session._recompile()
    out = session.dispatch(f"CALL {old_ref}.activate()")

    assert out.startswith("ERR STALE_TARGET"), out
    assert "hint=" in out and "refind by name" in out, out
    assert session.provider.executed == [], "a failed relocation must not act"


def test_two_identical_candidates_are_not_chosen_by_guess():
    session = _session(_tree(variant="standard"))
    old_ref = _ref(session, "七")

    session.provider._trees["w1"] = copy.deepcopy(_tree(variant="ambiguous"))
    session._recompile()
    out = session.dispatch(f"CALL {old_ref}.activate()")

    assert out.startswith("ERR STALE_TARGET"), out
    assert "hint=" in out, out
    assert session.provider.executed == [], "a tie must not silently retarget"


def test_relocation_never_crosses_windows():
    """The same role/name in another window is a different element."""
    w1 = _tree(variant="standard")
    w2 = _tree(variant="scientific", window_id="w2")

    provider = MockProvider(fixtures=[w1, w2])
    session = Session(provider, allow_packs=False)
    session.observe()
    old_ref = _ref(session, "七")

    session.observe(window="w2")
    assert session.registry.relocate(old_ref, session.result) is None
