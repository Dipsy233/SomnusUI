# Playwright 可借鉴方案评估（SomnusUI）

> 评估对象：`E:\dao\gui\playwright-main`（Playwright 主仓库）
> 落地对象：SomnusUI（`E:\0down\SomnusUI`，分支 `play`）
> 版本说明：2026-11-06 按新项目理念（低消耗、不识图、按需注入、自体验）重估此前基于 token 硬上限的结论；2026-11-07 从 `scratch/` 迁入 `docs/archive/`，作为历史评估留档。
> 存放约定：本文归档在 `docs/archive/`，属于历史评估而非当前规范。Trace 的开发期 raw 数据仍不纳入 Git；收尾期脱敏后才入库。
> 本文只做方案评估与落点设计，不改动业务代码。

## 0. 结论先行

Playwright 是 **浏览器自动化 + 测试运行器**，SomnusUI 是 **Windows UIA 的 GUI→文本运行时**。两者在感知层、等待模型、身份模型上并不等价，不能整段照搬。真正值得迁移的是**工程方法与数据契约**，而不是具体 API。

### 0.1 新项目理念对评估的影响

项目理念是：

- **低消耗、不识图**；
- 把 GUI 转化成 LLM 可用的 **API 函数或类 shell 工具**；
- 让 LLM 在不识别图片的情况下，**快、准、狠**地理解屏幕现状并操控 GUI；
- `system_prompt.md` + 工具面 **不设硬上限**，但**尽量简短**；
- 可以**分批按需注入**，而不是一次性注入；
- agent 能够**自己实际体验**。

据此，本次重估有三条直接变化：

1. **取消「31 token 硬余量」作为方案否决理由。** 之前因为 `system_prompt.md + 工具面` 只有 31 token 余量而押后的定位器语法、结果落盘参数等，现在可以进入方案池。
2. **评估口径从「总量上限」改为「稳态每轮成本 + 发现成本」。** 不设硬上限不等于不计成本：核心提示词、常驻工具 schema、每次 observation 仍然是每轮都要付的钱；长解释应移到按需加载的文档。
3. **「按需注入」和「自体验」本身成为一等方案。** 工具面分层、能力开关、`help` 主题文档、`doctor/eval/replay` 自检回路都应纳入路线，而不是只作为运维附录。

本次已定：

- 自体验默认使用 **child session**；用户主桌面不是默认体验环境。
- 发现机制采用 **`help` + MCP/skill 过滤档位**：`help` 负责运行时发现，MCP `tools/list` 与 skill 安装负责档位过滤。
- 本文档已迁入 `docs/archive/`，作为历史评估留档。
- Trace 开发期先单独存放在 `scratch/traces/`，Git 不跟踪、不脱敏；项目结束统一脱敏后，才作为参考指标加入项目。

### 0.2 建议采纳清单

| 优先级 | 方案 | 迁移形态 | 一句话理由 |
| --- | --- | --- | --- |
| P0 | 决策轨迹记录（Trace） | 运行时旁路记录 | 后续调优与自体验需要可复现证据；开发期本地留存，收尾脱敏入库 |
| P0 | 导入边界测试（DEPS 思路） | 仅测试 + 文档 | 用测试锁住「功能解耦」，成本最低 |
| P0 | 快照模板与部分匹配 | 仅测试 | 给投影/编译重构提供安全网，不进模型面 |
| P1 | 渐进式工具面与按需注入 | 协议 + 文档 + CLI/MCP | 已定：`help` + MCP/skill 过滤档位 |
| P1 | 动作前检查 + 失败后等待 | 运行时内部 | 仿 Playwright actionability，但只在失败路径付代价 |
| P1 | 名称合成与状态规范化 | 编译期 | 减少无名节点，改善 FIND 与实体识别 |
| P2 | 自体验 / 自检回路 | CLI + 测试 + 示例 | 已定：默认 child session，让 agent 自己跑、自己看、自己改 |
| P2 | 可重解析定位器 | 先内部，后可选工具 | 降低 stale ref 的重规划回合；新理念允许新增语法 |
| P2 | 投影蒸馏插件链 | 投影期 | 把已有 `simplify.py` 的零散规则变成可组合管线 |
| P1 / P3 | 工具元数据与会话运维 | 协议与运维 | 元数据是分层工具面的前置（P1）；空闲回收与大结果落盘按需（P3） |

明确**不建议迁移**的：YAML aria snapshot 作为模型格式、快照级 ref、ElementHandle 语义、截图/视觉/坐标、`force` 绕过检查、任意代码执行、完整 Trace Viewer UI、网络/时钟/设备模拟、浏览器补丁、把所有工具一次性塞进上下文。

---

## 1. 证据标记

本文的结论按以下标记区分证据强度：

| 标记 | 含义 |
| --- | --- |
| 〔文〕 | 读 Playwright 官方文档确认 |
| 〔码〕 | 读 Playwright / SomnusUI 源码确认 |
| 〔实〕 | 引用 SomnusUI 仓库内已记录的实测数字 |
| 〔推〕 | 基于代码结构的推断，**尚未实测** |

---

## 2. 两个项目的架构对照

| 维度 | playwright-main | SomnusUI | 可迁移性判断 |
| --- | --- | --- | --- |
| 感知面 | DOM / ARIA 无障碍树 | Windows UI Automation 树 | 树形结构相似，**节点语义不同** |
| 模型可见格式 | YAML aria snapshot（role/name/attrs/children）〔文〕 | SUI-Text（类型/ref/名称/属性/能力，一行一节点）〔码〕 | SUI-Text 更紧凑；只借「模板匹配」不借格式 |
| 节点身份 | 快照内的 `e5` ref，以最后一次快照为准，过期需重取快照〔码〕 | 指纹稳定的会话 ref，跨 rev 保持〔码〕 | SomnusUI 更适合 LLM，**不要反向迁移** |
| 定位方式 | Locator 配方（role/name/filter/nth），每次动作重解析〔文〕 | `ref` + `FIND [TYPE] "name"` + registry 重定位〔码〕 | 可借「配方」思想，不借 API 形态 |
| 动作前检查 | visible/stable/receives events/enabled/editable/not-inert〔文〕 | 能力由 pattern 推导；`states` 里有 disabled/offscreen/readonly〔码〕 | 可借检查清单；`receives events` 无 UIA 等价物 |
| 动作后等待 | 等待动作完成 + 重取快照〔文〕 | settle（签名探针 + 静默窗口）+ 重编译 + delta + verifier〔码〕 | 已具备，且更细；只借「失败后等待」 |
| 失败恢复 | 超时错误、strict mode、调用日志〔文〕 | 有界纠正：陈旧 ref 重定位 → 孪生重试 → 路由重试，每种一次〔码〕 | 已具备；可借「结构化错误 + trace」 |
| 调试 | Trace（JSONL + before/after 快照 + viewer）〔文〕 | `SOMNUS_PROFILE` 分段计时 + `audit_log()` 原语级记录〔码〕 | **最值得补的一块** |
| 测试 | Playwright Test：fixtures/retries/annotations/projects/snapshot〔文〕 | pytest + scenario fixtures + golden〔码〕 | 借 fixtures/快照模板/重试，不借整套 runner |
| 协议面 | MCP 工具 + CLI + 可安装 skills〔文〕 | 8 个工具 + CLI + MCP，全部工具一次性列出〔码〕 | **工具面分层/按需注入最值得借** |
| 依赖约束 | `DEPS.list` 强制方向，52+ 处〔文〕 | README 写明方向，**无自动检查**〔码〕 | 直接可借，成本极低 |

### 2.1 关键差异：成本模型不同

Playwright 的很多「便宜操作」在 UIA 上并不便宜：

- Playwright 的 `stable` 是在页面内用 `requestAnimationFrame` 比较 bounding box，几乎零成本〔码 `injectedScript.ts:712-760`〕。
- SomnusUI 的等价信号是 **整窗签名探针**。大树一次探针实测 **384–404ms**，全量快照更贵〔实 `docs/archive/performance-2026-11.md:73-74`〕。
- 因此：**语义可以借，实现不能照抄**。任何「每步都探测」的设计都必须先证明成本可接受。

### 2.2 新理念下的预算口径

旧口径是「`system_prompt.md + 工具面` 不超过 2700 token」。新理念把这个硬上限移除，改为两条可测量的线：

