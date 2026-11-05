# SomnusUI

**语言模型会梦见像素吗？**

[English](README.md) | 中文

SomnusUI 将原生 GUI 编译为纯文本的语义 API：语言模型通过**能力调用**
（capability calls）而不是像素点击来操作桌面软件。模型收到的是每个控件的语义
描述——它是什么、能做什么；模型不会收到截图、坐标或任何原生标识符。

```
用户意图
   ↓
LLM 规划器（七个工具）
   ↓
Somnus 运行时 ─ observe / find / inspect / call / run / switch / configure
   ├─ 会话注册表        ref ↔ 指纹 ↔ 原生元素
   ├─ 解析器            语义能力 → 原生原语
   ├─ 验证器            稳定 + 效果 + 增量
   └─ 策略              白名单 + 输入通道开关
   ↓
语义图
   ↑
语义编译器（确定性：normalize → infer → match → 去重 → project → compress）
   ↑
AccessibilityProvider（Windows UI Automation；后续支持 AX / AT-SPI）
   └─ peer provider：位于会话边界另一侧的 Session
```

## 设计原则

- **无视觉（No Vision）。** 无障碍树是唯一的感知通道。树只从**可视视口**读取：
  屏幕外的行汇总成 `rows= / visible=`，不会静默丢弃；`IsOffscreen` 不作为判据
  （实测一个 240 行的列表里只有 12 行在屏内，它却报告 0 行 offscreen）。
- **所有操作都会抢占前台。** 窗口在被读取或写入之前会被提到前台；文本通过应用
  能够验证的通道写入——优先 `ValuePattern`，其次剪贴板通道，最后是键盘通道。
  它们都要求目标处于前台，响应中会说明实际由哪条通道完成。运行时不做无焦点
  写入：一次落在"当时焦点在哪"上的写入，既不可预期也无法归因。需要在不打扰
  用户的前提下工作，请把 agent 放进子会话（见
  [docs/child-session.md](docs/child-session.md)）。发布不出可用接口的元素会被
  **如实报错**。
- **能力优先，没有确认门控。** 没有二次确认、没有风险评分、没有危险操作拦截：
  节点行上印出的能力，被调用就直接执行。`!` 后缀（`+submit!`）只是能力的声明
  性元数据，不是权限请求。是否要问人由宿主决定，运行时不做。
- **低 token。** 观察结果经过投影与预算控制，并且只从可视视口读取：同类列表只
  预览少量**完整**条目，其余折叠为 `… +N more`；匿名叶子折叠为一行汇总。L0
  概览通常为几十到数百 token。
- **语义优先。** 模型调用 `CALL p1.add()`，而非 `click(312, 528)`。
- **确定性编译器。** 运行时与编译器内部不运行任何语言模型。
- **如实失败（Fail Loud）。** 无障碍 API 无法表达的内容一律报告为 `UNKNOWN`，
  绝不猜测；UIA 声称可用、控件实际拒绝的能力也会如实回报。

## 快速开始

以下命令请在**仓库根目录**执行（`bench/` 与 `tests/` 是按路径引用、并未安装的
包）。

```bash
pip install -e ".[dev]"

# 回放内置的餐厅演示（无需 Windows）：
somnus --demo observe
somnus --demo call 'CALL p1.add()'
python -m somnus.cli bench --runs 100   # 控制台脚本形式无法 import bench/

# 操作真实的 Windows 应用（需要 [windows] 扩展）：
pip install -e ".[windows]"
somnus windows                     # 列出窗口
somnus observe                     # 目标窗口提供了什么
somnus observe w1                  # 只观察某个窗口
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # 多步会话，不应用 App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP 服务器（stdio），七个工具

# agent 可以待在其中、不干扰用户的第二个桌面（Windows 专业版及以上）：
somnus child-session status        # 本机支持吗？
somnus child-session setup         # 一次性，需管理员
somnus child-session start         # 打开
somnus child-session close         # 结束并清理
```

