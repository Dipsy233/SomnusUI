"""LLM 控制 GUI 的完整演示（无需 Windows / 真实桌面）。

模拟一个 LLM Agent 通过 SomnusUI 的 4 个工具完成任务：
「帮我点一个巨无霸和一份薯条，然后打开购物车」。

每一步都打印 LLM *实际看到*的 SUI-Text，以及它据此发出的下一步调用，
从而展示：LLM 只看语义实体 + 能力，从不接触像素 / 坐标 / 原生 id。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from somnus.providers.mock import MockProvider
from somnus.protocol.tools import call_tool
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

RULE = "─" * 68


def banner(step: str) -> None:
    print(f"\n{RULE}\n  {step}\n{RULE}")


def as_llm(text: str) -> None:
    """LLM 收到的工具返回值。"""
    for line in text.splitlines():
        print(f"  {line}")


def llm_says(text: str) -> None:
    """LLM 发出的下一步动作（它自己决定，runtime 不参与规划）。"""
    print(f"\n  🧠 LLM: {text}")


def main() -> int:
    # 一个「LLM 宿主」：持有一个带 mock 后端的 Session。
    provider = MockProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
    )
    session = Session(provider)

    user_intent = "帮我点一个巨无霸和一份薯条，然后打开购物车"
    print(f"👤 用户: {user_intent}")
    print(
        "\n（说明：下面每一步都是 LLM 通过工具调用与 runtime 交互；\n"
        "  括号里是内部真实动作，LLM 本身看不到原生元素。）"
    )

    # ---- 第 1 步：observe（LLM 先看屏幕上有哪些能力）----------------------
    banner("① 工具调用 call_tool('observe')")
    llm_says("observe  # 先看看这个界面能做什么")
    observation = call_tool(session, "observe", {})
    as_llm(observation)

    # ---- 第 2 步：find（精确定位巨无霸）------------------------------------
    banner('② 工具调用 call_tool("find", {"query": \'FIND product "Big Mac"\'})')
    llm_says('find  FIND product "Big Mac"  # 找到巨无霸的实体 ref')
    found = call_tool(session, "find", {"query": 'FIND product "Big Mac"'})
    as_llm(found)
    llm_says("→ 得到 ref = p1，它有 +add 能力")

    # ---- 第 3 步：call p1.add() --------------------------------------------
    banner("③ 工具调用 call_tool('call', {'action': 'CALL p1.add()'})")
    llm_says("call  CALL p1.add()")
    result = call_tool(session, "call", {"action": "CALL p1.add()"})
    as_llm(result)
    llm_says("→ 购物车 count 0→1，动作被 effect 验证通过（OK）")

    # ---- 第 4 步：加薯条 ---------------------------------------------------
    banner("④ 工具调用 call_tool('call', {'action': 'CALL p2.add()'})")
    llm_says('find  FIND product "Fries"  →  得到 p2')
    as_llm(call_tool(session, "find", {"query": 'FIND product "Fries"'}))
    llm_says("call  CALL p2.add()")
    result = call_tool(session, "call", {"action": "CALL p2.add()"})
    as_llm(result)
    llm_says("→ 购物车 count 1→2，total 5.99→8.48")

    # ---- 第 5 步：inspect（LLM 想确认购物车能力）---------------------------
    banner("⑤ 工具调用 call_tool('inspect', {'ref': 'c1'})")
    llm_says("inspect  c1  # 看看购物车有哪些能力")
    as_llm(call_tool(session, "inspect", {"ref": "c1"}))
    llm_says("→ 购物车有 +open 能力")

    # ---- 第 6 步：打开购物车 -----------------------------------------------
    banner("⑥ 工具调用 call_tool('call', {'action': 'CALL c1.open()'})")
    llm_says("call  CALL c1.open()")
    result = call_tool(session, "call", {"action": "CALL c1.open()"})
    as_llm(result)

    # ---- 第 7 步：展示 critical 动作的安全闸门 ------------------------------
    banner('⑦ 任务完成后，用户没有要求结账——LLM 若误调 submit 会被拦截')
    llm_says("call  CALL b5.submit()  # （假设 LLM 越权尝试）")
    as_llm(call_tool(session, "call", {"action": "CALL b5.submit()"}))
    llm_says("→ CONFIRMATION_REQUIRED：critical 动作必须由宿主向用户确认")

    print(f"\n{RULE}\n  任务完成 ✅\n{RULE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
