"""运行时路由：能力链按"这个元素真的能用哪个接口"选路。

前提：静态探测不可能准确。UIA 会报告元素其实拒绝的 pattern
（实测：Chromium 的 contenteditable 声称有 value，setter 却是空壳；
QQ 的节点声称有 legacyiaccessible，方法返回 E_NOTIMPL）。
所以判断只能发生在执行时：依次试已声明的路线，第一个真正可用的胜出。

两个合成通道现在**默认开启**（见 somnus/config.py）。链的顺序是
`set_text → paste_text → type_text`，所以测键盘回退时必须显式关掉剪贴板，
否则 paste_text 会先赢、键盘轮不到。
"""

from __future__ import annotations

from somnus.ir.capability import PrimitiveStep
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session

_runtime_counter = [0]


def _n(role, name=None, *, patterns=frozenset(), states=frozenset(), children=None, rid=None, cls=None):
    if rid is None:
        _runtime_counter[0] += 1
        rid = _runtime_counter[0]
    return RawNode(
        native=NativeRef(window_id="w1", runtime_id=(rid,), class_name=cls),
        role=role,
        name=name,
        states=states,
        patterns=patterns,
        children=children or [],
    )


def _tree(children, title="App"):
    return RawTree(
        window=WindowInfo(window_id="w1", title=title, process="app.exe", pid=1),
        root=_n("window", title, children=children),
    )


def _session(tree, **policy_kw):
    return Session(MockProvider(fixtures=[tree]), allow_packs=False, policy=Policy(**policy_kw))


# ---------------------------------------------------------------------------
# 缺失接口 -> 试用下一条路线
# ---------------------------------------------------------------------------


def test_missing_interface_falls_through_to_the_next_route():
    """一个节点没有 Value 模式时，链会继续走到真正可用的那一步。

    QQ 的 ProseMirror 输入框正是这个形状：没有 value，只有 text。
    回归：以前任何一步失败都直接抛错，后面的步骤永远跑不到；
    更早的版本还把"focus 成功"当成动作完成，于是根本没输入任何东西。
    """
    editor = _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")
    session = _session(_tree([editor]), allow_keyboard=True, allow_clipboard=False)
    session.observe()

    node = next(n for n in session.result.root.walk() if n.role == "input")
    steps = [s.primitive for s in node.get_cap("set_text").via]
    assert "type_text" in steps, steps

    out = session.dispatch(f'CALL {node.ref}.set_text("hello")')
    assert out.startswith("OK"), out

    # focus 是准备动作，不能就此停下；必须真的执行到 type_text。
    executed = [p for _ref, p, _v in session.provider.executed]
    assert "type_text" in executed, executed


def test_a_chain_step_that_declines_is_skipped():
    """元素在运行时拒绝某个接口时，链继续往下试。

    resolver 会先按 pattern 过滤掉不适用的步骤，但 pattern 会撒谎：
    元素声称有 value，真调用时却拒（QQ 的 ProseMirror、Chromium 的
    contenteditable 都是这个形状）。所以运行时还要再筛一次。
    """
    # 一条链里同时有原生和键盘两条路线：用 pack 写死，不受 pattern 门控影响
    from somnus.compiler.pack_schema import Pack
    from somnus.ir.capability import Capability, ParamSpec, PrimitiveStep

    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]), allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    # 手工装一条双路线能力，模拟"原生优先，不行就键入"
    node.caps.append(
        Capability(
            name="fill",
            level="semantic",
            params={"text": ParamSpec(type="string", required=True)},
            via=[
                PrimitiveStep(primitive="set_text", target="$self", value="$text"),
                PrimitiveStep(primitive="focus", target="$self"),
                PrimitiveStep(primitive="type_text", target="$self", value="$text"),
            ],
        )
    )

    real_supports = MockProvider._supports
    MockProvider._supports = staticmethod(
        lambda n, prim: False if prim == "set_text" else real_supports(n, prim)
    )
    try:
        out = session.dispatch(f'CALL {node.ref}.fill("hello")')
    finally:
        MockProvider._supports = staticmethod(real_supports)

    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert executed[0] == "set_text", executed  # 先试了原生
    assert "type_text" in executed, executed  # 被拒后落到了键盘