> **ref 只活在一个会话里。** `somnus observe` / `somnus call` 是单次命令：执行
> 完这一次工具调用进程就退出，所有 ref（`p1`、`w1`、`q1` …）随进程一起失效。
> 只要任务超过一次调用（绝大多数真实任务都是），请用 **`somnus repl`**（一个
> 存活的会话、可连续输入）、**MCP 服务器**（每个客户端一个长生命周期会话），或
> **`run`** 工具（一次调用跑完整个流程）。`somnus --demo` / `somnus --scenario`
> 同理——它们适合探索单次观察，不适合多步操作。
>
> 为什么是 `python -m somnus.cli bench` 而不是 `somnus ... bench`：控制台脚本的
> `sys.path` 从 `.venv/Scripts` 开始，仓库内的 `bench` 包不在其中；在仓库根用
> `python -m` 才会把仓库放进 `sys.path`。上面其余命令都可以用 `somnus` 入口。

### 作为 MCP 服务器

任何 MCP 客户端都可接入，例如 Claude Desktop、Cursor、pi：

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

服务端暴露 `observe` / `find` / `inspect` / `call` / `run` / `switch` /
`configure` 七个工具。provider 自动选择：安装了 `[windows]` 扩展时驱动真实窗口，
否则回放录制的固件。启动时服务端会在 `instructions` 里写明当前生效的 settle
参数（`settle_quiet_ms=40`、`settle_poll_ms=10`、`settle_timeout_s=3.0`）与
`post_action_enum=on` 默认值，模型在第一次调用前就能知道这些值及其取舍；
`configure` 可以在运行时修改。

## 模型看到什么

一个真实窗口，四次工具调用。唯一的桌面上下文是那一行 `WINDOWS` 摘要（窗口
ref、标题、是否 active），不包含其他窗口的内容。

**① `observe`** —— 先给桌面摘要，再以能力形式呈现当前屏幕：

```
@rev 1
WINDOWS w1 "agent 属性" active
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

`WINDOWS` 行最多列出**六个**顶层窗口，并且只标记一个 `active`；超过六个的部分
折叠为 `+N more`，active 窗口始终在列（若它落在前六之外，会替换第六席）。它
**不带** `ops` 计数（统计可操作元素需要每个窗口做一次浅读，实测 5 个窗口合计
约 111 ms，超过观察约 80 ms 的预算）。只想看窗口清单时，`OBSERVE windows` 依然
最便宜。

超过视口的大列表**只读可见部分**，屏幕外的行以摘要呈现，不会静默缺失：

```
@rev 4
WINDOWS w1 "Store" active
WINDOW w1 "Store" active
  SCREEN s1 "catalog"
    LIST l1 "Products" rows=240 visible=12 scroll=5% +find
      ITEM li1 "Item 0" price=$1.00 +add          ← 可见行是完整读取的
      ITEM li2 "Item 1" price=$1.50 +add
      ITEM li3 "Item 2" price=$2.00 +add
      +more items=228 use FIND "<name>" or OBSERVE w1 expand l1
```

`rows=` 是真实总数，`visible=` 是屏内行数。要够到屏幕外的行，用
`FIND "<name>"`，或用 `OBSERVE w1 expand l1` 让采集层为这个容器再读一页（页的
大小没有规定，取决于采集层对该 ref 的读取）。滚动/展开会让视口失效，当采集集
与屏幕不再对应时，下一次响应会带 `~ viewport changed; OBSERVE again`。

**② `find`** —— 按关键词定位，每条命中都是完整的实体行：

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

`find` 还接受一个可选的 `timeout`（秒）：给了就等，直到命中或超时；省略则立即
回答（一次内存搜索，通常 0–4 ms）。

**③ `inspect`** —— 查看单个节点的全部信息，含隐含能力与关系：

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**④ `call`** —— 执行操作，返回增量（delta）而非全新观察：

```
CALL tx1.set_text("Hello SomnusUI")
→ OK rev=2
  ~ tx1 value →Hello SomnusUI                  已修改
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   新增
  - t2                                          移除
