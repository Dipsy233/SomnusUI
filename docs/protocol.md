# 工具与协议

## 1. 工具列表

完整工具面共九个调用。MCP、JSON-RPC 与 `examples/agent.py` 使用同一组工具定义和同一个 `call_tool` 入口。

| 工具 | 用途 | 返回 |
| --- | --- | --- |
| `observe` | 目标窗口的 L0 概览，范围限于视口，并附带一行 `WINDOWS` | SUI-Text |
| `find` | `FIND [type] "query" [in ref]`；可选 `timeout` 等待命中 | 匹配的实体行 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 细节 |
| `call` | `CALL ref.cap(args)`；多行作为一次批处理执行 | `OK`/`ERR` 与增量 |
| `run` | `CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap(args)` | `RUN OK`/`RUN PARTIAL`、每步一行、合并增量 |
| `help` | `HELP [topic]`；不带主题时列出工具、档位与主题 | 一篇主题文档或发现摘要 |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open / close / detach` | `OK` 与窗口清单 |
| `launch` | 在当前桌面或子桌面启动程序；`wait_s` 默认 3 秒 | `OK pid=…` 与新增窗口行 |
| `configure` | 调整 `settle_quiet_ms` / `settle_poll_ms` / `settle_timeout_s` / `post_action_enum` | `SETTINGS …` |

工具参数：

| 工具 | 参数 |
| --- | --- |
| `observe` | `window`（`wN`，省略为当前窗口）、`windows_only`、`include_other_windows` |
| `find` | `query`（可写完整 `FIND` 语法或裸查询字符串）、`timeout`（秒） |
| `inspect` | `ref`、`depth`、`start`、`end` |
| `call` | `action`、`rev` |
| `run` | `script`、`window` |
| `help` | `topic` |
| `switch` | `target`：`main`、`child`、`open`、`close`、`detach` |
| `launch` | `command`、`args`、`cwd`、`window`、`wait_s` |
| `configure` | 四个时序键，以及 `persist`、`reset` |

`configure` 只调整节奏参数，不授予或拒绝能力。

## 2. 工具档位

| 档位 | 工具 |
| --- | --- |
| `core` | `observe`、`find`、`inspect`、`call`、`run`、`help` |
| `desktop` | `switch`、`launch`、`configure` |

默认 MCP 宿主只暴露 core 六工具。`SOMNUS_CAPABILITIES` 接受逗号或分号分隔的档位名，`all` 表示全部档位：

```bash
somnus mcp                                 # 默认：core 六个工具
SOMNUS_CAPABILITIES=desktop somnus mcp     # core + desktop（九个）
SOMNUS_CAPABILITIES=all somnus mcp         # 同上，显式写法
```

`core` 始终包含。未设置或值非法时按 core 处理。工具可见性由 `somnus/protocol/policy.py` 的 `TOOL_POLICY` 决定，`tests/protocol/test_tool_policy.py` 锁定该表。档位实验数据见 [benchmarks/tool-surface.md](benchmarks/tool-surface.md)。

## 3. SUI-Text

一行一个节点。默认能力与默认状态省略。

```
@rev 7
WINDOWS w1 "Demo" | w2 "Docs" active
WINDOW w1 "Demo" active
  SCREEN s1 "menu"
    LIST l1 "Products" count=3 entity=product +find
      PRODUCT p1 "Big Mac" price=5.99 +add +customize(size:small|medium|large)
    CART c1 "Cart" count=0 total=0 +open
    CHECKOUT b5 "Checkout" +submit!
  ? r3 canvas reason=visual
