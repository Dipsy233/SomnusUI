# SomnusUI

**Do language models dream of pixels?**
**语言模型会梦见像素图片吗？**

English | [中文](README.zh-CN.md)

**将原生 GUI 编译为纯文本、无视觉、低 token 的语义 API（Semantic API）**，
让 LLM 通过*能力调用（capability calls）*而非*像素点击*来操作软件。

```
用户意图
   ↓
LLM（规划器，仅 4 个工具）
   ↓
Somnus 运行时 ─ observe / find / inspect / call
   ├─ 会话注册表        （ref ↔ 指纹 ↔ 原生元素）
   ├─ 解析器            （语义能力 → 原生原语）
   ├─ 验证器            （稳定 + 效果 + 增量）
   └─ 策略              （风险、确认、白名单）
   ↓
语义图
   ↑
语义编译器（确定性：normalize → infer → match → project → compress）
   ↑
AccessibilityProvider（Windows UIA；后续支持 AX / AT-SPI）
```

LLM 永远不会看到像素、坐标、截图、句柄、`AutomationId` 或 COM 指针。
它看到的是实体、状态、关系、能力和值。

## 设计原则

- **无视觉（No Vision）** – 无障碍树是唯一的感知通道。
- **不使用合成输入（No Synthetic Input）** – 通过应用自己发布的接口驱动它，
  绝不假装有人在敲键盘。这不是偏好，而是这个项目存在的理由：一次按键或粘贴
  要求窗口前台、与目标的消息泵竞争，而且无法验证。发布不出可用接口的元素会被
  **明确报错**，而不是被偷偷输入；`--allow-clipboard` / `--allow-keyboard`
  用于确实别无他法时（两者都会抢占屏幕焦点），两种情况下回复都会说明。
- **低 token（Low Token）** – 观察经过投影与预算控制；同名类列表只展示前
  3 个样例，匿名叶子折叠为一行汇总，L0 概览通常几十到三百 token。
- **语义优先（Semantic First）** – LLM 调用 `CALL p1.add()`，而非 `click(312, 528)`。
- **确定性编译器** – 运行时与编译器内部不运行任何 LLM。
- **大声失败（Fail Loud）** – 凡是无障碍 API 无法表达的内容一律为
  `UNKNOWN`，绝不猜测。

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
somnus mcp                         # MCP 服务器（stdio），4 个工具
```

### 作为 MCP 服务器接入任意客户端

Claude Desktop、Cursor、pi 等任何 MCP 客户端都可接入：

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

暴露 `observe` / `find` / `inspect` / `call` 四个工具，provider 自动选择
（有 UIA 依赖时驱动真实窗口，否则回退 mock）。

## LLM 看到什么

一个真实窗口，四个工具调用。除此之外的任何内容都不会呈现给模型。

**① `observe`** —— 以能力形式呈现的当前屏幕（一个 Windows 文件夹属性
对话框仅 304 token）：

```
@rev 1
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← 文本框：可写入
  INPUT q2 "只读路径" readonly               ← 同为 INPUT 但没有 +set_text：不可写入
  BUTTON b1 "共享(S)..."                      ← 可点击（activate 隐含）
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← 当前不可用
  TAB t3 "共享"
