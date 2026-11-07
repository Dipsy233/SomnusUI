# 技术选型

本文记录项目当前使用的技术、依赖与已移除或未采用的方案。工具面实验数据见 [benchmarks/tool-surface.md](benchmarks/tool-surface.md)，其他历史评估见 `docs/archive/`。

## 1. 语言与构建

| 项 | 当前值 | 说明 |
| --- | --- | --- |
| 语言 | Python | `requires-python = ">=3.11"` |
| 构建后端 | setuptools + wheel | `setuptools>=68`，`build-backend = "setuptools.build_meta"` |
| 包名 / 版本 | `somnusui` / `0.1.0` | 见 `pyproject.toml` |
| 许可证 | MIT | |
| 入口 | `somnus`、`somnus-mcp` | 分别指向 `somnus.cli:main` 与 `somnus.protocol.mcp_server:main` |
| 验收应用 | .NET 8 WPF | `net8.0-windows`；`scenario-suite` 同时启用 WinForms 以承载原生控件 |

## 2. 依赖

| 依赖 | 版本约束 | 用途 | 现状 |
| --- | --- | --- | --- |
| `msgspec` | `>=0.18` | 在 `pyproject.toml` 中声明为核心依赖 | 当前 `somnus/` 源码未直接导入；JSON 处理使用标准库 `json` |
| `PyYAML` | `>=6.0` | 单个 YAML 文件形式的 App Pack | `somnus/compiler/yaml_pack.py` 直接导入；缺少该依赖时内置 Python pack 仍可加载 |
| `comtypes` | `>=1.2`，`[windows]` 扩展 | Windows UI Automation 与 COM 接口 | `somnus/providers/windows/` 使用 |
| `pywinauto` | `>=0.6.8`，`[windows]` 扩展 | 在 `pyproject.toml` 中声明 | 当前源码未直接导入；`somnus doctor` 会检查是否安装 |
| `mcp` | `>=2,<3`，`[mcp]` 扩展 | MCP stdio 服务器 | `somnus/protocol/mcp_server.py` 使用 2.x 的构造函数注册处理器 |
| `pytest` | `>=8.0`，`[dev]` 扩展 | 测试 | |
| `pytest-cov` | `>=5.0`，`[dev]` 扩展 | 覆盖率 | |

尚未安装 `comtypes` 时可以运行 mock 固件与离线场景；真实 Windows 操作需要 `pip install -e ".[windows]"`。

## 3. 感知与动作

| 选择 | 实现 |
| --- | --- |
| 感知通道 | Windows UI Automation 无障碍树，经 `comtypes` 调用 |
| 离线感知 | `MockProvider` 回放录制的 `RawTree` 固件，并支持脚本化变更 |
| 跨会话感知 | `peer` provider：子会话内的瘦 peer 通过命名管道响应 provider 接口 |
| 动作执行 | UIA 模式（Invoke、SelectionItem、Toggle、Value、ExpandCollapse、Scroll 等）；文本输入按 Value、剪贴板、键盘三条路径依次尝试 |
| 稳定判定 | 签名探测加静默窗口，默认 `settle_quiet_ms=40`、`settle_poll_ms=10`、`settle_timeout_s=3.0` |
| 窗口提升 | 读取与动作前提升目标窗口；被遮挡窗口的原始状态在 provider 关闭时恢复 |

项目不使用截图、坐标点击、OCR 或视觉模型。`BoundingRectangle` 仅作为视口判定信号进入缓存，不用于点击。

已移除的文本写入路径：`WM_SETTEXT` / `EM_REPLACESEL`。该路径的门控条件是元素映射了 HWND，而实测中能承载可编辑文本且映射 HWND 的控件同时发布 Value 模式，因此该路径未被实际执行。详见 [reference/text-input-channels.md](reference/text-input-channels.md)。

## 4. 格式与协议