```

节点行形状：`TYPE REF "Name" attr=value flags +caps`。

- 能力打印在所属节点上：`PRODUCT p1 +add` 表示 `CALL p1.add()`。
- `+submit!` 的 `!` 是 critical 能力元数据。调用直接执行，没有确认门控。
- `count=` 是真实总数；`visible=` 是屏内数量；`scroll=` 是滚动位置。`… +N more` 表示折叠。
- `? ref role reason=visual` 表示该内容无法在无视觉条件下读取，应如实报告。
- 增量标记：`~` 变更、`+` 新增、`-` 删除。判断结果以增量为准。
- 引号字符串转义 `\\`、`\"`、`\r`、`\n`、`\t`；一个节点始终占一行。解析器只解码 `\\` 与 `\"`。
- ref 使用 ASCII；名称保留原语言。数字值可携带货币属性，例如 `price=5.99`、`cur=USD`。
- `observe` 与 `inspect` 为文本类节点打印最长 80 字符的 `value=` 预览；超过 160 字符的值使用 `len= / preview=` 形式。

隐式能力按角色与原生模式推导，并始终对解析器可用：

- 所有节点具备 `focus`。
- 可读节点具备 `read`：具有 Value/Text 模式，或角色为 `TEXT` / `STATUSBAR` / `HEADING` / `PARAGRAPH`。
- 暴露 `Invoke` / `LegacyIAccessible` / `Toggle` / `SelectionItem` / `GridItem` / `Value` / `RangeValue` / `ExpandCollapse` 的控件具备 `context_menu`，`MENU` 与 `MENUITEM` 除外。
- `LINK`、`MENUITEM` 以及具有 `Invoke` / `LegacyIAccessible` 的节点具备 `activate`。
- `CHECKBOX` / `SWITCH` 或具有 `Toggle` 模式的节点具备 `toggle`。
- `SELECT` / `LISTITEM` / `TAB` / `RADIO` / `ROW` / `TREEITEM` 或具有 `SelectionItem` / `GridItem` 的节点具备 `select`。
- `LIST` / `TREE` / `TABLE` / `PANE` 或具有 `Scroll` / `ScrollItem` 的节点具备 `scroll(to=up|down|top|bottom)`。

`set_text` 始终显式打印。输入框没有 `+set_text` 表示该字段不可写（`readonly` 或 `disabled`）。

三个手势的含义：

| 能力 | 原生模式 | 含义 |
| --- | --- | --- |
| `select` | SelectionItem | 高亮或选中，对应单击 |
| `activate` | Invoke | 元素的默认动作；按钮为按下，文件或列表行为打开 |
| `context_menu` | `IUIAutomationElement3::ShowContextMenu` | 打开元素的上下文菜单，对应右键 |

`activate` 由真实 `Invoke` 模式或 `LegacyIAccessible` 回退推导，不从角色猜测。`link` 与 `menuitem` 由无障碍层自身激活，作为例外。`context_menu` 通过 UIA 语义调用打开菜单，不移动指针、不发送按键，也不要求窗口在前台。UIA 无法查询元素是否注册了菜单，因此结果按调用后的实际情况报告：弹出菜单返回 `OK`；未弹出返回 `OK_UNVERIFIED`，并在存在直接能力时指出来。

`activate` 以 `ACTION_NOT_SUPPORTED` 失败且节点仍暴露 `select` 时，同一错误行携带 `hint=try select`。运行时不会自动改调 `select`。

完整文法见 `somnus/sui/grammar.md`。

## 4. 观察输出

`observe` 先给桌面上下文，再以能力形式呈现当前窗口：

```
@rev 1
WINDOWS w1 "agent 属性" active
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)
  INPUT q2 "只读路径" readonly
  BUTTON b1 "共享(S)..."
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled
  TAB t3 "共享"
```

`WINDOWS` 行最多列出 6 个顶层窗口，并且只标记一个 `active`。多出的窗口折叠为 `+N more`；active 窗口落在前六之外时替换第 6 席。该行不包含 `ops=` 计数。`OBSERVE windows` 只返回窗口清单，形式为 `WINDOWS count=N` 后跟 `WINDOW wN "title"` 行，且不提升任何窗口。

大列表只读取可见部分，屏幕外的行汇总：

```
@rev 4
WINDOWS w1 "Store" active
WINDOW w1 "Store" active
  SCREEN s1 "catalog"
    LIST l1 "Products" rows=240 visible=12 scroll=5% +find
      ITEM li1 "Item 0" price=$1.00 +add
      ITEM li2 "Item 1" price=$1.50 +add
      ITEM li3 "Item 2" price=$2.00 +add
      +more items=228 use FIND "<name>" or OBSERVE w1 expand l1
```