```

`OK_UNVERIFIED` 用于"已经稳定、但增量为空"的情况（执行了，但没有可见变化）；
只要产生了增量，状态就是 `OK`。

失败时输出一行错误信息并附恢复提示，不会出现堆栈跟踪：

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

模型永远看不到句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始
`control_type`，也看不到 UIA 转储中大量无名的 `Pane` 包装节点。完整示例见
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md)。

**⑤ `read`** —— 隐式的内容能力。`CALL q1.read()` 返回控件发布的内容数据，而不是增量：

```
CALL q1.read()
→ OK rev=5
  ~ q1 value="共享" len=2
```

- 载荷是 `value="…" len=N`。`len=` 通常精确；如果触发了 provider 的
  **4096 字符读取上限**，则变成 `len>=4097`，行尾追加
  `truncated=true; use INSPECT with range=0:2000 for more`。
- 内联最多返回 **200 字符**（超长时为 199 + `…`）；更长的内容用 `INSPECT`
  的 `range=` 读取。
- 先试 UIA `Text` 模式，再试 `Value` 属性；两者都没有的纯标签退回其
  accessible Name。
- `read` 是隐式的：L0 不打印 `+read`，`INSPECT` 只在节点确实发布可读接口时列出它。

SUI-Text 中所有带引号的字符串（包括 `value=`）都转义 `\\`、`\"`、`\r`、`\n`、
`\t`，因此一个节点永远只占一行。预览只用于**阅读**，不用于回写：解析器只解码
`\\` 与 `\"`。`observe`/`inspect` 还会为文本类节点打印一个短的 `value=` 预览
（最多 **80 字符**，截断时带 `…`）；超过 160 字符的值改用序列化器的
`len= / preview=` 形式。

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

  | 顺序 | 路径 | 前提 | 合成输入 / 借剪贴板 |
  | --- | --- | --- | --- |
  | 1 | `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 / 否 |
  | 2 | 剪贴板 + `Ctrl+V` | `allow_clipboard`（默认开启） | 否 / **是** |
  | 3 | 聚焦 + 键入 | `allow_keyboard`（默认开启） | 是 / 否 |

  第 1 条走的是应用自身发布的接口，写入在应用自己的线程上完成：原子、不产生
  合成输入、不借用户的剪贴板。第 2 条成本恒定（10 到 3000 字符都约 37 ms），
  能到达不发布任何可写接口的控件，但会借用用户的剪贴板并逐格式还原。第 3 条
  是最后手段，可能在目标的输入队列上排队。（早期设计里还有一条
  `WM_SETTEXT` / `EM_REPLACESEL` 路径，已移除：能承载可编辑文本且映射了 HWND
  的控件同时也会发布 Value 模式，因此它永远不会被命中。）**三条路径都要求目标
  窗口在前台**——运行时先把窗口提上来，而不是无焦点地写入。哪条路径可用
  **在执行时**判定，因为静态检查不可能准确：UIA 会报告元素实际拒绝的接口。
  生效的路径会在整个会话内被记住。

  **两条合成通道在当前 build 中默认开启**；要临时关闭，用
  `--no-allow-clipboard` / `--no-allow-keyboard`，要持久关闭用
  `somnus config --no-allow-clipboard` / `--no-allow-keyboard`。都不可用时如实
  报错，而不是静默改道：

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

持久设置放在一个小型、可手改的 TOML 文件里。**当前 build 两条合成通道默认开启**
（50 任务基准显示，拒绝它们会让整类任务无法完成）；这个文件是用来**关掉**并保持
关闭的：

```bash
somnus config --path              # 文件位置
somnus config                     # 查看当前生效值（首次会写出默认文件）
somnus config --no-allow-clipboard   # 持久关闭剪贴板通道
somnus config --no-allow-keyboard    # 持久关闭键盘通道
somnus config --allow-clipboard      # 重新打开
```

两条通道的优先级：**单次命令行参数（`--allow-clipboard` /
`--no-allow-clipboard`）> 环境变量（`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`）> 文件 > 内置默认（开）**。配置文件损坏时会降级为默认
值，并在 `somnus config` 中报告问题，绝不会变成"全部允许"。

时间参数可以写进同一个文件，也可以由模型侧用 `configure` 工具**在运行时修改**：

| 键 | 默认 | 范围 | 含义 |
| --- | --- | --- | --- |
| `settle_quiet_ms` | `40` | 10–1000 | UI 需要保持多久不变，才认为一次调用结束。调小更快；太小会在动画未结束时读中间态。 |
| `settle_poll_ms` | `10` | 1–100 | 探测间隔。 |
| `settle_timeout_s` | `3.0` | 0.5–30 | 单次 settle 的硬上限；超时后响应为 `settled=false`，但仍带增量。 |
| `post_action_enum` | `true` | 布尔 | 动作后重新枚举窗口，从而报告"动作顺手打开的新窗口"；关掉每次调用约省 100 ms，但可能漏报新窗口。 |

这些键的优先级：**内置默认 < 文件 < 环境变量（`SOMNUS_SETTLE_QUIET_MS`、
`SOMNUS_SETTLE_POLL_MS`、`SOMNUS_SETTLE_TIMEOUT_S`、
`SOMNUS_POST_ACTION_ENUM`）< `configure`**。`configure` 作用域是当前会话，对
下一次调用生效；`persist=true` 会写回文件，`reset=true` 回到启动时的值。任一键
非法则整批拒绝。

```json
{"settle_quiet_ms": 80, "settle_poll_ms": 20, "post_action_enum": false}
```

```text
SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