| 指标 | 含义 | 观察方式 |
| --- | --- | --- |
| 稳态每轮成本 | 核心提示词 + 常驻工具 schema + observation | 每次工具调用的注入 token 数 |
| 发现成本 | 从「不知道有这个能力」到「会正确使用」要读多少内容 | 首次使用前加载的主题文档 token 数 |

设计规则：

- **核心提示词尽量短**：只保留工具名、一句话用途、SUI-Text 最小语法、必须遵守的规则、错误码表。详细语法、批处理、手势、桌面切换等移到主题文档。
- **常驻工具只保留核心集**：`observe/find/inspect/call/run` 常驻；`switch/launch/configure`、定位器、录制、trace、help 等按能力档位或按需出现。
- **长解释按需加载**：`help <topic>` 或可安装 skill 只在需要时注入，不进入一次性的 `instructions`。
- **不设硬上限，但要设回归观察值**：例如「核心提示词控制在 1k token 量级」可以作为设计目标与回归观察值，而不是否决线〔推〕。
- **新增工具必须过两关**：① 稳态成本可接受；② 工具选择准确率不下降（加工具不等于 agent 更会用）。

当前基线：`system_prompt.md` 约 1780 token，工具面约 889 token，合计 2669〔实 `docs/archive/performance-2026-11.md:116-123`〕；MCP `instructions` 另计约 798 token〔实 同上〕。这些数字现在是**基线**，不是天花板。

---

## 3. 值得借鉴的方案

### 方案 A：决策轨迹记录（Trace）

**判定：采纳（P0）。** 这是后续所有调优与自体验的地基。

#### A.1 Playwright 的原做法

Playwright 的 trace 不是「日志」，而是一套**以 callId 为中心的可回放数据模型**〔文 `.claude/skills/playwright-dev/trace_system_guide.md`〕：

- `Tracing` 类实现 `InstrumentationListener`：
  - `onBeforeCall` 记录 `title/class/method/params/stack/beforeSnapshot`；
  - `onBeforeInputAction` 记录坐标与 `inputSnapshot`；
  - `onAfterCall` 记录 `endTime/afterSnapshot/error/attachments/result/point`；
  - `onCallLog` 记录动作日志。
- 快照命名 `before@<callId>` / `after@<callId>`，通过 `callId` 把「动作前 / 动作后 / 日志 / 附件」串起来。
- 存储为 JSONL（`.trace` / `.network`），资源按 SHA1 去重存 `resources/`。
- 双时间基：`wallTime` 用于展示，`monotonicTime` 用于关联。
- 支持分组与分块，格式带版本号，`TraceModernizer` 可把旧版本升级到新版本。
- Viewer 提供 Actions / Snapshots / Network / Console / Timeline 等页签〔文 `docs/src/trace-viewer.md`〕。

核心可借的不是 viewer，而是 **callId 关联 + before/after 快照引用 + JSONL + 内容寻址 + 版本化**。

#### A.2 迁移到 SomnusUI 的落点

SomnusUI 已有两处可复用的观测基础：

- `SOMNUS_PROFILE` 的分段计时 `self.timing`〔码 `session.py:269-272`，`_timed` 在 `session.py:70-75`〕；
- `audit_log()` 的原语级记录：`ts/rev/ref/capability/primitive/ok/unsupported`〔码 `session.py:3832-3846`，`3884-3885`〕。

但缺少**调用级信封**：一次 `call` 的路由链、settle 帧数、delta、verify 结果、错误码没有统一落盘。

建议新增：

```
somnus/observability/
├── __init__.py
├── trace.py        # TraceSink 协议 + 内存/空实现
├── jsonl.py        # JSONL 写入、快照内容寻址、manifest
└── view.py         # 文本时间线渲染（somnus trace show）
```

`TraceSink` 只定义事件，不依赖 `Session` 具体类型：

```python
class TraceSink(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...
    def close(self) -> None: ...
```

建议的事件（全部为内部事件，不对模型暴露）：

| 事件 | 关键字段 | 触发点 |
| --- | --- | --- |
| `call.start` | `call_id/tool/ref/cap/rev/window/target_states` | `Session.call` 入口 |
| `route.chain` | `steps/route_order/written/healed` | `_execute_with_twin` 之后 |
| `settle` | `settled/timed_out/snapshots/probes/waited_ms` | `_read_after_action` 内 |
| `compile` | `nodes/rev/duration_ms/truncated` | `_compile_tree` 之后 |
| `delta` | `changed/added/removed/folded` | `diff_trees` 之后 |
| `verify` | `status/checked/failed` | `verifier.verify` 之后 |
| `call.end` | `status/duration_ms/error_code/hint` | `call` 返回前 |
| `observe` | `rev/window/tokens/truncated` | `observe` 返回前 |
| `window.change` | `before/after` | `_refresh_after_action` |

写入策略：

- **默认关闭**，`somnus --trace` 打开；默认写到 `scratch/traces/<session-id>/`，可用 `--trace-dir` 覆盖；
- **开发期不脱敏**：保留窗口标题、表单值、文本内容，用于复现与调优；因此 raw trace 只允许留在本地，绝不提交；
- 快照存 **SUI-Text 文本**，不是 RawTree。动作前存模型实际看到的 observation；动作后存 settle 后重编译树的 SUI-Text（仅在 trace 打开时渲染一次）或退化为「delta + 指纹集合」；
- 文本快照按 `sha256` 内容寻址，重复快照只存一份；
- `manifest.json` 记录版本、时间基、Python/OS 版本、当时的 settings（`settle_*`、`allow_*`、`post_action_enum`）；
- `--trace-full` 才额外存 RawTree，用于深挖 COM 侧问题；同样只留本地。

`call_id` 建议用 `f"{session_uuid}:{seq}"`，`run` 的每个步骤用 `parent_id=call_id` + `step_no`，与 Playwright 的 `parentId/group` 同构。

##### 存放与生命周期（已定）

| 阶段 | 位置 | Git | 脱敏 | 用途 |
| --- | --- | --- | --- | --- |
| 采集（开发/调优） | `scratch/traces/<session-id>/` | 不跟踪 | 否 | 复现、调试、对比 |
| 冻结（候选指标） | `scratch/traces/frozen/` | 不跟踪 | 否 | 选定要入库的样本 |
| 收尾（项目结束） | `docs/reference/traces/` | 跟踪 | 是 | 参考指标 |
| 归档 | 本地删除或移出仓库 | 不跟踪 | 否 | 释放空间 |

约定：

- `scratch/traces/` 是本地目录；建议用 `.git/info/exclude`（本地生效、不进入仓库）排除，**不改 `.gitignore`**；即使不排除，也绝不 `git add`。
- `somnus trace` 在 raw 模式下拒绝写入 `docs/`、`bench/`、`tests/` 等会被跟踪的目录；只有收尾期的 `trace redact` 产物允许进入 `docs/reference/traces/`。
- 收尾前 raw trace 不复制、不引用、不提交；收尾后只保留脱敏产物与指标摘要。

#### A.3 代价与风险

- **落点数量**：`Session` 内约 6–8 处钩子；新增 3 个文件；`cli.py` 增加 `trace` 子命令。`Session` 已有 213KB，钩子必须收敛到一个 `_trace_event()` 辅助函数，避免散落。
- **热路径开销**：默认关闭时应严格零成本；打开时用缓冲写，`close()` 时 flush。
- **隐私（开发期不脱敏）**：SUI-Text 含窗口标题、表单值、聊天内容；raw trace 可能含用户数据。缓解：默认写 `scratch/traces/`，Git 不跟踪，绝不提交；收尾期统一脱敏后再入库。
- **raw trace 误提交**：`git add -A` 或手动添加可能把本地 trace 带进历史。缓解：`.git/info/exclude` 本地排除；raw 模式拒绝写受跟踪目录；收尾前审查 `git status`。
- **脱敏不彻底**：收尾期字段遗漏。缓解：按字段白名单脱敏（见 A.5），自动扫描 + 人工复核 + 脱敏黄金测试。
- **格式漂移**：v1 只做加法，不引入 `modernizer`；等格式稳定后再考虑版本升级器。
- **钩子漂移**：用契约测试保证每个工具调用至少产生 `start/end`。

#### A.4 验收方式