`rows=` 是真实总数，`visible=` 是屏内行数。`FIND "<name>"` 可以到达屏幕外的行。`OBSERVE w1 expand l1` 请求采集层为该容器再读一页；页大小未在实现中固定。滚动与展开会使视口失效；采集集与屏幕不再对应时，下一次响应包含 `~ viewport changed; OBSERVE again`。

## 5. 查找与检查

`FIND` 成功时返回 `FOUND N` 和完整实体行：

```
FIND "共享"
FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

`FIND` 按元素去重，每个元素保留一条命中，并优先父行。子节点暴露父行没有的动作（例如 `+set_text`）时，仍单独占一行。`timeout` 超时返回 `ERR TARGET_NOT_FOUND after=Ns`。

`INSPECT` 返回单个节点的全部属性、能力、参数与关系：

```
INSPECT mi2
@rev 1
MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

关系包括 `acts_on=`、`label_for=`、`opens=` 等，在节点具备对应关系时打印。

## 6. 调用与增量

```
CALL tx1.set_text("Hello SomnusUI")
OK rev=2
  ~ tx1 value ->Hello SomnusUI
  + TAB t4 "Hello SomnusUI. 已修改。" +close!
  - t2
```

`OK` 表示动作执行并观察到可见变化，或声明的 effects 全部满足。`OK_UNVERIFIED` 表示动作执行、未观察到可见变化，也没有可验证的声明效果。`EFFECT_NOT_CONFIRMED` 表示声明效果未满足。

失败为一行错误加恢复提示：

```
ERR TARGET_DISABLED b5 hint=inspect
ERR NOT_ACTIONABLE b5 hint=wait msg="state=disabled waited=300ms"
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
ERR ACTION_NOT_SUPPORTED b1 hint=try select
```

`NOT_ACTIONABLE` 表示被阻塞元素的短暂等待已经用尽，状态仍然存在。没有签名探针的 provider 无法观察状态变化，保持立即返回 `TARGET_DISABLED`。

## 7. 读取

```
CALL q1.read()
OK rev=5
  ~ q1 value="共享" len=2
```

载荷为 `value="…" len=N`。`len=` 通常精确；触发 provider 的 4096 字符上限时变为 `len>=4097`，行尾追加 `truncated=true; use INSPECT with range=0:2000 for more`。内联最多返回 200 字符（超长时为 199 字符加 `…`）。

读取顺序：UIA Text 模式，其次 Value 属性。UIA Text 控件（control type 50020）的两个表面都返回空时读取 accessible Name；空 `Edit`（50004）保持 `value=""`，不使用 Name 作为内容。Text 与 Value 都不可读的元素回退到 accessible Name。`read` 是隐式能力，L0 不打印 `+read`，`INSPECT` 在节点发布可读接口时列出它。

观察与检查结果不包含句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始 `control_type` 或无名的 `Pane` 包装节点。`launch` 的回复包含启动程序返回的 `pid`，该字段只出现在启动回复中。

## 8. 批量调用

`call` 接受多条 `CALL` 语句，每行一条，最多 20 条。它们在一次往返内按顺序执行。所有步骤先对照当前编译结果校验；无法工作的步骤在任何状态改变之前拒绝整批。校验通过后，各步之间不做 settle，整批以一次 settle、一份增量、一次 rev 结束。

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
BATCH OK steps=3/3 rev=7
  1. CALL q1.set_text("张三") -> ok
  …
~ q1 text="张三"
```

两语句在预校验阶段被拒绝：`CALL wN.focus()`（切换窗口会重新分配 ref）和 `CALL ref.context_menu()`（菜单只在 settle 后存在，后续步骤无法引用）。因此每一步必须指向批处理开始时存在的元素。先打开菜单或对话框、再操作其中元素的流程需要拆成多次调用。

某一步失败时批处理停止。响应指明失败步、已经执行的步骤及其增量：

```
BATCH PARTIAL steps=1/2 failed-at=2 rev=7
  1. CALL p1.add() -> ok
  2. CALL p2.add() -> ERR TARGET_DISABLED