MCP 服务器在启动时会重复一遍当前生效值及其代价，因此模型在动手之前就知道默认
值与调整方向。

- **`context_menu`** 在可能有菜单的控件上提供（即暴露了可操作内容的元素）。它是
  面对忽略原生模式的应用时的出口：菜单通过 UIA 自身打开（`ShowContextMenu`），
  指针不移动、不发送按键，菜单项本身是标准菜单项，其 `activate` 可用。UIA 无法
  查询元素"是否有菜单"，因此调用后如实报告结果：弹出菜单返回 `OK`，未弹出返回
  `OK_UNVERIFIED`，并指出真正可用的手势。
- **实体**按结构识别：标题加动词按钮的行，或同一容器下三个以上的标题行（列表、
  工具栏、导航栏）。

```bash
somnus --no-pack repl          # 驱动任意应用
somnus --no-pack observe w4
```

pack 仍适合编码通用路径无法推断的领域词汇（`PRODUCT p1 +add`、`+submit!`），但
为了"够到并操作一个控件"从不需要它。

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

当提窗口被拒绝，或快照返回"窗口有标题但根节点没有任何子节点"时，观察结果会
直接说明，而不是把一个空窗口当成事实：

```
~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows
~ tree looks empty; window may be covered or app not rendering
```

### 不够用时：子会话

提窗口会拿走用户的焦点，剪贴板与键盘通道还会向前台窗口发送合成输入。**子会话**
——同一账户的第二次交互式登录，拥有自己的 input desktop、前台窗口和输入队列——
彻底消除了这种干扰：在其中工作不会影响到控制台会话中的用户。

**前置条件与代价**

- Windows **专业版 / 企业版 / 教育版**；家庭版无法创建子会话。
- 一次管理员权限的 `somnus child-session setup`（启用功能、RDP 监听、凭据委托，
  并把 `TermService` 设为自动）。
- 子会话通过 **RDP 环回**进入，所以连接在它就存在，连接断它就没。Windows Hello
  **PIN 不能**用于这次环回登录；若委托被拒，用
  `somnus child-session credential --save` 存一次账户密码。
