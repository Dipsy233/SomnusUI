# SomnusUI

[English](README.md) | 中文

SomnusUI 把原生 GUI 编译为文本接口。运行时读取 Windows 无障碍树，将其投影为
SUI-Text（节点、能力与属性），再通过 UI Automation 执行能力调用；文本输入可选
剪贴板与键盘通道。模型收到 SUI-Text，不收到截图、坐标或原生标识符。

输入是七种工具调用之一：`observe`、`find`、`inspect`、`call`、`run`、`switch`、
`configure`。输出是 SUI-Text：一次观察、一份命中列表、一行带增量的 `OK`/`ERR`，
或一行设置结果。

## 概述

- 无障碍树是唯一感知通道。树只从可视视口读取。屏幕外的行汇总为
  `rows= / visible=`（桌面行最多列出 6 个窗口）。`IsOffscreen` 不作为可见性
  判据：在一个 240 行、屏内 12 行的列表上，它报告 0 行 offscreen。
- 每次读取或写入前先提升目标窗口。目标已在前台时不重复提升。`OBSERVE windows`
  是例外。自绘界面（Chromium、Electron、Qt Quick）在可见之前可能发布空壳树。
- 节点行上打印的能力在被调用时直接执行。运行时没有确认步骤、风险评分或危险
  操作拦截。`+submit!` 的 `!` 后缀是元数据，不是权限请求。
- 文本写入按顺序尝试三条路径：`ValuePattern.SetValue`、剪贴板 + `Ctrl+V`、
  聚焦后键入。响应说明实际生效的路径。`WM_SETTEXT` / `EM_REPLACESEL` 已移除。
  原因：能映射 HWND 且承载可编辑文本的控件同时发布 Value 模式。
- 编译器是确定性的。运行时与编译器内部不运行语言模型。
- 不支持或不可读的内容如实报告。无视觉无法读取的内容渲染为
  `? ref role reason=visual`。控件在执行时拒绝的 UIA 接口返回
  `ACTION_NOT_SUPPORTED`，并附带恢复提示。

## 开始使用

在仓库根目录执行以下命令。`bench/` 与 `tests/` 按路径引用，未安装。

```bash
pip install -e ".[dev]"

# 回放内置餐厅演示（无需 Windows）：
somnus --demo observe
somnus --demo call 'CALL p1.add()'
python -m somnus.cli bench --runs 100

# 操作真实 Windows 应用（需要 [windows] 扩展）：
pip install -e ".[windows]"
somnus windows                     # 列出窗口
somnus observe                     # 描述目标窗口
somnus observe w1                  # 只观察一个窗口
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # 多步会话，不应用 App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP 服务器（stdio）
```

使用 `python -m somnus.cli bench` 的原因：控制台脚本的 `sys.path` 从
`.venv/Scripts` 开始，仓库内的 `bench` 包不在其中。在仓库根目录使用
`python -m` 会把仓库放进 `sys.path`。其他命令通过 `somnus` 入口执行。

ref（`p1`、`w1`、`q1` …）只在一个会话内有效。`somnus observe` 与
`somnus call` 执行一次工具调用后进程退出，所有 ref 随进程失效。多步任务使用
`somnus repl`（一个存活会话）、MCP 服务器（每个客户端一个长生命周期会话），或
`run` 工具（一次调用执行完整流程）。`somnus --demo` 与 `somnus --scenario`
遵循相同的单次规则。

### MCP 服务器

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

服务器暴露下列七个工具。provider 自动选择：安装 `[windows]` 扩展时使用
UI Automation，否则回放录制的固件。服务器启动时在 `instructions` 中写明当前
生效的 settle 值：`settle_quiet_ms=40`、`settle_poll_ms=10`、
`settle_timeout_s=3.0`、`post_action_enum=on`。

## 工具与协议

