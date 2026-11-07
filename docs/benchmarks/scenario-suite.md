# 验收界面与测试方案

目标：用接近真实用户与 agent 使用的端到端场景，验证 GUI 到语义 API 全链路的正确性、鲁棒性与指标承诺。

分层：

- S1：纯 mock 回放，任意平台、CI 必跑。
- S2：真实窗口 UIA，使用 Windows 标记。
- S3（LLM-in-the-loop）属于早期规划；当前真模型回路由 `examples/eval_agent.py` 与 `bench/` 承担。

## 1. 验收应用

`apps/scenario-suite` 是一个 WPF 应用，按命令行参数启动一个界面：

| 界面 | 内容 | 覆盖点 |
| --- | --- | --- |
| `store` | 搜索框、240 行可滚动列表、购物车 | 视口折叠（`rows=` / `visible=`）、`FIND` 到达屏幕外的行、原生搜索写入 |
| `form` | 带标签字段、复选框、下拉、禁用按钮、受校验提交 | 可写与只读字段、校验、提交记录 |
| `chat` | 自绘编辑器、消息历史、发送按钮、发送记录 | 没有写入接口的控件上的 `set_text` |
| `console` | 文本框编辑器、联系人列表 | 路径选择：有自身写入接口的控件使用原生写入 |
| `tree` | 可展开的嵌套树 | 按展开等级压缩 |
| `native` | 真实 Win32 EDIT 控件（有 HWND）；固件按 UIA 形状发布 `value` 模式 | 原生控件形态；Value 路径写入；只读字段没有 `set_text` |
| `menu` | 菜单栏与上下文菜单 | 菜单项在打开后才出现；菜单导航与增量 |
| `dialog` | 模态确认对话框 | 对话框节点在打开后出现；破坏性动作的确认流程 |

每个界面存在两份：真实 WPF 窗口（`ScenarioSuite.exe <界面>`）与固件（`tests/fixtures/scenarios.py`）。`tests/scenarios/` 因此可在任意平台运行，`tests/e2e/test_windows_scenarios.py` 验证固件与真实控件一致。

```bash
dotnet build apps/scenario-suite
apps/scenario-suite/bin/Debug/net8.0-windows/ScenarioSuite.exe store

somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
```

## 2. 场景清单

### A. 核心操作链路

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| A1 | 餐厅点单：observe → find "Big Mac" → p1.add() → 验证购物车 count/total 增量 | delta 只含变化项；rev 递增；token 预算不涨 | S1/S2 |
| A2 | 记事本输入：set_text 后关闭并触发保存确认对话框 | 动态新增节点（TAB/对话框）出现在下一次 observe；confirm 流程走通 | S2 |
| A3 | 多步会话：连续 5 次以上操作，ref 稳定性与指纹失效重建 | ref 失效时先尝试同窗唯一匹配重定位一次（`~ healed old→new`）；失败才返回语义化错误，不泄漏 AutomationId | S1/S2 |
| A4 | 系统属性对话框：只读字段与 disabled 按钮 | 返回 `TARGET_DISABLED`；readonly INPUT 正确标注 | S2 |
| A5 | 菜单导航：activate 菜单栏项 → 子菜单出现 → activate menuitem | 菜单展开的增量渲染；菜单未打开时 find 不到子项 | S1/S2 |

### B. 通用模式

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| B1 | 用 `--no-pack` 驱动任意 Win32 应用 | 无 pack 也能到达并操作；有 Value 模式的控件走原生 `SetValue`，不产生合成按键 | S2 |
| B2 | 自绘编辑器（Chromium/Electron） | 关闭合成通道时返回 `ACTION_NOT_SUPPORTED` 并提示两个开关；开启后优先走剪贴板通道；剪贴板非纯文本时拒用 | S2 |
| B3 | 列表判定：标题加动词按钮行，或三个以上同级标题 | 实体行有语义能力，纯容器无能力 | S1 fixture + S2 |
| B4 | 默认动作只做高亮的应用 | 通过 context_menu 驱动；指针不移动、无合成按键；操作生效 | S2 |

### C. 失败与恢复

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| C1 | 错误语法：`click(312,528)` / 原语动词 | `PARSE_ERROR` 加 hint，单行无堆栈 | S1 |
| C2 | 不存在的能力或 ref | `CAPABILITY_NOT_FOUND` / `NOT_FOUND` 加 `hint=inspect` | S1 |
| C3 | critical 能力 | 直接执行，动作真实下发；当前构建没有确认门控 | S1 |
| C4 | UI 突变后调用 | 指纹校验失败 → 同窗唯一匹配重定位；失败则明确错误并建议 observe | S1 |
| C5 | 窗口关闭或崩溃后调用 | 语义化错误，session 可恢复或干净退出 | S2 |
| C6 | 超时或元素永不 settle | settle 超时返回一行错误，可重试 | S1（可注入延迟）/S2 |

### D. 前台提升与状态恢复

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| D1 | 目标窗口被遮挡时 observe 与 call | 目标窗口被提到前台；快照完整，操作成功 | S2 |
| D2 | 最小化或被遮挡窗口：CEF 应用与普通 Win32 | provider 先提升窗口再快照，树完整；`OBSERVE windows` 不提升任何窗口 | S2 |
| D3 | 其他虚拟桌面窗口 | 自动移到当前桌面；close 后恢复原状 | S2 |
| D4 | 状态恢复：结束时窗口回到原最小化状态或位置 | 自动恢复，无桌面残留 | S2 |

