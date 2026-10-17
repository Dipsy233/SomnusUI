# SomnusUI

**语言模型会梦见像素吗？**

[English](README.md) | 中文

SomnusUI 将原生 GUI 编译为纯文本的语义 API：语言模型通过**能力调用**（capability
calls）而不是像素点击来操作桌面软件。模型收到的是每个控件的语义描述——它是什么、
能做什么；模型不会收到截图、坐标或任何原生标识符。

```
用户意图
   ↓
LLM 规划器（五个工具）
   ↓
Somnus 运行时 ─ observe / find / inspect / call / switch
   ├─ 会话注册表        ref ↔ 指纹 ↔ 原生元素
   ├─ 解析器            语义能力 → 原生原语
   ├─ 验证器            稳定 + 效果 + 增量
   └─ 策略              风险、确认、白名单
   ↓
语义图
   ↑
语义编译器（确定性：normalize → infer → match → project → compress）
   ↑
AccessibilityProvider（Windows UI Automation；后续支持 AX / AT-SPI）
   └─ peer provider：位于会话边界另一侧的 Session
```

## 设计原则

- **无视觉（No Vision）。** 无障碍树是唯一的感知通道。
- **默认不使用合成输入。** 应用通过其自身发布的接口被驱动。发布不出可用接口的
  元素会**如实报错**，而不是被键入内容；剪贴板与键盘通道（`--allow-clipboard`、
  `--allow-keyboard`）用于确实别无他法时，两者都会抢占前台，响应中会说明实际
  由哪条通道完成。
- **低 token。** 观察结果经过投影与预算控制：同类列表只预览少量条目，匿名叶子
  折叠为一行汇总。L0 概览通常为几十到数百 token。
- **语义优先。** 模型调用 `CALL p1.add()`，而非 `click(312, 528)`。
- **确定性编译器。** 运行时与编译器内部不运行任何语言模型。
- **如实失败（Fail Loud）。** 无障碍 API 无法表达的内容一律报告为 `UNKNOWN`，
  绝不猜测。

## 快速开始

```bash
pip install -e ".[dev]"

# 回放内置的餐厅演示（无需 Windows）：
somnus --demo observe
somnus --demo call 'CALL p1.add()'
somnus --demo bench --runs 100

# 操作真实的 Windows 应用（需要 [windows] 扩展）：
pip install -e ".[windows]"
somnus windows                     # 列出窗口
somnus observe                     # 目标窗口提供了什么
somnus observe w1                  # 只观察某个窗口
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # 多步会话，不应用 App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP 服务器（stdio），五个工具

# agent 可以待在其中、不干扰用户的第二个桌面（Windows 专业版及以上）：
somnus child-session status        # 本机支持吗？
somnus child-session setup         # 一次性，需管理员
somnus child-session start         # 打开
somnus child-session close         # 结束并清理
```

### 作为 MCP 服务器

任何 MCP 客户端都可接入，例如 Claude Desktop、Cursor、pi：

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

服务端暴露 `observe` / `find` / `inspect` / `call` / `switch` 五个工具。
provider 自动选择：安装了 `[windows]` 扩展时驱动真实窗口，否则回放录制的固件。

## 模型看到什么

一个真实窗口，四次工具调用。除此之外的内容都不会呈现给模型。

**① `observe`** —— 以能力形式呈现的当前屏幕：

```
@rev 1
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← 可写入的文本框
  INPUT q2 "只读路径" readonly               ← 同为 INPUT 但没有 +set_text：只读
  BUTTON b1 "共享(S)..."                      ← 可点击（activate 隐含）
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← 当前不可用
  TAB t3 "共享"
```

**② `find`** —— 按关键词定位，每条命中都是完整的实体行：

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

**③ `inspect`** —— 查看单个节点的全部信息，含隐含能力与关系：

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**④ `call`** —— 执行操作，返回增量（delta）而非全新观察：

```
CALL tx1.set_text("Hello SomnusUI")
→ OK_UNVERIFIED rev=2
  ~ tx1 value →Hello SomnusUI                  已修改
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   新增
  - t2                                          移除
```

失败时输出一行错误信息并附恢复提示，不会出现堆栈跟踪：

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

模型永远看不到句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始
`control_type`，也看不到 UIA 转储中大量无名的 `Pane` 包装节点。完整示例见
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md)。

## 通用模式（Generic mode）

pack 是优化手段，不是必要条件。使用 `--no-pack`（或窗口本来就没有对应 pack）时，
编译器完全从结构与原生模式推导，因此从未见过的应用依然可用：

