# 交互式使用指南：REPL vs 单条 CLI vs 脚本

SomnusUI 提供三种使用方式，选对方式能让多步操作流程快很多。

## 三种方式对比

| 方式 | 命令 | 会话状态 | 适用场景 |
| --- | --- | --- | --- |
| 单条 CLI | `somnus call '...'` | ❌ 每次新进程，ref 丢失 | 一次性、无状态操作（`windows`、单次 `observe`） |
| **交互式 REPL** | `somnus repl` | ✅ 同一 Session，ref 稳定 | **多步工作流（推荐）** |
| Python 脚本 | 自行编写 | ✅ 可编程 | 需要循环、条件、批量逻辑时 |

## 为什么多步操作必须用 REPL

`ref`（如 `g7`、`q2`、`b12`）由 `Session` 内存中的注册表分配，**进程退出即失效**。
用单条 CLI 连续执行多步命令时：

- 每条命令都是全新进程，重新编译、重新分配 ref，编号可能漂移（如窗口 `w13` → `w4` → `w1`）
- 上一步打开的新窗口、输入框里的内容等会话状态，下一步完全不可见

REPL 在一个进程里维持同一个 `Session`，ref 稳定、状态连续，逐条交互即可。

## REPL 用法

```bash
# 演示模式
somnus --demo repl

# 真实 Windows 应用（加载对应 pack）
somnus --provider uia --pack qq repl

# 浏览器
somnus --provider cdp repl
```

启动后进入 `sui>` 提示符，输入 `quit` / `exit` / `:q` 退出。
危险操作（critical 风险，如 QQ 的发送按钮）需以 `--confirmed` 启动 REPL 预先确认：

```bash
somnus --provider uia --pack qq --confirmed repl
```

## 实例：用 REPL 给 QQ "我的手机" 发消息

全程零视觉、零坐标，LLM/操作者只看到语义实体和能力：

```text
sui> OBSERVE                     ← 观察主面板，找到会话
sui> FIND conversation "我的手机"
→ FOUND 1
CONVERSATION g7 "我的手机" +open +select

sui> CALL g7.open()              ← 打开会话（双击，pack 声明的合成输入）
OK_UNVERIFIED rev=2
+ WINDOW w13 "我的手机"          ← QQ 弹出独立聊天窗口

sui> OBSERVE                     ← 重新观察新窗口，定位输入框与发送按钮
sui> INSPECT s1
INPUT q2 +set_text(text)         ← 消息编辑器（ProseMirror）
SEND  b12 "发送" +send!           ← critical 风险操作

sui> CALL q2.set_text("你好，这是SomnusUI的测试消息")
OK_UNVERIFIED rev=2
+ TEXT tx2 "你好，这是SomnusUI的测试消息"   ← 编辑器增量确认

sui> CALL b12.send()             ← 发送（需 --confirmed）
OK_UNVERIFIED rev=3
- tx2                            ← 编辑器被清空 = 消息已发出
```