~ c1 count 0→1
```

步骤状态包括 `ok`、`dispatched`、`ERR <code>`、`not-run`。`dispatched` 表示动作已发送、结果正在 settle；模态窗口打开时批处理停在 `stopped-at=N`。

## 9. 脚本（`run`）

`run` 接受脚本，每行一步，最多 40 步。纯 `CALL` 脚本沿用批处理的全量预校验。出现 `AWAIT` 后改为逐步执行：执行、settle、重读、解析下一步。后续步骤可以引用前面刚创建的元素。

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
RUN OK steps=3/3 rev=9
  1. CALL mb1.expand() -> ok
  2. AWAIT MENUITEM "新建" timeout=2 -> ok ($it=mi5)
  3. CALL $it.activate() -> ok
~ t1 len 0→4
```

- `AWAIT [TYPE] "name" [in ref] [timeout=s]` 把 `$it` 绑定到第一个命中。默认 2 秒；`timeout=0` 执行一次搜索。名称必须加引号。
- `$it` 在绑定前无效。没有其他绑定形式。
- 控制流三种行：`EACH [TYPE] "name" [in ref] [max=N]` 开循环块（`END` 收尾），每轮迭代对当前编译结果重新求值并把 `$it` 重绑到下一个命中，零命中跳过循环体；`TRY` 前缀让步骤失败只记录不停机；`IF ok|failed` 按上一个**执行过**的步骤状态决定是否运行。`EACH` 禁嵌套，`IF` 不能作为脚本或循环体的第一步。
- 预算按执行步计：CALL/AWAIT 每执行一次计一步，总计最多 40；耗尽以 `RUN PARTIAL reason=budget` 停止。循环体内步骤的行号带迭代后缀（`4.2` 表示文本第 4 步第 2 轮）。
- 某一步失败（无 `TRY`）时脚本停止：`RUN PARTIAL steps=2/3 failed-at=3`，随后给出每步状态与已执行步骤的合并增量。
- 跨窗口流程必须显式写窗口 ref，因为切换会重新分配 ref。增量是脚本首尾的净差。
- `window=REF` 在第一步前聚焦该窗口。`CALL wN.focus()` 作为步骤被拒绝。
- 含 `AWAIT` 的脚本允许 `context_menu`，菜单是可以由后续 `AWAIT` 找到的真实步骤。

## 10. 通用模式

App Pack 可选。使用 `--no-pack`，或窗口没有匹配的 pack 时，编译器从结构与原生模式推导能力。

- 控件由平台定义：暴露交互模式（`invoke`、`selectionitem`、`toggle`、`value` 等）的元素。静态文本与布局容器不获得操作。
- 能力由已发布接口推导。`activate` 只在平台提供默认动作（`Invoke` 模式或 `LegacyIAccessible` 回退）时出现。只暴露 `ExpandCollapse` 的按钮显示 `+expand +collapse`。
- `set_text` 为每个可输入表面推导。路径由控件接口决定，在执行时判定，因为 UIA 会报告元素随后拒绝的接口。生效路径在整个会话内保留。
- 实体按结构判定：带标题与动词按钮的行，或同一容器下三个以上标题行（列表、工具栏、导航栏）。
- 平台未给出名称的控件从可见标签合成名称：字段前方的文本兄弟节点，或它唯一的叶子文本。合成名称用于展示、`FIND` 与实体匹配，不参与指纹，因此会变化的标题不会让 ref 重新分配。没有标签的控件保持无名。

文本输入三条路径：

