"""The same LLM tool interface driving a *real* Windows GUI (the WPF demo app).

Identical to ``demo_llm.py`` except that the backend is the real UIA provider.
Usage: start ``RestaurantDemo.exe`` first, then run::

    python bench/demo_llm_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from somnus.protocol.tools import call_tool

RULE = "─" * 68


def banner(step: str) -> None:
    print(f"\n{RULE}\n  {step}\n{RULE}")


def show(text: str, indent: str = "  ") -> None:
    for line in text.splitlines():
        print(f"{indent}{line}")


def main() -> int:
    try:
        from somnus.providers.windows.uia import UiaProvider
    except Exception as exc:  # pragma: no cover
        print(f"the Windows UIA provider is unavailable: {exc}")
        return 1
    from somnus.runtime.session import Session

    provider = UiaProvider(allow_list=["restaurantdemo.exe"])
    session = Session(provider, pack_id="restaurant-demo")

    print("👤 用户: 帮我点一个巨无霸和一份薯条，然后打开购物车")
    print("   （后端 = 真实 Windows UIA，目标 = 正在运行的 WPF 应用）")

    banner("① LLM: observe")
    show(call_tool(session, "observe", {}))

    banner('② LLM: find  FIND product "Big Mac"')
    show(call_tool(session, "find", {"query": 'FIND product "Big Mac"'}))

    banner("③ LLM: call  CALL p1.add()")
    show(call_tool(session, "call", {"action": "CALL p1.add()"}))

    banner('④ LLM: find  FIND product "Fries" → call  CALL p2.add()')
    show(call_tool(session, "find", {"query": 'FIND product "Fries"'}))
    show(call_tool(session, "call", {"action": "CALL p2.add()"}))

    banner("⑤ LLM: call  CALL c1.open()")
    show(call_tool(session, "call", {"action": "CALL c1.open()"}))

    print(f"\n{RULE}\n  真实 GUI 操作完成（WPF 里应弹出购物车对话框）✅\n{RULE}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