- **控件**由平台定义：暴露交互模式（`invoke`、`selectionitem`、`toggle`、
  `value` 等）的元素。纯静态文本与布局容器不会获得任何操作。
- **能力由平台证明，绝不依据角色推断。** 只有平台确实提供默认动作（`Invoke`
  模式，或执行器的 `LegacyIAccessible` 回退）时才出现 `activate`。只暴露
  `ExpandCollapse` 的按钮显示 `+expand +collapse` 而没有 `activate`；什么都没
  暴露的控件不会得到一个注定失败的能力。
- **`set_text`** 为每个可输入表面推导，写入路径由控件自身的接口决定，绝不看类名
  或角色。路径按对用户的打扰程度排序，依次尝试、第一个真正可用的胜出：

  | 顺序 | 路径 | 前提 | 抢占前台 |
  | --- | --- | --- | --- |
  | 1 | `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 |
  | 2 | `WM_SETTEXT` / `EM_REPLACESEL` | 元素自己映射了 HWND | 否 |
  | 3 | 剪贴板 + `Ctrl+V` | 需 `--allow-clipboard` | 是 |
  | 4 | 聚焦 + 键入 | 需 `--allow-keyboard` | 是 |

  前两条走的是应用自身发布的接口，写入在应用自己的线程上完成：原子、不抢焦点、
  不产生合成输入。只有前两条都用不上时——典型是自绘输入框——才使用后两条，而
  它们都要求目标在前台。哪条路径可用**在执行时**判定，因为静态检查不可能准确：
  UIA 会报告元素实际拒绝的接口。生效的路径会在整个会话内被记住。

  没有可用路径时如实报错，而不是静默改道：

  ```
  CALL q1.set_text("x")
  ERR ACTION_NOT_SUPPORTED q1 hint=report-user
      msg="the element offers no interface this runtime can use, and synthetic
           keyboard input is disabled; enable it for one run with
           --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
  ```

  模型只看到 `set_text` 一个名字，底层由哪条通道完成由运行时决定。各通道的实测
  数据、剪贴板还原保证与已否定的路径见
  [docs/text-input-channels.md](docs/text-input-channels.md)。

### 设置文件

MCP 客户端会自行启动进程，命令行参数并不是可行的 opt-in 方式，因此两条通道都可以
写进一个小型 TOML 配置文件：

```bash
somnus config --path              # 文件位置
somnus config                     # 查看状态（首次会写出默认文件）
somnus config --allow-clipboard   # 打开剪贴板通道，持久生效
somnus config --allow-keyboard    # 打开键盘通道
```

优先级：**命令行参数 > 环境变量（`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`）> 文件 > 内置默认**。配置文件损坏时会降级为默认值，并在
`somnus config` 中报告问题，绝不会变成“全部允许”。

- **`context_menu`** 在可能有菜单的控件上提供（即暴露了可操作内容的元素）。它是
  面对忽略原生模式的应用时的出口：菜单通过 UIA 自身打开（`ShowContextMenu`），
  指针不移动、不发送按键，菜单项本身是标准菜单项，其 `activate` 可用。UIA 无法
  查询元素“是否有菜单”，因此调用后如实报告结果：弹出菜单返回 `OK`，未弹出返回
  `OK_UNVERIFIED`，并指出真正可用的手势。
- **实体**按结构识别：标题加动词按钮的行，或同一容器下三个以上的标题行（列表、
  工具栏、导航栏）。

```bash
somnus --no-pack repl          # 驱动任意应用
somnus --no-pack observe w4
```

pack 仍适合编码通用路径无法推断的领域词汇（`PRODUCT p1 +add`、`+submit!`），但
为了“够到并操作一个控件”从不需要它。

## 提供者（Providers）

运行时通过唯一的 `AccessibilityProvider` 接口与外界通信，编译器、投影和运行时对
所有提供者完全一致。

| 提供者 | 数据来源 | 状态 |
| --- | --- | --- |
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | 可用 |
| `mock` | 回放录制的 `RawTree` 固件（任意平台） | 可用 |
| `peer` | 运行在子会话里的 `Session` | 可用 |

```bash
somnus --provider uia windows          # 原生窗口
```

`peer` 提供者支撑下文所述的子会话：窗口枚举、无障碍树与合成输入都局限于调用方
所在的 Windows 会话，因此访问第二个桌面需要在那些窗口真正所在的会话中运行一个
provider。

## 使用前先把窗口提到前台

`OBSERVE` 和 `CALL` 会把目标窗口提到前台。被遮挡、最小化或位于其他桌面的窗口
并不可靠地渲染——自绘界面（Chromium、Electron、Qt Quick）在真正显示之前什么都
不渲染。因此所有读取或操作窗口的路径都会先提窗口，窗口已在前台时跳过；
`OBSERVE windows`（窗口清单）是例外，永远不抢前台。每个被接触窗口的原始状态都会
被记录，并在 provider 关闭时恢复。

### 不够用时：子会话

提窗口会拿走用户的焦点，剪贴板与键盘通道还会向前台窗口发送合成输入。**子会话**
——同一账户的第二次交互式登录，拥有自己的 input desktop、前台窗口和输入队列——
彻底消除了这种干扰：在其中工作不会影响到控制台会话中的用户。

```bash
somnus child-session setup     # 一次性，需管理员
somnus child-session start     # 打开环回
somnus child-session close     # 结束并清理
```

对 agent 来说这只是一个工具；它同一时间只在一个桌面上，用 `SWITCH` 移动：

| 命令 | 效果 |
| --- | --- |
| `SWITCH main` | 回到用户桌面 |
| `SWITCH child` | 切到子会话（须已打开） |
| `SESSION open` | 打开并切过去 |
| `SESSION close` | 结束并回来 |
| `SESSION detach` | 本进程退出后仍保留 |

编译器、投影、注册表、解析器、策略和 ref 命名空间全部留在主侧；子会话中只运行
一个瘦 peer，通过命名管道应答工具调用。要求：Windows 专业版 / 企业版 / 教育版、
一次管理员配置、全系统同时只允许一个子会话。两个会话是同一账户，不提供安全隔离。
设计说明、实测数据与试过但不采用的方案见 [docs/child-session.md](docs/child-session.md)。

## 五个工具

| 工具 | 用途 | 返回 |
| --- | --- | --- |
| `observe` | 目标窗口的 L0 概览 | SUI-Text |
| `find` | `FIND [type] "query" [in ref]` | 匹配的实体行 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 细节 |
| `call` | `CALL ref.cap(args)` | `OK`/`ERR` + 增量 |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` + 窗口清单 |