- mock provider 跑一次 `call`，trace 里有 `call.start → route.chain → settle → delta → verify → call.end`，且 before/after 快照可读；
- raw trace 写在 `scratch/traces/`，不出现在 `git status`（使用 `.git/info/exclude` 时）；
- `somnus trace show <file>` 输出可读时间线；
- 关闭 trace 时，`bench` 的墙钟无可见变化；
- 阶段 1 不实现脱敏；阶段 7 的 `trace redact` 有黄金测试，脱敏产物不含标题/路径/值。

#### A.5 收尾：脱敏与参考指标（项目结束）

1. **冻结样本**：从 `scratch/traces/` 选 3–5 条代表性流程（observe/find/call、batch/run、错误/恢复、大列表、child session），复制到 `scratch/traces/frozen/`，记录校验和。
2. **离线脱敏**：新增 `somnus trace redact <in> --out <out>`：
   - 删除/替换：窗口标题、进程名/PID/hwnd、绝对路径、表单值、文本内容、剪贴板内容、`--trace-full` 的 RawTree、可能含路径的错误消息；
   - 保留：工具名、能力名、route 链、settle/probe 次数、耗时、节点数与角色分布、delta 计数、错误码、token 数、成功/失败、步数；
   - 文本用占位符保留长度，如 `<text len=42>`、`<title len=12>`。
3. **生成指标**：`metrics.json` 汇总成功率、步数中位数、p50/p95 延迟、settle 帧数、探针数、每次 observation token 数、错误码分布、healing 成功率。
4. **入库**：收尾阶段才把 `docs/reference/traces/*.jsonl`（脱敏后）、`metrics.json`、`README.md`（说明环境、版本、如何复现）纳入版本控制；本轮不执行提交。
5. **清理**：raw trace 留在本地或删除，不进入 Git 历史。

---

### 方案 B：快照模板与部分匹配（测试侧）

**判定：采纳（P0，仅测试）。**

#### B.1 Playwright 的原做法

`toMatchAriaSnapshot` 把 aria snapshot 当模板做**部分匹配**〔文 `docs/src/aria-snapshots.md:222-347`〕：

- 省略属性 = 通配；
- 省略子节点 = `contain`（默认），另有 `equal` / `deep-equal`；
- 名称支持 `/regex/`；
- 单个顶层节点可在任意深度命中；
- 支持 `--update-snapshots` 的 `missing/changed/all/none` 模式与 `patch/3way/overwrite` 更新方式〔文 `:349-429`〕；
- 匹配实现是纯函数：`matchesNode` / `containsList` / `listEqual` / `matchesNodeDeep`〔码 `ariaSnapshot.ts:357-466`〕。

#### B.2 迁移落点

SomnusUI 已有 `somnus/sui/parse.py` 的 SUI-Text 解析与 `tests/golden/test_golden.py` 的整文件比对〔码〕。建议新增：

```
tests/support/sui_template.py   # 模板解析 + 匹配
tests/templates/*.sui           # 场景模板
```

最小语法沿用 SUI-Text，不发明第二套语言：

```text
@rev ?                       # rev 可省略或通配
WINDOWS w? "..." active      # w? 表示任意 ref
WINDOW w? "..." 
  LIST l? "商品" count=240   # 只校验写出的字段
    PRODUCT p? "Special Item 17" +add
    ...
```

匹配规则：

- 节点类型必须相等；`?`/省略 ref 表示任意 ref；
- 名称：精确匹配，或 `/.../` 正则；
- 写出的属性/状态必须匹配，未写出的不校验；
- 写出的能力必须存在，未写出的不校验；
- 子节点默认 `contain`；模板里出现 `...` 表示「允许任意剩余子节点」；
- 单个顶层节点可在任意深度命中；
- 可选 `strict=True` 时退化为当前整文件 golden。

配套：

- `assert_sui(actual, template)`；
- `UPDATE_SUI_TEMPLATES=1` 重新生成模板；
- 模板文件按场景命名（`store_l0.sui`、`form_l1.sui`、`chat_l1.sui`）。

#### B.3 代价与风险

- 只动 `tests/`，不进生产、不进 prompt，成本最低。
- **模板过松会掩盖回归**。缓解：保留 `restaurant_l0.txt` 的严格 golden；每个场景至少一条严格断言；模板变更需在 PR 里单独审。
- **解析器重复**：必须复用 `somnus/sui/parse.py` 的词法/转义规则，不能另写一套。
- **测试价值取决于场景覆盖**：现有 `tests/scenarios/` 已是「驱动屏幕 → 检查答案」结构〔码 `tests/scenarios/conftest.py:1-80`〕，模板化后能显著减少逐行 `assert "..." in text`。

---

### 方案 C：导入边界测试（DEPS.list 思路）

**判定：采纳（P0）。** 直接服务「功能解耦」。

#### C.1 Playwright 的原做法

Playwright 用 52+ 个 `DEPS.list` 声明「哪个目录可以 import 哪个目录」，由 `npm run flint` 强制检查〔文 `AGENTS.md:106-111`〕。核心规则：

- client 代码永不 import server 代码；
- server 代码永不 import client 代码；
- 双向通信只能走 protocol。

这是**架构约束自动化**，不是文档约定。

#### C.2 迁移落点

SomnusUI 的 README 已写明依赖方向：`providers → compiler → projection → serialization → protocol`，`runtime` 依赖全部〔文 `README.md:570-571`〕；`ir/node.py` 也写明「不 import providers / projection / LLM」〔码 `ir/node.py:1-12`〕。但没有自动检查。

建议新增 `tests/architecture/test_import_boundaries.py`，用 `ast` 解析 `somnus/**/*.py` 的 `Import` / `ImportFrom`，按矩阵断言：

| 模块 | 允许 import | 禁止 import |
| --- | --- | --- |
| `somnus/ir/*` | 标准库、`somnus.ir.*` | `providers`、`compiler`、`projection`、`runtime`、`protocol` |
| `somnus/compiler/*` | `ir`、`compiler.*`、标准库 | `providers`、`projection`、`runtime`、`protocol`、`cli` |
| `somnus/projection/*` | `ir`、`compiler.pack_schema`、`projection.*` | `providers`、`runtime`、`protocol`、`cli` |
| `somnus/sui/*` | `ir`、`sui.*`、标准库 | `providers`、`compiler`、`runtime`、`protocol` |
| `somnus/runtime/*` | 上述核心层、`providers.base`（协议） | `providers.windows`、`protocol`、`cli` |
| `somnus/protocol/*` | `runtime`、`config`、`errors` | `providers.windows` |
| `somnus/providers/*` | `ir`、`providers.*`、标准库 | `compiler`、`projection`、`runtime`、`protocol` |
| `somnus/cli.py` | 全部 | — |

注意：

- 延迟 import（如 `get_provider` 在函数内 import `UiaProvider`〔码 `providers/base.py:89-108`〕）也会出现在 AST 里，应一并检查；
- `TYPE_CHECKING` 下的 import 单独放行；
- 首次落地时用「当前代码的实际依赖」生成 allowlist，**不要先写理想矩阵再改代码**——本轮不改业务代码。

#### C.3 代价与风险

- 成本：一个测试文件 + 一份矩阵文档；无运行时开销。
- 风险：allowlist 维护成本；动态 `importlib` 字符串无法静态发现（可在文档里标注为已知缺口）。
- 收益：任何跨层 import 会在 CI 立即暴露，比 code review 可靠。

---

### 方案 D：动作前检查 + 失败后等待（Actionability）

**判定：改造采纳（P1）。** 借语义，不借实现。

#### D.1 Playwright 的原做法

Playwright 在执行动作前做一组检查，任一不满足就自动重试直到超时〔文 `docs/src/actionability.md:11-42`〕：

| 检查 | 含义 | 实现 |
| --- | --- | --- |
| Visible | 非空 bounding box、非 `visibility:hidden` | 页面内计算 |
| Stable | 连续两帧 bounding box 不变 | `requestAnimationFrame` 比较〔码 `injectedScript.ts:712-760`〕 |
| Receives Events | 命中测试点在目标上，未被遮挡 | hit-target 拦截器〔码 `injectedScript.ts:1148`〕 |
| Enabled | 非 disabled | `elementState`〔码 `:799-805`〕 |
| Editable | enabled 且非 readonly | `:815-824` |
| Not Inert | 不在 inert 子树 | `:807-813` |

