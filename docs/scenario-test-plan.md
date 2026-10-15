# 真实场景测试方案（Scenario Test Plan)

目标：用贴近真实用户/Agent 使用的端到端场景，验证 SomnusUI
「GUI → 语义 API」全链路的正确性、鲁棒性与指标承诺。
分层：**S1 纯 mock 回放（任意平台、CI 必跑）→ S2 真实窗口 UIA（Windows 标记）→ S3 LLM-in-the-loop（夜间/手动）**。

---

## 一、场景清单

### A. 核心操作链路（Happy Path）

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| A1 | 餐厅点单：observe → find "Big Mac" → p1.add() → 验证购物车 count/total 增量 | delta 只含变化项；rev 递增；token 预算不涨 | S1/S2 |
| A2 | 记事本输入：set_text("Hello SomnusUI") → 关闭时触发保存确认对话框 | 动态新增节点（TAB/对话框）出现在下一次 observe；confirm 流程走通 | S2 |
| A3 | 多步会话状态：连续 5+ 次操作，ref 稳定性与指纹失效重建 | ref 失效时返回语义化错误 + 重建后 id 迁移提示，绝不泄漏 AutomationId | S1/S2 |
| A4 | 系统属性对话框（只读字段 + disabled 按钮）：observe → call b5.activate() | 返回 `TARGET_DISABLED`；readonly INPUT 正确标注 | S2 |
| A5 | 菜单导航：activate mi "编辑" → 子菜单出现 → activate menuitem | 菜单展开的增量渲染；菜单未打开时 find 不到子项 | S1/S2 |

### B. 通用模式（--no-pack，未见过的应用）

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| B1 | 用 `--no-pack` 驱动任意 Win32 应用（如 calc/notepad）：observe 可见、控件有 activate/set_text | 无 pack 也能到达并操作；有 Value 模式的控件走原生 `SetValue` 写入（不产生合成按键） | S2 |
| B2 | 自绘编辑器（Chromium/Electron，如 Steam/QQ/VS Code）：set_text 落到键盘路径 | 输入生效；context_menu 打开的菜单项可 activate | S2 |
| B3 | 列表识别：打开资源管理器/聊天列表，验证"标题+动词按钮行"与"≥3 同级标题"两种实体识别规则 | 实体行有语义能力，纯容器无能力 | S1 fixture + S2 |
| B4 | 默认动作只是高亮的应用（聊天列表）：通过 context_menu 驱动 | 指针不移动、无合成按键（可 hook 断言）、操作生效 | S2 |

### C. 失败与恢复（Fail Loud）

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| C1 | 错误语法：`click(312,528)` / 原语动词 | `PARSE_ERROR` + hint，单行无堆栈 | S1 |
| C2 | 不存在的能力/引用：`CALL p1.submit()`、`CALL ghost.add()` | `CAPABILITY_NOT_FOUND` / `NOT_FOUND` + hint=inspect | S1 |
| C3 | 被确认门禁的操作：`b5.submit()` | `CONFIRMATION_REQUIRED` + hint=ask-user；状态未变 | S1 |
| C4 | UI 突变后调用：find 到 ref 后 UI 重建（fixture 注入变更）再 call | 指纹校验失败 → 明确错误 + 建议 observe，而非静默错点 | S1 |
| C5 | 窗口关闭/崩溃后 call | 语义化错误（不是 traceback），session 可恢复或干净退出 | S2 |
| C6 | 超时/元素永不 settle | settle 超时返回一行错误，可重试 | S1（可注入延迟）/S2 |

### D. 后台工作区与无干扰

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| D1 | 目标窗口被遮挡（非前台）时 observe + call | 快照完整、操作成功，前台窗口焦点不变 | S2 |
| D2 | 最小化窗口：CEF 应用 vs 普通 Win32 | provider 自动 SW_SHOWNOACTIVATE 展开；CEF 树萎缩时的降级行为/错误 | S2 |
| D3 | 其他虚拟桌面窗口 | 自动移到当前桌面；close() 后恢复原状 | S2 |
| D4 | 状态恢复：结束时窗口回到原最小化/位置 | 自动恢复，无桌面残留痕迹 | S2 |

### E. 信息隔离与指标承诺

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| E1 | 泄露扫描：全流程采集 LLM 可见文本 | 0 个 AutomationId/句柄/坐标/进程 ID/control_type（正则扫描） | S1/S2 |
| E2 | token 预算：不同复杂度窗口（<10 控件 → 500+ 控件）observe | L0 仍在预算内；同名类截断/匿名折叠规则生效 | S1 |
| E3 | 压缩比：RawTree 节点数 vs SUI-Text token | ≥ 10× 门槛 | S1 |
| E4 | 零视觉：hook 全部视觉/坐标/合成输入 API | 0 次调用 | S2 |

### F. 协议与会话

| # | 场景 | 验证点 | 层 |
|---|------|--------|----|
| F1 | MCP stdio：JSON-RPC 恶意/畸形输入、并发请求、大参数 | 协议不崩，错误响应合法 | S1 |
| F2 | 窗口切换：多窗口间 switch → observe → 操作 | ref 不串窗口；切换后 rev 一致性 | S1/S2 |
| F3 | REPL 长会话 + record 回放：录制 fixture → MockProvider 重放结果一致 | golden 比对通过 | S1 |

---

## 二、落点建议（映射到现有测试结构）

```
tests/e2e/test_scenarios_mock.py      # A1 A3 A5 B3 C1-C4 E1-E3 F3 —— 纯 mock，每 commit 跑
tests/e2e/test_scenarios_windows.py   # A2 A4 B1 B2 B4 D1-D4 C5 E4 —— @pytest.mark.windows
examples/eval_scenarios.py            # S3：LLM agent 跑任务清单（复用 eval_agent.py）
```

- 复用 `tests/fixtures/builders.py` 构造 UI 突变、延迟、萎缩树等注入；
- mock 层断言"LLM 可见文本"而非内部对象，直接对 SUI-Text 输出做正则泄露扫描；
- S2 场景尽量复用 `apps/restaurant-demo` + notepad，减少对环境的具体依赖。

## 三、S3 LLM-in-the-loop 任务清单（bench/eval 扩展）

1. "把两个 Big Mac 加进购物车并结账"（含 confirm 交互）
2. "在记事本写 'meeting notes' 并另存为 xy.txt"（涉及对话框导航 + 文件名输入）
3. "在未知应用（no-pack）里把窗口标题栏文字改掉/执行一个可见动作"——考察泛化
4. 干扰任务：中途移动/遮挡窗口、弹出无关对话框——考察恢复
5. 恶意/误导 UI：按钮文案与功能不符——考察语义能力 vs 文本的取舍

指标口径沿用 bench：动作成功率 ≥99%、任务成功率 ≥90%、零视觉、零泄露、压缩 ≥10×。

## 四、通过标准

- S1 全绿为合并门槛；S2 全绿为发布门槛（Windows runner）。
- 任何场景输出中出现堆栈、原生 ID、坐标即失败（fail loud 契约）。
- 新增场景必须能先以 fixture 形式在 mock 层复现，再补 UIA 版本。