## SUI-Text

唯一面向模型的格式。能力直接写在节点行上；默认能力与默认状态省略（系统提示词中
携带默认表）。

```
@rev 1
WINDOW w1 "Restaurant Demo" active
  SCREEN s1 "menu"
    LIST l1 "Products" count=3 entity=product +find
      PRODUCT p1 "Big Mac" price=5.99 +add +customize(size:small|medium|large)
    CART c1 "Cart" count=0 total=0 +open
    CHECKOUT b5 "Checkout" +submit!
```

操作与结果：

```
FIND product "Big Mac"   →  FOUND 1 / PRODUCT p1 "Big Mac" price=5.99 +add
CALL p1.add()            →  OK rev=2
                            ~ c1 count 0→1
                            ~ c1 total 0→5.99
CALL b5.submit()         →  ERR CONFIRMATION_REQUIRED b5 hint=ask-user
```

完整文法见 [`somnus/sui/grammar.md`](somnus/sui/grammar.md)。

## 仓库结构

```
somnus/
├── ir/            节点 / 能力 / 动作值对象（平台无关）
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── session/       子会话：childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── packs/         generic.py restaurant_demo.py（内置演示 pack）
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      持久化设置（两个合成通道的 opt-in 写在这里）
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
apps/scenario-suite/    WPF（.NET 8）验收应用：覆盖真实 GUI 五种形态的界面
tools/rdp-host/         .NET（WinForms）OLE 容器，承载 RDP 控件；这是 Python
                        无法承载的一环。用 dotnet build 构建。
docs/
  what-the-llm-sees.md      一个窗口、五个工具，附真实输出
  interactive-usage.md      REPL / 单条 CLI / 脚本三种驱动方式
  packs.md                  App Pack 编写指南
  child-session.md          第二个桌面：设计、代价与已否定的方案
  text-input-channels.md    文本如何写入控件，以及每条路径的代价
  scenario-suite.md         验收界面清单与通过标准
  benchmark-50-tasks.md     语义 API 与视觉点击对比基准（方案）
examples/               agent 循环、契约检查、任务评估（宿主示例代码）
bench/                  基准门槛 + LLM 控制演示
tests/{
  fixtures/             原始树构造器，含 `scenarios.py`（验收界面固件）
  scenarios/            S1：验收界面在固件上跑 —— 任意平台
  golden,compiler,projection,runtime,providers/
  e2e/                  S2：真实窗口（`-m windows`）
}/
```