动作矩阵按动作类型选择检查项；`force` 可跳过非必要检查。

#### D.2 迁移落点

SomnusUI 的能力本身已由 pattern/state 推导〔码 `compiler/caps.py`、`providers/windows/uia.py:1356-1436`〕，`states` 里已有 `disabled/offscreen/readonly/focused/required/selected/checked/expanded`。缺的是**执行前的统一判定**与**短等待**。

建议新增 `somnus/runtime/preflight.py`：

```python
def check(node: SemanticNode, cap: str) -> PreflightResult:
    # 只读已编译的 node.states / node.patterns，不新增 COM 请求
    # 返回 ok / blocked(state) / unknown
```

检查项（零额外 COM 成本）：

| 检查 | SomnusUI 数据源 | 适用能力 |
| --- | --- | --- |
| 存在 | `CompileResult.find(ref)` | 全部 |
| disabled | `node.states` 含 `disabled` | 全部 |
| offscreen | `node.states` 含 `offscreen`，或视口采集已判定不可见 | 全部 |
| readonly | `node.states` 含 `readonly` | `set_text` |
| collapsed | `node.states` 含 `collapsed` | 菜单/展开类 |
| loading | `node.states` 含 `loading` | 全部 |

**失败后等待**：

- 当 preflight 返回 `blocked` 时，不立即失败，而是进入一个有界等待（`actionable_timeout_s`，建议默认 0.3s，配置项放 `config.py`）；
- 等待期间只重读**目标窗口的签名**（复用 `snapshot_signature`，`uia.py:660-672`），状态解除后重编译并执行一次；
- 超时则返回结构化错误：`ERR NOT_ACTIONABLE <ref> state=disabled waited=0.3s hint=wait`。

**明确不做**：

- 不做 `Stable` 的逐帧实现：整窗探针 384–404ms〔实〕，每次动作前都做会直接吞掉性能预算。只有当 provider 将来提供**元素级签名**（见「决策点」）时，才作为可选开关。
- 不做 `Receives Events`：UIA 没有命中测试等价物；要用它就必须引入坐标点击，违反「零视觉、零坐标」不变量〔文 `README.md:11-38`〕。
- 不做 `force`：SomnusUI 的能力模型是显式的，`force` 只会掩盖真实状态。

#### D.3 代价与风险

- 新增模块 + 错误码 + 一个配置键；happy path 不增加 COM 调用。
- **状态陈旧风险**：`uia.py:1379-1400` 记录了 Chromium 的 cached `IsEnabled` 可能滞后。缓解：只对**成功读到**的 state 做阻断；读不到就按现有默认「假定可用」放行。
- **等待放大失败**：必须硬上限，且只在失败路径触发。
- **错误码兼容**：新增 `NOT_ACTIONABLE` 时保留 `TARGET_DISABLED` 的既有语义，避免已有测试与 agent 提示失效。

---

### 方案 E：名称合成与状态规范化

**判定：改造采纳（P1）。**

#### E.1 Playwright 的原做法

`roleUtils.ts`（62KB）实现的是**可访问名称计算**与状态聚合〔码〕：

- 名称来源优先级：`aria-labelledby` → `<label>` → `alt` → `title` → 内容文本；
- 状态：`checked/disabled/expanded/invalid/level/pressed/selected`；
- `ariaSnapshot.ts:281-302` 把状态写进节点属性；
- 渲染时省略默认值，只打印有意义的属性〔码 `ariaSnapshotRenderer.ts:38-81`〕。

#### E.2 迁移落点

SomnusUI 的 `compiler/normalize.py` 负责角色/状态词汇，`compiler/relations.py` 已能推导 `label_for`（文本标签 → 输入框）〔码 `relations.py:160-179`〕，`compiler/simplify.py` 会在「子文本重复父名」时删掉子文本〔码 `simplify.py:102-128`〕。

缺口是：**父节点没名字、子节点有文本**时，信息只存在于子节点，父节点在 `FIND`/实体匹配里不可命名。

建议新增 `somnus/compiler/names.py`，在 `simplify` 之后、`match_entities` 之前执行一次确定性合成：

1. `raw.name` 非空 → 直接用；
2. `LabeledBy` / `label_for` 兄弟文本 → 用标签文本；
3. 唯一叶子文本子节点 → 用该文本，并标记 `name_synthetic=True`；
4. 输入类的 `description` / placeholder → 仅在前三步为空时使用；
5. **永不**从 `value` 或可变状态合成名称。

同时把状态词汇收敛到 `serialize.py` 的 `FLAG_ORDER`〔码 `serialize.py:68-83`〕，新增状态必须同时更新：
`normalize.py` 的映射、`serialize.py` 的顺序、`sui/grammar.md` 的表、至少一个 fixture 测试。

#### E.3 关键风险：合成名称会改变指纹

`compute_fingerprint` 的规则是：**没有稳定 identity 时有名字就用名字，没名字才用 runtime_id**〔码 `ir/node.py:291-309`〕。

如果给原本无名的节点合成名称，它的指纹会从「runtime_id/role_index」切到「name」。一旦文本变化（倒计时、价格、状态行），ref 就会重新分配，破坏 SomnusUI 目前「ref 跨 rev 稳定」的优势〔码 `runtime/registry.py:1-30`〕。

因此必须：

- 在 `RawNode` / `SemanticNode` 上增加隐藏标记 `name_synthetic`；
- `compute_fingerprint` 对合成名称**不作为身份证据**（仍走 runtime_id/role_index）；
- 合成名称只影响渲染、FIND 和实体匹配，不影响 ref 稳定性。

#### E.4 代价与风险

- 新模块 + 指纹规则微调 + fixture 测试；无新增 COM 读取。
- **错误名称**：容器可能继承整段子文本。缓解：只对交互节点、行节点、或「唯一叶子文本」合成；容器不合成。
- **token 可能上升**：名称会让更多节点可被命名，但也会让原本靠 `count=` 折叠的集合多出名字。必须用 `count_tokens` 在 scenario suite 上对比，**以实测决定是否默认开启**。
- 需真机取样：WPF/Win32/Chromium 三类的无名控件比例不同，mock fixture 不能代表全部。

---

### 方案 F：可重解析定位器（Locator 配方）

**判定：改造采纳，先内部后可选工具（P2）。**

#### F.1 Playwright 的原做法

- `Locator` 是**配方**而不是句柄：每次动作都重新定位 DOM〔文 `docs/src/locators.md:111-114`〕；`ElementHandle` 指向具体元素，会 stale〔文 `docs/src/handles.md:259-308`〕。
- 内置定位器：`getByRole` / `getByLabel` / `getByPlaceholder` / `getByText` / `getByTestId`；
- 支持链式 `filter({ hasText, hasNotText, visible })`、`nth/first/last`、名称正则〔文 `locators.md:190-212, 800-830`〕；
- strict mode：匹配多个就报错，需显式 `first()/nth()` 消歧〔文 `class-locator.md:2125-2133`〕；
- MCP 的 `targetLocator` 把 `eN` ref 解析为 `getByRef`，失败提示「Try capturing new snapshot」〔码 `tools/backend/tab.ts:592-619`〕。

#### F.2 迁移落点

SomnusUI 的 ref 已经比 Playwright 更稳定，**不要**改成快照级 ref。真正值得借的是「**用配方重新找到同一个元素**」的能力：

- 新增 `somnus/runtime/locator.py`：
  - 输入：`role`、`name`（精确或 `/regex/`）、`entity`、`layer`、`states`、`cap`、`index`、`in`（容器 ref）；
  - 输出：`match(CompileResult) -> LocatorResult`（唯一命中 / 0 命中 / N 命中 + 候选）；
  - 纯函数，不依赖 `Session`，便于测试。
- 使用点（按优先级）：
  1. **陈旧 ref 重定位**：当前 `registry.relocate` 用 `RefLocator`（role/name/depth/role_index/ancestor_key）〔码 `registry.py:41-58`〕；`locator.py` 可作为第二策略，处理「指纹变了但角色+名称没变」的情况；
  2. **`AWAIT` 扩展**：`run` 的 `AWAIT [TYPE] "name"` 已是连续两次查找的前身〔码 `parse.py:70-78`〕；可先内部支持 `role+name+state` 组合；
  3. **错误提示**：`TARGET_AMBIGUOUS` 目前是「N matches」〔码 `errors.py:87-90`〕，可附带前 3 个候选的 `ref/role/name`，帮助 agent 直接选一个。
