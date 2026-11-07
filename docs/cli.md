# 命令行与运行方式

## 1. 安装

需要 Python 3.11 或更高版本。在仓库根目录执行：

```bash
pip install -e ".[dev]"       # 可移植测试与开发依赖
pip install -e ".[windows]"   # 真实 Windows 应用：comtypes、pywinauto
pip install -e ".[mcp]"       # MCP 服务器依赖
```

安装后提供两个入口：`somnus` 与 `somnus-mcp`。`bench/` 与 `tests/` 按仓库路径引用，不随包安装。

## 2. 全局选项

| 选项 | 作用 |
| --- | --- |
| `--provider {auto,uia,mock}` | 选择无障碍来源；`auto` 在安装 `[windows]` 时使用 UIA，否则使用固件 |
| `--fixture PATH` | 载入录制的 `RawTree` 固件；可重复 |
| `--demo` | 使用内置餐厅演示固件 |
| `--scenario NAME` | 使用验收应用的某个界面固件：`store`、`form`、`chat`、`console`、`tree`、`native`、`menu`、`dialog` |
| `--pack ID` | 指定已注册的 App Pack |
| `--no-pack` | 所有窗口按通用模式编译，不应用 App Pack |
| `--allow PROCESS` | 允许操作或启动的进程；可重复 |
| `--allow-clipboard` / `--no-allow-clipboard` | 单次开启或关闭剪贴板通道 |
| `--allow-keyboard` / `--no-allow-keyboard` | 单次开启或关闭键盘通道 |
| `--config PATH` | 从指定文件读取设置 |
| `--trace [DIR]` | 记录决策轨迹；省略 `DIR` 时在平台数据目录下新建运行目录。环境变量 `SOMNUS_TRACE` |

合成输入通道默认开启。持久开关见 [configuration.md](configuration.md)。

## 3. 命令

| 命令 | 作用 |
| --- | --- |
| `somnus windows` | 列出顶层窗口 |
| `somnus observe [window]` | 输出 L0 SUI-Text；`-o/--out PATH` 写入文件，stdout 只留一行回执 |
| `somnus call "CALL p1.add()"` | 执行一次动作 |
| `somnus repl` | 交互式会话，`sui>` 提示符 |
| `somnus help [topic]` | 打印发现摘要或一篇主题文档 |
| `somnus record <window> -o fixture.json` | 将窗口录制为 `RawTree` 固件 |
| `somnus config` | 查看或修改持久设置；`--path` 只打印文件位置 |
| `somnus child-session <action>` | 子会话管理，见 [reference/child-session.md](reference/child-session.md) |
| `somnus trace show <path>` | 打印轨迹时间线；`--snapshots` 附带前后 SUI-Text，`--limit N` 限制条目 |
| `somnus trace to-script <path>` | 将轨迹蒸馏为 `run` 脚本草稿；`--out PATH`、`--await-timeout S` |
| `somnus doctor` | 环境报告与 observe/find/call 冒烟；`--no-smoke`、`--real` |
| `somnus eval` | 通用自检，输出 JSON 判定；`--real`、`--no-trace` |
| `somnus replay <path>` | 回放轨迹并报告首个分歧；`--json`、`--real`、`--no-trace` |
| `somnus heal <script>` | 回放 `run` 脚本并修补一个失败步骤；`--window`、`--json`、`--no-retry`、`--real`、`--no-trace` |
| `somnus bench [--runs 100]` | 运行验收基准 |
| `somnus mcp` | 启动 MCP 服务器（stdio） |
| `somnus rpc` | 启动 JSON-RPC 服务器（stdio） |

`bench` 推荐使用 `python -m somnus.cli bench --runs 100`。控制台脚本的 `sys.path` 从 `.venv/Scripts` 开始，仓库内的 `bench` 包不在其中；在仓库根目录使用 `python -m` 会把仓库放入 `sys.path`。其他命令通过 `somnus` 入口执行。

## 4. ref 生命周期与 REPL

ref（`p1`、`w1`、`q1` …）由会话内存中的注册表分配，进程退出即失效。

- `somnus observe` 与 `somnus call` 执行一次工具调用后退出，ref 随进程失效。
- `somnus repl` 保持一个会话，ref 稳定，状态连续。
- MCP 服务器为每个客户端维护一个长生命周期会话。
- `run` 在一次调用内执行完整流程。
- `somnus --demo` 与 `somnus --scenario` 遵循相同的单次规则。

REPL 启动后先打印窗口清单，然后在 `sui>` 提示符读取 SUI-Text。`quit`、`exit`、`:q` 退出。

```bash
somnus repl                    # 应用匹配的 App Pack
somnus --no-pack repl          # 通用模式
somnus --pack restaurant-demo repl
somnus --demo repl             # 离线演示
```

REPL 可用的观察形式：

| 命令 | 返回 |
| --- | --- |
| `OBSERVE windows` | 窗口清单 |
| `OBSERVE w4` | 只观察 w4 的内容 |
| `OBSERVE w4 desktop` | w4 的内容，并附其他窗口清单 |
| `OBSERVE w4 full` | 完整树，不折叠、不限预算 |
| `OBSERVE w4 expand g3` | 展开一个折叠节点，其余保持折叠 |

手势命令为 `CALL ref.select()`、`CALL ref.activate()`、`CALL ref.context_menu()`。三个手势含义不同，运行时不做替代。`context_menu` 走 UIA `ShowContextMenu`，不移动指针、不发送按键；XAML 中声明的 WPF `ContextMenu` 无法通过该方式触发，运行时会报告未弹出菜单并指出可用的直接能力。

## 5. MCP 服务器

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

