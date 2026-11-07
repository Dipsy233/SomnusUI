# SomnusUI

[English](README.md) | 中文

## 项目作用

SomnusUI 把原生 GUI 编译为文本接口。运行时读取 Windows 无障碍树，将其投影为 SUI-Text（节点、能力与属性），再通过 UI Automation 执行能力调用；文本输入可选剪贴板与键盘通道。观察与回复中不含截图、坐标或原生树标识符；`launch` 回复会报告启动进程的 `pid`。

工具面共九个调用：`observe`、`find`、`inspect`、`call`、`run`、`help`、`switch`、`launch`、`configure`。默认 MCP 宿主暴露六个 `core` 工具；`switch`、`launch`、`configure` 属于按需开启的 `desktop` 档。响应为 SUI-Text：一次观察、一份命中列表、一行带增量的 `OK`/`ERR`、一行设置结果，或一篇主题文档。

编译器是确定性的，运行时内部不运行语言模型。无视觉无法读取的内容按不可读如实报告，不做猜测。

## 使用方法

需要 Python 3.11 或更高版本。在仓库根目录执行以下命令。

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
somnus repl                        # 一个存活会话，用于多步操作
somnus mcp                         # MCP 服务器（stdio）
```

ref（`w1`、`p1`、`q1` …）只在一个会话内有效。`somnus observe`、`somnus call` 这类单次命令执行一次工具调用后进程退出，所有 ref 随进程失效。多步任务使用 `somnus repl`、MCP 服务器或 `run` 工具。

MCP 配置：

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

完整命令说明见 [docs/cli.md](docs/cli.md)。设置与环境变量见 [docs/configuration.md](docs/configuration.md)。

## 文档指引

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 仓库结构、编译/投影/运行时流水线、模块职责、依赖方向 |
| [docs/technology.md](docs/technology.md) | 技术选型及其当前实现形态 |
| [docs/protocol.md](docs/protocol.md) | 九个工具、SUI-Text、观察、find/inspect/call、批处理、脚本、错误码、工具档位 |
| [docs/cli.md](docs/cli.md) | 安装、CLI 命令、REPL、MCP、决策轨迹、自检与回放 |
| [docs/configuration.md](docs/configuration.md) | 设置文件、环境变量、优先级与 `configure` 工具 |
| [docs/development.md](docs/development.md) | 构建、测试、固件、基准与验收应用 |
| [docs/guides/observation-walkthrough.md](docs/guides/observation-walkthrough.md) | 一个窗口及其工具调用，附真实输出 |
| [docs/reference/packs.md](docs/reference/packs.md) | App Pack 编写 |
| [docs/reference/child-session.md](docs/reference/child-session.md) | 子会话设计、代价与未采用方案 |
| [docs/reference/text-input-channels.md](docs/reference/text-input-channels.md) | 文本输入路径、代价与验证 |
| [docs/benchmarks/tool-surface.md](docs/benchmarks/tool-surface.md) | 工具档位与工具暴露基准 |
| [docs/benchmarks/app-tasks.md](docs/benchmarks/app-tasks.md) | 50 个用户任务与实测覆盖 |
| [docs/benchmarks/scenario-suite.md](docs/benchmarks/scenario-suite.md) | 验收界面与通过标准 |
| [docs/archive/performance-2026-11.md](docs/archive/performance-2026-11.md) | 性能与视口采集：实测、改动与剩余工作 |
| [docs/archive/plan-v3.zh-CN.md](docs/archive/plan-v3.zh-CN.md) | 黑盒测试方案 v3（历史提案） |