- **全系统同时只允许一个子会话。**
- 不提供安全隔离：两个会话是同一账户。

**CLI（配置与诊断）**

| 命令 | 效果 |
| --- | --- |
| `somnus child-session status` | 只读：功能是否可用、是否有活着的会话 |
| `somnus child-session setup` | 唯一需要管理员的一步；`--dry-run` / `--disable` |
| `somnus child-session start` | 打开环回（`--headless`、`--auto`） |
| `somnus child-session peer-setup` | 让 peer 随子会话登录启动（每用户 Run 键） |
| `somnus child-session probe` | 向管道另一侧的 peer 询问它能看见什么 |
| `somnus child-session close` | 结束并清理残留 |
| `somnus child-session run -- <cmd>` | 在子桌面运行一个程序，打印退出码与 stdout/stderr |

`somnus child-session run` 向 peer 发 `process.start`，轮询 `process.status`
直到命令退出（或 `--timeout`，默认 600 秒，超时杀死），然后打印退出码与完整
输出。输出落在 `%TEMP%\somnusui-child-run\<timestamp>\`，只保留最近一次。它受
两个开关约束：`child_allow_spawn`（默认 `true`，能力本身）与 `allow_processes`
（空 = 不限制；与"允许操作哪些进程"是同一份列表——不能被操作的进程也不能被
启动）。它是**CLI-only**：没有 `SESSION run`，因为面向模型的接口不暴露任意进程
启动器。

**给 agent：同一时间一个桌面**

| 命令 | 效果 |
| --- | --- |
| `SWITCH main` | 回到用户桌面 |
| `SWITCH child` | 切到子会话（须已打开） |
| `SESSION open` | 打开并切过去 |
| `SESSION close` | 结束并回来 |
| `SESSION detach` | 本进程退出后仍保留 |

**ref 只在当前桌面有效。** 切换会换 provider，并重置注册表与窗口 ref 映射：
`SWITCH` 之后，`w1` 不再是之前那个窗口。动手前先重新 `OBSERVE`。

编译器、投影、注册表、解析器、策略和 ref 命名空间全部留在主侧；子会话中只运行
一个瘦 peer，通过命名管道应答工具调用。设计说明、实测数据与试过但不采用的方案见
[docs/child-session.md](docs/child-session.md)。

## 七个工具

| 工具 | 用途 | 返回 |
| --- | --- | --- |
| `observe` | 目标窗口的 L0 概览（视口范围）+ `WINDOWS` 首行 | SUI-Text |
| `find` | `FIND [type] "query" [in ref]`；`timeout` 可等待命中 | 匹配的实体行 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 细节 |
| `call` | `CALL ref.cap(args)` —— 可写多条，每行一条，作为批处理执行 | `OK`/`ERR` + 增量 |
| `run` | `CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap(args)` | `RUN OK`/`RUN PARTIAL` + 每步一行 + 合并增量 |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` + 窗口清单 |
| `configure` | 调整 `settle_quiet_ms` / `settle_poll_ms` / `settle_timeout_s` / `post_action_enum` | `SETTINGS …` |

它们都没有确认门控：节点行上印出的能力，被调用就直接执行。`configure` 只是节奏
旋钮，不是权限开关。

### 批量调用

`call` 可以接受多条 `CALL` 语句（每行一条）。它们在**一次往返内按顺序执行**：
每一步都先对照当前编译结果做校验（永远不可能成功的那一步会在任何东西尚未动过时
整批拒绝），然后步与步之间不做 settle 地依次运行，整批以一次 settle、一份增量、
一次 rev 收尾。

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
→ BATCH OK steps=3/3 rev=7
    1. CALL q1.set_text("张三") -> ok
    …
  ~ q1 text="张三"