- **模型面语法**：新理念允许新增，但应设计成**可选能力**而不是核心提示词的一部分：
  - 方案一：新增可选工具 `locate`，默认不出现在核心工具列表，通过能力档位或 `help locator` 按需加载；
  - 方案二：扩展 `find` 的可选语法（如 `FIND role=button state=enabled "保存"`），核心提示词只保留一句「详情见 help locator」。
  - 两种方案都要先做工具选择准确率

  - 试，确认新增语法不会让 agent 更容易选错。

#### F.3 代价与风险

- 新增一个纯函数模块 + 两处调用点；FIND 本身是线性扫描，实测 0–4ms〔实 `docs/archive/performance-2026-11.md:190-191`〕，成本可接受。
- **误匹配**：role+name 可能命中同名的多个节点；必须返回候选而不是随便挑一个。
- **与指纹重定位冲突**：两套策略要有明确顺序（指纹优先，locator 次之），否则会出现「重定位到错误但同名节点」。
- **不引入 strict 语义**：SomnusUI 的歧义是「延迟处理 + 孪生重试」〔码 `session.py:2474-2478`〕，比 Playwright 的 strict 报错更适合 agent；不要替换。
- **新增语法的学习成本**：即使不设硬上限，语法也不是免费的。定位器应保持「角色 + 名称 + 状态 + 容器」四类，不要发展成完整选择器语言。

---

### 方案 G：投影蒸馏插件链（Distiller）

**判定：改造采纳，P2（收益需实测）。**

#### G.1 Playwright 的原做法

`ariaSnapshotDistiller.ts` 在完整 aria 树建好后，用**一条遍历 + 有序插件链**做无损压缩〔码 `:51-99`〕：

- 插件可返回 `remove`（删子树）或 `unwrap`（用子节点替换自己）；
- `enter` 先序、`exit` 后序；
- `ctx.pendingContentRefs` 记录「名称由内容贡献、且这些内容已在输出中表达」的 ref，用于安全地删掉重复名称；
- `aiPlugins` 顺序：合并文本 → 删无名图片 → 删冗余名称 → 文本内联 → 删与父名重复的子节点 → 展开单子 generic〔码 `:266-274`〕。

#### G.2 迁移落点

SomnusUI 已经有两套相关机制：

- `compiler/simplify.py`：单子容器提升、匿名容器折叠、子文本重复父名时删除〔码 `:56-168`〕；
- `projection/rules.py`：keep / flatten / prune / collapse 四态输出策略〔码 `:104-160`〕。

**不建议再写第三套树重写逻辑**。建议把 `simplify.py` 的零散 `if` 重构为**同样的插件链**，并补齐两条安全规则：

1. 删除「无名、无子、无能力」的装饰图片；
2. 把「唯一子节点是无名文本叶」的匿名容器内联到父节点。

同时引入一个轻量的 `NameOrigin` 映射：

- 记录「父节点的名称来自哪个子节点」；
- 如果该子节点仍然渲染 → 删除重复名称；
- 如果该子节点被删 → 保留名称。

这比现在「名称优先、删子节点」的单向策略更稳，能覆盖嵌套场景。

#### G.3 代价与风险

- 重构 `simplify.py` 会触碰 788 个通过测试〔实 `README.md:630-633`〕与 golden；必须先用方案 B 建模板网。
- **收益可能有限**：SUI-Text 已经省略默认能力、折叠集合、去掉 `focus/read`〔码 `serialize.py:45-66`〕，再压缩的空间主要是「无名包装层」和「重复名称」。不要承诺两位数百分比，先在 scenario suite 上测 `count_tokens`。
- **删错可操作节点**：插件必须有一条硬不变量——**任何带 ref 且可操作的节点不得被删除或 unwrap**；Playwright 的 `isClickTargetRoot` 就是这条守卫〔码 `ariaSnapshotDistiller.ts:106-109`〕。

---

### 方案 H：工具元数据与会话运维

**判定：元数据部分随方案 I 一起做（P1）；空闲回收/大结果落盘按需（P3）。**

#### H.1 Playwright 的原做法

- `defineTool` 给每个工具声明 `capability` 与 `type`（`readOnly` / `input` / `assertion` / `action`），并支持 `skillOnly`〔码 `tools/backend/tool.ts:25-33, 51-60`〕；
- `filteredTools` 按能力过滤：核心工具始终保留，其余只在 `config.capabilities` 里声明时才出现；`skillOnly` 工具默认不进入 MCP 列表〔码 `tools/backend/tools.ts:83-93`〕；
- 每个工具自行决定是否附带快照：输入类工具调用 `response.setIncludeSnapshot()`，只读工具不调用〔码 `snapshot.ts:35-56`、`response.ts:161-171`〕；
- 不需要快照时，`captureSnapshot` 直接跳过整棵树的遍历〔码 `tab.ts:528-533`〕；
- 大结果可写文件并返回链接（`filename` 参数）〔码 `snapshot.ts:43`、`find.ts:34`〕；
- MCP 默认 1 小时无工具调用关闭 headless 浏览器，headed/attach 不关〔文 `docs/src/getting-started-mcp.md:187-209`〕。

#### H.2 迁移落点

SomnusUI 只有 8 个工具，`TOOL_SPECS` 是扁平列表，`on_list_tools` 一次性返回全部〔码 `protocol/tools.py:28+`、`mcp_server.py:81-97`〕，行为散落在 `call_tool` 与 `Session`。可借的是**内部元数据 + 可选能力档位**：

```python
# somnus/protocol/policy.py（内部；是否序列化给模型由方案 I 决定）
TOOL_POLICY = {
    "observe": ToolPolicy(capability="core", kind="read", includes_delta=False),
    "find":    ToolPolicy(capability="core", kind="read", includes_delta=False),
    "inspect": ToolPolicy(capability="core", kind="read", includes_delta=False),
    "call":    ToolPolicy(capability="core", kind="act", includes_delta=True),
    "run":     ToolPolicy(capability="core", kind="act", includes_delta=True),
    "switch":  ToolPolicy(capability="desktop", kind="act"),
    "launch":  ToolPolicy(capability="desktop", kind="act"),
    "configure": ToolPolicy(capability="session", kind="config"),
    "locate":  ToolPolicy(capability="locator", kind="read"),
    "help":    ToolPolicy(capability="docs", kind="read"),
}
```

收益：

- 协议层不再按工具名写 `if/elif`；
- CLI / MCP / JSON-RPC 三端行为一致；
- 方案 I 的「核心 / 可选工具」有了唯一数据源；
- 未来加 `--json` 输出时，有一处可派生「是否附 delta / 是否枚举窗口」。

**大结果落盘**：新理念下可以加参数，但建议优先放在**可选工具**或 CLI：

- CLI 级 `--out <path>`，把 `observe/find/inspect` 的结果写文件；
- 可选工具参数 `filename`（仅当该工具已按需加载时出现，不增加核心提示词）；
- `SOMNUS_MAX_RESULT_LINES` 环境变量控制截断；
- 已有 `INSPECT range=A:B` 与 `read` 的 200 字符内联上限〔文 `README.md:255-281`〕可继续复用。

**空闲回收**：MCP server 当前每个客户端一个长会话〔码 `mcp_server.py:33-78`〕。可加 `idle_timeout_s`（默认关闭，建议 1 小时），在每次工具调用后重置；超时关闭 provider，下次调用重建会话。风险是 agent 认为会话还在，因此必须默认关闭或返回明确的 `session closed (idle)`。

#### H.3 代价与风险

- 策略表是内部重构，但会成为方案 I 的依赖；必须一次做对，否则工具筛选与输出策略会双份真相。
- 空闲回收涉及线程/定时器与 provider `close()`，要防止与正在执行的动作竞争。
- 大结果落盘要处理路径、覆盖与权限；默认写到调用方指定目录，不写系统目录。

---

### 方案 I：渐进式工具面与按需注入

**判定：采纳（P1）。** 这是新理念的直接落点。

#### I.1 Playwright 的原做法

Playwright 对「工具太多、上下文太贵」给了三套机制〔文/码〕：