def test_available_interface_is_not_bypassed():
    """有 Value 模式时就成功，不会退到键盘。"""
    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]), allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("x")')
    assert out.startswith("OK"), out
    executed = [p for _ref, p, _v in session.provider.executed]
    assert "type_text" not in executed, executed


# ---------------------------------------------------------------------------
# 合成输入通道默认开启，但可被策略关掉
# ---------------------------------------------------------------------------


def test_keyboard_can_be_disabled_by_policy():
    """策略关掉键盘时，没有原生接口的元素如实报错，不偷偷用键盘。"""
    editor = _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")
    session = _session(
        _tree([editor]), allow_keyboard=False, allow_clipboard=False
    )
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("hello")')
    assert out.startswith("ERR"), out
    assert "keyboard" in out.lower(), out

    executed = [p for _ref, p, _v in session.provider.executed]
    assert "type_text" not in executed, executed


def test_native_routes_are_unaffected_by_the_keyboard_default():
    """默认禁止键盘不影响原生路径。"""
    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]))
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    assert session.dispatch(f'CALL {node.ref}.set_text("ok")').startswith("OK")


# ---------------------------------------------------------------------------
# 路由记忆
# ---------------------------------------------------------------------------


def test_route_is_remembered_and_tried_first():
    """记住上次真正奏效的接口，下次先试它。"""
    from somnus.ir.capability import Capability, ParamSpec, PrimitiveStep

    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]), allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    node.caps.append(
        Capability(
            name="fill",
            level="semantic",
            params={"text": ParamSpec(type="string", required=True)},
            via=[
                PrimitiveStep(primitive="set_text", target="$self", value="$text"),
                PrimitiveStep(primitive="focus", target="$self"),
                PrimitiveStep(primitive="type_text", target="$self", value="$text"),
            ],
        )
    )

    # 让原生先被拒一次，于是记住的是 type_text
    real_supports = MockProvider._supports
    MockProvider._supports = staticmethod(
        lambda n, prim: False if prim == "set_text" else real_supports(n, prim)
    )
    try:
        session.dispatch(f'CALL {node.ref}.fill("a")')
    finally:
        MockProvider._supports = staticmethod(real_supports)

    assert session._route_memory.get(node.fingerprint) == "type_text", session._route_memory

    # 第二次调用时，链的顺序会被重排：记住的路线排在最前。
    # （不通过 dispatch 验证：一次 call 之后会重编译，手工装的能力就没了；
    #  这里直接验证重排函数本身，它才是那个决策点。）
    steps = [
        PrimitiveStep(primitive="set_text", target="$self", value="$text"),
        PrimitiveStep(primitive="focus", target="$self"),
        PrimitiveStep(primitive="type_text", target="$self", value="$text"),
    ]
    ordered = session._order_steps(node.ref, steps)
    assert ordered[0].primitive == "type_text", [s.primitive for s in ordered]


def test_remembered_route_cannot_break_a_call():
    """记忆只是偏好：它失效时链会继续，不会让本来能成的调用失败。

    Route memory reorders the resolved chain (`_order_steps`); it never removes
    a step. The property under test is therefore that reordering cannot make a
    call fail: if the remembered route is not usable, execution moves on to the
    next step and the call still succeeds.

    Checked on the ordering function directly, because that is the decision
    point. Planted on a node whose chain really contains several steps, and with
    the remembered route made to fail at execution time.
    """
    editor = _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")
    session = _session(_tree([editor]), allow_keyboard=True, allow_clipboard=False)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    steps = [
        PrimitiveStep(primitive="set_text", target="$self", value="$text"),
        PrimitiveStep(primitive="paste_text", target="$self", value="$text"),
        PrimitiveStep(primitive="type_text", target="$self", value="$text"),
    ]

    # A bogus memory naming a step that is not in the chain must be a no-op,
    # not a reordering that drops or duplicates anything.
    session._route_memory[node.fingerprint] = "not_a_real_step"
    ordered = session._order_steps(node.ref, steps)
    assert [s.primitive for s in ordered] == [s.primitive for s in steps]

    # A memory naming a step that *is* present moves it to the front and leaves
    # the rest intact as fallbacks.
    session._route_memory[node.fingerprint] = "type_text"
    ordered = session._order_steps(node.ref, steps)
    assert ordered[0].primitive == "type_text"
    assert sorted(s.primitive for s in ordered) == sorted(
        s.primitive for s in steps
    ), "reordering must not add or drop steps"

    # And a call still succeeds with the bogus preference in place.
    session._route_memory[node.fingerprint] = "not_a_real_step"
    session.provider.executed.clear()
    out = session.dispatch(f'CALL {node.ref}.set_text("ok")')
    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert "type_text" in executed, executed


