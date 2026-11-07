# 架构与项目结构

## 1. 信息流

SomnusUI 在感知与动作两个方向上使用同一套编译结果。

感知路径：

```
Windows UI Automation / mock 固件 / 子会话 peer
        │  list_windows、snapshot
        ▼
RawTree（原始无障碍树）
        │  normalize → simplify → 名称合成 → 实体匹配 → 孪生折叠 → 关系推导
        ▼
CompileResult（语义节点、ref、能力、实体）
        │  projection（keep / flatten / prune / collapse + 预算）
        ▼
SUI-Text
        │  MCP / JSON-RPC / CLI
        ▼
模型或调用方
```

动作路径：

```
CALL ref.cap(args)
        │  parse（SUI-Text 解析）
        ▼
resolver（ref + 能力 → 可执行步骤）
        │  policy（进程允许列表、合成输入通道）
        ▼
executor（UI Automation 模式 / 剪贴板 / 键盘）
        │  settle（签名探测与静默窗口）
        ▼
重新编译 → delta / verifier → OK / ERR 回复
```

ref 在会话内稳定；切换桌面、滚动或展开等会改变视口的动作会使部分 ref 失效，回复中按实际结果报告。

## 2. 模块职责

| 目录 | 职责 |
| --- | --- |
| `somnus/ir/` | `RawNode` / `RawTree` / `SemanticNode`、能力与动作值对象、指纹计算 |
| `somnus/providers/` | provider 协议、mock 固件回放 |
| `somnus/providers/windows/` | `uia.py` UIA 读写；`cache.py` 缓存请求；`executor.py` 动作执行；`keyboard.py` 键盘通道；`workspace.py` 前台与虚拟桌面；`events.py` UIA 事件；`clipboard.py` 剪贴板保护 |
| `somnus/compiler/` | `normalize` 角色/状态规范化；`simplify` 结构简化；`distill` 投影蒸馏插件链；`names` 名称合成；`match` 实体匹配；`twins` 孪生折叠；`relations` 关系推导；`compile` 编译入口；`pack_schema` / `yaml_pack` App Pack |
| `somnus/projection/` | `rules.py` 投影规则；`budget.py` token 预算；`engine.py` 渲染计划 |
| `somnus/sui/` | `serialize.py` SUI-Text 序列化；`parse.py` 解析；`grammar.md` 文法 |
| `somnus/runtime/` | `registry` ref 注册表；`locator` 重定位；`resolver` 能力解析；`preflight` 动作前检查；`policy` 允许列表与通道开关；`settle` 稳定等待；`verifier` 效果校验；`delta` 增量；`session` 会话主流程 |
| `somnus/session/` | 子会话：`childsession`、`setup`、`credentials`、`rdp`、`launch`、`bridge`、`wire`、`peerprovider`、`peerstart`、`peerboot`、`lifecycle` |
| `somnus/selfcheck/` | `doctor`、`eval`、`replay`、`heal`、`distill_script`、沙箱选择 |
| `somnus/packs/` | `generic.py`、`restaurant_demo.py` 内置 pack |
| `somnus/protocol/` | `tools.py` 工具定义与 `call_tool`；`policy.py` 工具档位；`help.py` 主题文档；`mcp_server.py`、`jsonrpc.py` 传输；`system_prompt.md`、`agent/*.md` |
| `somnus/observability/` | `trace.py` 事件接收器；`jsonl.py` 轨迹写入；`view.py` 时间线渲染 |
| `somnus/config.py` | 持久设置读取、校验与写入 |
| `somnus/cli.py` | 命令行入口 |

## 3. 仓库结构

```
somnus/
├── ir/            节点、能力与动作值对象
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace,clipboard}.py
├── compiler/      normalize → simplify/distill → names → match → twins → relations → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry locator resolver preflight policy settle verifier delta session
├── session/       子会话：childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart peerboot lifecycle
├── selfcheck/     doctor eval replay heal distill_script sandbox
├── packs/         generic.py restaurant_demo.py
├── protocol/      tools.py policy.py help.py mcp_server.py jsonrpc.py
│                  system_prompt.md agent/*.md
├── observability/ trace.py jsonl.py view.py
├── config.py      持久设置
└── cli.py
apps/restaurant-demo/   WPF（.NET 8）餐厅演示应用
apps/scenario-suite/    WPF（.NET 8）验收应用，八个界面
tools/rdp-host/          .NET（WinForms）OLE 容器，承载 RDP 控件
docs/                    guides/ reference/ benchmarks/ archive/
examples/               agent 循环、契约检查、任务评估
bench/                  基准门槛、工具面暴露实验、LLM 控制演示
tests/                  按模块划分的单元测试、场景固件、golden 与 e2e
```

`tests/` 的子目录包括 `architecture`、`bench`、`compiler`、`e2e`、`fixtures`、`golden`、`projection`、`protocol`、`providers`、`runtime`、`scenarios`、`support`、`templates`。各层的具体测试入口见 [development.md](development.md)。

## 4. 依赖方向

```
providers → compiler → projection → serialization → protocol
runtime 依赖以上全部
```

约束：

- 任何模块都不导入语言模型。
- `ir` 只包含数据对象，不导入 provider、compiler、projection 或 protocol。
- 传输层（MCP、JSON-RPC、CLI）只通过 `somnus.protocol.tools.call_tool` 调用会话，三种入口行为一致。
- 依赖边界由 `tests/architecture/test_import_boundaries.py` 检查。

## 5. 设计约束

- 无障碍树是唯一感知通道。视口之外的列表行以 `rows= / visible=` 汇总，不通过 `IsOffscreen` 判断可见性。
- 读取与写入前提升目标窗口；已在前台的窗口不重复提升。`OBSERVE windows` 不提升窗口。
- 节点上打印的能力被调用时直接执行；运行时没有确认步骤、风险评分或危险操作拦截。`+submit!` 的 `!` 是能力元数据。
- 编译器确定性运行；模型不参与编译、投影或执行。
- 无法在无视觉条件下读取的内容标记为 `? ref role reason=visual`；无名称、无子节点、无身份标识的图片在编译前丢弃。
- 动作失败按错误码与恢复提示返回，堆栈只写入日志。
