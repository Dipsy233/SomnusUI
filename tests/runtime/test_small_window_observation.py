"""Default observation of a small UWP-shaped window must show its controls.

Diagnosis from the live calculator (offline numbers reproduced by the same
tree shape below): the problem is not offscreen filtering and not only the
provider walker. Three L0 shaping rules stack up:

* the provider skeleton stops at depth 4, while UWP buttons sit at depth 5-6;
* the L0 projection stops at depth 3;
* the L0 token/node caps (300 tokens / 40 nodes) and the auto-collapse
  threshold (8 homogeneous children) would still hide the digit pad.

The fixes are adaptive: the provider re-walks a small, untruncated skeleton
deeper in the same request, and the session gives that window a bounded deeper
budget with auto-collapse effectively off. A long/truncated window keeps L0
and the viewport rows.
"""

from __future__ import annotations

import copy

from somnus.ir.node import RawTree, WindowInfo
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.engine import ProjectionEngine
from somnus.providers.mock import MockProvider
from somnus.providers.windows import uia
from somnus.runtime.session import Session
from somnus.sui.serialize import render_document
from tests.fixtures.builders import _n

_BUTTON = frozenset({"invoke"})
_TEXT = frozenset({"text"})


def _btn(path, name):
    return _n("w1", path, "button", name, patterns=_BUTTON)


def _txt(path, name):
    return _n("w1", path, "text", name, patterns=_TEXT)


def _calc_tree() -> RawTree:
    """A calculator-shaped tree: buttons at depth 5, the digit pad homogeneous."""
    groups = [
        _n(
            "w1",
            (3, 1, 3),
            "group",
            "标准函数",
            children=[
                _btn((3, 1, 3, i), name)
                for i, name in enumerate(
                    ["百分比", "清除", "清除输入", "退格", "倒数", "平方", "平方根"]
                )
            ],
        ),
        _n(
            "w1",
            (3, 1, 4),
            "group",
            "标准运算符",
            children=[
                _btn((3, 1, 4, i), name)
                for i, name in enumerate(["除以", "乘以", "减", "加", "等于"])
            ],
        ),
        _n(
            "w1",
            (3, 1, 5),
            "group",
            "数字键盘",
            children=[
                _btn((3, 1, 5, i), name)
                for i, name in enumerate(
                    ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
                )
            ],
        ),
    ]
    nav = _n(
        "w1",
        (3,),
        "unknown",
        None,
        children=[_btn((3, 0), "打开导航"), _n("w1", (3, 1), "group", None, children=groups)],
    )
    content = _n(
        "w1",
        (2,),
        "window",
        "计算器",
        children=[_txt((2, 0), "计算器"), nav],
    )
    titlebar = _n(
        "w1",
        (1,),
        "window",
        "计算器",
        children=[
            _n("w1", (1, 0), "menu", "系统", children=[_n("w1", (1, 0, 0), "menuitem", "系统")]),
            _btn((1, 1), "最小化 计算器"),
            _btn((1, 2), "使 计算器 最大化"),
            _btn((1, 3), "关闭 计算器"),
        ],
    )
    root = _n("w1", (), "window", "计算器", children=[titlebar, content])
    return RawTree(
        window=WindowInfo(
            window_id="w1",
            title="计算器",
            process="CalculatorApp.exe",
            pid=1,
            active=True,
        ),
        root=root,
    )


class CalculatorViewport(MockProvider):
    def __init__(self, tree: RawTree, meta=None):
        super().__init__(fixtures=[tree])
        self.tree = tree
        self.viewport_meta = meta or {}
        self.viewport_calls = 0
        self.full_calls = 0

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.viewport_calls += 1
        self.last_viewport_partial = True
        return copy.deepcopy(self.tree)

    def snapshot(self, window_id, scope=None, max_depth=12):
        self.full_calls += 1
        return copy.deepcopy(self.tree)


def test_default_observe_renders_the_buttons_below_l0_depth():
    provider = CalculatorViewport(_calc_tree())
    session = Session(provider)
    obs = session.observe()

    assert session._small_observe_window == "w1"
    for name in ("七", "乘以", "等于", "清除"):
        assert f'"{name}"' in obs.text, f"{name!r} missing from default observe:\n{obs.text}"
    assert "+10 more" not in obs.text, "the digit pad must not fold into one group"
    assert obs.tokens <= 760, obs.tokens

    # The same tree under the old L0 budget: depth 3 already hides the buttons,
    # so this is the depth cap, not offscreen filtering.
    engine = ProjectionEngine(
        ObservationBudget(max_nodes=40, max_depth=3, max_tokens=300, hard_tokens=300),
        auto_collapse_threshold=8,
        l0=True,
    )
    old = render_document(engine.plan(session.result.root).lines)
    assert '"七"' not in old and '"乘以"' not in old, old


def test_a_second_observe_keeps_the_small_window_budget():
    provider = CalculatorViewport(_calc_tree())
    session = Session(provider)
    first = session.observe()
    assert '"七"' in first.text

    # The small-window read path now switches to one full snapshot; the
    # adaptive budget is a property of the window, not of the capture path.
    second = session.observe()
    assert provider.full_calls == 1, provider.full_calls
    assert '"七"' in second.text and '"乘以"' in second.text, second.text


def test_a_truncated_long_list_keeps_l0_and_the_viewport_surface():
    rows = [
        _txt((1, i), f"Item {i}")
        for i in range(40)
    ]
    tree = RawTree(
        window=WindowInfo(
            window_id="w1", title="Store", process="demo.exe", pid=1, active=True
        ),
        root=_n(
            "w1",
            (),
            "window",
            "Store",
            children=[
                _n("w1", (0,), "text", "Store"),
                _n("w1", (1,), "list", "Items", children=rows),
            ],
        ),
    )
    meta = {
        "w1": {(1,): {"items_total": 40, "items_visible": 2, "items_truncated": True}}
    }
    provider = CalculatorViewport(tree, meta=meta)
    session = Session(provider)
    obs = session.observe()

    assert session._small_observe_window is None
    assert "rows=" in obs.text or "truncated" in obs.text or "+more" in obs.text, obs.text
    assert obs.tokens <= 320, "a truncated window must stay inside the L0 cap"


def test_the_provider_only_deepens_small_untruncated_skeletons():
    provider = object.__new__(uia.UiaProvider)
    provider.viewport_meta = {}

    small = _n(
        "w1",
        (),
        "window",
        "Small",
        children=[_btn((0,), "One"), _btn((1,), "Two")],
    )
    assert provider._viewport_should_deepen(small) is True

    big = _n(
        "w1",
        (),
        "window",
        "Big",
        children=[_n("w1", (i,), "text", f"Item {i}") for i in range(130)],
    )
    assert provider._viewport_should_deepen(big) is False

    truncated = _n(
        "w1",
        (),
        "window",
        "Cut",
        children=[_btn((0,), "One")],
    )
    truncated.truncated = True
    assert provider._viewport_should_deepen(truncated) is False

    provider.viewport_meta = {"w1": {(0,): {"items_truncated": True}}}
    assert provider._viewport_should_deepen(small) is False
