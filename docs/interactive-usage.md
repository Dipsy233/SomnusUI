# 交互使用：REPL、单条 CLI 与脚本

SomnusUI 有三种驱动方式。多步操作必须用 REPL，原因是 `ref` 的生命周期。

## 三种方式

| 方式 | 命令 | 会话状态 | 适用场景 |
| --- | --- | --- | --- |
| 单条 CLI | `somnus observe` / `somnus call '...'` | 每次新进程，ref 失效 | 一次性、无状态操作 |
| **交互式 REPL** | `somnus repl` | 同一 Session，ref 稳定 | **多步工作流（推荐）** |
| Python 脚本 | 自行编写 | 可编程 | 需要循环、条件、批量逻辑 |

## 为什么多步操作要用 REPL

`ref`（如 `q1`、`b12`、`g7`）由 `Session` 的内存注册表分配，**进程退出即失效**。
用单条 CLI 连续执行多步命令时：

- 每条命令都是新进程，重新编译、重新分配 ref，编号会漂移
- 上一步打开的新窗口、输入框里的内容等状态，下一步不可见

REPL 在一个进程里维持同一个 `Session`，ref 稳定、状态连续。

## 用法

```bash
# 通用模式：不应用任何 App Pack
somnus --no-pack repl

# 应用某个 App Pack
somnus --pack notepad repl

# 演示数据（不需要 Windows）
somnus --demo repl
```

启动后进入 `sui>` 提示符，输入 `quit` / `exit` / `:q` 退出。

危险操作（`critical` 风险，如发送、删除）默认返回 `CONFIRMATION_REQUIRED`。
在确认过意图后，用 `--confirmed` 启动以预先授权：

```bash
somnus --no-pack --confirmed repl
```

## 一次典型会话

```text
sui> OBSERVE windows
WINDOWS count=6
  WINDOW w1 "任务栏"
  WINDOW w4 "Steam"
  ...

sui> OBSERVE w4                    ← 只观察该窗口
@rev 2
WINDOW w15 "Steam"
  LIST l1 "库" +find
    LINK lnk32 "..."

sui> FIND link "..."              ← 定位，返回完整实体行
FOUND 1
LINK lnk32 "..." +activate +context_menu +focus +read

sui> INSPECT lnk32                 ← 需要时展开全部能力与参数
LINK lnk32 "..." +activate +context_menu +focus +read

sui> CALL lnk32.activate()         ← 操作，返回 delta
OK_UNVERIFIED rev=3
+ ...                              ← 只有发生变化的部分
```

## 观察范围

| 命令 | 返回 |
| --- | --- |
| `OBSERVE windows` | 只要窗口清单 |
| `OBSERVE w4` | 只观察 w4 的内容（默认） |
| `OBSERVE w4 all` | w4 的内容，并附上其他窗口 |

其他窗口默认不列出：只要一个窗口的内容，就不该为整张桌面付 token。

## 手势

`select`、`activate`、`context_menu` 是三个不同的能力，运行时不替你选择：

| 能力 | 含义 | 人类对应 |
| --- | --- | --- |
| `select` | 高亮/选中 | 单击 |
| `activate` | 执行元素的**默认动作** | 双击 |
| `context_menu` | 打开上下文菜单 | 右键 |

`activate` 不是“点一下”：对按钮是按下，对文件或列表行是**打开**。
某些应用对 `activate` 只做高亮（如聊天列表的会话行），它既不打开也不报错——
此时用 `context_menu` 打开菜单，再对菜单项 `activate`：

```text
sui> CALL li5.context_menu()
OK_UNVERIFIED rev=4
  LISTITEM li5 "宝宝" +open +activate +context_menu
  MENU mn1 "菜单"                        ← 菜单就挂在原节点下
  MENUITEM mi3 "打开独立聊天窗口"
  MENUITEM mi4 "从消息列表中移除"

sui> CALL mi3.activate()
OK_UNVERIFIED rev=5
+ WINDOW w20 "宝宝"                      ← 会话打开
```

`context_menu` 走的是 UIA 自身的 `ShowContextMenu`，**不是合成输入**：
指针不移动、不发送按键、也不要求窗口在前台。菜单是弹出层，所以：

- 应用把菜单画在自己窗口里时，它作为 `MENU` 子节点挂在原节点下；
- 应用把菜单交给系统（Win32 Shell，窗口类 `#32768`）时，运行时同样把它
  取回来挂在原节点下。

菜单消失（被点了、被别处焦点顶掉）后挂载自动解除。
以返回的 delta 为准，它比状态字更可信。
