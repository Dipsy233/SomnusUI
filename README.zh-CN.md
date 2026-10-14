# SomnusUI

**Do language models dream of pixels?**
**语言模型会梦见像素图片吗？**

English | [中文](README.zh-CN.md)

**将原生 GUI 编译为纯文本、无视觉、低 token 的语义 API（Semantic API）**，
让 LLM 通过*能力调用（capability calls）*而非*像素点击*来操作软件。

## 为什么是 SomnusUI

主流的 GUI agent 走的是"看见→点击"的路：截图，用视觉模型找出按钮坐标，
合成一次鼠标点击。这条路能工作，但每一层都有代价——截图烧 token，坐标是
对布局的猜测，合成的点击抢走用户的鼠标，而模型看一张位图的理解深度，
远不如读它本可以直接读到的语义结构。

SomnusUI 反过来问：**如果模型不"看"界面，而是"读"界面呢？**

每个现代应用都在无障碍树（accessibility tree）里完整描述自己——每个按钮
是什么、能做什么、当前是什么状态。这些信息原生、精确、随布局更新，却被
大多数 agent 绕开，因为原始无障碍树是几千个无名 `Pane` 的噪声海。SomnusUI
做的就是这个翻译：把噪声海编译成一份带类型、能力和增量的语义文档，让
`CALL p1.add()` 取代 `click(312, 528)`。

名字里的 Somnus（罗马睡眠之神）是这个赌注的一半：agent 在后台工作，
用户不必看着它动鼠标；另一半是确定性——编译器与运行时里没有一个 LLM，
同一个窗口永远编出同一棵语义树。大模型负责规划，语言之外的翻译交给代码。

至于开头那个问题——不，它不需要梦见像素。所有它需要的，应用早就自己说了。

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
  INPUT q1 "网络路径(N):" readonly          ← 一个字段，且为只读
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
- **`set_text`** 为每个可输入表面推导，执行时才选择路径：普通字段走 Value
  模式，富文本/文档编辑器则用键盘输入。自绘编辑器（Chromium/Electron）
  无需 pack 即可写入。
- **`context_menu`** 与 `activate` 并列提供给每个控件。它是面对"忽略原生模式
  的应用"时的出口：菜单通过 UIA 自身打开（`ShowContextMenu`），指针不会移动、
  不发送任何按键，而菜单项本身是标准菜单项，其 `activate` 可用。默认动作
  只做高亮的应用（如聊天列表）正是这样被驱动的，且无需合成输入。
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
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | v0.1 |
| `mock` | 回放录制的 `RawTree` 固件（任意平台） | v0.1 |

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
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py（内置演示 pack）
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
docs/
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
