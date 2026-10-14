# SomnusUI

English | [中文](README.zh.md)

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
AccessibilityProvider（Windows UIA；后续支持 AX / AT-SPI / CDP）
```

LLM 永远不会看到像素、坐标、截图、句柄、`AutomationId` 或 COM 指针。
它看到的是实体、状态、关系、能力和值。

## 设计原则

- **无视觉（No Vision）** – 无障碍树是唯一的感知通道。
- **低 token（Low Token）** – 观察经过投影与预算控制；L0 概览约 200 token。
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
somnus windows
somnus observe w1
somnus call 'CALL b1.activate()'
somnus record w1 -o fixture.json
somnus mcp           # MCP 服务器（stdio），4 个工具
```

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

## 提供者（Providers）

运行时通过唯一的 `AccessibilityProvider` 接口与外界通信。目前有三个实现，
而编译器、投影和运行时对所有提供者完全一致。

| 提供者 | 数据来源 | 状态 |
| --- | --- | --- |
| `uia` | Windows UI Automation（Win32 / WPF / UWP） | v0.1 |
| `cdp` | Chrome DevTools Protocol 无障碍树（Edge / Chrome） | v0.3 |
| `mock` | 回放录制的 `RawTree` 固件（任意平台） | v0.1 |

```bash
somnus --provider uia windows          # 原生窗口
somnus --provider cdp windows          # 浏览器标签页
somnus --provider cdp observe          # 一个页面，以 SUI-Text 呈现
```

### 浏览器（CDP）

浏览器提供者读取页面自身的无障碍树（`Accessibility.getFullAXTree`）——
绝不使用截图，绝不抓取 DOM——并通过树中指名的 DOM 节点驱动操作
（`Runtime.callFunctionOn`），因此触发的是页面真实的事件处理器，而非在
坐标上注入合成输入。它可以附加到回环调试端点上可达的任何页面，包括以
`--remote-debugging-port=0` 启动的浏览器（通过其 `DevToolsActivePort`
文件发现）。需要 `[cdp]` 扩展。

一个完全不加载 pack 读取的网页表单：

```
WINDOW w1 "SomnusUI CDP 测试页" active
  GROUP g1 +scroll(...)
    TEXT tx2 "用户名"
    INPUT q1 "用户名"
    TEXT tx4 "密码"
    INPUT q2 "密码"
    CHECKBOX cb1 " 记住我" unchecked
    BUTTON b1 "登录"
```

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
├── providers/     base.py  mock.py  cdp.py
│                  windows/{uia,cache,executor,events,keyboard}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
│                  yaml_pack.py（v0.2 YAML pack 加载器）
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py（v0.1）
│                  notepad / explorer / windows_settings .pack.yaml（v0.2）
├── protocol/      tools.py mcp_server.py jsonrpc.py
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）验收应用
docs/
  packs.md              如何编写 App Pack
  what-the-llm-sees.md  一个窗口、四个工具，附真实输出
examples/               agent 循环、契约检查、任务评估
bench/                  指标测试框架 + LLM 控制演示
  run.py                基准门槛
  demo_llm.py           在 mock 后端上的端到端演示（任意平台）
  demo_notepad.py       对真实记事本使用相同的工具
tests/{fixtures,golden,compiler,projection,runtime,providers,e2e}/
```

依赖方向（CI 中强制约束）：`providers → compiler → projection →
serialization → protocol`；`runtime` 依赖以上全部；任何地方都不导入 LLM。

## App API 通道

能力的 `via` 步骤可以指向 `app_api` 动作而非原生原语，运行时会将其路由到
应用自身的接口（COM、CLI、脚本），而不是驱动其控件。对 LLM 的暴露完全
不变：它仍然调用 `CALL x.do()`，且不知道使用了哪条通道。

```yaml
caps:
  list_files:
    via: [{ primitive: app_api, target: list, value: { path: "$path" } }]
```

```
CALL l1.list_files(path=".")
→ OK_UNVERIFIED rev=2
  = [".git", "apps", "bench", "docs", "somnus", "tests"]   ← 应用自己的数据