```

**② `find`** —— 按关键词定位，返回完整的实体行：

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

**③ `inspect`** —— 查看单个节点的全部信息（含隐含能力与关系）：

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

失败时输出一行错误信息并附恢复提示，绝不会出现堆栈跟踪：

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

模型永远看不到句柄、坐标、`AutomationId`、进程 ID、COM 指针、原始
`control_type`，也看不到 UIA 转储中大量无名的 `Pane` 包装节点。完整演练见
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md)。

## 通用模式（Generic mode）

pack 是优化手段，不是必要条件。使用 `--no-pack`（或窗口本来就没有对应 pack）
时，编译器完全从结构与原生模式推导，从未见过的应用依然可用：

- **控件**由平台定义——暴露交互模式（`invoke`、`selectionitem`、`toggle`、
  `value` 等）的元素。纯静态文本和布局容器不会获得任何操作。
- **能力由平台证明，绝不靠角色猜测。** 只有平台真的提供默认动作时才出现
  `activate`（`Invoke` 模式，或执行器回退用的 `LegacyIAccessible`）。只暴露
  `ExpandCollapse` 的按钮显示 `+expand +collapse` 而没有 `activate`，因此行上写的
  就是能用的手势；什么都没暴露的控件不会得到一个注定失败的能力。
- **`set_text`** 为每个可输入表面推导，路径由控件自身的接口决定，绝不看类名或角色。
  写入通道按“对用户的打扰程度”排序，**依次尝试、第一个真正可用的胜出**：

  | 顺序 | 通道 | 前提 | 抢焦点 |
  | --- | --- | --- | --- |
  | 1 | `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 |
  | 2 | `WM_SETTEXT` / `EM_REPLACESEL` | **元素自己有非零 HWND** | 否 |
  | 3 | 剪贴板 + `Ctrl+V` | 需 `--allow-clipboard` | **是** |
  | 4 | 聚焦 + 键入 | 需 `--allow-keyboard` | **是** |

  **原则是优先走 UIA 编辑**：前两条是应用自己发布的接口，写入在应用自己的线程上完成，
  原子、不抢焦点、不产生任何合成输入，用户完全无感。只有前两条都用不上时——
  典型就是自绘输入框（QQ / 豆包的富文本编辑器）——才回退到后两条，
  而它们**都会抢占屏幕焦点**（后缀两类的前提就是目标必须在前台）。

  哪条路真的能用**在执行时**才能判定：静态检查不可能准确——UIA 会报告元素其实
  拒绝的接口（实测：可编辑控件声称有 `value`，setter 却是空壳）。

  第 2 条是无焦点写入，实测（记事本窗口置后台）：**3/3 精确写入，焦点一次未被抢走**，
  含 CJK 与 50 字符长串。这正是合成输入做不到的（同条件下 `SendInput` 0/20 完整）。
  判据是**元素自己映射的 HWND**，而不是“进程里有没有原生 Edit 子窗口”——
  后者会把同进程内本来可写的控件一起误杀。

  第 3 条（剪贴板）虽要抢焦点，但在自绘控件上是**远优于键盘**的选择：
  文本走剪贴板而非输入队列，投递耗时与长度无关（实测 10~3000 字符恒定 ~37ms），
  而逐字符 `SendInput` 会把 2N 个事件堆进队列被目标滞留。

  它虽然借用了用户的剪贴板，但**用完逐格式无损还回去**：快照保存的是每一种格式的
  原始字节（不只是文本），还原时逐个重建——文本、RTF、HTML、文件列表，
  以及**图片**（位图经 `GetDIBits` 序列化、`CreateDIBitmap` 重建）。
  实测：含位图在内的 8 种格式全部字节级一致；QQ 输入框投递后用户的图片仍在。
  写入的内容附带四条 opt-out 格式，**不会进入剪贴板历史或云同步**。
  仅少数格式无法重建（`CF_PALETTE`、OLE 承载格式），且会**列出名字**而非静默丢弃。
  细节见 [docs/interface-capability-report.md](docs/interface-capability-report.md) 3.3。

  ```
  CALL q1.set_text("x")
  ERR ACTION_NOT_SUPPORTED q1 hint=report-user
      msg="the element offers no interface this runtime can use, and synthetic
           keyboard input is disabled; enable it for one run with
           --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
  ```

  两条合成通道各自授权、默认关闭。因此记事本走原生写入，而发布不出接口的
  自绘控件（QQ/Steam/VS Code）默认会被**如实报错**，只有显式开启对应开关后
  才走剪贴板或键盘。哪条路奏效会被记住整个会话，重复调用同一元素不再重新探测。

  **LLM 只会看到 `set_text` 一个名字**，看不到哪条通道在跑。

  每条通道的原理、实测数据与已否定的路线（为什么 Chromium 自绘编辑器用
  UIA / MSAA / `WM_SETTEXT` 都写不进去）详见
  [docs/interface-capability-report.md](docs/interface-capability-report.md)。

#### 设置文件：把 opt-in 写下来

两个开关如果只能当命令行参数传，就不算真正的 opt-in —— **MCP 客户端是自己
启动这个进程的，用户根本没有地方传 flag**。所以它们可以写进一个小配置文件：

```bash
somnus config --path              # 文件在哪
somnus config                     # 看当前状态（首次会写出默认文件）
somnus config --allow-clipboard   # 只开剪贴板，持久生效
somnus config --allow-keyboard    # 只开键盘
```

文件是普通 TOML，**带注释、可直接手改，也足够小，可以交给 LLM 改**：

```toml
# Both synthetic-input channels are OFF unless set to true below.
# They seize the screen's foreground, ...
allow_clipboard = false
allow_keyboard  = false
allow_processes = []
```

优先级：**命令行 flag > 环境变量（`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`）> 文件 > 内置默认（全关）**。
所以一次性的覆盖不需要改文件，而文件里的决定对 MCP、JSON-RPC、CLI 都生效。

坏文件不会让运行时启动失败，也不会意外变成“全部允许”：降级为默认值并在
`somnus config` 里报告问题。
- **`context_menu`** 在“可能有菜单”的控件上提供（即暴露了可操作内容的元素）。
  它是面对"忽略原生模式的应用"时的出口：菜单通过 UIA 自身打开
  （`ShowContextMenu`），指针不会移动、不发送任何按键，而菜单项本身是标准菜单项，
  其 `activate` 可用。默认动作只做高亮的应用（如聊天列表）正是这样被驱动的，
  且无需合成输入。UIA 无法查询元素“是否有菜单”，因此调用后会如实报告结果：
  真的弹出了菜单返回 `OK`，未弹出返回 `OK_UNVERIFIED`，并指出真正可用的手势。
- **实体**按结构识别：标题加动词按钮的行，或同一容器下三个以上的标题行
  （列表、工具栏、导航栏）。

