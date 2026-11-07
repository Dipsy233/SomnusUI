# 构建与开发

## 1. 环境

| 项 | 要求 |
| --- | --- |
| Python | 3.11 或更高 |
| 包管理 | `pip install -e ".[dev]"`；真实 Windows 操作另需 `[windows]`；MCP 另需 `[mcp]` |
| .NET | .NET 8 SDK，用于构建 `apps/scenario-suite`、`apps/restaurant-demo` 与 `tools/rdp-host` |
| Windows | 真实 UIA 测试与子会话需要 Windows；可移植套件与 mock 固件可在任意平台运行 |

## 2. 测试

```bash
pytest                      # 可移植套件，默认排除 -m windows
pytest tests/scenarios      # 固件上的验收界面套件
pytest --update-golden      # 重写 golden SUI-Text 固件
pytest -m windows           # 真实窗口测试，仅 Windows
```

`pyproject.toml` 默认 `addopts = "-q -m 'not windows'"`。真实窗口测试需要 Windows 与 UIA；`-m windows` 覆盖默认过滤。

当前 `python -m pytest --collect-only -q` 收集 955 项（默认过滤后）。历史文档记录的 788 passed 对应更早的测试规模；完整测试未在本轮重跑。

测试分层：

| 目录 | 内容 |
| --- | --- |
| `tests/scenarios/` | 八个验收界面固件上的行为测试：`test_store`、`test_form`、`test_chat`、`test_console`、`test_tree`、`test_native`、`test_menu`、`test_dialog`，以及 `test_contracts`、`test_bench`、`test_sui_templates` |
| `tests/e2e/` | 真实窗口测试：`test_windows_scenarios`、`test_windows_find_wait`、`test_windows_menu_dialog`、`test_windows_native`、`test_windows_real_apps`、`test_windows_settle_probe`、`test_windows_uia`、`test_windows_windows`、`test_examples`、`test_acceptance` |
| `tests/architecture/` | `test_import_boundaries.py`，检查模块依赖方向 |
| `tests/protocol/` | 工具档位、提示词预算、MCP 协议、工具选择测试 |
| `tests/golden/` | 整文件 SUI-Text 比对，`--update-golden` 重写 |
| `tests/fixtures/` | `scenarios.py` 八屏固件构建器、脚本化变更与回调 |
| `tests/support/`、`tests/templates/` | SUI-Text 模板匹配与按场景模板；`UPDATE_SUI_TEMPLATES=1` 重写模板 |
| `tests/compiler/`、`tests/projection/`、`tests/providers/`、`tests/runtime/` | 各层单元测试 |

`MockProvider` 回放录制的 `RawTree` 固件，并支持脚本化变更与逐行回调。新增场景先以固件形式在 mock 层复现，再补 UIA 版本。

## 3. 验收应用

`apps/scenario-suite` 是一个 WPF 应用，按命令行参数启动一个界面：

```bash
dotnet build apps/scenario-suite
apps/scenario-suite/bin/Debug/net8.0-windows/ScenarioSuite.exe store
```

界面为 `store`、`form`、`chat`、`console`、`tree`、`native`、`menu`、`dialog`，默认 `store`。每个界面同时有真实窗口与 `tests/fixtures/scenarios.py` 中的固件，因此 `tests/scenarios/` 可在任意平台运行，`tests/e2e/test_windows_scenarios.py` 在 Windows 上验证固件与真实控件一致。

`apps/restaurant-demo` 是内置 `restaurant-demo` pack 对应的 WPF 演示应用：

```bash
dotnet build apps/restaurant-demo
somnus --pack restaurant-demo observe
```

`tools/rdp-host` 是承载 RDP 控件的 .NET WinForms OLE 容器，用于子会话。

界面职责与断言口径见 [benchmarks/scenario-suite.md](benchmarks/scenario-suite.md)。

## 4. 基准

```bash
python -m somnus.cli bench --runs 100
python -m bench.run_exposure --scheme all --reps 3 --out runs/exposure
python -m bench.run_exposure --scheme all --layer live --reps 1 --out runs/exposure-live
python -m bench.run_exposure --scheme all --fake --reps 1 --out runs/smoke
```

- `somnus bench` 在 mock 固件上运行验收指标：零视觉、信息隔离、压缩比、动作成功率、任务成功率、L0 token 与耗时。
- `bench/run_exposure.py` 运行工具面暴露实验；方案定义在 `bench/exposure/schemes.py`，任务在 `bench/tasks/`，评分在 `bench/score/`。
- `bench/run_exposure.py --layer live` 在子会话中运行真实界面；子会话不可用时失败，不回退主桌面。
- `bench/demo_llm.py`、`bench/demo_llm_live.py` 为 LLM 控制演示。

指标口径与结果见 [benchmarks/tool-surface.md](benchmarks/tool-surface.md) 与 [archive/performance-2026-11.md](archive/performance-2026-11.md)。

## 5. 诊断

| 开关 | 作用 |
| --- | --- |
| `SOMNUS_PROFILE=1` | 输出枚举、快照、编译、settle 等分段耗时 |
| `SOMNUS_CACHE_TIER` | 选择缓存请求档位（`full`、`lean`、`sig`、`peek`、`rect`、`probe`），用于对比测量 |
| `SOMNUS_TRACE_STATES=1` | 输出每一步 primitive 的结果与目标前后状态 |
| `SOMNUS_LOG=DEBUG` | 打开日志，输出到 stderr |
| `--trace DIR` | 记录决策轨迹，见 [cli.md](cli.md) |

运行时代码保持静默：工具只返回 SUI-Text，日志默认不输出，避免日志进入模型上下文。

## 6. 开发约定

- 修改提示词后运行 `pytest tests/protocol/test_prompt_budget.py`。核心提示词上限为 1000 token；core 与完整工具面都必须低于拆分前的 2669 token。
- 修改工具或档位后运行 `tests/protocol/test_tool_policy.py`，`TOOL_POLICY` 与 `TOOL_SPECS` 必须一一对应。
- 修改 SUI-Text 输出后按需运行 `pytest --update-golden`，并在变更说明中记录 golden 变化。
- 新增跨层 import 前先检查 `tests/architecture/test_import_boundaries.py` 的允许矩阵。
- 不引入截图、坐标点击、OCR 或视觉模型；`BoundingRectangle` 只用于视口判定。