| 工具 | 用途 | 返回 |
| --- | --- | --- |
| `observe` | 目标窗口的 L0 概览（视口范围），以及一行 `WINDOWS` | SUI-Text |
| `find` | `FIND [type] "query" [in ref]`；可选 `timeout` 等待命中 | 匹配的实体行 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 细节 |
| `call` | `CALL ref.cap(args)`；多行作为一次批处理执行 | `OK`/`ERR` 与增量 |
| `run` | `CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap(args)` | `RUN OK`/`RUN PARTIAL`、每步一行、合并增量 |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` 与窗口清单 |
| `configure` | 调整 `settle_quiet_ms` / `settle_poll_ms` / `settle_timeout_s` / `post_action_enum` | `SETTINGS …` |

`configure` 是节奏控制工具，不授予或拒绝能力。

### SUI-Text

能力打印在节点行上。默认能力与默认状态省略；系统提示词携带默认表。

```
@rev 1
WINDOWS w1 "Restaurant Demo" active
WINDOW w1 "Restaurant Demo" active
  SCREEN s1 "menu"
    LIST l1 "Products" count=3 entity=product +find
      PRODUCT p1 "Big Mac" price=5.99 +add +customize(size:small|medium|large)
    CART c1 "Cart" count=0 total=0 +open
    CHECKOUT b5 "Checkout" +submit!
```

```
FIND product "Big Mac"   →  FOUND 1 / PRODUCT p1 "Big Mac" price=5.99 +add
CALL p1.add()            →  OK rev=2
                            ~ c1 count 0→1
                            ~ c1 total 0→5.99
CALL b5.submit()         →  OK rev=3
```

完整文法见 [`somnus/sui/grammar.md`](somnus/sui/grammar.md)。

### 观察输出

一个真实窗口，四次工具调用。桌面上下文是单行 `WINDOWS`（窗口 ref、标题、
active 标记）。其他窗口的内容不包含在内。

**`observe`**：先给桌面行，再以能力形式呈现当前屏幕：

```
@rev 1
WINDOWS w1 "agent 属性" active
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← 可写入的文本框
  INPUT q2 "只读路径" readonly               ← 没有 +set_text 的 INPUT
  BUTTON b1 "共享(S)..."                      ← 可点击（activate 隐含）
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← 当前不可用
  TAB t3 "共享"
```

`WINDOWS` 行最多列出六个顶层窗口，并且只标记一个 `active`。多出的窗口折叠为
`+N more`；active 窗口始终在列，落在前六之外时替换第六席。该行不带 `ops=`
计数。统计可操作元素需要每个窗口做一次浅读，5 个窗口实测 111 ms，而观察预算
约为 80 ms。`OBSERVE windows` 只返回窗口清单。

超过视口的大列表只读可见部分，屏幕外的行以摘要呈现：

```
@rev 4
WINDOWS w1 "Store" active
WINDOW w1 "Store" active
  SCREEN s1 "catalog"
    LIST l1 "Products" rows=240 visible=12 scroll=5% +find
      ITEM li1 "Item 0" price=$1.00 +add          ← 可见行完整读取
      ITEM li2 "Item 1" price=$1.50 +add
      ITEM li3 "Item 2" price=$2.00 +add
      +more items=228 use FIND "<name>" or OBSERVE w1 expand l1
```

`rows=` 是真实总数；`visible=` 是屏内行数。`FIND "<name>"` 可到达屏幕外的行。
`OBSERVE w1 expand l1` 让采集层为该容器再读一页；页的大小没有规定。滚动与展开
会使视口失效。采集集与屏幕不再对应时，下一次响应包含
`~ viewport changed; OBSERVE again`。

**`find`**：按关键词定位。每条命中是完整的实体行：

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

**`inspect`**：查看单个节点的全部信息，包括隐含能力与关系：

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**`call`**：执行操作并返回增量：

```
CALL tx1.set_text("Hello SomnusUI")
→ OK rev=2
  ~ tx1 value →Hello SomnusUI                  changed
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   added
  - t2                                          removed
```

`OK_UNVERIFIED` 用于已 settle 且增量为空的调用：操作已执行，未观察到可见变化。
产生增量的调用报告 `OK`。

失败时输出一行错误与恢复提示：

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

**`read`**：隐式内容能力。

```
CALL q1.read()
→ OK rev=5
  ~ q1 value="共享" len=2
```

载荷为 `value="…" len=N`。`len=` 通常精确；触发 provider 的 4096 字符读取
上限时，变为 `len>=4097`，行尾追加
`truncated=true; use INSPECT with range=0:2000 for more`。内联最多返回 200
字符（超长时为 199 个字符加 `…`）。读取先试 UIA Text 模式，再试 Value 属性；
两者都没有的标签读取其 accessible Name。`read` 是隐式的：L0 不打印 `+read`，
`INSPECT` 在节点发布可读接口时列出它。

SUI-Text 中所有带引号的字符串（包括 `value=`）转义 `\\`、`\"`、`\r`、`\n`、
`\t`，一个节点只占一行。预览用于阅读；解析器只解码 `\\` 与 `\"`。`observe`
与 `inspect` 还会为文本类节点打印短的 `value=` 预览（最多 80 字符，截断时带
`…`）。超过 160 字符的值改用序列化器的 `len= / preview=` 形式。