1. **能力过滤**：`filteredTools` 让核心工具常驻，其余按 `capabilities` 配置出现；`skillOnly` 工具默认不进 MCP 列表〔码 `tools/backend/tools.ts:83-93`〕。
2. **CLI 优先**：`playwright-cli` 面向 coding agent，用简洁命令避免加载大工具 schema 与冗长无障碍树；MCP 则留给需要持久状态与迭代推理的循环〔文 `docs/src/getting-started-cli.md:8-13`〕。
3. **可安装 skills**：`playwright-cli install --skills` 把命令参考作为技能装进 agent 环境，按需读取；不装 skills 时，agent 也可以通过 `--help` 自行发现〔文 `getting-started-cli.md:37-58`〕；`SKILL.md` 本身就是一份按主题组织的命令手册〔码 `tools/skills/playwright-cli/SKILL.md`〕。

这三套机制的共同点：**核心常驻、细节后置、按需发现**。

#### I.2 迁移到 SomnusUI 的落点

SomnusUI 当前是「一次性注入」：

- `system_prompt.md` 约 1780 token，包含工具表、格式、隐式能力、批处理、手势、双桌面、设置、规则、错误表〔码 `protocol/system_prompt.md`〕；
- `settings_brief` 约 798 token，在 MCP `initialize` 时一次性发送〔码 `mcp_server.py:111-183`；数字见 `docs/archive/performance-2026-11.md:123`〕；
- `on_list_tools` 返回全部 8 个工具〔码 `mcp_server.py:81-97`〕。

建议改成三层：

**第一层：核心常驻**

- 工具：`observe/find/inspect/call/run`；
- 提示词只保留：项目不变量、五个工具的一句话用途、SUI-Text 最小语法、五条硬规则、错误码表；
- 目标：核心提示词比现在短，而不是更长（具体数值以实测为准，不设硬上限）。

**第二层：能力档位**

- `desktop`：`switch/launch/configure`；
- `locator`：`locate`（或 `find` 的扩展模式）；
- `docs`：`help`；
- `dev`：`trace/record/replay`。
- 通过会话配置选择档位，例如 `SOMNUS_CAPABILITIES=core,desktop,locator,docs`；MCP `on_list_tools` 只返回当前档位内的工具。

**第三层：主题文档按需加载**

- 把长内容从 `system_prompt.md` 拆到 `somnus/protocol/agent/*.md`：
  - `format.md`（SUI-Text 完整语法）
  - `batching.md`（批处理与 `run`）
  - `gestures.md`（select/activate/context_menu）
  - `desktops.md`（main/child 与 ref 失效）
  - `locator.md`（定位器语法与歧义处理）
  - `errors.md`（错误码与恢复建议）
- MCP `instructions` 只保留跨工具共享的最小规则与实时设置，不再承载完整语法。

**发现机制（已定：`help` + MCP/skill 过滤档位）**

- **`help`（运行时入口）**：`HELP` 无参数返回「当前档位 + 全部可用/可开启能力 + 主题列表」；`HELP <topic>` 返回对应主题文档。核心提示词只说「需要细节时调用 help」。
- **MCP 过滤档位**：`on_list_tools` 只返回当前 capability 档位内的工具；未启用的不出现在列表里，但 `help` 会说明如何开启。
- **skill 过滤档位**：安装时选择核心/桌面/定位/开发等 skill 包，agent 只读取当前档位对应的文档；skill 带版本号，`help` 返回运行时版本以便发现不一致。
- **CLI**：`somnus help <topic>` 与 MCP 的 `help` 共用同一份主题文档，适合 coding agent。

#### I.3 代价与风险

- 需要拆 `system_prompt.md`、给 `TOOL_SPECS` 加元数据、改 `on_list_tools`/`instructions`、加 `help` 工具或 CLI 命令；属于协议层重构，不影响 runtime。
- **工具发现失败**：藏得太深，agent 不知道有 `locate`。缓解：`help` 无参数时列出全部可用与可开启能力；MCP/skill 档位只过滤列表、不隐藏说明；核心提示词保留一句「更多能力见 help」。
- **工具选择准确率下降**：工具变多后，模型可能选错。必须做「给定任务 → 期望工具」的离线测试；新增工具时同步扩充测试集。
- **文档与运行时漂移**：主题文档写 `FIND` 语法，但 parser 已变。缓解：主题文档中的语法样例进入 contract test，或从同一份 schema 生成。
- **版本错配**：skill 是静态文件，运行时升级后 skill 可能过期。缓解：skill 带版本号，`help` 返回运行时版本，发现不一致时提示重新安装。
- **不设硬上限 ≠ 无限扩张**：核心提示词每增加一段，都要在稳态成本里可见；长内容一律外置。

#### I.4 验收方式

- 核心档位下，MCP `tools/list` 只返回 5 个工具；开启 `desktop` 后返回 8 个；
- skill 安装只提供当前档位对应的文档，且 `help` 能说明未安装档位的开启方式；
- `HELP` 无参数能列出全部能力与开启方式；
- 同一组任务在「核心档位」与「全档位」下的工具选择准确率不下降；
- 核心提示词 token 数低于改造前（作为方向性指标，不作为硬门槛）。

---

### 方案 J：自体验 / 自检回路

**判定：采纳（P2）。** 这是「能够自己实际体验」的直接落点。

#### J.1 Playwright 的原做法

Playwright 的 Test Agents 把「自己体验」拆成三个角色〔文 `docs/src/test-agents-js.md:12-21`〕：

- **planner**：探索应用，产出 Markdown 测试计划；
- **generator**：把计划变成可执行测试，边写边验证选择器与断言；
- **healer**：重放失败步骤，检查当前 UI，定位等价元素或流程，给出补丁并重跑，直到通过或触发护栏〔文 `:194-213`〕。

底层是「可执行的环境 + 可观察的结果 + 有界的修复循环」，而不是让模型空想。

#### J.2 迁移落点

SomnusUI 已经具备自体验的基础：

- `examples/agent.py`：真实 LLM agent loop，host 侧驱动 `TOOL_SPECS` 与 `call_tool`〔码 `examples/agent.py:1-140`〕；
- `examples/eval_agent.py`：从**运行时状态**判定成功，而不是相信模型自述〔码 `examples/eval_agent.py:1-56`〕；
- `apps/scenario-suite` 与 `tests/scenarios/` 提供可复现屏幕；
- `somnus --demo` / `--scenario` / `bench` 提供离线运行方式〔文 `README.md:45-61`〕；
- 方案 A 的 trace 提供逐步证据。

建议新增一组**运行在 runtime 之外**的 CLI/测试能力：

| 能力 | 作用 | 依赖 |
| --- | --- | --- |
| `somnus doctor` | 检查 provider/UIA 额外依赖/**child session 状态**/配置；在可用桌面上跑一次 observe→find→call 冒烟，打印 SUI-Text | 无 |
| `somnus scenario <name>` | 在 fixture 或真实 scenario-suite 上启动一个可操作屏幕；真实运行默认放进 child session | 已有 |
| `somnus eval --tasks ...` | 默认在 child session 中跑固定任务，从运行时状态判定成功，输出 JSON + trace 路径 | A + 已有 eval |
| `somnus replay <trace>` | 重放记录的动作序列，报告首个分歧（状态/ref/delta）；默认在 child session | A |
| `somnus heal <script>` | 可选：重放 `run` 脚本，按 locator 重新定位失败步骤并给出补丁；默认在 child session | A + F |

**Agent 侧的分工**：

- 运行时保持「无 LLM」；
- planner/generator/healer 是 **host 侧**循环，可以用 `examples/agent.py` 扩展；
- healer 的修复动作限定为：重定位、换 route、补 `AWAIT`、调整 settle，不做无界重试；
- 自体验默认使用 **child session**（`SESSION open` 进入）：输入、焦点和窗口抬升都在隔离桌面，不干扰用户〔文 `docs/reference/child-session.md:22-56`〕。
- mock / scenario 用于无 Windows、无 child session 或纯离线回归；它们不是默认体验环境。
- 操作用户主桌面需要显式 `--real`，并作为例外而不是默认路径。

#### J.3 代价与风险