### E. 信息隔离与指标

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| E1 | 泄露扫描：全流程采集模型可见文本 | 0 个 AutomationId、句柄、坐标、进程 ID、control_type | S1/S2 |
| E2 | token 预算：从少于 10 个控件到 500 个以上控件的窗口 | L0 在预算内；同名类截断与匿名折叠规则生效 | S1 |
| E3 | 压缩比：RawTree 节点数与 SUI-Text token | ≥ 10× 门槛 | S1 |
| E4 | 零视觉：hook 全部视觉、坐标与合成输入 API | 0 次调用 | S2 |

### F. 协议与会话

| # | 场景 | 验证点 | 层 |
| --- | --- | --- | --- |
| F1 | MCP stdio：畸形输入、并发请求、大参数 | 协议不崩，错误响应合法 | S1 |
| F2 | 窗口切换：多窗口间 switch → observe → 操作 | ref 不串窗口；切换后 rev 一致 | S1/S2 |
| F3 | REPL 长会话与 record 回放 | 录制的固件经 MockProvider 重放，结果一致；golden 比对通过 | S1 |

## 3. 测试落点

```
tests/fixtures/scenarios.py             验收应用八个界面的固件与脚本化行为
tests/scenarios/test_store.py           A1  搜索、240 行列表、购物车
tests/scenarios/test_form.py            A4  表单：只读/可写、校验、提交记录
tests/scenarios/test_chat.py            B2  自绘编辑器：set_text 回退链与发送记录
tests/scenarios/test_console.py         B1  普通文本框：原生写入优先
tests/scenarios/test_tree.py            A5/E2  嵌套树：按展开等级压缩
tests/scenarios/test_native.py          原生 EDIT 控件：Value 写入与只读字段
tests/scenarios/test_menu.py            菜单项在打开后出现；菜单导航
tests/scenarios/test_dialog.py          模态对话框：节点出现与确认流程
tests/scenarios/test_contracts.py       E1/C1/C2/F2  泄露、能力、窗口切换
tests/scenarios/test_bench.py           E3  基准门槛
tests/scenarios/test_sui_templates.py   模板匹配
tests/e2e/test_windows_scenarios.py     S2：真实窗口版本
```

约定：

- 验收应用是一个应用、八个界面，取代对记事本与三行餐厅界面的依赖。记事本只提供“带 HWND 的文本控件”，真实 GUI 的难点是搜索、表单、自绘输入框、大列表与深树等形态。
- 固件复刻真实无障碍形状，不依赖 C# 实现，因此 S1 可在任意平台运行；S2 验证固件与真实控件一致，例如 WPF 把 `RichTextBox` 报告为没有 Value 模式的 `document`。
- mock 层断言模型可见文本，直接对 SUI-Text 输出做泄露扫描。扫描枚举树中真实存在的 id，再检查它是否出现在文本里；不使用四位数字正则，以免把应用数据 `REQ-0001` 误判为泄露。
- `MockProvider` 支持 `callback` 变更操作，使固件可以回放逐行行为。

## 4. 验收指标

`python -m somnus.cli bench --runs 100` 在 mock 固件上运行以下门槛：

| 指标 | 目标 | 记录值（100 次） |
| --- | --- | --- |
| 零视觉 | 0 次视觉或坐标调用 | 0（结构性） |
| 信息隔离 | 模型上下文中 0 个原生 ID | 0（泄露扫描） |
| 压缩比 | ≥10× | 1373.6×（最小值，完全展开的树） |
| 动作成功率 | ≥99% | 100% |
| 任务成功率 | ≥90% | 100%（中位 3 步） |
| L0 观察 | ≤300 token | 91 token（中位数与最大值） |

小型且未截断的视口窗口可将 L0 预算放宽到 80 节点、深度 10、700 token，以显示深层控件。

隔离检查枚举被测树中实际存在的 automation id、类名与 runtime id，断言它们不出现在模型收到的文本中。看起来像 ID 的应用数据（例如表单的 `REQ-0001`）不计为泄露。

这些数值来自早前基准运行，本轮未重新测量。

## 5. S3 历史任务清单

以下清单属于早期规划。当前真模型回路由 `examples/eval_agent.py` 与 `bench/tasks/`、`bench/loop/` 承担；本文件只保留 S1/S2 的验收口径。

1. 把两个 Big Mac 加进购物车并结账。
2. 在记事本写 "meeting notes" 并另存为 xy.txt（涉及对话框导航与文件名输入）。
3. 在未知应用（no-pack）中改窗口标题栏文字或执行一个可见动作。
4. 干扰任务：中途移动或遮挡窗口、弹出无关对话框。
5. 恶意或误导 UI：按钮文案与功能不符。

指标口径沿用 bench：动作成功率 ≥99%、任务成功率 ≥90%、零视觉、零泄露、压缩 ≥10×。

## 6. S2 夹具约定

- 记事本清理使用 `WM_CLOSE`，再通过 UIA 选择“不保存”/“Don't save”/“否”。5 秒后窗口仍未关闭时回退 `taskkill`，并打印 `fell back to taskkill` 日志。强制结束会保留未保存缓冲区，下一次启动会恢复为额外窗口。
- 每轮测量前调用 `clean_notepad_state()` 建立干净基线。启动后窗口行数与预期不符，或响应包含 `windows from one launch` 时，测试以 `DIRTY_BASELINE` 失败，不继续测量。

## 7. 通过标准

- S1 全绿为合并门槛；S2 全绿为发布门槛（Windows runner）。
- 任何场景输出中出现堆栈、原生 ID 或坐标即失败。
- 新增场景先在 mock 层以固件形式复现，再补 UIA 版本。