模型不收到句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始 `control_type`
或无名的 `Pane` 包装节点。完整示例见
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md)。

### 批量调用

`call` 接受多条 `CALL` 语句，每行一条。它们在一次往返内按顺序执行。每一步先
对照当前编译结果校验；无法工作的步骤会在任何状态改变之前拒绝整批。随后各步
之间不做 settle 地执行，整批以一次 settle、一份增量、一次 rev 结束。

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
→ BATCH OK steps=3/3 rev=7
    1. CALL q1.set_text("张三") -> ok
    …
  ~ q1 text="张三"
```

某一步失败时批处理停止。响应指明失败步、说明真正执行过的步骤，并携带它们产生
的增量。每一步必须指向批处理开始时存在的元素。先打开菜单再操作菜单项的流程
需要拆成多次调用，因为菜单项在两次调用之间的 settle 后才存在。语法与响应格式
见 [`somnus/sui/grammar.md`](somnus/sui/grammar.md)。

### 脚本（`run`）

`run` 接受一段脚本，每行一步。纯 `CALL` 脚本保留批处理的预校验。出现 `AWAIT`
后改为逐步执行：执行、settle、重读、解析下一步。后面的步骤可以引用前面刚创建
的元素。

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
→ RUN OK steps=3/3 rev=9
```

- `AWAIT [TYPE] "name" [in ref] [timeout=s]` 把 `$it` 绑定到第一个命中。
  默认 2 秒；`timeout=0` 执行一次搜索。它反复重读视口直到命中或超时。
- 某一步失败时脚本停止：`RUN PARTIAL steps=2/3 failed-at=3`，随后给出每步状态
  与真正执行过的步骤产生的合并增量。
- 限制：跨窗口流程必须显式写窗口 ref，因为切换会重排 ref。增量是脚本首尾的
  净差。`$it` 是第一个命中，不是集合。
- 真机菜单流程实测：一次 3 步 run 用 453 ms；两次分开的 `call` 用 428 ms。
  运行时成本不变，因为每一步仍然 settle。节省的是一次模型推理。

### 通用模式

pack 是可选优化。使用 `--no-pack`，或窗口没有匹配的 pack 时，编译器从结构与
原生模式推导能力。

- 控件由平台定义：暴露交互模式（`invoke`、`selectionitem`、`toggle`、`value`
  等）的元素。纯静态文本与布局容器不获得操作。
- 能力由已发布的接口推导。只有平台提供默认动作（`Invoke` 模式或
  `LegacyIAccessible` 回退）时才出现 `activate`。只暴露 `ExpandCollapse` 的
  按钮显示 `+expand +collapse`，没有 `activate`。
- `set_text` 为每个可输入表面推导。路径由控件自身的接口决定，不看类名或角色。
  路径在执行时判定，因为 UIA 会报告元素随后拒绝的接口。生效的路径在整个会话
  内保留。

| 顺序 | 路径 | 前提 | 合成输入 / 剪贴板 |
| --- | --- | --- | --- |
| 1 | `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 / 否 |
| 2 | 剪贴板 + `Ctrl+V` | `allow_clipboard`（默认开启） | 否 / 是 |
| 3 | 聚焦后键入 | `allow_keyboard`（默认开启） | 是 / 否 |

路径 1 通过应用自身发布的接口、在应用自己的线程上写入。路径 2 成本恒定
（10 到 3000 字符约 37 ms），借用剪贴板并逐格式还原。路径 3 是最后一条路径，
可能在目标的输入队列上等待。三条路径都要求目标窗口位于前台。

两条合成通道默认开启。单次关闭使用 `--no-allow-clipboard` /
`--no-allow-keyboard`，持久关闭使用 `somnus config --no-allow-clipboard` /
`--no-allow-keyboard`。无法完成的调用如实报错：

```
CALL q1.set_text("x")
ERR ACTION_NOT_SUPPORTED q1 hint=report-user
    msg="the element offers no interface this runtime can use, and synthetic
         keyboard input is disabled; enable it for one run with
         --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