服务器按 `SOMNUS_CAPABILITIES` 过滤 `tools/list`，默认六个 core 工具。`initialize` 时发送一次 `instructions`；`configure` 返回实时设置。默认会话与客户端同生命周期。`SOMNUS_IDLE_TIMEOUT_S` 设置空闲回收时长（秒，默认关闭），超时后关闭 provider，下一次调用重建会话，旧 ref 返回 `STALE_TARGET`。

传输层与工具定义分离：MCP 与 JSON-RPC 都调用 `somnus.protocol.tools.call_tool`，行为一致。

## 6. 决策轨迹

`--trace DIR` 或 `SOMNUS_TRACE=DIR` 记录运行时的决策：能力路由、settle 帧、增量、验证状态、错误码，以及调用前后模型看到的 SUI-Text。未指定 `DIR` 时，在平台数据目录（Windows 为 `%LOCALAPPDATA%\somnusui\traces`）下创建运行目录，位于仓库之外。

```bash
somnus --demo --trace /tmp/somnus-demo-trace call 'CALL p1.add()'
somnus trace show /tmp/somnus-demo-trace                     # 时间线
somnus trace show /tmp/somnus-demo-trace --snapshots         # 附带前后 SUI-Text
somnus trace to-script /tmp/somnus-demo-trace --out flow.sui # run 脚本草稿
```

跟踪默认关闭。轨迹文件是开发证据，留在仓库外，当前阶段未脱敏；快照可能包含窗口标题、表单值和聊天文本。脱敏入口在 `somnus/observability/jsonl.py` 中标记为 `TODO(stage-final)`。

`trace to-script` 把成功的 `CALL` 转为 `AWAIT "name"` 加 `CALL $it.cap(args)`；无法命名的步骤降级为 `# TODO` 注释。生成的脚本可由 `heal` 修补。

## 7. 自检与回放

```bash
somnus doctor                 # 环境报告 + observe/find/call 冒烟
somnus --demo doctor          # 离线版本
somnus eval                   # JSON 判定，结论来自运行时状态
somnus --scenario store eval  # 离线
somnus replay <trace>         # 回放记录的调用，报告首个分歧
somnus --demo replay <trace>  # 离线
somnus heal script.sui        # 回放 run 脚本，修补一个失败步骤
somnus --demo heal script.sui # 离线
```

沙箱规则：

- 默认桌面是子会话；mock 与 scenario 固件是离线路径；操作用户主桌面需要显式 `--real`。
- 没有活的子会话时明确报错并给出启动命令，不回退到主桌面。
- `doctor` 对子会话生命周期只读：报告是否存在，不自行开启。

`eval` 执行 `observe`、`find`、`call` 三个通用任务，成功判定来自运行时：`observe` 必须编译出结果，`find` 命中的 ref 必须能在会话自己的结果中解析，`call` 必须返回 `OK`。任务只使用 `focus`。

`replay` 读取轨迹中的 `call.start` 记录，从动作前快照重建元素身份：类型加名称（同名行按文档顺序），再通过当前桌面的 `FIND` 得到本次 ref；窗口 ref 按标题定位。它比较状态、错误码与 delta 计数，并在首个分歧处停止。`eval` 与 `replay` 默认各写一份轨迹，可用 `--no-trace` 关闭。

`heal` 运行 `run` 脚本，先观察目标窗口以保存每个 ref 的定位配方。某一步失败时，用配方中的角色、名称、实体与目标能力，通过运行时定位器重建目标元素。唯一命中时给出补丁并重跑一次；多个候选时列出候选，不执行动作。定位器不暴露为模型工具。

## 8. 演示、场景与基准

```bash
somnus --demo observe
somnus --demo call 'CALL p1.add()'
somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
python -m somnus.cli bench --runs 100
```

`--demo` 使用内置餐厅演示。`--scenario` 回放验收应用的某个界面。`bench` 在 mock 固件上运行验收指标，见 [benchmarks/scenario-suite.md](benchmarks/scenario-suite.md)。

## 9. 子会话命令

| 命令 | 作用 |
| --- | --- |
| `somnus child-session status` | 只读：功能可用性与会话状态 |
| `somnus child-session setup` | 启用功能；需要管理员权限；`--dry-run`、`--disable` |
| `somnus child-session start` | 打开 RDP 环回；`--headless`、`--width`、`--height`、`--no-clipboard`、`--timeout`、`--auto`、`--logon-timeout`、`--keep-alive` |
| `somnus child-session credential` | 保存或查询密码；`--save`、`--user`、`--status`、`--clear` |
| `somnus child-session peer` | 在子会话内运行瘦 peer；`--project` |
| `somnus child-session peer-setup` | 让 peer 随子会话登录启动；`--remove`、`--python`、`--project` |
| `somnus child-session peer-status` | 查看 peer 是否已注册自动启动 |
| `somnus child-session probe` | 向管道另一侧的 peer 查询可见内容 |
| `somnus child-session close` | 结束会话并清理残留 |
| `somnus child-session run -- <cmd>` | 在子桌面运行程序并打印退出码与输出；`--cwd`、`--timeout` |

设计与前置条件见 [reference/child-session.md](reference/child-session.md)。

## 10. 诊断开关

| 变量 | 作用 |
| --- | --- |
| `SOMNUS_LOG` | 日志级别，例如 `DEBUG`；输出到 stderr |
| `SOMNUS_TRACE_STATES` | 打印每一步 primitive 的结果与目标前后状态；隐含启用日志 |
| `SOMNUS_PROFILE` | 打印分段耗时（枚举 / 快照 / 编译 / settle） |
| `SOMNUS_CACHE_TIER` | 选择缓存请求档位，用于 A/B 测量 |