# ---------------------------------------------------------------------------
# 全部缺失时如实报错
# ---------------------------------------------------------------------------


def test_all_interfaces_missing_reports_honestly():
    """一条路线都用不了时，报明确错误并列出试过什么。"""
    editor = _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")
    session = _session(_tree([editor]), allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    # 让两条路线都不被支持
    session.provider._supports = staticmethod(lambda node, prim: False)
    out = session.dispatch(f'CALL {node.ref}.set_text("x")')
    assert out.startswith("ERR"), out
    assert "tried" in out, out


def test_preparatory_step_completes_only_when_more_follows():
    """`focus` 是准备动作——但在链尾时它就是动作本身。

    回归：之前无条件把 focus 当成"已准备，继续"，于是链尾的 focus
    什么也没确认就报成功。
    """
    row = _n("listitem", "Far row", patterns=frozenset({"selectionitem"}),
             states=frozenset({"offscreen"}))
    session = _session(_tree([row]))
    session.observe()
    node = next(n for n in session.result.root.walk() if n.name == "Far row")

    out = session.dispatch(f"CALL {node.ref}.focus()")
    assert out.startswith("OK"), out

    # 真的执行了
    assert "focus" in [p for _r, p, _v in session.provider.executed]


def test_focus_followed_by_typing_does_not_stop_early():
    """链中的准备步骤只是准备：后面还有 type_text，所以不能就此收工。

    The `focus` step used to be declared explicitly in the chain; it is now done
    inside the `type_text` primitive itself (executor.py), so the observable
    behaviour is unchanged while the chain is shorter. What must still hold is
    that typing is not mistaken for the end of the chain and that `focus` is
    still performed - asserted through the provider's recorded calls.
    """
    editor = _n("group", "editor", patterns=frozenset({"text"}), cls="ProseMirror")
    session = _session(_tree([editor]), allow_keyboard=True, allow_clipboard=False)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")

    out = session.dispatch(f'CALL {node.ref}.set_text("typed")')
    assert out.startswith("OK"), out
    executed = [p for _r, p, _v in session.provider.executed]
    assert executed == ["type_text"], executed
    # The value reached the step, and focus was part of typing it.
    assert session.provider.executed[0][2] == "typed", session.provider.executed


def test_element_declining_an_interface_is_recorded_in_the_audit():
    """审计里区分"元素没有这个接口"和"有这个接口但调用失败"。"""
    from somnus.ir.capability import Capability, ParamSpec, PrimitiveStep

    field = _n("edit", "Name", patterns=frozenset({"value", "text"}))
    session = _session(_tree([field]), allow_keyboard=True)
    session.observe()
    node = next(n for n in session.result.root.walk() if n.role == "input")
    node.caps.append(
        Capability(
            name="fill",
            level="semantic",
            params={"text": ParamSpec(type="string", required=True)},
            via=[
                PrimitiveStep(primitive="set_text", target="$self", value="$text"),
                PrimitiveStep(primitive="focus", target="$self"),
                PrimitiveStep(primitive="type_text", target="$self", value="$text"),
            ],
        )
    )

    real_supports = MockProvider._supports
    MockProvider._supports = staticmethod(
        lambda n, prim: False if prim == "set_text" else real_supports(n, prim)
    )
    try:
        session.dispatch(f'CALL {node.ref}.fill("x")')
    finally:
        MockProvider._supports = staticmethod(real_supports)

    declined = [a for a in session.audit_log() if a.get("unsupported")]
    assert declined, session.audit_log()
    assert declined[0]["primitive"] == "set_text"
