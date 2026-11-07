# 设置与配置

## 1. 设置文件

持久设置位于一个 TOML 文件。路径按以下顺序确定：

1. 环境变量 `SOMNUS_CONFIG` 指定的路径；
2. 平台配置目录下的 `somnusui/config.toml`：
   - Windows：`%APPDATA%\somnusui\config.toml`
   - macOS：`~/Library/Application Support/somnusui/config.toml`
   - Linux/BSD：`$XDG_CONFIG_HOME/somnusui/config.toml`，未设置时为 `~/.config/somnusui/config.toml`

```bash
somnus config --path              # 只打印文件位置
somnus config                     # 当前生效值；首次运行写出默认文件
somnus config --no-allow-clipboard
somnus config --no-allow-keyboard
somnus config --allow-clipboard
```

文件为普通 TOML，带注释，可手工编辑。保存时重新渲染带注释的模板，不覆写为裸键值对。

## 2. 设置项

```toml
# 两条合成输入通道默认开启，设为 false 关闭。
allow_clipboard = true
allow_keyboard  = true

# 可选：限制动作可操作的进程；空列表表示不限制。
allow_processes = []

# 可选：在子会话中运行 agent。
child_session = false
child_session_debug_window = true
child_allow_spawn = true

# 调用时序。
settle_quiet_ms   = 40
settle_poll_ms    = 10
settle_timeout_s  = 3.0
post_action_enum  = true
actionable_timeout_s = 0.3
```

| 键 | 默认 | 范围 | 含义 |
| --- | --- | --- | --- |
| `allow_clipboard` | `true` | 布尔 | 允许剪贴板 + `Ctrl+V` 通道；要求目标窗口在前台 |
| `allow_keyboard` | `true` | 布尔 | 允许合成键盘输入；要求目标窗口在前台 |
| `allow_processes` | `[]` | 进程名列表 | 限制可操作与可启动的进程；空表示不限制 |
| `child_session` | `false` | 布尔 | 在子会话中运行 agent |
| `child_session_debug_window` | `true` | 布尔 | 保留 RDP 环回窗口可见，便于测试 |
| `child_allow_spawn` | `true` | 布尔 | 允许子会话 peer 启动进程；`allow_processes` 仍决定哪些程序可启动 |
| `settle_quiet_ms` | `40` | 10–1000 | UI 保持不变的静默窗口；值越小越快，过小可能读到动画中间态 |
| `settle_poll_ms` | `10` | 1–100 | 探测间隔 |
| `settle_timeout_s` | `3.0` | 0.5–30 | 单次 settle 的硬上限；超时返回 `settled=false`，仍携带增量 |
| `post_action_enum` | `true` | 布尔 | 动作后重新枚举窗口；关闭后每次调用约省 100 ms，可能漏报动作打开的窗口 |
| `actionable_timeout_s` | `0.3` | 0–2 | 被阻塞的动作最多等待多久；`0` 关闭等待 |

`actionable_timeout_s` 只能通过文件或环境变量设置，`configure` 不接受该键。

## 3. 环境变量

| 变量 | 对应设置 |
| --- | --- |
| `SOMNUS_CONFIG` | 设置文件路径 |
| `SOMNUS_ALLOW_CLIPBOARD` | `allow_clipboard` |
| `SOMNUS_ALLOW_KEYBOARD` | `allow_keyboard` |
| `SOMNUS_CHILD_SESSION` | `child_session` |
| `SOMNUS_CHILD_SESSION_DEBUG_WINDOW` | `child_session_debug_window` |
| `SOMNUS_CHILD_ALLOW_SPAWN` | `child_allow_spawn` |
| `SOMNUS_SETTLE_QUIET_MS` | `settle_quiet_ms` |
| `SOMNUS_SETTLE_POLL_MS` | `settle_poll_ms` |
| `SOMNUS_SETTLE_TIMEOUT_S` | `settle_timeout_s` |
| `SOMNUS_POST_ACTION_ENUM` | `post_action_enum` |
| `SOMNUS_ACTIONABLE_TIMEOUT_S` | `actionable_timeout_s` |
| `SOMNUS_CAPABILITIES` | 工具档位，见 [protocol.md](protocol.md) |
| `SOMNUS_TRACE` | 决策轨迹目录，见 [cli.md](cli.md) |
| `SOMNUS_IDLE_TIMEOUT_S` | MCP 服务器空闲回收秒数，默认关闭 |

布尔环境变量接受 `true` / `false` 等常见写法；`0`、`false`、`no`、空值表示关闭，可用于在单次运行中覆盖文件里的 `true`。

## 4. 优先级

合成输入通道：

```
单次命令行参数（--allow-clipboard / --no-allow-clipboard）
    > 环境变量
    > 设置文件
    > 内置默认（开启）
```

时序参数：

```
内置默认 < 设置文件 < 环境变量 < configure
```

`configure` 作用于当前会话的下一次调用。`persist=true` 写入配置文件。`reset=true` 恢复本进程启动时的有效值。

## 5. `configure` 工具

```json
{"settle_quiet_ms": 80, "settle_poll_ms": 20, "post_action_enum": false}
```

```text
SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

`configure` 接受 `settle_quiet_ms`、`settle_poll_ms`、`settle_timeout_s`、`post_action_enum`，以及 `persist`、`reset`。任一值非法时整批拒绝，回复为 `ERR INVALID_ARGUMENT`，设置不变。`reset=true` 时忽略同时提交的补丁。`persist=true` 时回复追加 `persisted=<路径>`。

`configure` 只调整节奏参数，不授予或拒绝能力。

## 6. 文件读取失败

文件损坏、不可读或值类型错误时，运行时回退到内置默认，并在 `Settings.problems` 中记录问题；`somnus config` 会在输出中列出 `warning` 行。未知键被忽略，避免新旧版本互相覆盖。