```

这一点很重要：电子表格有数据模型，直接读取单元格比点击复制再抓取剪贴板
更便宜也更可靠；而且有些操作（重命名工作表、设置文档作者、运行 CLI）
根本没有对应的控件（开发文档第 20 节）。

## App Packs

Pack 为应用程序提供领域词汇，把通用控件树变成带有能力的实体。v0.2 每个
应用只需一个 YAML 文件，从 `somnus/packs/*.pack.yaml` 自动发现加载：

```yaml
app:
  id: notepad
  match: { process: "notepad.exe" }     # 按进程匹配，绝不按标题
entities:
  document:
    match: { role: text, patterns: [value, text] }
    key: "$self.automation_id"
    caps:
      set_text:
        via: ["$self.set_text"]
        params: { text: { type: string, required: true } }
```

已内置的 pack：`notepad` 与 `explorer`（均针对真实的 Windows 11 应用
验证过），以及 v0.1 的 `restaurant-demo` Python pack。完整编写指南见
[`docs/packs.md`](docs/packs.md)。

## 测试

```bash
pytest                      # 编译器/投影/运行时测试可在任意平台运行
pytest --update-golden      # 重写 golden SUI-Text 固件
pytest -m windows           # UIA + WPF 端到端（仅 Windows 运行器）
```

`MockProvider` 回放录制的 `RawTree` 固件并执行脚本化的变更，因此编译器、
投影、解析器、增量与验证等路径都可以在没有桌面的环境下得到覆盖。

## 指标（v0.1）

| 指标 | 目标 | 演示结果 |
| --- | --- | --- |
| 零视觉（Zero Vision） | 0 次视觉/坐标调用 | 0 |
| 信息隔离 | LLM 上下文中 0 个原生 ID | 0 |
| 概览 token（L0） | ≤ 200 | 176 |
| 压缩比 | ≥ 10× | 41× |
| 动作成功率 | ≥ 99% | 100% |
| 任务成功率 | ≥ 90% | 100% |
| 未确认的关键操作 | 0 | 0 |

复现方式：`somnus --demo bench --runs 100`。

## 用真实模型驱动运行时

四个工具由一个本地 OpenAI 兼容模型驱动验证。`examples/` 下有三个示例宿主
（agent 循环属于宿主代码，不属于核心——开发文档第 17 节）：

| 脚本 | 测量内容 |
| --- | --- |
| `examples/agent.py` | 基于四个工具的原生函数调用循环 |
| `examples/check_contract.py` | 工具契约正确性：模型能否为一次目标调用输出合法 SUI-Text |
| `examples/eval_agent.py` | 任务成功率，由运行时状态判定（而非模型自述） |

```bash
export SOMNUS_LLM_API_KEY=...            # 如端点需要密钥
python examples/agent.py --provider uia --allow restaurantdemo.exe \
    --pack restaurant-demo --task "帮我点一个巨无霸和一份薯条，然后打开购物车"
python examples/check_contract.py --runs 5
python examples/eval_agent.py --runs 3
```

针对 `hy3`（推理模型，原生工具调用）的实测结果：

| 检查项 | 结果 |
| --- | --- |
| 工具契约（`add_product`、`open_cart`） | 10/10 |
| 任务成功（`one_big_mac`、`big_mac_and_fries`、`three_colas`） | 9/9 |
| 真实 WPF 应用完整验收 | 3 次工具调用内完成 |

在真实 WPF 应用上，模型依次执行 `observe` → `p1.add()` → `p2.add()` →
`c1.open()`，每次都产生预期的增量（`~ c1 count 0→1 total 0→5.99`，随后
`1→2`，最后是一个真实的 `+ WINDOW "Cart"`），并自主报告任务完成。

## 许可证

MIT。设计借鉴自 `desktop-cli`（GPL-3.0 —— **仅借鉴设计，无任何代码**）、
`Touchpoint`（MIT）与 `agent-desktop`；详见开发文档第 20 节。