- 新增 CLI 命令与测试工具；不需要改 runtime 核心。
- **副作用**：自体验会真实点击。默认使用 **child session**；mock/scenario 仅用于离线；操作用户主桌面需显式 `--real`。
- **child session 前置条件**：Windows Pro/Enterprise/Education、一次提权 setup、系统范围内同时一个 child session、无安全隔离〔文 `docs/reference/child-session.md:162-174`〕。`somnus doctor` 必须先检查这些条件，不可用时明确报告，而不是静默回退到主桌面。
- **不稳定**：child session 隔离的是输入，不消除 GUI 动画/异步加载。缓解：trace + 失败重试 + 用运行时状态判定，而不是模型自述。
- **评测任务质量**：任务必须机器可判定（如 `eval_agent.py` 的 cart count/total）；不要用「模型说完成了」作为成功标准。
- **成本**：真实 LLM 评测会消耗推理；真实模型评测作为可选档位，默认跑无模型的固定任务或 mock。

---

## 4. 不建议迁移 / 明确拒绝

| Playwright 做法 | 为什么不适合 SomnusUI |
| --- | --- |
| YAML aria snapshot 作为模型格式 | SUI-Text 一行一节点、能力内联、默认省略，token 更省〔码 `serialize.py:1-23`〕；改成 YAML 只会变长 |
| 快照级 `e5` ref（以最后一次快照为准） | SomnusUI 的指纹 ref 跨 rev 稳定〔码 `registry.py:1-30`〕，这是优势，不能反向迁移 |
| ElementHandle 语义 | 句柄会 stale；SomnusUI 已有 ref + registry + 有界重定位，不需要句柄层 |
| 截图 / vision / 坐标 / `[box=...]` | 违反「零视觉、零坐标」不变量〔文 `README.md:11-38`〕；`BoundingRectangle` 只用于视口判定，不用于点击 |
| `force` 跳过 actionability | 会掩盖 disabled/readonly 等真实状态，与 SomnusUI「能力即事实」的设计冲突 |
| `browser_run_code_unsafe` 任意代码 | RCE 等价，且破坏「运行时无 LLM、语法封闭」；`run` 的 CALL/AWAIT 已覆盖流程编排 |
| 完整 Trace Viewer UI | 数据模型值得借，React viewer 不值得复制；先用 `somnus trace show` 文本时间线 |
| Network mocking / clock / emulation / auth storage | 桌面 UIA 没有对应层 |
| 浏览器补丁 / Docker / CI 矩阵 | 与 UIA 运行时无关 |
| Playwright Test 整套 runner | pytest 已够用；只借 fixtures、重试、快照模板 |
| strict mode 替换歧义延迟 | SomnusUI 的「先行动、失败再试孪生」更适合 agent；strict 会多一个失败回合 |
| 把所有工具和完整语法一次性塞进核心提示词 | 与「尽量简短、分批按需注入」冲突；长内容应外置为主题文档 |

---

## 5. 建议实施顺序

顺序按**依赖关系**排列。每个阶段都有明确出口条件。

### 阶段 0：安全网（C + B）

1. `tests/architecture/test_import_boundaries.py`（方案 C）——锁住依赖方向；
2. `tests/support/sui_template.py` + 首批场景模板（方案 B）——锁住 SUI-Text 形状；
3. 记录基线：`pytest`、`tests/scenarios`、`count_tokens`、当前 `system_prompt.md + 工具面` token 数。

**出口**：现有测试全绿；边界测试能对一次故意跨层 import 报错；模板能匹配当前输出。

### 阶段 1：可观测（A，仅本地 raw trace）

1. `TraceSink` 协议 + JSONL sink + `somnus trace` 命令；
2. `Session` 内 6–8 个钩子；
3. 默认写入 `scratch/traces/<session-id>/`，不做脱敏；
4. 用 trace 记录一次真实 3 步流程（observe → find → call）。

**出口**：默认关闭时零开销；raw trace 只在本地、Git 不跟踪；打开后能回答「这一步走了哪条 route、settle 几帧、delta 是什么、verify 为什么失败」。**阶段 1 不实现脱敏。**

### 阶段 2：工具元数据与渐进式工具面（H 元数据 + I）

1. `TOOL_SPECS` 增加内部元数据：`capability`、`kind`、`includes_delta`；
2. `on_list_tools` 按档位返回工具；`instructions` 缩减为核心共享规则；
3. `system_prompt.md` 拆分：核心 + `agent/*.md`；
4. 新增 `help` 工具或 `somnus help` CLI；
5. 建立「任务 → 期望工具」的工具选择测试集。

**出口**：核心档位只返回核心工具；`HELP` 能发现全部能力；核心提示词比改造前短；工具选择准确率不下降。

### 阶段 3：感知与动作质量（E + D）

1. 名称合成（E），带 `name_synthetic` 标记并排除出指纹；
2. preflight + 失败后有界等待（D）；
3. 用阶段 1 的 trace 对比改造前后的失败率与回合数。

**出口**：happy path 无新增 COM 调用；无名节点比例下降；`NOT_ACTIONABLE` 只出现在真实阻塞场景。

### 阶段 4：自体验回路（J）

1. `somnus doctor`（环境冒烟；先检查 child session 前置条件）；
2. `somnus replay <trace>`（默认在 child session 重放与分歧报告）；
3. `somnus eval`（默认在 child session 跑任务，运行时状态判定 + JSON 报告）；
4. 可选 `heal`（依赖阶段 5 的 locator）。

**出口**：agent 能在 child session 内自己跑、自己看 trace、自己判断成功/失败；child session 不可用时明确报错；默认不触碰用户主桌面。

### 阶段 5：定位与投影（F + G）

1. `runtime/locator.py` 纯函数模块，先用于陈旧 ref 第二重定位与 `TARGET_AMBIGUOUS` 候选提示；
2. 评估是否把它作为可选工具 `locate` 暴露；
3. 把 `simplify.py` 重构为插件链，补两条安全规则，引入 `NameOrigin`。

**出口**：不增加核心提示词负担；stale 重定位成功率提升；scenario suite token 数不上升；所有带 ref 的可操作节点未被删除。

### 阶段 6：运维与条件项

- MCP `idle_timeout_s`、CLI `--out`、可选 `filename`；
- trace 的 HTML 视图（可复用 html-report 思路，非必须）；
- 录制 → `run` 脚本（依赖 F 的定位配方）。

### 阶段 7：Trace 收尾与参考指标（项目结束前）

1. 冻结代表性 trace 样本；
2. 实现并运行 `somnus trace redact`，生成脱敏 trace + `metrics.json`；
3. 自动扫描 + 人工复核，确认无标题/路径/值；
4. 收尾阶段才把 `docs/reference/traces/` 下的脱敏产物纳入版本控制；本轮不执行提交；
5. 清理本地 raw trace。

**出口**：仓库内不存在 raw trace；参考指标可读、可复现；脱敏黄金测试通过。

---

## 6. 风险台账

| 风险 | 触发条件 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 上下文膨胀 / 工具过载 | 新增工具与语法都塞进核心提示词 | 每轮成本上升；模型注意力分散 | 核心最小化；长内容外置；按档位加载；稳态成本回归观察 |
| 工具发现失败 | 可选工具藏得太深 | agent 不知道能力存在 | `HELP` 无参数列出全部能力；MCP/skill 档位只过滤列表、不隐藏说明；CLI `--help` |
| 工具选择准确率下降 | 工具数量增加 | 选错工具，回合数上升 | 「任务 → 期望工具」测试集；新增工具必须过测试 |
| 文档与运行时漂移 | 语法演进但主题文档未更新 | agent 按旧语法调用 | 主题文档语法样例进 contract test；skill 带版本号 |
| 自体验副作用 | 在用户主桌面真实点击 | 干扰用户 | 默认 child session；mock/scenario 仅离线；`--real` 显式开启 |
| child session 不可用/被占用 | Home 版、未 setup、已有会话 | 自体验无法运行 | doctor 先检查并给出 setup 指引；明确报错，不静默回退主桌面 |
| 自体验不稳定 | 真实 GUI 动画/异步 | 评测抖动 | trace + 重试 + 运行时状态判定 |
| 探针成本失控 | 每次动作前都做整窗签名 | 大树 +384–404ms | preflight 只读已编译状态；等待只在失败路径；元素级签名作为可选能力 |
| UIA 状态陈旧 | Chromium cached `IsEnabled` 滞后 | 误判 disabled，动作被阻断 | 只阻断成功读到的状态；读不到按可用放行；trace 记录读值来源 |
| 合成名称破坏 ref 稳定 | 无名节点获得名称 | 指纹切换，ref 重 mint | `name_synthetic` 排除出指纹证据 |
| 模板过松 | 大量 `?`/`...` | 回归被掩盖 | 保留严格 golden；每场景至少一条严格断言 |
| 蒸馏删错节点 | 插件顺序或守卫缺失 | agent 无法操作 | 硬不变量：ref-bearing/可操作节点不删；逐插件测试 |
| raw trace 含隐私 | 开发期不脱敏；窗口标题/表单值/聊天内容 | 数据泄露 | 默认写 `scratch/traces/`；Git 不跟踪；绝不提交；收尾期统一脱敏 |
| raw trace 误提交 | `git add -A` 或手动添加 | 进入 Git 历史 | `.git/info/exclude` 本地排除；raw 模式拒绝写受跟踪目录；收尾审查 |
| 脱敏不彻底 | 收尾字段遗漏 | 参考指标泄露 | 字段白名单 + 自动扫描 + 人工复核 + 黄金测试 |
| 参考指标不可复现 | 环境/版本不同 | 指标不可比 | manifest 记录版本/OS/settings；README 写明环境 |
| 导入 allowlist 腐化 | 新增模块/延迟 import | CI 误报 | 矩阵文档与测试同源；显式豁免清单 |
| 空闲回收竞争 | 定时器与正在执行的动作 | provider 被提前关闭 | 引用计数/锁；默认关闭 |

