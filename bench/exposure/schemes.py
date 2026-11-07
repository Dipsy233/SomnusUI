"""The seven hierarchy schemes of benchmark round 1.

A scheme says which verbs are exposed as top-level tools and which are folded
into another tool as parameters. It is pure data: :mod:`bench.exposure.assemble`
turns it into tool specs + a system prompt, :mod:`bench.exposure.dispatch`
routes a call on the folded surface back to the flat :func:`call_tool`.

Routers (the fold implementations live in ``dispatch.py``):

* ``look``   — one observation tool: default overview, ``query`` = find,
  ``ref`` = inspect.
* ``act``    — one action tool: ``kind=invoke`` (default) = CALL lines,
  ``kind=run`` = a CALL/AWAIT script, ``kind=launch`` = start a program.
* ``look_help`` / ``act_switch_configure`` — the H4 extremes, where help
  folds into the look tool and switch/configure fold into the action tool.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scheme:
    """One exposure scheme: a list of exposed tools and how they dispatch."""

    id: str
    label: str
    #: exposed name -> base ``TOOL_SPECS`` name, dispatched unchanged
    flat: tuple[tuple[str, str], ...]
    #: exposed name -> router key (a merged tool)
    merged: tuple[tuple[str, str], ...]
    #: why this shape is plausible; goes into the report verbatim
    note: str
    #: description overrides for renamed tools (exposed name -> text); a
    #: renamed tool should not carry the old verb's wording
    desc_overrides: tuple[tuple[str, str], ...] = ()
    #: word-boundary renames applied to the prompt body so rules and error
    #: tables keep referring to the exposed names
    body_renames: tuple[tuple[str, str], ...] = ()
    #: expose the default-action capability under the pattern's own name
    #: (`invoke`) instead of the intent alias (`activate`) — a runtime
    #: compiler switch (somnus.compiler.caps.NATIVE_CAP_NAMES), applied per
    #: scheme by the runner
    native_caps: bool = False
    #: staged exposure: the tool list (and the prompt's Tools table) starts
    #: with these names; calling `help` reveals the full set. Empty = static
    #: full exposure (every scheme above).
    staged_initial: tuple[str, ...] = ()


ALL_9 = (
    "observe",
    "find",
    "inspect",
    "call",
    "run",
    "launch",
    "switch",
    "configure",
    "help",
)

SCHEMES: dict[str, Scheme] = {
    "H0": Scheme(
        id="H0",
        label="平铺基线(现状)",
        flat=tuple((n, n) for n in ALL_9),
        merged=(),
        note="九个工具全部平铺,当前线上形态,对照原点。",
    ),
    "H1": Scheme(
        id="H1",
        label="观察合并",
        flat=(
            ("call", "call"),
            ("run", "run"),
            ("launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(("observe", "look"),),
        note="find/inspect 折进 observe 的 query/ref 参数:观察类意图同源,"
        "少两个 schema,用户例 1。",
    ),
    "H2": Scheme(
        id="H2",
        label="动作合并",
        flat=(
            ("observe", "observe"),
            ("find", "find"),
            ("inspect", "inspect"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(("call", "act"),),
        note="run/launch 折进 call 的 kind 子动作:动作类一个入口,用户例 2。",
    ),
    "H3": Scheme(
        id="H3",
        label="看/动二分",
        flat=(
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(("look", "look"), ("do", "act")),
        note="每个意图类一个工具:look(observe+find+inspect)、"
        "do(call+run+launch),顶级最少且对称。",
    ),
    "H4": Scheme(
        id="H4",
        label="极简二元",
        flat=(),
        merged=(("look", "look_help"), ("do", "act_switch_configure")),
        note="只有 look/do 两个工具:help 折进 look 的 topic,switch/configure"
        "折进 do 的 target/configure 参数。极端点,测最少工具面的下限。",
    ),
    "H5": Scheme(
        id="H5",
        label="命名空间平铺",
        flat=(
            ("look.observe", "observe"),
            ("look.find", "find"),
            ("look.inspect", "inspect"),
            ("call.invoke", "call"),
            ("call.run", "run"),
            ("call.launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(),
        note="不折叠 dispatch,只把层级表达在名字里(look.*/call.*):"
        "测层级写在名字里与折进参数哪个更好。",
    ),
    "H5u": Scheme(
        id="H5u",
        label="命名空间平铺(下划线)",
        flat=(
            ("look_observe", "observe"),
            ("look_find", "find"),
            ("look_inspect", "inspect"),
            ("call_invoke", "call"),
            ("call_run", "run"),
            ("call_launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(),
        note="H5 的下划线变体:语义相同,但端点的 OpenAI 兼容上游拒绝带点号"
        "的工具名(H5 全部 502),层级只能用下划线表达。",
    ),
    "H6": Scheme(
        id="H6",
        label="观察合并+动作合并",
        flat=(
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(("observe", "look"), ("call", "act")),
        note="H1+H2 叠加但保留 help/configure/switch 独立:验证两个方向的"
        "收益是否可加。",
    ),
    # ---- 第 2 批:命名(层级固定为已证最优的 H0,只改动词名) ----
    "N1": Scheme(
        id="N1",
        label="能力名不做别名(activate→invoke)",
        flat=tuple((n, n) for n in ALL_9),
        merged=(),
        note="控件原生暴露 Invoke 模式的,能力就叫 invoke,不再别名成 "
        "activate——工具面与层级不动,只取消默认动作能力名上的别名。",
        native_caps=True,
        body_renames=(("activate", "invoke"),),
    ),
    "N2": Scheme(
        id="N2",
        label="call→act(意图动词)",
        flat=(
            ("observe", "observe"),
            ("find", "find"),
            ("inspect", "inspect"),
            ("act", "call"),
            ("run", "run"),
            ("launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(),
        note="工具名说意图(对这个界面做一件事),不暴露底层机制。",
        desc_overrides=(
            (
                "act",
                "Act on a node's capability; ≤20 CALL lines run as one "
                "batch. Use for independent actions; run for opened flows.",
            ),
        ),
    ),
    "N3": Scheme(
        id="N3",
        label="run→script(消歧命名)",
        flat=(
            ("observe", "observe"),
            ("find", "find"),
            ("inspect", "inspect"),
            ("call", "call"),
            ("script", "run"),
            ("launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(),
        note="`run` 与 `launch` 都读作『运行程序』,改名 script 消歧:"
        "它吃的是一段 CALL/AWAIT 脚本。",
        desc_overrides=(
            (
                "script",
                "Run a whole CALL/AWAIT flow in one call; AWAIT settles and "
                "binds $it. Use for opened flows; call for independent.",
            ),
        ),
    ),
    "N4": Scheme(
        id="N4",
        label="观察族口语化 look/search/peek",
        flat=(
            ("look", "observe"),
            ("search", "find"),
            ("peek", "inspect"),
            ("call", "call"),
            ("run", "run"),
            ("launch", "launch"),
            ("switch", "switch"),
            ("configure", "configure"),
            ("help", "help"),
        ),
        merged=(),
        note="观察三动词整体换口语化风格(look/search/peek),动作侧不动:"
        "测动词风格对路由的影响。",
        desc_overrides=(
            (
                "look",
                "L0 overview; assigns refs. Use first/after switch; search "
                "for a name. Returns @rev, WINDOWS, tree (`?` unreadable).",
            ),
            (
                "search",
                "Search current window incl folded lists; returns entity "
                "lines. Use for a name; look for layout. FOUND N or 0.",
            ),
            (
                "peek",
                "Full node: attrs, caps, params, relations. Use when "
                "caps/args look wrong.",
            ),
        ),
        body_renames=(
            ("observe", "look"),
            ("find", "search"),
            ("inspect", "peek"),
        ),
    ),
    # ---- 第 3 批:暴露时机(层级与命名固定为 H0,只动"何时可见什么") ----
    "T1": Scheme(
        id="T1",
        label="只暴露 core 六工具",
        flat=(
            ("observe", "observe"),
            ("find", "find"),
            ("inspect", "inspect"),
            ("call", "call"),
            ("run", "run"),
            ("help", "help"),
        ),
        merged=(),
        note="drop switch/launch/configure:这批任务用不到它们,少三个 schema "
        "也少三个可误选的项——等价于线上 MCP 的 core 档。",
    ),
    "T2": Scheme(
        id="T2",
        label="最小起步,help 展开",
        flat=tuple((n, n) for n in ALL_9),
        merged=(),
        note="开局只暴露 observe/call/help,模型需要别的工具时调一次 help "
        "即见全量——测『按需暴露』的展开成本与路由收益。",
        staged_initial=("observe", "call", "help"),
    ),
}

#: Routers a merged tool can use. Keys are router ids, values documented above.
ROUTERS = ("look", "act", "look_help", "act_switch_configure")


def get_scheme(scheme_id: str) -> Scheme:
    try:
        return SCHEMES[scheme_id]
    except KeyError:
        known = ", ".join(SCHEMES)
        raise ValueError(f"unknown scheme {scheme_id!r}; known: {known}") from None