```

模型只看到 `set_text`；底层通道由运行时选择。实测数据、剪贴板还原保证与已否定
的路径见 [docs/text-input-channels.md](docs/text-input-channels.md)。

### 提供者

| 提供者 | 数据来源 | 状态 |
| --- | --- | --- |
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | 可用 |
| `mock` | 回放的 `RawTree` 固件 | 可用 |
| `peer` | 运行在子会话中的 `Session` | 可用 |

```bash
somnus --provider uia windows
```

编译器、投影与运行时对所有提供者一致。`peer` 提供者的窗口枚举、无障碍树与
合成输入局限于调用方所在的 Windows 会话。

### 窗口焦点与采集告警

`OBSERVE` 与 `CALL` 在读取或操作前提升目标窗口。目标已在前台时不重复提升。
`OBSERVE windows` 不抢占前台。每个被接触窗口的原始状态会被记录，并在 provider
关闭时恢复。

提升被拒绝，或快照返回有标题但没有子节点的窗口时，观察结果报告：

```
~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows
~ tree looks empty; window may be covered or app not rendering
```

## 设置

持久设置放在 TOML 文件中。两条合成通道默认开启。

```bash
somnus config --path              # 文件位置
somnus config                     # 当前生效值；首次运行写出默认文件
somnus config --no-allow-clipboard
somnus config --no-allow-keyboard
somnus config --allow-clipboard
```

通道优先级：单次命令行参数（`--allow-clipboard` / `--no-allow-clipboard`）>
环境变量（`SOMNUS_ALLOW_CLIPBOARD` / `SOMNUS_ALLOW_KEYBOARD`）> 文件 > 内置
默认（开）。配置文件损坏时降级为默认值，并在 `somnus config` 中报告问题。

时间参数可写入同一文件，也可由 `configure` 在运行时修改：

| 键 | 默认 | 范围 | 含义 |
| --- | --- | --- | --- |
| `settle_quiet_ms` | `40` | 10–1000 | UI 需要保持多久不变，才认为一次调用结束。值越小越快；值过小可能读到动画中间态。 |
| `settle_poll_ms` | `10` | 1–100 | 探测间隔。 |
| `settle_timeout_s` | `3.0` | 0.5–30 | 单次 settle 的硬上限；超时后响应写明 `settled=false`，仍携带增量。 |
| `post_action_enum` | `true` | 布尔 | 动作后重新枚举窗口，报告动作打开的新窗口。关闭后每次调用约省 100 ms，可能漏报该窗口。 |

时间参数优先级：内置默认 < 文件 < 环境变量（`SOMNUS_SETTLE_QUIET_MS`、
`SOMNUS_SETTLE_POLL_MS`、`SOMNUS_SETTLE_TIMEOUT_S`、`SOMNUS_POST_ACTION_ENUM`）
< `configure`。`configure` 作用于当前会话的下一次调用。`persist=true` 写回文件。
`reset=true` 恢复启动时的值。任一键非法时整批拒绝。

```json
{"settle_quiet_ms": 80, "settle_poll_ms": 20, "post_action_enum": false}
```

```text
SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

两项通用行为：

- `context_menu` 在可能有菜单的元素上提供，条件是元素暴露可操作内容。菜单通过
  UIA（`ShowContextMenu`）打开，指针不移动，不发送按键。UIA 无法报告元素是否
  有菜单，因此结果在调用后如实报告：弹出了菜单返回 `OK`；未弹出返回
  `OK_UNVERIFIED`，并指出真正可用的手势。
- 实体按结构判定：带标题和动词按钮的行，或同一容器下三个以上的标题行（列表、
  工具栏、导航栏）。

## 子会话

子会话是同一账户的第二次交互式登录。它拥有自己的 input desktop、前台窗口与
输入队列，因此合成输入与窗口提升不影响控制台会话。

前置条件与代价：

- Windows 专业版 / 企业版 / 教育版。家庭版无法创建子会话。
- 一次管理员权限的 `somnus child-session setup`。该命令启用功能、RDP 监听与
  凭据委托，并把 `TermService` 设为自动。
- 子会话通过 RDP 环回进入，连接存在时它才存在。Windows Hello PIN 不能用于这次
  登录。凭据委托被拒时，用 `somnus child-session credential --save` 保存一次
  账户密码。
- 全系统同时只允许一个子会话。
- 两个会话是同一账户，没有安全隔离。

CLI：

