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

# 应用某个 App Pack（内置 restaurant-demo，配合演示应用）
somnus --pack restaurant-demo repl

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

sui> FIND link "..."              ← 定位，返回完整实体行 + 父级链
FOUND 1
LINK lnk32 "..." +activate +context_menu +focus +read
  in: WINDOW w15 "Steam" > SCREEN s3 "库" > LIST l1

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
| `OBSERVE w4 desktop` | w4 的内容，并附上其他窗口 |
| `OBSERVE w4 full` | 完整树，不折叠、不限预算 |
| `OBSERVE w4 expand g3` | 定点展开一个折叠节点，其余保持折叠 |

其他窗口默认不列出：只要一个窗口的内容，就不该为整张桌面付 token。

大同类列表（游戏列表、消息列表）只展示前 3 项，其余折叠为 `… +N more`；
用 `FIND` 直接在其中搜索，无需先展开。无名字、无能力的匿名叶子在 L0
折叠为一行 `… +N unnamed` 汇总，`INSPECT` / `expand` / `full` 可展开。

## 在两个会话之间切换

当需要用剪贴板/键盘通道（它们要求目标在前台，会和用户抢焦点）时，可以把
agent 移到子会话里。agent 同一时间只在一个桌面上，用 `SWITCH` 移动：

```text
sui> SWITCH child
OK session=child windows=3
  WINDOW w1 "此电脑"
  ...

sui> OBSERVE w2                 ← 现在 ref 指的是子会话里的窗口
sui> CALL q1.set_text("hi")     ← 输入发生在子会话，不影响用户

sui> SWITCH main
OK session=main windows=11      ← 回到用户的桌面
```

| 命令 | 效果 |
| --- | --- |
| `SWITCH main` | 回到用户桌面 |
| `SWITCH child` | 切到子会话（须已打开） |
| `SESSION open` | 打开子会话并切过去 |
| `SESSION close` | 结束子会话并回到 main |
| `SESSION detach` | 本进程退出后仍保留它 |

**切换会丢弃全部 ref。** 切换后窗口列表不同，`w1` 不是"过期"，而是变成了
新桌面上的另一个窗口——先重新 `OBSERVE` 再操作。

系统级配置（需管理员，只需一次）：

```bash
somnus child-session setup
somnus child-session start
somnus child-session close
```

原理、失败模式和「为什么不用虚拟桌面」见 [child-session.md](child-session.md)。

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

### 两个实测的平台坑（WPF）

**菜单栏顶层项要用 `expand`，不是 `activate`。** WPF 的 `MenuItem` **声称**
支持 `activate`（它继承了 `ButtonBase` 的 Invoke pattern），但菜单栏顶层的项
不实现这个行为——调用会被如实拒绝：

```text
sui> CALL mi2.expand()          ← 菜单栏项：expand 才打开子菜单
OK_UNVERIFIED rev=2
+ MENUITEM mi5 "新建"

sui> CALL mi2.activate()        ← 声明了，但执行时被拒
ERR ACTION_NOT_SUPPORTED mi2 hint=report-user
```

里面真正的**命令项**才用 `activate`。"声明了"不等于"能执行"——UIA 里这两件
事是分开的，所以以打印出的能力和返回值为准，不靠猜。

**XAML `ContextMenu` 触发不了 `context_menu`。** WPF 在 XAML 里挂的
`ContextMenu` 是框架层的东西，UIA 的 `ShowContextMenu`（系统层的动词）够不着它。
运行时检测到没弹出菜单就**如实报**，并给出该元素真正有的能力：

```text
sui> CALL li1.context_menu()
OK_UNVERIFIED rev=2
~ context_menu produced no menu (this element registers none);
  li1 does expose activate/select - try CALL li1.activate()
```

能触发 `context_menu` 的是走真正 Win32 菜单循环的控件（托盘图标、Shell 菜单，
窗口类 `#32768`）；自绘控件里由应用自己响应 `ShowContextMenu` 的也行。这不是
运行时的缺陷，是平台的边界，运行时的处理（报实情 + 给替代）就是为它准备的。

## 使用前先把窗口提到前台

agent 读或操作一个窗口时，会**先把该窗口提到前台**。这不是副作用，是前提：
被遮挡或最小化的窗口并不可靠地渲染。普通 Win32/WPF/UWP 应用通常仍有完整树，
但自绘界面（Chromium/Electron/Qt Quick：QQ、豆包、Steam）在被真正显示前什么都
不渲染——无障碍树萎缩到壳，agent 会以为这个应用没有控件。最小化是最极端的情况：
CEF 应用完全不布局。

因此运行时在做任何事之前先提窗口：

1. provider 的 `snapshot` 与每个非平凡的 `execute` 都经过
   `UiaProvider._enter_foreground_sync`，`CALL wN.focus()` 同理；
2. 窗口已经在前台时跳过，因此对同一窗口的一串调用只付一次激活；
3. 前台锁（另一个进程持有前台时 `SetForegroundWindow` 返回 0）由
   `workspace.raise_window` 处理：普通调用 → `AttachThreadInput` → 一次空按
   Alt 后重试；
4. 在其他虚拟桌面的窗口用官方 `IVirtualDesktopManager` 移到当前桌面
   （同一进程无需二次登录：QQ 主桌面登录一次即可）。

唯一不提窗口的是 `OBSERVE windows`：回答"桌面上有什么"不需要任何窗口渲染，
它是每个工作流的入口，保持无代价。

窗口接触前的状态会被记录，会话结束（provider `close()`）时自动恢复：
原来最小化的重新最小化，agent 的工作不给用户留下桌面被重新布置的痕迹。

```text
（用户把 QQ 最小化后离开）
sui> OBSERVE w4              ← 窗口被提到前台，QQ 完整渲染
@rev 1
WINDOW w15 "QQ" ...          ← 完整内容

sui> CALL q2.set_text("...")
sui> CALL b213.activate()

（会话结束 → 原来最小化的窗口重新最小化）
```

想要**完全不影响用户**，用子会话（`SWITCH child`）：那里有自己的前台窗口和
输入队列，提窗口、打字都不影响用户桌上的东西，见 `docs/child-session.md`。
