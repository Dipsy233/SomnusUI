# 子会话

## 1. 问题

应用通过自身发布的接口（`Invoke`、`Value`、`Toggle` 等）被驱动。对于没有可写接口的控件——自绘编辑器、终端面、只在回车时提交的地址栏——只能使用合成输入：剪贴板通道（`allow_clipboard`）或键盘通道（`allow_keyboard`）。

每次写入都会先提升目标窗口，见 [text-input-channels.md](text-input-channels.md)。运行时不在无焦点状态下写入，因为落点取决于当时的前台状态，副作用无法归因。前台因此成为 agent 需要的资源，而一个桌面只有一个前台。agent 工作期间会与使用键盘的人竞争，任一方失去前台都会停止工作。子会话用于消除这种争用。

## 2. 子会话的作用

子会话是同一账户的第二次交互式登录。文件、注册表与程序相同，输入桌面、前台窗口与输入队列独立。

agent 在其中可以提升窗口、键入与粘贴。控制台会话中的用户不受影响，因为两个会话不共享输入资源。

实测的双向结果：会话 1 中的进程枚举到会话 0 和 1；会话 2 中的进程枚举到会话 0 和 2。无障碍树遵循同样规则。

## 3. 桌面切换

agent 同一时间只驱动一个桌面。桌面切换是一个工具。运行时不向 agent 同时提供两个桌面，因为每个 ref（`w1`、`b3`、`q1`）只在某个桌面的命名空间内有效，同时暴露两个桌面会使每次调用产生歧义。

```
SWITCH main          回到用户桌面
SWITCH child         切到子会话（必须已打开）
SESSION open         打开子会话并切过去
SESSION close        结束子会话并返回
SESSION detach       本进程退出后保留子会话
```

切换桌面时不分配 ref。回复为 `OK session=… windows=N`，随后一行 `desktop changed; observe again`。重新 `OBSERVE` 之前，`CALL` 与 `INSPECT` 引用旧节点或旧窗口 ref 时返回 `STALE_TARGET`，并带 `hint=observe`。未改变桌面的切换返回 `OK session=… (already here)`，当前 ref 继续有效。

## 4. 在子桌面启动程序

程序必须在子会话内部启动才会出现在该桌面。peer 已在子会话中运行，使用自身令牌启动程序，不使用 `CreateProcessAsUser`、提权或令牌借用。

```bash
somnus child-session run --cwd E:/0down/SomnusUI -- python.exe -m pytest -m windows
somnus child-session run -- cmd /c start notepad.exe
```

`run` 向 peer 发送 `process.start`，轮询 `process.status` 直到子进程退出，或达到 `--timeout`（默认 600 秒）后杀死该进程；随后打印退出码与完整 stdout/stderr。输出文件位于 `%TEMP%\somnusui-child-run\<timestamp>\`，只保留最近一次，旧运行会被删除。

peer 线方法（不对模型暴露）：

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `process.start` | `command`、`args[]`、`cwd`、`stdout_path`、`stderr_path` | `{pid}` |
| `process.status` | `pid` | `{pid, alive, exit_code}`；存活时 `exit_code` 为 null |
| `process.kill` | `pid` | `{pid, killed}` |

错误码为 `PERMISSION_DENIED`、`INVALID_ARGUMENT`、`NATIVE_EXECUTION_FAILED`，并带消息。`process.kill` 供 `run --timeout` 使用；通过启动器（`cmd /c start ...`）启动的程序留给调用方观察。

两个开关决定启动权限：

- `child_allow_spawn`（默认 `true`）：是否允许启动能力本身；
- `allow_processes`（空表示任意）：可执行文件基名必须在该列表中。该列表同时决定哪些进程可被操作；不能被操作的程序也不能被启动。

`run` 仅提供 CLI。模型面完整工具为九个；默认 MCP 宿主列出六个 core 工具，`switch`、`launch`、`configure` 属于按需开启的 `desktop` 档。`launch` 在当前桌面（main 上本地 `CreateProcess`，子桌面上 peer 的 `process.start`）启动程序。两条路径都应用 `allow_processes`；peer 路径还要求 `child_allow_spawn`。`wait_s` 默认 3 秒。回复携带 `pid` 与最多六条 `+ WINDOW` 行，之后为 `+ N more`；新增窗口多于一个时追加 `~ N windows from one launch`。在当前桌面启动时分配 ref，若会话在切换后仍等待新的 `OBSERVE`，则追加 `desktop changed; observe again`。从 main 使用 `window="child"` 时，程序通过 peer 启动，回复只列标题，并追加 `~ on child; switch child to address them`。SUI-Text 中没有 `SESSION run`。

## 5. 架构

```
main session                              child session
────────────                              ─────────────
LLM
 │ tool calls
Somnus Runtime
 ├─ compiler / projection / registry / resolver / policy
 ├─ ref namespace
 └─ PeerProvider ──┐
                   │  named pipe  \\.\pipe\somnus-peer-<id>
                   └──────────────►  peer: Session + UiaProvider
                                        │
                                        └─►  agent 驱动的窗口