| 命令 | 效果 |
| --- | --- |
| `somnus child-session status` | 只读：功能可用性与会话状态 |
| `somnus child-session setup` | 唯一需要管理员的一步；`--dry-run` / `--disable` |
| `somnus child-session start` | 打开环回（`--headless`、`--auto`） |
| `somnus child-session peer-setup` | 让 peer 随子会话登录启动（每用户 Run 键） |
| `somnus child-session probe` | 向管道另一侧的 peer 询问可见内容 |
| `somnus child-session close` | 结束会话并清理残留 |
| `somnus child-session run -- <cmd>` | 在子桌面运行一个程序，打印退出码与输出 |

`somnus child-session run` 向 peer 发送 `process.start`，轮询
`process.status` 直到命令退出（或 `--timeout`，默认 600 秒，超时后杀死），
然后打印退出码与完整 stdout/stderr。输出文件位于
`%TEMP%\somnusui-child-run\<timestamp>\`，只保留最近一次。该命令受
`child_allow_spawn`（默认 `true`）与 `allow_processes`（空 = 不限制）约束。
`allow_processes` 与“允许操作哪些进程”是同一份列表；不能被操作的进程也不能被
启动。`run` 只提供 CLI，SUI-Text 中没有 `SESSION run`。

对 agent 而言，同一时间只有一个活动桌面：

| 命令 | 效果 |
| --- | --- |
| `SWITCH main` | 回到用户桌面 |
| `SWITCH child` | 切到子会话（须已打开） |
| `SESSION open` | 打开子会话并切过去 |
| `SESSION close` | 结束并返回 |
| `SESSION detach` | 本进程退出后保留 |

切换会替换 provider，并重置注册表与窗口 ref 映射。切换后 `w1` 不再指向之前的
窗口。操作前重新 `OBSERVE`。编译器、投影、注册表、解析器、策略与 ref 命名空间
留在主侧；子会话通过命名管道运行一个瘦 peer。设计说明、实测数据与已否定的方案
见 [docs/child-session.md](docs/child-session.md)。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/what-the-llm-sees.md](docs/what-the-llm-sees.md) | 一个窗口及其工具调用，附真实输出 |
| [docs/interactive-usage.md](docs/interactive-usage.md) | REPL、单次 CLI 与脚本用法 |
| [docs/packs.md](docs/packs.md) | App Pack 编写 |
| [docs/child-session.md](docs/child-session.md) | 子会话设计、代价与已否定的方案 |
| [docs/text-input-channels.md](docs/text-input-channels.md) | 文本输入路径及其代价 |
| [docs/optimization-plan.md](docs/optimization-plan.md) | 实测、改动与剩余工作 |
| [docs/viewport-capture-proposal.md](docs/viewport-capture-proposal.md) | 视口采集的测量与设计 |
| [docs/scenario-suite.md](docs/scenario-suite.md) | 验收界面与通过标准 |
| [docs/benchmark-50-tasks.md](docs/benchmark-50-tasks.md) | 50 个用户任务与实测覆盖 |

## 仓库结构

```
somnus/
├── ir/            节点 / 能力 / 动作值对象
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → twins → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── session/       子会话：childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── packs/         generic.py restaurant_demo.py
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      持久设置
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
apps/scenario-suite/    WPF（.NET 8）验收应用
tools/rdp-host/         .NET（WinForms）OLE 容器，承载 RDP 控件
docs/                   上表所列文档
examples/               agent 循环、契约检查、任务评估
bench/                  基准门槛与 LLM 控制演示
tests/{fixtures,scenarios,golden,compiler,projection,runtime,providers,e2e}
```

依赖方向：`providers → compiler → projection → serialization → protocol`；
`runtime` 依赖以上全部。任何模块都不导入语言模型。

### 验收应用

`apps/scenario-suite` 包含五个界面，每个对准一类难点：

| 界面 | 内容 | 覆盖点 |
| --- | --- | --- |
| `store` | 搜索框、240 行可滚动列表、购物车 | 视口折叠（`rows=` / `visible=`）、`FIND` 到达屏幕外的行、原生搜索写入 |
| `form` | 带标签字段、复选框、下拉、禁用按钮、受校验提交 | 可写与只读字段、校验、提交记录 |
| `chat` | 自绘编辑器、消息历史、发送按钮、发送记录 | 无写入接口的控件上的 `set_text` |
| `console` | 文本框编辑器、联系人列表 | 路径选择：有自身写入接口的控件不使用合成通道 |
| `tree` | 可展开嵌套树 | 按展开等级压缩 |

每个界面存在两份：真实 WPF 窗口（`ScenarioSuite.exe <界面>`）与固件
（`tests/fixtures/scenarios.py`）。因此 `tests/scenarios/` 可在任意平台运行。
`tests/e2e/test_windows_scenarios.py` 验证固件与真实控件一致。

```bash
somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
```

### App Packs

pack 为应用增加领域词汇（`PRODUCT p1 +add`）。没有匹配 pack 的窗口走通用编译
路径，仍然可操作；见通用模式。

```python
from somnus.packs.restaurant_demo import build_pack
from somnus.runtime.session import Session
from somnus.providers.mock import MockProvider

