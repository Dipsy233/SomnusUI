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


def _tie_tree(*, variant: str, second_patterns=frozenset({"expandcollapse"})) -> RawTree:
    """Two same-named buttons under structurally identical groups.

    The registry's structural score cannot tell them apart (same role, name,
    depth, role-index and ancestor shape), which is exactly the tie the
    locator is for. Only one of them exposes `activate`.
    """
    wid = "w1"
    left_id = "seven_v2" if variant == "changed" else "seven"
    body = [
        _n(
            wid,
            (0,),
            "group",
            "数字键盘",
            children=[
                _n(wid, (0, 0), "button", "七", patterns=_BUTTON,
                   automation_id=left_id),
            ],
        ),
        _n(
            wid,
            (1,),
            "group",
            "数字键盘",
            children=[
                _n(wid, (1, 0), "button", "七", patterns=second_patterns,
                   automation_id="seven_toggle"),
            ],
        ),
    ]
    return RawTree(
        window=WindowInfo(
            window_id=wid,
            title="计算器",
            process="CalculatorApp.exe",
            pid=1,
            active=True,
        ),
        root=_n(wid, (), "window", "计算器", children=body),
    )



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


def test_the_locator_breaks_a_structural_tie_with_the_capability():
    """Second strategy: the registry tied, the caller's capability decided."""
    session = _session(_tie_tree(variant="standard"))
    old_ref = _ref(session, "七")

    session.provider._trees["w1"] = copy.deepcopy(_tie_tree(variant="changed"))
    session._recompile()

    # The registry really cannot pick: both buttons share role/name/depth/index
    # and the same ancestor shape.
    assert session.registry.relocate(old_ref, session.result) is None

    out = session.dispatch(f"CALL {old_ref}.activate()")
    assert out.startswith("OK"), out
    assert "~ healed " in out and "via locator" in out, out
    assert "cap=activate" in out, out
    new_ref = next(
        n.ref for n in session.result.root.walk()
        if (n.name or "") == "七" and "activate" in n.cap_names()
    )
    assert new_ref != old_ref
    assert session.provider.executed[-1][0] == session.result.sources[new_ref].native


def test_a_tie_the_locator_cannot_break_keeps_stale_target_with_candidates():
    """Two invokable lookalikes: no guess, but the candidates are named."""
    session = _session(_tie_tree(variant="standard", second_patterns=_BUTTON))
    old_ref = _ref(session, "七")

    session.provider._trees["w1"] = copy.deepcopy(
        _tie_tree(variant="changed", second_patterns=_BUTTON)
    )
    session._recompile()

    out = session.dispatch(f"CALL {old_ref}.activate()")
    assert out.startswith("ERR STALE_TARGET"), out
    assert "candidates:" in out, out
    assert session.provider.executed == [], "a tie must not silently retarget"