| 选择 | 实现 |
| --- | --- |
| 模型可见格式 | SUI-Text：一行一个节点，能力内联，默认能力与默认状态省略 |
| 内部格式 | 数据对象与 JSON；轨迹文件为 JSONL |
| 传输 | MCP stdio、JSON-RPC、CLI；三者共用 `somnus.protocol.tools.call_tool` |
| 提示词分层 | 每轮注入简短核心提示词 `somnus/protocol/system_prompt.md`；完整语法位于 `somnus/protocol/agent/*.md`，由 `help <topic>` 按需读取 |
| 工具档位 | `core`（六工具）与 `desktop`（三个工具），由 `SOMNUS_CAPABILITIES` 选择，默认 core |
| 能力元数据 | `somnus/protocol/policy.py` 的 `TOOL_POLICY` 是工具可见性的唯一数据源 |

当前提示词实测值（使用 `somnus.projection.budget.estimate_tokens`）：

| 项 | token |
| --- | ---: |
| 核心提示词 `system_prompt.md` | 715 |
| core 档六个工具 schema | 596 |
| core 档合计 | 1311 |
| 九个工具 schema | 981 |
| 九工具合计 | 1696 |
| MCP `initialize` instructions（core 档，默认设置） | 279 |

这些数值随提示词与 schema 修改而变化，`tests/protocol/test_prompt_budget.py` 约束 core 提示词不超过 1000 token，并要求 core 与完整工具面均低于拆分前的 2669 token。

## 5. 子会话

子会话是同一账户的第二次交互式登录，拥有独立的 input desktop、前台窗口与输入队列。实现使用：

- Windows 子会话与 RDP 环回；
- 命名管道 `\\.\pipe\somnus-peer-<id>` 上运行的瘦 peer；
- 凭据委托，或在被拒绝时使用 DPAPI 加密的存储密码；
- `process.start` / `process.status` / `process.kill` 三个 peer 线方法。

未采用方案的事实记录见 [reference/child-session.md](reference/child-session.md)。

## 6. 已移除或未采用的方案

| 方案 | 当时内容 | 现状 |
| --- | --- | --- |
| 确认门控 | `+submit!` 等 critical 能力返回 `CONFIRMATION_REQUIRED`，需 `--confirmed` | 已移除；`!` 作为能力元数据保留，调用直接执行 |
| `WM_SETTEXT` / `EM_REPLACESEL` | 在 Value 路径与剪贴板路径之间增加 HWND 写入路径 | 已删除；门控场景由 Value、剪贴板、键盘三条路径覆盖 |
| 坐标点击、截图、OCR、视觉模型 | 作为无 UIA 接口时的回退 | 未采用 |
| 顶层工具合并（H1–H6） | observe/find/inspect 合并、call 合并、look/do 二分等七个层级变体 | 实验后保留平铺九工具 H0；见 [benchmarks/tool-surface.md](benchmarks/tool-surface.md) |
| 能力名 `invoke` | 将 `activate` 改为控件原生模式名 `invoke` | 未采用；`activate` 别名保留。`NATIVE_CAP_NAMES` 开关位于 `somnus/compiler/caps.py`，默认关闭 |
| 工具改名 `act` / `script` / `look-search-peek` | 第 2 批命名实验 | 未采用 |
| 最小起步工具面 T2 | 初始只暴露 observe/call/help，调用 help 展开 | 未采用；采用 T1 原则，默认 core 档 |
| `Windows.UI.UIAutomation.Core` | 跨会话 UIA 桥接 | 实现后 `ConnectionRequested` 未触发，`RemoteAutomationServer.ReportSession` 返回“class not registered”；未采用 |
| 虚拟桌面隔离 | 用虚拟桌面分离 agent 输入 | 未采用；虚拟桌面共享输入桌面与前台窗口 |
| Playwright 的 YAML aria snapshot、快照级 ref、ElementHandle、`force`、任意代码执行、完整 Trace Viewer、Playwright Test runner | 评估过的外部方案 | 未采用 |