```

编译器、投影、注册表、解析器、策略与 ref 命名空间全部留在主侧。远侧运行一个瘦 peer，持有 `Session` 并响应工具调用，负责感知与执行，不含模型，也没有自己的策略。

`Session` 针对 `AccessibilityProvider` 编写，远侧会话因此是另一个 provider（`peerprovider.py`）。切换替换 provider，重置 ref 命名空间，不分配 ref；编译器与运行时不需要识别 provider 是远程的。

传输使用命名管道。它直接承载 provider 接口——`list_windows`、`snapshot`、`execute`——由 `wire.py` 逐字段序列化。

## 6. 登录

RDP 环回仍需一次登录。凭据按以下顺序尝试：

1. 凭据委托（由 `setup` 配置）。Windows 复用用户已登录的凭据，不输入密码，不存储密码。这是常规路径。
2. 存储密码（`somnus child-session credential --save`），用于策略拒绝委托的情况。密码由 DPAPI 加密，CurrentUser 作用域，附加额外熵，只能由本机当前账户解密。密码不写入 `config.toml`。

在一台标记为“password not required”（`UF_PASSWD_NOTREQD`）的本地账户上，即使允许列表正确，委托仍会被拒绝。仅清除该标记不足以解决问题；存储密码使该机器的登录成功。`setup` 不猜测机器需要哪条路径，它配置委托并报告结果；存储密码是显式回退。

Windows Hello PIN 不能用于该登录。PIN 不是密码，环回登录不接受 PIN。

## 7. 前置条件与代价

- Windows 专业版 / 企业版 / 教育版。家庭版没有子会话。
- 一次管理员权限的 `somnus child-session setup`，启用功能、启用 RDP 监听、配置凭据委托，并把 `TermService` 设为自动。
- 全系统同时只允许一个子会话。这是 Windows 的限制。
- 没有安全隔离。两个会话使用同一账户，agent 可以读写用户可读写的所有文件。子会话分离输入，不分离权限。
- 子会话只在 RDP 连接存在期间存在。关闭环回会结束会话及其中运行的程序。

## 8. 清理

孤立的环回会保持会话打开，后续 `start` 会附着到该会话，不创建新会话。该现象表现为“设置没有生效”。

- 本进程打开的会话在进程退出时清理（`atexit`），除非执行了 `SESSION detach`。
- `SESSION close` 立即清理环回、peer 与会话。
- 崩溃留下的会话由下一次 `close` 或 `status` 检测并清理。
- `SESSION detach` 放弃会话所有权，退出钩子不再处理它。

## 9. 评估过的其他方案

### 同时提供两个桌面

未采用。每个 ref 只在某个桌面命名空间内有效，同时暴露两个桌面会使每次调用产生歧义；要求模型逐次消歧的成本高于该功能收益。`SWITCH` 替换 provider，不合并两棵树。

### `Windows.UI.UIAutomation.Core`

这是微软文档中用于跨会话桥接 UIA 的受支持 API。实现在真实环回上运行的结果：

- `RemoteAutomationClientSession` 可以从 RDP 宿主构造并 `Start()`，`SessionId` 可读回；
- `ConnectionRequested` 不触发；
- `RemoteAutomationServer.ReportSession` 返回“class not registered”，在主会话与子会话内均如此，且连接已建立。

该 API 依赖微软远程桌面栈建立的私有 provider 上下文，第三方宿主无法复现。该结论记录在 `somnus/session/bridge.py` 中原本使用该接口的位置。

### 虚拟桌面

虚拟桌面与同会话的其他虚拟桌面共享输入桌面、前台窗口与输入队列，无法分离 agent 输入与用户输入。合成按键可能发送到错误的虚拟桌面；把目标移到当前虚拟桌面则会把 agent 的窗口带到用户桌面。虚拟桌面适用于“不显示在屏幕上”，不适用于“不触碰用户输入”。

### 将元素报告为不支持

运行时默认在无可用合成输入时返回 `ACTION_NOT_SUPPORTED`。该行为适用于无法验证输入效果的场景。子会话提供另一种取舍：合成通道在用户不使用的桌面中执行。

## 10. 诊断

```bash
somnus child-session status      # 功能是否可用、会话是否存活
somnus child-session probe       # 向 peer 查询可见内容
somnus child-session setup       # 重新应用系统配置
somnus child-session close       # 结束会话并清理残留
```

自检命令默认使用子会话作为沙箱：

```bash
somnus doctor                    # 环境与 observe/find/call 冒烟
somnus eval                      # 运行时状态判定（JSON）
somnus replay <trace>            # 回放记录的调用，报告首个分歧
```

这些命令对会话生命周期只读：`doctor` 报告会话是否存活，不主动打开；`eval` / `replay` 在没有存活会话时给出启动命令并拒绝运行，不回退主桌面。`--real` 显式选择用户桌面；`--demo` / `--scenario` 在固件上离线运行。

`status` 区分原始子会话 id 与存活 id。两者在会话半死时不同：登录被拒绝的尝试，或已被重置的会话。`WTSGetChildSessionId` 在这些情况下仍返回 id，但会话不在会话列表中；据此执行会得到“会话存在但没有 peer”，而实际没有会话。

## 11. 故障处理

先运行 `somnus child-session status`。

- 显示 `not available`：功能未启用，运行管理员权限的 `setup`。
- 窗口停在登录界面：凭据委托被拒绝。使用 `child-session credential --save` 存储密码，并使用 `--auto`。
- 提示指向一个已消失的会话：失败尝试留下的残留；`child-session close` 清理。
- 登录界面显示“其他用户”：没有交付任何凭据，属于委托问题。