session = Session(provider, pack=build_pack())
```

编写指南：[docs/packs.md](docs/packs.md)。

## 测试

```bash
pytest                      # 可移植套件，含验收界面套件
pytest tests/scenarios      # 固件上的验收界面套件
pytest --update-golden      # 重写 golden SUI-Text 固件
dotnet build apps/scenario-suite
pytest -m windows           # 真实窗口，仅 Windows
```

可移植套件（`tests/scenarios/`）在固件上驱动五个验收界面，可在任意平台运行。
真实窗口套件（`tests/e2e/test_windows_scenarios.py`）通过真实 UIA provider 驱动
同一批界面。`MockProvider` 回放录制的 `RawTree` 固件并执行脚本化变更，包括通过
具名回调实现的逐行行为。

本机实测：可移植套件 733 passed；`pytest -m windows` 38 passed，在子会话内运行，
约 62–66 秒，没有出现 `window ... did not appear` 超时。UWP 计算器在该子桌面
能启动并读取，`OBSERVE full` 读到 34 个按钮。

## 指标

验收门槛（`python -m somnus.cli bench --runs 100`，mock/固件）：

| 指标 | 目标 | 实测（100 次） |
| --- | --- | --- |
| 零视觉 | 0 次视觉/坐标调用 | 0（结构性） |
| 信息隔离 | 模型上下文中 0 个原生 ID | 0（泄露扫描） |
| 压缩比 | ≥ 10× | 1373.6×（最小值，完全展开的树） |
| 动作成功率 | ≥ 99% | 100% |
| 任务成功率 | ≥ 90% | 100%（中位 3 步） |
| L0 观察 | ≤ 300 token | 91 token（中位数与最大值） |

小型且未截断的视口窗口可把 L0 预算放宽到 80 节点 / 深度 10 / 700 token，以
露出深层控件。

隔离检查枚举被测树中实际存在的 automation id、类名与 runtime id，断言它们
不出现在模型收到的文本中。仅看起来像 ID 的应用数据（如表单的 `REQ-0001`）
不计为泄露。

### 真实窗口性能

同一台机器，Windows 11，`apps/scenario-suite` 的 store 界面（240 行非虚拟化
`ItemsControl`、1467 节点原始树），真实 UIA provider，
i5-13500HX / 2560×1440@96DPI。`CALL` 的区间来自多次重复；`observe` 为单次。

| 场景 | 之前 | 之后 | 变化 |
| --- | --- | --- | --- |
| 观察 store | 3385 ms | 594 ms（−82.5%） | 视口采集：骨架、行窥视、只完整读可见行 |
| store 上的 `CALL`（树无变化） | ~6 s | 686–706 ms | 签名探测；无变化时 0 次全量快照 |
| 签名探测本身 | 1622–1938 ms | 384–404 ms | 探测只读覆盖集上的签名字段 |
| 小窗口上的 `CALL` | 423 ms | 215–231 ms | settle 帧数减少（`quiet=40 ms`、`poll=10 ms`） |
| store 观察的 token | 292 | 292 | 不变 |
| 桌面变化检测（`top_level_signature`） | — | p50 0.31–0.52 ms（max ≤2.15 ms） | Win32 `(hwnd, title)` 走查；`list_windows` p50 70–78 ms |
| 写入后标题不变 | — | 总 81–104 ms / enum 0.6–0.9 ms | 桌面签名一致，跳过枚举 |
| 写入后标题变化 | — | enum 78–104 ms / 总 154–238 ms | 标题变化会改变窗口列表，执行全量枚举 |
| `observe()` 的窗口枚举 | — | 78–102 ms | 观察仍然全量枚举 |

store 上产生变化的 `CALL` 仍需一次全量快照，本轮未复测。`SOMNUS_PROFILE=1`
打印分段耗时（枚举 / 快照 / 编译 / settle）。

## 许可证

MIT。