```

某一步失败时批量就地停止：响应指明失败步、说明哪些步骤真正执行过，并携带
它们产生的增量。每一步都必须指向批处理开始时就存在的元素——「先打开菜单、
再点菜单项」这类流程要拆成多次调用，因为菜单项是在两次调用之间的 settle 里
才出现的。语法与响应格式见 [`somnus/sui/grammar.md`](somnus/sui/grammar.md)。

### 脚本（`run`）

`run` 接受一段脚本，每行一步。纯 `CALL` 脚本保留批处理的"全量预校验"；一旦出现
`AWAIT`，就改为**逐步执行**（执行 → settle → 重读 → 解析下一步），因此后面的步
可以引用前面刚创建出来的元素：

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
→ RUN OK steps=3/3 rev=9
```

- `AWAIT [TYPE] "name" [in ref] [timeout=s]` 把 `$it` 绑定到第一个命中（默认
  2 秒；`timeout=0` 表示只搜一次）。它会反复重读视口，直到命中或超时，而不是
  花掉一次模型推理去 `observe`。
- 某一步失败就地停止：`RUN PARTIAL steps=2/3 failed-at=3`，随后给出每步状态与
  真正执行过的步骤产生的合并增量。
- 限制：跨窗口流程必须显式写窗口 ref（切窗会重排 ref）；增量是脚本首尾的净差；
  `$it` 只取第一个命中，不是集合。
- 真机菜单流程实测：一次 3 步 run 用了 453 ms，两次分开的 `call` 用了 428 ms。
  **运行时成本并没有省**（每一步仍然要 settle），省下的是**一次模型推理**——这
  才是它的意义。

## SUI-Text

唯一面向模型的格式。能力直接写在节点行上；默认能力与默认状态省略（系统提示词中
携带默认表）。

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

操作与结果：

```
FIND product "Big Mac"   →  FOUND 1 / PRODUCT p1 "Big Mac" price=5.99 +add
CALL p1.add()            →  OK rev=2
                            ~ c1 count 0→1
                            ~ c1 total 0→5.99
CALL b5.submit()         →  OK rev=3   （critical 能力直接执行，无确认门控）
```

完整文法见 [`somnus/sui/grammar.md`](somnus/sui/grammar.md)。

## 仓库结构