| 顺序 | 路径 | 前提 | 合成输入 / 剪贴板 |
| --- | --- | --- | --- |
| 1 | `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 / 否 |
| 2 | 剪贴板 + `Ctrl+V` | `allow_clipboard`（默认开启） | 否 / 是 |
| 3 | 聚焦后键入 | `allow_keyboard`（默认开启） | 是 / 否 |

模型只看到 `set_text`，底层通道由运行时选择。详细代价与验证见 [reference/text-input-channels.md](reference/text-input-channels.md)，开关见 [configuration.md](configuration.md)。

## 11. 错误码

| 错误码 | 含义 | 恢复提示 |
| --- | --- | --- |
| `PARSE_ERROR` | 语法错误 | `fix-syntax` |
| `TARGET_NOT_FOUND` | ref 未知或查找无命中 | `observe` |
| `TARGET_AMBIGUOUS` | 多个匹配 | `refine-query` |
| `STALE_TARGET` | ref 已失效 | `find` |
| `CAPABILITY_NOT_FOUND` | 目标没有该能力 | `inspect` |
| `INVALID_ARGUMENT` | 参数错误或缺失 | `inspect` |
| `ACTION_NOT_SUPPORTED` | 没有可用的原生模式 | `report-user`；`activate` 失败且节点暴露 `select` 时为 `try select` |
| `TARGET_DISABLED` | 目标被禁用 | `inspect` |
| `NOT_ACTIONABLE` | 短暂等待后仍被阻塞 | `wait` |
| `PERMISSION_DENIED` | 允许列表或 UIPI 拒绝 | `report-user` |
| `NATIVE_EXECUTION_FAILED` | 原生模式抛出错误 | `retry-once` |
| `TIMEOUT` | 执行或 settle 超时 | `observe` |
| `EFFECT_NOT_CONFIRMED` | 声明效果未出现 | `observe` |

错误固定为一行：`ERR CODE ref hint=…`。堆栈只写入日志。

## 12. 窗口焦点与采集告警

`OBSERVE` 与 `CALL` 在读取或动作前提升目标窗口；已在前台的窗口不重复提升。`OBSERVE windows` 不抢占前台。被接触窗口的原始状态会被记录，并在 provider 关闭时恢复。

提升被拒绝，或快照返回有标题但没有子节点的窗口时，观察结果报告：

```
~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows
~ tree looks empty; window may be covered or app not rendering
```

自绘界面（Chromium、Electron、Qt Quick）在可见之前可能发布空壳树；最小化会加剧该现象。

## 13. Provider

| Provider | 来源 | 状态 |
| --- | --- | --- |
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | 可用 |
| `mock` | 录制的 `RawTree` 固件 | 可用 |
| `peer` | 子会话中运行的 `Session` | 可用，由子会话内部使用 |

CLI 的 `--provider` 接受 `auto`、`uia`、`mock`。`auto` 在安装 `[windows]` 扩展时选择 UIA，否则使用固件。`peer` 由子会话切换机制建立，不作为 `--provider` 取值。

编译器、投影与运行时对所有 provider 一致。`peer` provider 的窗口枚举、无障碍树与合成输入局限于调用方所在的 Windows 会话。

## 14. HELP 与 MCP

`HELP` 不带主题时列出已启用档位及其工具、仍可开启的档位、主题列表。`HELP <topic>` 返回一篇主题文档，主题为 `format`、`gestures`、`batching`、`desktops`、`errors`、`settings`。`somnus help <topic>` 打印相同内容。

MCP 服务器在 `initialize` 时发送一次 `instructions`，内容包括共享规则和当时生效的时序值。`configure` 返回实时生效值。默认会话与客户端同生命周期；设置 `SOMNUS_IDLE_TIMEOUT_S`（秒，默认关闭）后，超过该时长没有工具调用就关闭 provider，下一次调用自动重建，旧 ref 返回 `STALE_TARGET`。

## 15. 限制

- `call` 单批最多 20 步，`run` 最多 40 个执行步（`EACH` 循环按实际执行计）。
- `find` 的 `timeout` 等待成本随超时线性增长；视口截断的集合不会用全量快照重试。
- 虚拟化列表中未 realized 的行没有节点与 ref；`FIND` 对这些行返回 0 命中或 `STALE_TARGET`。
- `run` 的跨窗口流程需要显式窗口 ref，`$it` 只绑定第一个命中。
- `OBSERVE ... expand` 将容器路径交给采集层，页大小未固定。