---

## 7. 决策状态

### 7.1 已定（按用户决定）

1. **自体验默认 child session**：输入、焦点、窗口抬升都在隔离桌面；mock/scenario 仅离线；主桌面需显式 `--real`。
2. **发现机制 = `help` + MCP/skill 过滤档位**：`help` 负责运行时发现；MCP `tools/list` 与 skill 安装负责档位过滤。
3. **文档存放 = `scratch/`**：不加入 `.gitignore`，也不提交到 Git 历史。
4. **Trace 生命周期**：开发期 raw trace 单独存放在 `scratch/traces/`，Git 不跟踪、不脱敏；项目结束统一脱敏后，才作为参考指标加入 `docs/reference/traces/`。

### 7.2 待定（用户未决，本文不替用户决定）

1. **核心常驻工具集**：哪几个工具常驻？（本文建议 `observe/find/inspect/call/run`，仅为建议，不是决定。）
2. **定位器是否作为可选工具暴露**：先内部（healing/AWAIT），还是直接暴露 `locate`？

### 7.3 设计建议（不阻塞，可后续确认）

- 注入粒度：核心一句、主题按需。
- 稳态观察值：不设硬上限，但需要一个回归观察值（例如核心提示词 token 数）。
- 脱敏字段清单与样本数量：具体字段白名单、保留 3–5 条还是更多，属于收尾阶段的实现细节，可按 A.5 的建议执行。
- provider 元素级签名：这是把 Playwright `stable` 语义落到 UIA 的前提；不加则动作前稳定性检查永久缺席。

---

## 8. 阅读清单

### Playwright 侧（`E:\dao\gui\playwright-main`）

| 路径 | 用途 |
| --- | --- |
| `README.md` | 产品面：Test / CLI / MCP / Library |
| `AGENTS.md` | 仓库结构与 `DEPS.list` 规则 |
| `docs/src/actionability.md` | 动作前检查清单 |
| `docs/src/aria-snapshots.md` | 快照格式与模板匹配 |
| `docs/src/locators.md` | Locator 配方、filter、strict |
| `docs/src/handles.md` | Locator vs ElementHandle |
| `docs/src/getting-started-mcp.md` | MCP 工具面、空闲回收、run code |
| `docs/src/getting-started-cli.md` | CLI 的 token 效率、skills 安装与自行发现 |
| `docs/src/test-agents-js.md` | planner / generator / healer |
| `docs/src/trace-viewer.md` | trace 录制与查看 |
| `.claude/skills/playwright-dev/trace_system_guide.md` | trace 数据模型与文件格式 |
| `packages/injected/src/ariaSnapshot.ts` | `mode: 'ai'`、ref 分配、状态、深度 |
| `packages/injected/src/ariaSnapshotDistiller.ts` | 插件链与 AI 压缩规则 |
| `packages/isomorphic/ariaSnapshotRenderer.ts` | YAML 渲染与属性省略 |
| `packages/injected/src/injectedScript.ts` | `elementState`、stable、hit-target |
| `packages/playwright-core/src/tools/backend/tool.ts` | `defineTool` 元数据 |
| `packages/playwright-core/src/tools/backend/tools.ts` | `filteredTools`、ability 过滤、skillOnly |
| `packages/playwright-core/src/tools/backend/snapshot.ts` | 快照工具与 `filename` |
| `packages/playwright-core/src/tools/backend/find.ts` | 快照内搜索 + 上下文片段 |
| `packages/playwright-core/src/tools/backend/tab.ts` | `captureSnapshot`、`targetLocator` |
| `packages/playwright-core/src/tools/backend/response.ts` | 输出策略与快照开关 |
| `packages/playwright-core/src/tools/backend/verify.ts` | 断言工具 |
| `packages/playwright-core/src/tools/cli-daemon/commands.ts` | CLI 命令声明 |
| `packages/playwright-core/src/tools/skills/playwright-cli/SKILL.md` | 按主题组织的命令手册 |

### SomnusUI 侧（`E:\0down\SomnusUI`）

| 路径 | 用途 |
| --- | --- |
| `README.md` | 不变量、工具面、ref 生命周期、实测指标 |
| `docs/archive/performance-2026-11.md` | 当前性能基线、prompt 预算、未完成项 |
| `docs/archive/performance-2026-11.md` | 视口采集与 `rows/visible/scroll` 契约 |
| `somnus/protocol/system_prompt.md` | 当前一次性提示词，方案 I 的拆分对象 |
| `somnus/protocol/mcp_server.py` | `instructions`、`on_list_tools`、会话生命周期 |
| `somnus/protocol/tools.py` | 工具面与 `call_tool` |
| `examples/agent.py` | 真实 LLM agent loop（host 侧） |
| `examples/eval_agent.py` | 从运行时状态判定成功的评测 |
| `somnus/ir/node.py` | `RawNode` / `SemanticNode` / 指纹 |
| `somnus/compiler/normalize.py` | 角色/状态词汇 |
| `somnus/compiler/simplify.py` | 结构简化 |
| `somnus/compiler/match.py` | 实体匹配 |
| `somnus/compiler/relations.py` | `label_for` 等六种关系 |
| `somnus/projection/rules.py` | keep/flatten/prune/collapse |
| `somnus/projection/engine.py` | 渲染计划与预算 |
| `somnus/sui/serialize.py` | SUI-Text 序列化 |
| `somnus/sui/parse.py` | SUI-Text 解析 |
| `somnus/runtime/registry.py` | 稳定 ref 与重定位 |
| `somnus/runtime/settle.py` | 签名探针与静默窗口 |
| `somnus/runtime/delta.py` | delta 计算与折叠 |
| `somnus/runtime/verifier.py` | 效果校验 |
| `somnus/runtime/session.py` | 调用主流程、`audit_log`、有界纠正 |
| `somnus/providers/base.py` | Provider 协议 |
| `somnus/providers/windows/uia.py` | UIA 读取、状态、签名探针 |
| `somnus/providers/windows/cache.py` | CacheRequest 三层 |
| `tests/golden/test_golden.py` | 整文件 golden |
| `tests/scenarios/conftest.py` | 场景 fixtures |
| `tests/scenarios/test_store.py` | 场景断言风格 |

---

## 9. 本轮范围声明

- 已在 SomnusUI 从 `main` 创建并切换到 `play` 分支；
- 工作区原只有未跟踪的 `.zcode/`，无已跟踪改动，切换未丢失任何内容；
- 本文档曾从 `docs/playwright-patterns.zh-CN.md` 移到 `scratch/playwright-patterns.zh-CN.md`；2026-11-07 再迁入 `docs/archive/playwright-patterns.zh-CN.md` 留档；
- 按用户决定更新了 trace 生命周期：开发期 raw trace 本地留存、Git 不跟踪、不脱敏；项目结束统一脱敏后作为参考指标入库；
- 本轮只移动文档位置并更新内容，未改动任何业务代码、测试或配置，未执行任何 Git 提交操作；
- 文中所有「建议落点」都是设计，不是已实现状态。