```
somnus/
├── ir/            节点 / 能力 / 动作值对象（平台无关）
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → twins → match → compile
├── projection/    rules.py budget.py engine.py（视口 rows/visible/+more）
├── sui/           serialize.py parse.py grammar.md（含 RUN / AWAIT）
├── runtime/       registry resolver settle verifier delta policy session
├── session/       子会话：childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── packs/         generic.py restaurant_demo.py（内置演示 pack）
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      持久化设置（通道 + settle 时间参数）
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
apps/scenario-suite/    WPF（.NET 8）验收应用：覆盖真实 GUI 五种形态的界面
tools/rdp-host/         .NET（WinForms）OLE 容器，承载 RDP 控件；这是 Python
                        无法承载的一环。用 dotnet build 构建。
docs/
  what-the-llm-sees.md      一个窗口、各个工具，附真实输出
  interactive-usage.md      REPL / 单条 CLI / 脚本三种驱动方式
  packs.md                  App Pack 编写指南
  child-session.md          第二个桌面：设计、代价与已否定的方案
  text-input-channels.md    文本如何写入控件，以及每条路径的代价
  optimization-plan.md      实测了什么、改了什么、还剩什么
  viewport-capture-proposal.md  视口采集：测量与设计
  scenario-suite.md         验收界面清单与通过标准
  benchmark-50-tasks.md     50 个真实用户任务：这个项目能干什么、干到什么程度
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
| `store` | 搜索框 + 240 行可滚动商品列表 + 购物车 | 大数据量：视口折叠（`rows=` / `visible=`）、`FIND` 够到屏幕外的行、按原生接口写入搜索框 |
| `form` | 带标签的字段（一个可写、一个只读）、复选框、下拉、禁用按钮、受校验的提交 | 可写 / 只读在行上可分辨，校验是真实结果，**提交记录**才是验证面 |
| `chat` | 自绘富文本编辑器、消息历史、发送按钮、可见的发送记录 | 不发布任何可写接口的控件：如实拒绝，或走剪贴板/键盘通道（默认开启，可用 `--no-allow-*` 关闭），然后记录发送 |
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
somnus --scenario tree --no-allow-clipboard repl
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

**验收门槛**（`python -m somnus.cli bench --runs 100`，mock/固件）：

| 指标 | 目标 | 实测（100 次） |
| --- | --- | --- |
| 零视觉（Zero Vision） | 0 次视觉/坐标调用 | ✓（结构性为 0） |
| 信息隔离 | 模型上下文中 0 个原生 ID | ✓（泄露扫描 = 0） |
| 压缩比 | ≥ 10× | **1373.6×**（最小值，完全展开的树） |
| 动作成功率 | ≥ 99% | **100%** |
| 任务成功率 | ≥ 90% | **100%**（中位 3 步） |
| L0 观察 | ≤ 300 token | **91 token**（中位数与最大值） |

运行方式：`python -m somnus.cli bench --runs 100`（见快速开始里的说明：控制台
脚本形式无法 import `bench/`）。隔离检查是**取证式**而非正则臆测：它枚举被测树
中实际存在的 automation id、类名和 runtime id，断言它们没有出现在模型会收到的
文本里；因此仅仅看起来像 ID 的应用数据（表单自己的 `REQ-0001`）不会被误判成
泄露。

### 真实窗口性能（实测）

同一台机器，Windows 11，`apps/scenario-suite` 的 **store** 界面（240 行非虚拟化
`ItemsControl`、1467 节点原始树），真实 UIA provider（非 mock），
i5-13500HX / 2560×1440@96DPI。`CALL` 的区间来自多次重复；`observe` 为单次。

| 场景 | 之前 | 之后 | 变化 |
| --- | --- | --- | --- |
| 观察 store | 3385 ms | **594 ms**（−82.5%） | 视口采集：骨架 + 行窥视 + 只完整读可见行 |
| store 上的 `CALL`（树无变化） | ~6 s | **686–706 ms** | 签名探测：无变化时 **0 次全量快照** |
| 签名探测本身 | 1622–1938 ms | **384–404 ms** | 探测只读覆盖集上的签名字段 |
| 小窗口上的 `CALL` | 423 ms | **215–231 ms** | settle 帧数减少（`quiet=40 ms`、`poll=10 ms`） |
| store 观察的 token | 292 | **292** | 不变——投影仍折叠为同样的行 |
| 桌面变化检测（`top_level_signature`） | — | **p50 0.31–0.52 ms**（max ≤2.15 ms） | Win32 `(hwnd, title)` 走查；同机 `list_windows` p50 **70–78 ms** |
| 写入后标题不变 | — | 总 **81–104 ms** / enum **0.6–0.9 ms** | 桌面签名一致，跳过枚举 |
| 写入后标题变化 | — | enum **78–104 ms** / 总 **154–238 ms** | 标题变化会改变窗口列表，必须全量枚举；与旧实现同量级，修的是正确性 |
| `observe()` 的窗口枚举 | — | **78–102 ms** | 观察仍然全量枚举，没有签名短路 |

这些是本轮唯一端到端实测过的性能数字。store 上**有变化**的 `CALL` 仍需一次全量
快照，且本轮**没有复测**，因此不给出数字。想在自己的机器上复现，可设
`SOMNUS_PROFILE=1`，它会打印分段耗时（枚举 / 快照 / 编译 / settle）。

本机测试数：可移植套件 **733 passed**（默认排除 `-m windows`）；真机
`pytest -m windows` **38 passed**，在子会话内运行，约 **62–66 s**，没有出现
`window ... did not appear` 超时。UWP 计算器在该子桌面能起、能读，
`OBSERVE full` 读到 **34 个按钮**。

## 许可证

MIT。