```bash
somnus --no-pack repl          # 驱动任意应用
somnus --no-pack observe w4
```

pack 仍适合编码通用路径无法推断的领域词汇（`PRODUCT p1 +add`、`+submit!`），
但为了“够到并操作一个控件”从不需要它。

## 提供者（Providers）

运行时通过唯一的 `AccessibilityProvider` 接口与外界通信。目前有两个实现，
而编译器、投影和运行时对所有提供者完全一致。

| 提供者 | 数据来源 | 状态 |
| --- | --- | --- |
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | 可用 |
| `mock` | 回放录制的 `RawTree` 固件（任意平台） | 可用 |

```bash
somnus --provider uia windows          # 原生窗口
```

## 后台工作区（无干扰操作）

agent 的工作不要求窗口在前台，也不抢占用户焦点。实测事实：

- **可见但被遮挡**的窗口：快照完整、所有 UIA 原语可用——前台不是任何操作的前提；
- **最小化**窗口：CEF 应用（Steam/QQ/Chrome）渲染挂起、树萎缩到壳，是唯一
  真正阻断操作的状态；
- **其他虚拟桌面**（未最小化）的窗口：通常依然完整可用。

因此 provider 在快照/操作前自动做两件事（`workspace.py`）：

1. 最小化的窗口用 `SW_SHOWNOACTIVATE` 展开——**绝不抢焦点**，用户正在
   输入的内容不受影响；
2. 位于其他虚拟桌面的窗口用官方 `IVirtualDesktopManager` 移到当前桌面
   （同一进程无需重新登录：QQ 在主桌面登录一次即可）。

窗口被接触前的状态会被记录，provider `close()` 时**自动恢复原状**
（原来最小化的重新最小化），agent 的工作永远不给用户留下桌面被重新布置
的痕迹。

已知限制：无激活展开无法完全隐藏窗口（任务栏图标可见；若它原来处于顶层
z-order 会短暂出现在屏幕上，被完全遮挡则无感）。这是无激活路径的固有限制，
但已比任何需要 focus 的方案（含合成输入类）干扰都小。

## 四个工具

| 工具 | 用途 | 返回 |
| --- | --- | --- |
| `observe` | 目标窗口的 L0 概览 | SUI-Text |
| `find` | `FIND [type] "query" [in ref]` | 匹配的实体行 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 细节 |
| `call` | `CALL ref.cap(args)` | `OK`/`ERR` + 增量 |

## SUI-Text

唯一面向 LLM 的格式。能力直接写在节点行上；默认能力与默认状态省略
（系统提示词中携带默认表）。

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
│                  windows/{uia,cache,executor,events,keyboard,clipboard,
│                            workspace}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py（内置演示 pack）
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      持久化设置（两个合成通道的 opt-in 写在这里）
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
docs/
  interface-capability-report.md  文本写入通道的原理与实测依据
  packs.md              App Pack 编写指南
  interactive-usage.md  REPL / CLI / 脚本三种驱动方式
  what-the-llm-sees.md  一个窗口、四个工具，附真实输出
examples/               agent 循环、契约检查、任务评估（宿主示例代码）
bench/                  基准门槛 + LLM 控制演示
tests/{fixtures,golden,compiler,projection,runtime,providers,e2e}/
```

依赖方向：`providers → compiler → projection → serialization → protocol`；
`runtime` 依赖以上全部；任何地方都不导入 LLM。

## App Packs（可选）

Pack 为应用程序提供领域词汇，把通用控件树变成带有能力的实体（如
`PRODUCT p1 +add`）。它是优化手段而非必要条件：没有 pack 的窗口走通用
编译路径，一样可以到达和操作（见通用模式）。

```python
from somnus.packs.restaurant_demo import build_pack
from somnus.runtime.session import Session
from somnus.providers.mock import MockProvider

session = Session(provider, pack=build_pack())
```

编写指南见 [`docs/packs.md`](docs/packs.md)。

## 测试

```bash
pytest                      # 编译器/投影/运行时测试可在任意平台运行
pytest --update-golden      # 重写 golden SUI-Text 固件
pytest -m windows           # UIA + WPF 端到端（仅 Windows 运行器）
```

`MockProvider` 回放录制的 `RawTree` 固件并执行脚本化的变更，因此编译器、
投影、解析器、增量与验证等路径都可以在没有桌面的环境下得到覆盖。

## 指标

| 指标 | 目标 | 当前（`--demo bench`） |
| --- | --- | --- |
| 零视觉（Zero Vision） | 0 次视觉/坐标调用 | ✓（结构性保证） |
| 信息隔离 | LLM 上下文中 0 个原生 ID | ✓（泄露扫描 = 0） |
| 压缩比 | ≥ 10× | ≥ 73× |
| 动作成功率 | ≥ 99% | 100% |
| 任务成功率 | ≥ 90% | 100% |

运行方式：`somnus --demo bench --runs 100`。

## 许可证

MIT。