依赖方向：`providers → compiler → projection → serialization → protocol`；
`runtime` 依赖以上全部；任何位置都不导入语言模型。

### 验收应用：一个应用，五种界面

GUI agent 的水平取决于它被什么样的界面试过，因此验收应用是一组**界面**，每个
对准一类真实难点：

| 界面 | 它是什么 | 它逼运行时做对什么 |
| --- | --- | --- |
| `store` | 搜索框 + 240 行可滚动商品列表 + 购物车 | 大数据量：折叠、`FIND` 进入被折叠的列表、按原生接口写入搜索框 |
| `form` | 带标签的字段（一个可写、一个只读）、复选框、下拉、禁用按钮、需确认的提交 | 可写 / 只读在行上可分辨，校验是真实结果，**提交记录**才是验证面 |
| `chat` | 自绘富文本编辑器、消息历史、发送按钮、可见的发送记录 | 不发布任何可写接口的控件：如实拒绝，或走显式开启的通道，然后记录发送 |
| `console` | 普通文本框编辑器 + 联系人列表 | 路由对照：有自己写入接口的控件绝不能退到合成通道 |
| `tree` | 可展开的嵌套树（数千节点） | 按展开等级压缩：概览有界，明细只为真正展开的部分付费 |

每个界面存在两份：真实 WPF 窗口（`apps/scenario-suite`，用
`ScenarioSuite.exe <界面>` 启动）与复刻真实无障碍形状的固件
（`tests/fixtures/scenarios.py`）。因此 `tests/scenarios/` 可以在任意平台运行，
而 `tests/e2e/test_windows_scenarios.py` 负责证明固件没有偏离真实控件。

无需 Windows 即可回放任意界面：

```bash
somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
```

## App Packs（可选）

Pack 为应用程序提供领域词汇，把通用控件树变成带有能力的实体（如
`PRODUCT p1 +add`）。它是优化手段而非必要条件：没有 pack 的窗口走通用编译路径，
一样可以到达和操作（见通用模式）。

```python
from somnus.packs.restaurant_demo import build_pack
from somnus.runtime.session import Session
from somnus.providers.mock import MockProvider

session = Session(provider, pack=build_pack())
```

编写指南见 [`docs/packs.md`](docs/packs.md)。

## 测试

```bash
pytest                      # 全部可移植测试，含验收界面套件（S1）
pytest tests/scenarios      # 只跑固件上的验收界面套件
pytest --update-golden      # 重写 golden SUI-Text 固件

# S2：真实窗口（仅 Windows 运行器）
dotnet build apps/scenario-suite
pytest -m windows
```

分两层是刻意的。**S1**（`tests/scenarios/`）在固件上驱动五个验收界面，因此任何
平台、每次提交都能运行。**S2**（`tests/e2e/test_windows_scenarios.py`）用真实
WPF 窗口经真实 UIA provider 驱动同一批界面，它才是 S1 可信的依据：固件一旦与
真实控件脱节，S1 会通过而 S2 会失败。

`MockProvider` 回放录制的 `RawTree` 固件并执行脚本化的变更——包括通过具名回调
实现的逐行行为——因此编译器、投影、解析器、增量、路由链与验证等路径都可以在
没有桌面的环境下得到覆盖。

## 指标

| 指标 | 目标 | 当前（`somnus --demo bench --runs 100`） |
| --- | --- | --- |
| 零视觉（Zero Vision） | 0 次视觉/坐标调用 | ✓（结构性保证） |
| 信息隔离 | 模型上下文中 0 个原生 ID | ✓（泄露扫描 = 0） |
| 压缩比 | ≥ 10× | ≥ 1500×（完全展开的树） |
| 动作成功率 | ≥ 99% | 100% |
| 任务成功率 | ≥ 90% | 100% |
| L0 观察 | ≤ 300 token | 80 token（240 行目录 + 展开树） |

运行方式：`somnus --demo bench --runs 100`。隔离检查是**取证式**而非正则臆测：
它枚举被测树中实际存在的 automation id、类名和 runtime id，断言它们没有出现在
模型会收到的文本里；因此仅仅看起来像 ID 的应用数据（表单自己的 `REQ-0001`）
不会被误判成泄露。

## 许可证

MIT。
