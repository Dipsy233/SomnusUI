"""真实 GUI 演示：用 SomnusUI 控制 Notepad 写入 "Hello SomnusUI"。

与 demo_llm.py 相同的四工具接口，后端换成真实 Windows UIA。
用法：先启动 Notepad，再运行本脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from somnus.protocol.tools import call_tool

RULE = "─" * 68
TEXT = "Hello SomnusUI"


def banner(step: str) -> None:
    print(f"\n{RULE}\n  {step}\n{RULE}")


def show(text: str, indent: str = "  ") -> None:
    for line in text.splitlines():
        print(f"{indent}{line}")


def main() -> int:
    try:
        from somnus.providers.windows.uia import UiaProvider
    except Exception as exc:  # pragma: no cover
        print(f"需要 Windows UIA: {exc}")
        return 1
    from somnus.runtime.session import Session

    provider = UiaProvider(allow_list=["notepad.exe"])
    session = Session(provider, pack_id="notepad")

    print(f"👤 任务: 在 Notepad 里写入 \"{TEXT}\"")
    print("   后端 = 真实 Windows UIA，目标 = 正在运行的 Notepad")

    banner("① LLM: observe")
    show(call_tool(session, "observe", {}))

    # 找到编辑器（pack 把 document 提升为 DOCUMENT 实体）
    doc = next(
        (n for n in session.result.root.walk() if n.entity == "document"), None
    )
    if doc is None:
        print("未找到编辑器实体")
        return 1
    print(f"\n  🧠 LLM: 编辑器是 {doc.ref}，带 +set_text 能力")

    banner(f"② LLM: call  CALL {doc.ref}.set_text(\"{TEXT}\")")
    show(call_tool(session, "call", {"action": f'CALL {doc.ref}.set_text("{TEXT}")'}))

    banner("③ LLM: observe（确认写入结果）")
    show(call_tool(session, "observe", {}))

    print(f"\n{RULE}\n  Notepad 里已显示 \"{TEXT}\" ✅\n{RULE}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
