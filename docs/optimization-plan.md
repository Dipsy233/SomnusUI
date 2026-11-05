# SomnusUI 性能与精准率优化方案（源码核对版）

> 输入：四路（A/B/C/D）各两份报告。本文件是核对后的**结论 + 可执行清单**，不是四份报告的摘要。
> 原则：只按「实测收益 / 改动成本」排序；安全/确认/权限/风险类建议按要求全部剔除，本文不涉及。
> 证据等级：**[实]** 本机真实实测（B 路，或本机可运行的真机数据）；**[实-本轮]** 本轮改造在 `branch test` 上的真机实测（由实现方报告）；**[码]** 我亲自读代码确认；**[算]** 本机合成的纯 Python 计时（不含 COM）；**[推]** 尚未实测的推断，已在文中标注。

> **2026-11-05 更新**：本轮改造已在 `branch test` 落地。**§0–§7 保留为实施前的核对与分析**；每一项的实施结果、实测数字与偏差已就地补在对应条目下。

---

## 当前状态（2026-11-05，branch `test`）

**提交序列（`main..test`，共 8 笔）**：`aa47ccd`（视口采集 + 分层探测 + 精度修正 + 设置项）→ `1b2d146`（bench 墙钟）→ `1742a66`（FIND 等待 + 桌面摘要）→ `30412a8`（脚本化 `run` + 确认门控移除 + 空壳告警 + 效果重试）→ `87030df`（小窗口自适应）→ `eba3926`（陈旧 ref 自愈 + FIND 单次全量升级 + pid 复用修正）→ `94b6c77`（子桌面进程启动）→ `326d2ec`（`read()` 输出 + 唯一 active + settle 后刷新 + CR/TAB 转义）。本文档为**本轮第三笔**（前两笔是 `94b6c77`、`326d2ec`），只改文档、不改生产代码。
实测口径：本机 i5-13500HX / 2560×1440@96DPI，`apps/scenario-suite`（store 240 行、原始树 1467/1470 节点）；真机数字在子桌面会话内取得（同机会话 2）。

### 已完成 / 未完成一览

| 项 | 状态 | 本轮实测 | 原计划预期 | 判定 |
|---|---|---|---|---|
| observe 视口采集 | ✅ | store **3385ms → 594ms（−82.5%）**，tokens **292 不变** | ≈360ms（viewport §2.1） | −82.5% 达成；**594ms 未达 360ms** |
| 分层探测覆盖 | ✅ | 无变化 CALL **~6s → 686–706ms**；探测 **1622–1938 → 384–404ms**；无变化时 **0 次全量快照**；小窗 CALL **215–231ms** | 大树无变化 0.4–0.9s；探测 150–300ms | 调用级达标；**大树探测未进 150–300ms** |
| P0-0 仪表 | ✅ | `tests/runtime/test_settle_frames.py`（512 行）+ `bench/run.py` 墙钟 + `SOMNUS_PROFILE` 分段 | — | 达成 |
| P0-1 cache 瘦身 | ⚠️ 未证实 | 六档 `full/lean/sig/peek/rect/probe` 已实现；**lean/sig 效果与噪声同量级**；默认仍 `full` | 大树 −30%+ | **收益未证实** |
| P1-1 签名探测 settle | ✅ | 无变化 0 全量；有变化 1 全量 + 探测（见上两行） | 1 全量+1 探测 | 达成 |
| P1-2 设置项 + 提示 + `configure` | ✅ | `settle_quiet_ms=40 / settle_poll_ms=10 / settle_timeout_s=3.0 / post_action_enum=true`；优先级 内置<文件<env<运行时；MCP `instructions` + 工具名 | 默认 40/10、可调、加载可见 | 达成；提示预算 **2639/2700**（确认门控文案移除后；`run` 刚进 prompt 时是 2695） |
| P1-3 动作后枚举 | ✅ | **默认开启**，`configure` 可关 | 默认开启、可关 | 达成 |
| P2-1 下拉选择链 | ✅ 实现 | `+select` + `SelectionItem` 首路由 + 选中值回读 + `activate` 升级（单测 `test_select_capability.py` / `test_executor_select_readback.py`） | 0%→90%+ | **本轮未给真机成功率，旧目标未以数字确认** |
| P2-2 孪生 | ✅ | 可证明同元素才折叠，否则标 `twin=true`；失败原地重试孪生 | 一次 CALL 命中 | 达成 |
| P2-3 空壳/抬升失败上浮 | ✅ | `uia.last_foreground_error` 记录原因；observe 头部输出 `~ foreground denied (…)` 与 `~ tree looks empty; …`（有标题且根 `children==0`） | 空壳有告警 | 达成（`test_observation_warnings.py`） |
| P2-4 状态词归真 | ✅ | 有可见 delta → `OK`（`test_verifier_status.py`） | — | 达成 |
| P2-5 投影去噪 | ✅ | 带子节点的 pane/group 不再发 `set_text`（`caps.py`） | — | 达成 |
| P2-6 registry 按窗口隔离 | ✅ | 指纹键为 `(window_id, fingerprint)`（`test_registry_window_scope.py`） | — | 达成 |
| P2-7 原地重试 | ✅ | `EFFECT_NOT_CONFIRMED` 时同目标换下一条未试 route 重试**一次**；成功 `OK ~ retried via <primitive>`，仍失败抛原错；真机未触发（首条路由都生效），由构造用例保证 | 换 route 再试一次 | 达成（`test_effect_retry.py`） |
| 确认门控移除 | ✅ | `call`/`call_batch`/`run` 直接执行 `!` 能力；`CONFIRMATION_REQUIRED`、`--confirmed`、`policy.check_action`、`CRITICAL_NAMES` 全删；保留进程 allow-list、权限校验、通道开关、路由链、效果校验、批处理预校验 | 项目取向：不做安全机制 | 达成（提示预算降到 2639） |
| `BoundingRectangle` 进缓存 | ✅ | 30001 已加；full **+6.2%**、sig **+11.6%**（视口测量） | 需要 | 达成（代价已测） |
| P3-1 FIND 等待 | ✅ | 异步菜单 **140–172ms** 命中；`timeout=1.0` 约 **1010ms** 返回；大树 `timeout=3.0` 约 **3.1s** | 省 1 回合 | 达成（运行时按 timeout 线性） |
| P3-2 桌面摘要 | ⚠️ 部分 | `WINDOWS` 行默认出现；成本 **0.002ms**、tokens **+3**；**`ops` 未实现** | 带 `ops` | 部分 |
| P3-3 脚本化 `run` | ✅ 提前 | 菜单流程一次 run **453ms** vs 两次 CALL **428ms**；收益=**少一次模型推理** | 押后到阶段 3 | **提前落地（`30412a8`）** |
| P3-4 宏 / 事件订阅 / 键盘超时 / MCP 线程 | ❌ | — | 阶段 4 | 剩余 |
| 小窗口自适应 | ✅ | 未截断的小窗下一次改走全量单请求；小窗 settle 不再付全尺寸基线（`87030df`，`test_small_window_adaptation.py`） | 小窗不退化 | 达成 |
| 陈旧 ref 自愈 | ✅ | 同一窗口内按唯一匹配重定位一次，响应 `~ healed old→new (via …)`（`eba3926`，`test_stale_relocation.py`） | 少一个重规划回合 | 达成 |
| FIND 单次全量升级 | ✅ | 几次零命中后，**仅"部分但未截断"的小树**可花一次全量；截断集合永不升级（`eba3926`，`test_find_escalation.py`） | — | 达成（未覆盖未 realized 行） |
| `read()` 输出 | ✅ | `~ ref value="…" len=N`；读取上限 4096（溢出报 `len>=4097 truncated=true`）；内联 200 字符预览（`326d2ec`，`test_read_capability.py`） | — | 达成 |
| 唯一 active 窗口 | ✅ | 多个 claimant 只输出一个 `active`，目标优先（`326d2ec`，`test_active_window.py`） | — | 达成 |
| settle 后窗口刷新 | ✅ | 标题可能在 settle 后才落地，刷新移到 settle 之后；桌面签名不变则跳过枚举，**任何差异都全量枚举**（`326d2ec`） | — | 达成 |
| CR/TAB 转义 | ✅ | `escape_text` 转义 `\\` `"` `\r` `\n` `\t`，保证一节点一行（`326d2ec`；见 `sui/grammar.md`） | — | 达成 |
| 子桌面启动进程 | ✅ | peer 的 `process.start/status/kill` + CLI `child-session run`；`child_allow_spawn` + `allow_processes` 双开关；SUI-Text 不暴露（`94b6c77`，`test_peer_process.py`） | — | 达成（CLI-only） |
| 测试 | ✅ | 非 GUI **733 passed**；真机 `-m windows` **38 passed**（子桌面，约 62–66s，零 `window ... did not appear`） | — | 达成 |

### 关键裁决（本轮定案）

| 裁决 | 落点 / 证据 |
|---|---|
| 确认门控**彻底移除** | `!` 只是能力元数据，`call`/`call_batch`/`run` 直接执行；`CONFIRMATION_REQUIRED`、`--confirmed`、`policy.check_action`、`CRITICAL_NAMES` 全删（`30412a8`）。保留进程 allow-list、原生权限错误、通道开关、路由链、效果校验、批处理预校验 |
| **不做坐标 / OCR / 截图** | 全库无截图、坐标、DPI 路径；三个真实失败（下拉、孪生、空壳）均在 UIA 内解决。`BoundingRectangle` 只作为视口信号进缓存，不是坐标点击（[viewport-capture-proposal.md](viewport-capture-proposal.md) §1.1） |
| 刷新在 **settle 之后**判定，**任何差异都全量枚举** | `Session._refresh_after_action` 在 settle 后比较 `provider.top_level_signature()`；`False`（签名完全一致）才跳过枚举，`True`/`None` 一律 `refresh_windows()`（`326d2ec`；`session.py` 的 `_desktop_changed` / `_refresh_after_action`） |
| `escape_text` 保证**一节点一行** | `sui/serialize.py` 的 `escape_text` 转义 `\\`、`"`、CR、LF、TAB；Windows 文本面的换行是裸 CR，只转义 `\n` 会把一行拆成两行。`executor._escape_read_text` 对 `read` 载荷用同一套规则 |
| SUI-Text **面不暴露进程启动** | peer 的 `process.start/status/kill` 只由 `child-session run` 驱动；`SESSION` 只解析 `open/close/detach`，七个工具里没有进程启动（`94b6c77`） |

### 后续三笔的实测数字

| 指标 | 实测 | 说明 |
|---|---|---|
| `top_level_signature()` | **p50 0.31–0.52ms**，max ≤**2.15ms** | Win32 顶层窗口 `(hwnd, title)` 走查；同机 `list_windows` p50 **70–78ms**，所以"签名不变跳过枚举"省下的是一次完整 UIA 枚举 |
| 写入路径（标题不变） | 总 **81–104ms** / enum **0.6–0.9ms** | 签名未变 → 跳过枚举 |
| 写入路径（标题变化） | enum **78–104ms** / 总 **154–238ms** | 标题变化会改变窗口列表，**必须全量枚举**；与旧实现同量级，修的是"重命名晚一 CALL"的正确性 |
| `observe()` 的 enum | 仍 **78–102ms**（全量） | 观察本身不做签名短路 |
| 子桌面 UWP | 计算器能起、能读，`OBSERVE full` 读到 **34 个按钮** | 真机 `-m windows` 38 passed 的子桌面会话内 |
| 探针验收 | A 段 **7/7**、B 段 **4/4** | A：read 85ms、4096 上限命中、预览 199/79 字符、按钮报 `CAPABILITY_NOT_FOUND`；B：active 唯一、`focus()` 跟随、关闭后无残留 |

### 未达成项与原因（如实写）

1. **observe 594ms vs 目标 360ms**：−82.5% 达成，但 360ms 未达。差距来自"骨架层 + 容器窄窥视 + 可见行完整读"之后仍未消掉的固定成本；本轮**没有做分段拆分**，因此具体构成未测，[推] 与容器窥视的行数/层数相关。
2. **大树探测 384–404ms vs 150–300ms**：这是 provider 侧固有开销（请求物化/跨进程），客户端遍历省不掉；`lean`/`sig` 档与噪声同量级也说明"缩 cache request"不是大树探测的杠杆。
3. **`ops=` 未实现**：5 个窗口浅读合计 **111ms**（其中 form 窗口 43ms），超过 observe 的 ~80ms 红线；桌面摘要只给窗口清单。`projection/engine.py` 的 `ops=` 渲染分支保留，但没有数据源。
4. **`lean`/`sig` 档收益未证实**：两档已实现、可用 `SOMNUS_CACHE_TIER` A/B，但同窗口重复测量下与噪声同量级；默认仍 `full`。原"cache request 瘦身 → 大树 −30%"的推断被本轮否定。
5. **大树"有变化"的 CALL 本轮没有数字**：只测了无变化路径（686–706ms）。旧预期 3.1–3.6s 未复核——这是本方案唯一没有被实测覆盖的主路径。
6. **`run` 提前落地**：计划把它押后到阶段 3，实际随 `30412a8` 落地。它的收益是"少一次模型推理"（453 vs 428ms 说明运行时并不省），与计划判断一致，但落地顺序改变了。
7. **P2-1 的真机成功率仍未以数字确认**：下拉链已实现且有单测，但"0%→90%+"仍是旧目标；本轮报告没有给出真机成功率。

### 剩余项与优先级（功能优先）

**第一梯队（直接影响大列表可用性/成功率）**

1. **虚拟化列表未 realized 行**：`FIND` 0 命中时 provider 侧按名查询 / 滚动分页（viewport §4 R3）。现在未 realized 的行"不存在"，只能 0 命中或 STALE——这是大列表剩下的核心缺口。
2. **`run` 的跨窗口支持**：当前跨窗口流程需显式 ref（切窗会重排 ref）、`delta` 是首尾净差、`$it` 只取首个命中。补跨窗口解析与 `$steps[N]` 引用。

**第二梯队（精度/体验，成本低）**

3. **observe 594 → 360ms**：先补分段计时（骨架/窥视/可见行/编译各占多少），再决定是否值得动；没有拆分前不定方案。
4. **`ops=`（可选）**：渲染支持但**当前无数据源**（5 窗口浅读 111ms > 80ms 红线）；只有在预算放宽或窗口浅读变便宜时才做，否则维持"只给窗口清单"。
5. **P2-1 的真机成功率复测**：给下拉链补一个真机数字（当前只有实现与单测）。

**第三梯队（后置/已判定不做）**

6. **坐标点击兜底**：已判定后置——三个真实失败（下拉、孪生、空壳）都能在 UIA 内解决，且会破坏"零坐标、零视觉"不变量。
7. **事件订阅**：已判定降级——签名探测已把无变化路径做到 0 全量快照；事件只剩"去掉 40ms 静默"，且需要独立 COM 线程/PropertyChanged handler。
8. **键盘输入超时缩放**：`keyboard.py` 仍是每字符 0.5s 固定预算；只在"无 Value 且剪贴板失败"的第 3 条路由触发。
9. **MCP handler 线程化**：`mcp_server._make_call_tool` 仍是同步调用；单 agent 串行下无收益。
10. **宏录制/重放**（原 P3-4）：未做；收益取决于任务重复率，仍缺数据。
11. **`lean`/`sig` 档**：保留为实验开关，不作为默认。

### 未做 / 已知限制（单列，避免"看起来都做了"）

| 限制 | 现状 | 为什么现在不做 |
|---|---|---|
| **虚拟化列表未 realized 行的 FIND 回退** | 未 realized 的行没有节点/ref；`FIND "Item 600"` 只能 0 命中或 `STALE_TARGET`。`eba3926` 的"单次全量升级"只对**部分但未截断**的小树生效，**截断集合永不升级** | 需要 provider 侧按名查询 / 滚动分页（viewport §4 R3）；这是大列表剩下的核心缺口，排第一梯队 |
| **`run` 跨窗口** | 跨窗口流程必须显式写窗口 ref；`delta` 是脚本首尾净差；`$it` 只取首个命中 | 补跨窗口解析与 `$steps[N]` 引用会改 `run` 语义，留到下一轮 |
| **`ops=`** | `projection/engine.py` 保留渲染分支，但 `Session._desktop_summary` **不产生该字段** | 统计可操作元素需要每窗口一次浅读：5 窗口合计 **111ms**（form 窗口 43ms），超过 observe 的 **~80ms 红线**；没有数据源就不能印 |
| **"写入首帧确认"方案** | **暂缓**：没有用"写入后的首帧"替代 settle 后的效果确认 | 标题/状态可能在动作返回后几毫秒才落地（记事本标题的 `*` 约 8ms 后出现，`326d2ec` 因此把刷新移到 settle 之后）；首帧判定会读到中间态，也会把重命名漏一 CALL。正确性优先，保持 settle 后判定 |
| **healing / 重试链** | 最坏 **3 个纠正步骤**，每种只做一次：① 同窗唯一匹配的陈旧 ref 重定位；② 目标不答时换孪生一次；③ `EFFECT_NOT_CONFIRMED` 时换下一条 route 一次（另外允许一次普通重编译，不算纠正） | 不做循环、不做多轮重试：一次 CALL 内的纠正是有界的，失败就如实报原错（`session.py` 的 `call` 注释与 `_retry_unconfirmed`） |
| **大树"有变化"的 CALL** | 本轮仍**没有数字**（只测了无变化路径 686–706ms）；旧预期 3.1–3.6s 未复核 | 唯一未被实测覆盖的主路径；不给推断数字 |
| **ComboBox 真机成功率** | 已实现 `+select` / `SelectionItem` 首路由 / 回读 / `activate` 升级，但**没有真机成功率数字** | "0%→90%+"仍是旧目标，未以数字确认 |
| **observe 594ms vs 360ms** | −82.5% 达成，360ms 未达；没有做分段拆分 | 未拆清"骨架 / 窥视 / 可见行 / 编译"的占比前，不确定是否值得再动 |

---

## 0. 结论先行：三条真实瓶颈

### 瓶颈 1｜每次 CALL 固定付 2–4 次**全树快照**，这是"慢"的第一因
`Settler.wait` 用"连续两帧形状一致 + 150ms 静默"判定稳定（`somnus/runtime/settle.py:78-115`）。**该值是现值**：P1-2 后默认改为 `quiet_ms=40 / poll_ms=10`，且运行时可调。
它的帧数不是 2，而是：

```
帧数 F = 1 + ceil(quiet_ms / (poll_ms + 单帧快照耗时 S))
```

- 小窗口（S≈90ms，quiet=150，poll=50）：F=3，**加 100ms 纯 sleep**；
- 大树（S≈2790ms）：F=2，两帧共 5.5s；
- 极快 provider（S≈0，如 mock 关闭 deterministic 后）：F=4。

**[码+本机计数]** 我用假 provider（无 COM，只计数）跑 `Session.call`：120 行窗口、每帧 90ms → **3 帧、378ms**；1466 行、每帧 1450ms → **2 帧、3179ms**。B 路实测 store CALL 5867–5941ms 中"树抓取 5516ms"，正好是 2×2786ms，与公式吻合。
A-1 说"一次普通 CALL 5~8 秒花在重复抓树上"只在大树成立；小窗口是 3×90=270ms（占 423ms 的 64%）。
A-1 还说"peek ×2 各自全枚举"——**错**，`_peek_sync`（`uia.py:252-280`）在窗口已登记于 `_windows` 时不枚举；每次 CALL 只有 1 次窗口枚举（我的假 provider 计数 `enum=1`）。

### 瓶颈 2｜快照单价由"元素数 × 每个元素请求的属性/模式数"决定，且 CacheRequest 是 **Subtree 级**
`cache.py:16-55` 一次性请求 14 个属性 + 16 个模式，`uia.py:774-783` 在窗口根一次 `BuildUpdatedCache(TreeScope_Subtree)`，再靠 `GetCachedChildren` 逐节点读缓存。
关键在于：**Subtree 级的 BuildUpdatedCache 会先把整棵子树的全部属性/模式取回来**，所以你"走到第 500 个节点就停"并不会减少 provider 端的取数工作。
**[算]** 相反，纯 Python 侧几乎不花时间：1470 节点上 `compile` 39ms、`reconcile` 1.9ms、`index+diff` 1.1ms、`same_shape` 0.9ms、L0 渲染 1.6ms——合计 ~45ms，占 2900ms 的 **1.5%**。所以 2.8s 的 ~98% 在 COM/provider 侧。
结论：想降快照单价，**必须改 cache request 的内容（属性集/模式集）**，而不是在 Python 遍历层截断；A-2/C-2 的"MaxNodes=500 就能封顶 0.8s"机制不成立（详见 §1 冲突 1）。

### 瓶颈 3｜"不准"不是缺视觉，而是三处**静默**
1. 抬升失败被丢弃：`_snapshot_sync`（`uia.py:745`）与 `_peek_sync:267` 调用 `_enter_foreground_sync` 后**不看返回值**。投不出前台 → 自绘窗口发空壳树 → 没有一行告警。
2. 空壳树无信号：`observe` 对 5 个节点的树照常渲染，agent 会把它当成"这应用没控件"。
3. 孪生/下拉：`session.py:936-942` 对同一指纹的多个节点**直接 TARGET_AMBIGUOUS 死路**（而 ref 本身是唯一的，本来可以打）；下拉项缺 `select` 通路与当前值回读（`caps.py:305-313`、`cache.py:16-35` 无 `SelectionPattern.CurrentSelection`），B 路实测 ComboBox 0% 而 pywinauto 137ms 成功。

全库 grep 确认**没有**截图/坐标/OCR/DPI 代码（无 `30001`/`SetCursorPos`/`screenshot`）。这是设计选择，不是缺陷；C 路"无截图故无 DPI 问题"成立，但它不能解释精准率。

---

## 1. 冲突裁决（逐条点名）

### 冲突 1：2.5s 花在哪 / "抓树最快"？
**裁决：B-2 的方向修正对一半，A-1 的量化错，A-2 的自我降级对，但三家都漏了决定性事实。**

| 主张 | 判定 | 依据 |
|---|---|---|
| A-1「每节点 ~17 次 GetCachedPattern 是 2.5s 主因」 | **错（机制）**，位置对 | `_PATTERN_IDS` 16 项（`uia.py:110-127`）+ 5-6 次属性读/节点，确实是 ~22 次/节点；但这些都是**进程内缓存读**。真正贵的是 `AddPattern(16 个)` 让 provider 为子树里每个元素**物化 16 个 pattern 对象**——发生在 BuildUpdatedCache，不在 Python 循环里。[推，需实验 0.3 证实] |
| A-2「这些读是微秒级，应降级」 | **对**（方向）；但它把"模式请求的 provider 侧成本"也算成了微秒级，这是漏项 | 同上 |
| A-2「MaxNodes=500 直接砍掉 2.5s 的根源」 | **错** | Subtree CacheRequest 在遍历前已取全量；本地遍历截断只能省下每节点的 COM 缓存读 + 45ms Python。[码 `uia.py:774-783`；算 §0] |
| B-1「每次调用付 2–3 次全量抓取」 | **对**（精确值 2–4，有公式） | §0 瓶颈 1 |
| B-2「somnus 已是 7 家里树抓取最快档」 | **过度断言，不能作为事实** | B-1 自己给的对照组是「pywinauto 裸枚举 1832ms vs somnus 2933ms」（somnus 慢 60%），B-2 同一任务给 3172ms vs 2933ms。同一份实测内部差 73%，两者都不能当"最快档"的定论。唯一稳定的是 windows2text 9542ms（逐属性）远慢于两者。 |
| B-1「FIND/INSPECT ≈0–4ms 是最大亮点」 | **对** | `CompileResult.find` 线性扫描 + 命中少，[算] 1470 节点的 map 构建+diff 才 1.1ms |

**定论**：慢 = 帧数（settle 策略）× 快照单价（provider 侧物化量）。批取（Subtree）已存在且优于逐属性读取，不必再"引入批取"；下一步要动的是**帧数**和**cache request 内容**。

### 冲突 2：接通事件订阅能省多少？
**裁决：A-1 的 5s→0.3–0.6s 错一个数量级；C-2 修正后的 1–1.5s 仍然乐观；B-2 的"settle 100→30ms、小窗 CALL 400→300ms"量级最靠谱，而且它只是"睡眠那一段"。**

代码路径决定的硬事实：
- 事件计数器 `_event_count` 自 `session.py:160` 初值后再无写入，`subscribe` 从未被调用（`uia.py:1094-1105`），`make_handler` 只实现了 StructureChanged（`events.py:45-64`），没有 PropertyChanged——状态翻转（QQ 发送键禁用→可用）根本不会计数。
- 即使接上：`settle.py:89-94` 里"事件到达就重置 `last_change`"。**只要第一帧抓取（2.8s）期间来了任何一个事件，就必然再抓一帧**，也就是仍然 2 帧 = 5.5s。事件只有在"抓取期间零事件"时才省一帧，而且是概率性的。
- 而且一次 CALL 至少要保留**一帧落地快照**（settle 返回的树就是本次编译的树）。大树落地帧 2.8s 无法被事件省掉。
- 接线真实成本被三家低估：需要独立 COM 线程/消息泵 + PropertyChanged handler + 管道坏死重建（C-2 引 Windows-MCP WatchDog 是对的）。`events.py` 现在返回的是裸 `COMObject`，不是"纯接线"。

**定论**：事件订阅不是第一枪。按代码路径，大树呼叫下界仍是 1 帧 ≈ 2.9s；要让 CALL 真正低于这个数，必须让"确认帧"变成**便宜的签名探测**（§3 P1-1），或让 cache request 变窄（§3 P0-1）。

### 冲突 3：C「无截图/坐标/DPI 问题」、A「raise 失败被吞 → 空树」、B「Steam 未渲染 5 节点」
**裁决：三条指的不是同一件事，但确实是同一类后果。**

- C 成立且可验证：全库无截图/坐标/DPI 路径，所以"没有坐标偏差"是真的；但它只说明"我们没走那条路"，不能推出"精准率没有感知问题"。
- A 成立且可验证：`uia.py:745`（快照）与 `267`（peek）丢弃 `_enter_foreground_sync` 的 `FOREGROUND_DENIED`，空壳树带着"成功"返回。
- B 的现象成立，但成因不同：B 是**故意不提前台**拍快照得到的 5 节点；生产路径会 raise（B 自己也写了"运行时强制提前台有效"）。所以 B 复现的是"未渲染 → 空壳"这一类现象，而 A 指出的是生产代码里唯一会把它变成"静默空壳"的路径（前台锁拒绝时）。

**定论**：精准率问题 #1 是"**静默的不完整**"，不是"缺视觉"。修法是把 raise 结果与"节点数骤降"上浮到观察结果（§3 P2-3），不需要引入视觉层。

### 冲突 4：D「慢的主因是回合数」vs B「批量 3 步 398ms ≈ 单步价格」
**裁决：B 的实测对，D 的结论半对；D 的推理漏了"固定税"这个词。**

**[码+本机计数]** 用同一个假 provider：
```
单步 select（帧 90ms）  snapshots=3 enum=1 exec=1  wall=378.0ms
批量 3 步              snapshots=3 enum=1 exec=3  wall=379.5ms
批量 10 步             snapshots=3 enum=1 exec=10 wall=377.1ms
```
即：**每多一步的边际成本只有一次注入（~5ms）**；固定税（3 帧快照 + 1 次枚举）在批量里只付一次。所以：
- B 的"批量几乎免费"成立 → 批量是目前**零代码成本**的最强回合削减手段（但提示词只在一段话里提了它，`tools.py:82-107` 的 call 描述是唯一入口）；
- D 的"主因是回合数"只有在"每回合都要付固定税"时成立：回合 × 固定税 = 真正的痛。store 场景 5.9s/回合，5 步工作流单步执行是 29.5s，批量是 5.9s + 注入——**这正好说明固定税和回合数是乘法的两端，先削固定税，再削回合**；
- 但批量的三个硬限制（`session.py:1428-1446`：不能含 `wN.focus()`、不能含 `context_menu`；所有 ref 必须在批开始时存在）恰好卡住最常见的菜单/对话框流程。D 的脚本化 `run` 是对的，应排在固定税优化之后。

---

## 2. 成本模型（后面所有数字都基于它）

```
OBSERVE(n)  = E + S(n) + C(n)
CALL(n)     = E + F·S(n) + sleep + I + C(n) + P·(确认帧数)
BATCH(k,n)  = E + F·S(n) + sleep + k·I + C(n)
F = 1 + ceil(quiet_ms / (poll_ms + S(n)))
```

| 符号 | 含义 | 实测值 | 来源 |
|---|---|---|---|
| E | 一次窗口枚举 | 85–121ms | [实] B |
| S(小) | 小窗口一次全量快照 | 72–197ms（取 90） | [实] B |
| S(1466) | 大树一次全量快照 | 2786–2933ms（取 2790） | [实] B |
| C(小) | 编译+reconcile | 0.8–5ms | [实] B + [算] |
| C(1470) | 编译+reconcile | 34ms（B）/ 41ms（本机合成） | [实]+[算] |
| I | 一次注入 | 3–50ms | [实] B |
| F | settle 帧数 | 2 / 3 / 4（现值 150/50 下） | [码+本机计数] |
| sleep | settle 纯睡眠 | 0–150ms（现值；P1-2 后默认 0–40ms） | [码] |
| P | 一次"签名探测" | **未知，目标 0.1–0.3·S** | [推]，实验 0.3 测 |

校验：小窗口 100 + 3×90 + 100 + 5 ≈ 475（B 实测 423）；大树 95 + 2×2790 + 5 + 34 ≈ 5614（B 实测 5867）。模型可用。

---

## 3. 可执行清单（按实施顺序，含依赖）

> **读法提醒**：本节保留的是**实施前**的行号与"预期"。本轮大改后 `session.py`/`uia.py`/`settle.py` 行号已全部漂移（见 §9 第 7 条），**定位请以函数名/符号为准**；每条的"本轮结果"是实际发生的事实。

**顺序总览**

```
阶段 0（先能测）      ✅ P0-0 仪表 → ⚠️ P0-1 六档已实现，但收益与噪声同量级
阶段 1（削固定税）    ✅ P1-1 签名探测 → ✅ P1-2 设置项/加载提示 → ✅ P1-3 窗口枚举开关
阶段 2（提准）        ✅ P2-1 下拉 → ✅ P2-2 孪生 → ✅ P2-3 空壳上浮 → ✅ P2-4 状态词
                      → ✅ P2-5 投影去噪 → ✅ P2-6 registry 隔离 → ✅ P2-7 效果重试（一次）
阶段 3（削回合）      ✅ 提前：P3-1 FIND、⚠️ P3-2 桌面摘要（无 ops）、✅ P3-3 run（`30412a8`）、❌ P3-4 宏
阶段 4（可选/不做）   事件订阅、键盘超时、MCP 线程、坐标/视觉、500 节点预算：本轮均未动
```

> 排序依据：`Δ(小窗口 CALL ms)` / `Δ(大树 CALL ms)` / 成本（S≈≤50 行、M≈50–200 行、L≈>200 行或新语法）。
> 所有"预期"都标注了等级；**[推]** 的数字必须在阶段 0 的仪表上复核后才能对外承诺。

### 阶段 0｜先能测（否则下面每一项都无法验收）

#### P0-0 加计时与帧数仪表
- **文件/函数**：新增 `tests/runtime/test_settle_frames.py`（用 `MockProvider` 子类计数 `snapshot`/`list_windows` 调用）；`bench/run.py` 增加墙钟列（现在只跑 mock，`bench/run.py:1-15` 的指标里没有任何时间）。
- **改法**：
  1) 测试里断言"单步 CALL 的 snapshot 次数 == 期望 F"，用假的 `S` 参数化 0/20/90/1450ms（可直接照抄本文件 §1 冲突 4 的计数方式）。
  2) `bench/run.py` 输出 `enum_ms/snapshot_ms/compile_ms/settle_ms`。
  3) 在 `uia._snapshot_sync`（`uia.py:699-763`）临时加 `perf_counter` 分段：`BuildUpdatedCache` / `_build_node` 递归 / `_attach_tray_overflow`，用 `SOMNUS_PROFILE=1` 打开。
- **预期**：确认大树 2.8s 在 `BuildUpdatedCache` 与"逐节点缓存读"之间的真实比例（这是全案唯一的关键未知量）。
- **验证**：`pytest -q tests/runtime/test_settle_frames.py`；真机 `SOMNUS_PROFILE=1 python -m somnus.cli windows` 或跑 scenario-suite。
- **本轮结果**：✅ 已落地。`test_settle_frames.py`（512 行）+ `bench/run.py` 墙钟列 + `SOMNUS_PROFILE` 分段（`provider.timing`）。**但 observe 仍没有分段拆分**，这正是 594 vs 360ms 无法归因的原因。
- **依赖**：无。**成本 S。**

#### P0-1 变量实验：cache request 瘦身能省多少
- **文件/函数**：`somnus/providers/windows/cache.py:16-55`（`CACHED_PROPERTIES`/`CACHED_PATTERNS`）、`cache.py:58-79 build_cache_request`。
- **改法**：用环境变量或构造参数支持三档探针：`full`（现状 15 属性 + 16 模式，含 `BoundingRectangle`）、`lean`（去掉 4 个从未被消费的模式 `selection(10001)/grid(10006)/tableitem(10011)/table(10012)`）、`sig`（`SIGNATURE_PROPERTIES`：control_type/name/value + 全部状态源属性，外加 `value` 模式）。**实际实现最终是六档**：`full/lean/sig/peek/rect/probe`，由 `SOMNUS_CACHE_TIER` 选择。
- **预期**：`lean` 在 1466 节点上未知，可能 −15%~−35%（[推]）；`sig` 目标 0.1–0.3·S（280–840ms，[推]）。
- **验证**：P0-0 的分段计时 + 场景树同一个窗口重复 10 次取中位数。
- **本轮结果**：⚠️ **假设未证实**。六档 `full/lean/sig/peek/rect/probe` 已实现（`SOMNUS_CACHE_TIER` 可 A/B），但同窗口重复测量下 `lean`/`sig` 与噪声同量级；默认仍 `full`。原"大树 −30%+"的推断被否定，收益实际来自帧数与视口采集。
- **依赖**：P0-0。**成本 S。**

---

### 阶段 1｜削"每次 CALL 的固定税"（收益最大、风险最低）

#### P1-1 settle 改为"签名探测驱动"，默认不再抓第二帧全树
- **文件/函数**：`somnus/runtime/settle.py:51-115`（`Settler.wait`）、`somnus/runtime/session.py:1003-1010` 与 `1526-1535`（两处调用）、`somnus/providers/windows/uia.py:489-495`（新增 `snapshot_signature`）、`somnus/providers/base.py:60-65`（协议加可选方法）。
- **改法**：
  1) `Settler.wait` 增加 `previous: RawTree | None` 与 `probe: Callable[[], str] | None`；
     循环改成：`if probe and previous: sig = probe(); if sig == _sig(previous.root): return previous, settled`；不同则 `tree = fetch()`（一次全量），把 `previous=tree` 再循环直到 quiet。
     这样"无变化"= **1 次探测、0 次全量**；"有变化"= **1 次全量 + 1 次探测**（原来 2 次全量）。
  2) `Session` 保存最近一帧与它的签名（`self._last_raw` / `self._last_sig`），在 `_recompile`（`session.py:294-327`）和 `call` 的编译点（`session.py:1025-1028`）更新。
  3) 探测的字段契约**必须与 `_same_shape` 完全一致**（role/name/value/child-count/states，`settle.py:118-145`），且 states 必须含 `disabled`（QQ 发送键那条注释就是为此）。做成一个表驱动常量，两处共用它的文档化清单，并加测试防漂移。
- **预期**（[算]，以模型校验过的参数；`post_action_enum` 默认开启，故含一次枚举）：
  - 小窗口无变化 CALL：475 → **~110–150ms**（1 次探测 + 枚举；`post_action_enum=false` 时 ~10–30ms）。
  - 小窗口有变化 CALL：475 → **~250–290ms**（1 全量 + 1 探测 + 40ms 静默；关枚举后 ~150–170ms）。
  - 大树无变化 CALL：5614 → **~0.4–0.9s**（1 次探测 + 枚举）。
  - 大树有变化 CALL：5614 → **~3.1–3.6s**（1 全量 + 1 探测）。
  - 若 P0-1 的 `sig` 档真能到 0.1·S，大树有变化可到 **~3.1s**，且 `lean` 档若真能砍三分之一快照成本，可到 **~2.4s**。
- **验证**：P0-0 的帧数测试；真机 `pytest -m windows tests/e2e/test_windows_scenarios.py -k "store or form or chat"`，对照 `settled=true` 与 delta 是否与改动前一致（尤其 QQ/表单的 disabled→enabled 翻转）。
- **本轮结果**：✅ 已落地，形态与计划基本一致（`Settler.wait(previous=, previous_signature=, probe=, signature=)`）。实测无变化 CALL **~6s → 686–706ms**、探测 **1622–1938 → 384–404ms**、**0 次全量**；小窗 CALL **215–231ms**（高于"无变化 110–150ms"的预期 1.5–2×，仍是 200ms 级）。实现比计划多了一条保护：`previous_signature` 必须是**编译前**读到的签名（编译会原地规范化节点，否则永远判"变了"）。**大树"有变化"的 CALL 本轮未测。**
- **依赖**：P0-0、P0-1。**成本 M。风险：签名实现与 `_same_shape` 漂移 → 过早判定 settled；必须用状态翻转用例守住。**

#### P1-2 settle 参数：默认 40ms/10ms，**运行时可调 + 加载时可见**（已定，不再写死）
- **文件/函数**：
  - 默认值：`somnus/runtime/settle.py:33-49`（`Settler.__init__`）
  - 启动读取：`somnus/config.py:78-86`（`_KNOWN_KEYS`）、`113-135`（`Settings`）、`177-204`（`_apply_file`）、`207-222`（`_apply_env`）、`240-273`（`render_toml`）、`137-150`（`render`）
  - 注入会话：`somnus/protocol/mcp_server.py:26-56`（`build_session`）、`somnus/cli.py` 的 `build_session`
  - 会话状态：`somnus/runtime/session.py:145-147`（构造 `Settler`）、`156-184`（新增 `self.post_action_enum` 等字段）、新增 `Session.apply_settings()/settings_brief()`
  - 加载时提示：`mcp_server.py:89-105`（`make_mcp_server`）
  - 系统提示词：`somnus/protocol/system_prompt.md:8-14`（工具表）+ 新增 `## Settings` 段
  - 工具面：`somnus/protocol/tools.py:21-131`（`TOOL_SPECS`）、`134-184`（`call_tool`）
- **改法（设置模型）**：

| 键 | 默认 | 范围 | 含义 / 取舍 |
|---|---|---|---|
| `settle_quiet_ms` | **40** | 10–1000 整数 | 判定"没再变"的静默窗口。调小→更快，但动画/异步重绘未结束时会读到中间态（可能出现"禁用按钮"误判）；调大→更稳，但每次 CALL 多等这么久。 |
| `settle_poll_ms` | **10** | 1–100 整数 | 探测间隔。调小→判定更细；调大→等待粒度变粗。 |
| `settle_timeout_s` | **3.0** | 0.5–30 浮点 | settle 总上限；到点返回 `settled=false` 但照常给 delta。 |
| `post_action_enum` | **true** | 布尔 | 动作后是否重枚举窗口（用于报"动作顺手打开了新窗口"），见 P1-3。 |

  - **作用域**：会话级（当前 `Session` 对象）。修改只影响**后续**的 `call/observe/find/call_batch`，正在执行的那一次不受影响（`Settler.wait` 每次进入时读字段，直接改对象即生效）。
  - **持久化**：默认**不持久化**；`configure(persist=true)` 时才写入与 `config.load()` 同一个 `config.toml`（全局，供后续进程继承）。这样模型能按当前应用调参，又不会把一次实验永久写死。
  - **优先级**：内置默认 < 配置文件 < 环境变量 < `configure`（运行时最高，直到进程结束）。`reset=true` 回到"启动时的有效值"（文件+env 的结果）。
  - **非法值**：全有或全无校验；任一键非法则**一个都不改**，返回 `ERR INVALID_ARGUMENT key=settle_quiet_ms expected int in 10..1000 got=-5; nothing changed`。不做字符串转数字（`"40"` 也拒绝，避免静默放大窗口）。
  - **加载时提示（两处，保证"一加载就知道默认值和取舍"）**：
    1) **MCP `Server(instructions=...)`**（`mcp_server.py:89-105`）：由当前生效值动态生成，客户端 initialize 时只下发一次，不占每轮 token。文案示例：
       `Settings in this process: settle_quiet_ms=40 settle_poll_ms=10 settle_timeout_s=3.0 post_action_enum=on. settle_quiet_ms = how long the UI must stay unchanged before an action counts as done: smaller = faster calls, too small can read an animation mid-flight; settle_poll_ms = probe spacing; post_action_enum=on reports a window the action opened (off saves ~100ms/call and can hide it). Change with configure({...}) — session scope; persist=true also writes the global config.`
    2) **`system_prompt.md`**：工具表加 `configure` 一行 + `## Settings` 三行（默认值 + 一句取舍 + "调小更快/更易误判"）。
    - 为什么是这两处：MCP `instructions` 在 `initialize` 响应里下发、宿主通常直接注入系统上下文，**只发一次、不占每轮 token**，是"一加载就知道"的最早且最稳的通道；`system_prompt.md` 覆盖文本协议宿主（`examples/agent.py` 读它）。工具描述每轮都重发，只在 `configure` 自己的描述里放一句"改什么、默认多少"，不重复长文案。
  - **可改入口（最小实现）**：新增 MCP 工具 `configure`，`inputSchema = {settle_quiet_ms?, settle_poll_ms?, settle_timeout_s?, post_action_enum?, persist?, reset?}`，返回一行生效值：
    `SETTINGS quiet_ms=40 poll_ms=10 timeout_s=3.0 post_action_enum=on scope=session`
    （`persist=true` 时追加 `persisted=<config path>`）。`call_tool` 里一个分支，调用 `Session.apply_settings(...)`；写盘用 `config.load()→改字段→config.save()`。`examples/agent.py` 与 MCP 都经由 `TOOL_SPECS`/`call_tool`，因此一处实现全部宿主可用。
- **预期**：[算] 小窗口有变化 3 帧→2 帧，再省 ~180ms；大树无影响（2 帧已是最小）。**必须和 P1-1 一起上**，单独改只是把睡眠从 100ms 降到 40ms。
- **本轮结果**：✅ 已落地。四个键 + 优先级 + `configure` + MCP `instructions`；提示预算收尾后 **2639/2700**（`run` 刚进 prompt 时 2695，确认门控文案移除后降到 2639）。`persist=true` 走 `Session._persist_settings → config.persist_session_settings`；`tools._configure` 保留 `NotImplementedError` 兜底。
- **偏差**：计划里的 `tests/protocol/test_configure_tool.py` 实际落在 **`tests/test_settings.py`**（466 行）；预算余量 61 token，后续加文案仍应先减字。
- **验证**：
  - `tests/protocol/test_prompt_budget.py` 会强制"每个 tool 名出现在 system_prompt 里"，且总预算 ≤2700 token（`test_prompt_budget.py:24,38-41`）——加 `configure` 与 3 行说明后必须仍在预算内。
  - 新增 `tests/protocol/test_configure_tool.py`：默认值回显、非法值不改、`reset`、`persist` 往返、`quiet_ms` 改动后下一次 CALL 的帧数变化。
  - 扩展 `tests/runtime/test_config.py`：新键的环境变量/文件/坏值三条路径。
  - 真机："状态翻转用例连跑 20 次无 TARGET_DISABLED 误判"（quiet 40 的门槛）。
- **依赖**：P1-1（否则降 quiet 只是省睡眠）。**成本 S–M。**

#### P1-3 动作后窗口枚举：**默认开启、可开关**（已定）
- **文件/函数**：`session.py:999`（`call` 的 `refresh_windows`）、`session.py:1527`（`call_batch`）、`session.py:188-192`（`refresh_windows`）、`uia.py:1259-1277`（`_process_name`）。
- **改法**：
  1) 新增会话字段 `post_action_enum: bool = True`（`session.py:156-184`）。`call`/`call_batch` 处写成 `if self.post_action_enum: self.refresh_windows()`；`session.py:1015` 的 `KeyError` 恢复路径（目标窗口消失）**保持无条件枚举**，因为那是正确性路径。
  2) `_process_name` 加 `functools.lru_cache(maxsize=256)`（pid→名在会话内稳定），`_add_window`（`uia.py:399-478`）在多窗口枚举里少掉每个窗口的 `OpenProcess`。
  3) 加载提示与开关同 P1-2 的 `configure`：说明"默认开启；关掉每次 CALL 约省 100ms，但动作顺手打开的新窗口不会出现在回复里"。
- **预期**：默认开启 → 收益仅来自 `_process_name` 缓存（[推] 20–40ms/次）；显式 `post_action_enum=false` 时 [实] 省 85–121ms/次 CALL。
- **本轮结果**：✅ 已落地，默认开启；`_process_name` 已加 `functools.lru_cache(maxsize=256)`。真实收益没有单独测（本轮所有 CALL 数字都是在默认开启下取得的）。
- **验证**：`tests/runtime/test_window_refs.py`、`test_window_switching.py`；真机确认默认开启时"activate 打开了对话框"的窗口 delta 仍在；显式关闭时该窗口 delta 不再出现（这是已声明的预期，不是故障），且下一次 `observe` 仍能看到新窗口。
- **依赖**：无（可并行）。**成本 S。**

---

### 阶段 2｜提"精准率"（动作成功率优先，不做安全机制）

#### P2-1 下拉/列表选择链：`select` + 回读 + 升级
- **文件/函数**：`somnus/compiler/caps.py:305-313`（`select` 授予条件）、`caps.py:53`（`_TEXT_ENTRY_ROLES` 含 `select` 导致 ComboBox 误发 `set_text`）、`somnus/providers/windows/cache.py:16-35`（缺 `SelectionPattern` 的当前值回读）、`executor.py:66-71`（`select` 执行）。
- **改法**：
  1) `select` 授予补上 `role == "select"` 与"容器 `selection` 模式"；
  2) 快照时对 `role == select` 的节点额外读一次 `SelectionPattern.CurrentSelection`（或 `SelectionItem.IsSelected`），写进 `attrs`，让 delta 能显示"低→高"，让 Verifier 有可验证字段；
  3) capability 的 `via` 链：`expand` → 子项 `select` → 回读校验 → 不符则 `invoke`（**不做坐标点击**，保持"零坐标"不变量；pywinauto 的 `click_input` 兜底不搬）。
  4) `_TEXT_ENTRY_ROLES` 去掉 `select`（ComboBox 不是自由文本输入；它现在的 `set_text` 是谎报）。
- **预期**：[实] B 测 ComboBox 0/2 路径，pywinauto 137ms 成功 → 目标 **0%→90%+**；附带消灭"虚假 set_text"引发的无效回合。
- **本轮结果**：✅ 已实现（`+select`、`SelectionItem` 首路由、选中值回读、`activate` 升级；`_TEXT_ENTRY_ROLES` 已去掉 `select`）。单测齐全，但**本轮没有给出真机成功率**，"0%→90%+"仍是旧目标、未以数字确认。
- **验证**：`tests/e2e/test_windows_scenarios.py` 新增下拉用例（scenario-suite 已有 store/form/chat 用例可挂）；`pytest -m windows`。
- **依赖**：无（可与阶段 1 并行）。**成本 M。**

#### P2-2 孪生/歧义不再死路：按 ref 打，失败再试孪生
- **文件/函数**：`session.py:936-942`（提前 raise）、`session.py:306-308`（`_ambiguous_refs` 构造）、`runtime/registry.py:200-201`（`candidates_for`）、`uia.py:1057-1084`（`_find_by_native` 的 AutomationId 回退无校验）。
- **改法**：
  1) 删掉"ref 属于歧义集合就 raise"的前置判断——该 ref 在本次编译里指向**唯一节点**，`resolution` 里有它的 `NativeRef`，可以先打；
  2) 打不通（`ACTION_NOT_SUPPORTED` / `STALE_TARGET`）时，用 `registry.candidates_for(fingerprint)` 取另一个孪生 ref 重试一次，成功则在响应里注明 `healed ref→ref2`；
  3) `_find_by_native` 的 AutomationId 回退：命中后校验 `control_type` 与 `name` 至少一项与 ref 一致，否则视为未命中（避免拿到"第一个同名"）。
- **预期**：B 的 F1（下拉项孪生 `activate` 全拒）与"幻影孪生"从"必然 ERR + 一次重规划"变成"一次 CALL 命中"；每处失败省 1 个回合。
- **本轮结果**：✅ 已实现，但形态比计划更严格：只有**可证明是同一元素**时才折叠，否则保留并标 `twin=true`；失败时 `_execute_with_twin` 原地重试孪生一次。`_find_by_native` 的 AutomationId 回退校验也补了。
- **验证**：`tests/runtime/test_ambiguous_collapse.py` 扩展；真机 ComboBox/Explorer 双窗口。
- **依赖**：P2-1（同一个下拉场景）。**成本 S–M。**

#### P2-3 空壳/抬升失败上浮
- **文件/函数**：`uia.py:745`（丢弃返回值）、`uia.py:267`（peek 同样丢弃）、`session.py:539-658`（`observe` 输出）、`session.py:1102`（call 的 body）。
- **改法（实际实现）**：`UiaProvider` 记录 `last_foreground_error`（如 `FOREGROUND_DENIED`）；`Session._observation_warnings` 在 observe 头部输出两类行：`~ foreground denied (…); tree may be empty for covered windows`，以及**窗口有标题且根节点 `children == 0`** 时的 `~ tree looks empty; window may be covered or app not rendering`。原计划里的"节点数较上次骤降 20%"没有做——实际用的是更严格、更少误报的空树判据。
- **预期**：把"5 节点 = 没控件"的误判从"必然"降到"有提示"，直接减少 B-F6 类错误结论与随后的错误规划。无速度收益。
- **本轮结果**：✅ 已实现（`test_observation_warnings.py`）。真机 Steam 后台窗口场景未复测，但两条文案与触发条件已由构造用例覆盖。
- **验证**：真机 Steam 后台窗口（B 的复现步骤）；单测伪造 raise 失败断言该行出现。
- **依赖**：无。**成本 S。**

#### P2-4 状态词归真（只改结果语义，不做任何策略）
- **文件/函数**：`session.py:1099-1171`（`status` 决策）、`verifier.py:45-63`。
- **改法**：`verify.status == OK_UNVERIFIED` 且 `settled=True` 且 `delta` 非空时 → `OK`（现状只有 delta 为空时才归真，所以"真的改了但没声明 effects"永远是 UNVERIFIED，B 的"语义通胀"由此而来）。
- **预期**：消除"已经成功却报 UNVERIFIED → agent 重试"的回合（B 的 F4/F5 与"set_text 值已变仍 UNVERIFIED"）。
- **本轮结果**：✅ 已实现（`test_verifier_status.py`）：有可见 delta 且 `settled` 时归真为 `OK`。
- **验证**：`tests/runtime/test_batch_call.py`、`test_session.py` 的状态断言；golden 更新。
- **依赖**：无。**成本 S。**

#### P2-5 投影噪声：给"带子节点的 pane/group"不再发 `set_text`
- **文件/函数**：`caps.py:182-214`（`_is_text_entry`，尤其 212-213）。
- **改法**：`if "value" in node.patterns: return node.role in (...) and not node.children`（有子节点的 pane/group 是容器，不是输入面）。同时删掉死表 `ROLE_DEFAULT_CAPS`（`caps.py:21-38`，全库无引用）。
- **预期**：消灭 B 观察到的"每个窗口根部幻影 `pn1 +set_text(text)`"，减少 agent 误打根节点的一轮。
- **本轮结果**：✅ 已实现：`_is_text_entry` 对 `pane/document/group/unknown` 要求 `not node.children`；死表 `ROLE_DEFAULT_CAPS` 已删。
- **验证**：golden + 真机 observe 对比行数/token。
- **依赖**：无。**成本 S。**

#### P2-6 registry 按窗口隔离，杜绝跨窗口实例重绑
- **文件/函数**：`registry.py:52-165`（`SessionRegistry` 头部注释写着 "for one session/window"，但 `session.py:148` 建的实例在切窗口后继续用）、`session.py:223`、`session.py:585`（target 变更处）。
- **改法**：`by_fingerprint` / `records` 的键改成 `(window_id, fingerprint)`；或者 target 变化时清空 `by_fingerprint` 并把旧 ref 标记为 `last_seen=0`（保留记录用于 STALE 提示）。后者 10 行。
- **预期**：B-F5（关掉 form 重开，旧 `q1` 静默写到新实例）从"静默成功"变成"STALE_TARGET + 旧节点描述"，避免一次错误写入与后续重试。
- **本轮结果**：✅ 已实现：`SessionRegistry` 的指纹键改为 `(window_id, fingerprint)`，`candidates_for` 支持按窗口收窄（`test_registry_window_scope.py`）。
- **验证**：`tests/runtime/test_window_switching.py` 构造"同进程同标题新 hwnd"；真机关窗重开。
- **依赖**：无。**成本 M。**

#### P2-7 失败原地重试一次（不新增回合）
- **文件/函数**：`session.py:1073-1079`（`EFFECT_NOT_CONFIRMED` 直接 raise）、`session.py:936-942`（`TARGET_AMBIGUOUS` 直接死路）、`session.py:1723-1755`（`_route_memory` 已有替代路由）。
- **改法（实际实现）**：`_retry_unconfirmed` 只在链上还有未试过的 route 时行动，对**同一 ref** 再执行一次；成功返回 `OK ~ retried via <primitive>`，否则回到原错误。不做重定位、不做多轮重试。
- **预期**：把"一次失败 = 一整轮 observe/find/call"压成"一次 CALL 内的一次重试"；命中率取决于 pack 覆盖率，[推] 中等。
- **本轮结果**：✅ 已实现。`EFFECT_NOT_CONFIRMED` 时对**同一目标**换下一条尚未试过的 route 重试**一次**；成功返回 `OK ~ retried via <primitive>`，仍失败抛原错。真机未触发（首条路由都生效），由 `test_effect_retry.py` 的构造用例保证。孪生重试（P2-2）仍独立存在。
- **验证**：`tests/runtime/test_capability_routing.py`；`tests/scenarios/test_store.py` 的加购效果校验。
- **依赖**：无。**成本 S。**

---

### 阶段 3｜削"回合数"（**P3-1/P3-2 已提交；P3-3 随 `30412a8`；P3-4 未做**）
> 原计划把这一阶段押后到阶段 1 验收后。实际实现方在阶段 1 落地的同一轮里把 P3-1/P3-2 做了，P3-3（`run`）随后一起进 `30412a8`。阶段 1 的真机验收数字已拿到（见 §0），因此提前启动不构成风险。

#### P3-1 FIND 带 timeout（把"等菜单出现"从一轮变零轮）
- **文件/函数**：`Session.find(action, window=None, timeout_s=None)`（`session.py`）、`sui/parse.py` 的 `parse_find`、`tools.py` 的 `find` 分支（`timeout` 参数）。
- **改法**（实际实现，已与计划不同）：**每轮重读视口采集**（结构 + 屏上行），命中即返回；不是"settle 一次再编译"——实测视口重读只比探测便宜约 10%，且不能直接用于编译，因此按视口轮询更划算。`timeout_s=None/0` 保持原来的一次内存搜索（0–4ms）。
- **预期**：菜单/对话框流程每次省 1 个"observe→找"回合。[推] 端到端 −1–10s/流程。
- **本轮结果**：✅ 已落地。异步菜单 **140–172ms 命中**；`timeout=1.0` 约 **1010ms** 返回；大树 `timeout=3.0` 约 **3.1s**（等待成本随 timeout 线性）。视口截断的集合**不会**用全量快照重试（避免 3.9s 级重读）。
- **验证**：`tests/runtime/test_find_wait_and_desktop_summary.py`、`tests/e2e/test_windows_find_wait.py`。
- **依赖**：阶段 1（否则每次等待又是 3.5–5.6s）。**成本 M。**

#### P3-2 observe 默认带桌面摘要
- **文件/函数**：`Session.observe` / `Session._desktop_summary`、`projection.engine._desktop_summary_line` / `_desktop_segment`。
- **改法**（实际实现）：默认（未显式要求"其它窗口"时）在观察首行输出 `WINDOWS w1 "QQ" | w2 "设置" | w3 "..." active`，正文仍是目标窗口 L0。**`ops=` 未实现**：需要每窗口一次浅读，5 个窗口合计 111ms（form 窗口 43ms），超过 observe 的 ~80ms 红线。
- **预期**：冷启动从 3 回合降到 1–2 回合；B 的 "420ms 冷启动" 之外主要省的是 LLM 回合。
- **本轮结果**：✅ 部分。成本 **0.002ms**、tokens **+3**；`ops` 无数据源（渲染分支保留）。冷启动回合数未在真机复测。
- **验证**：`tests/projection/test_desktop_summary.py`、`tests/runtime/test_find_wait_and_desktop_summary.py`；golden（观察 token ≤300）。
- **依赖**：无。**成本 S。**

#### P3-3 脚本化 `run`（批量的"逐步解析"档）——**已随 `30412a8` 落地**
- **状态**：✅ 已实现，随 `30412a8`。原计划"押后"已失效，原因见 §0 未达成项 6。
- **文件/函数**：`sui/parse.py` 的 `parse_script`（`CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap()`）、`Session.run_script`、`tools.py` 的 `run` 工具、`sui/grammar.md` 的 `await` 产生式。
- **改法（实际形态）**：纯 `CALL` 脚本保留**全量预校验**；一旦含 `AWAIT` 就改为逐步执行（执行→settle→重编译→解析下一步），失败就地停并报 `RUN PARTIAL steps=k/N failed-at=i`。`AWAIT` 默认 2s、`timeout=0` 为一次搜索。
- **本轮结果**：✅ 真机菜单流程一次 run **453ms** vs 两次 CALL **428ms**——**运行时并不省**，收益是**少一次模型推理**。限制：跨窗口流程需显式 ref；`delta` 是首尾净差；`$it` 只取首个命中。
- **验证**：`tests/runtime/test_run_script.py`（`30412a8` 新增）、`tests/e2e/test_windows_menu_dialog.py`。
- **依赖**：阶段 1 + P2-1/P2-2。**成本 L。**

#### P3-4 宏录制/重放
- **文件/函数**：新增 `somnus/runtime/macro.py`；持久化到 `~/.somnus/macros/`；入口加在 `tools.py`。
- **改法**：录 CALL 序列 + 每步指纹链，重放零 LLM；失败步局部重规划。
- **预期**：命中宏时 [推] 端到端 −80%+（AWM 报 +51% 成功率，D-1 引用）。但工程最大、收益依赖"任务重复率"。
- **本轮结果**：❌ 未做。仍是剩余项（§0 第三梯队）。

---

### 阶段 4｜低收益/可延后（做了也别排前面）

> **本轮状态：整张表都没有动。** 其中"事件订阅/坐标点击"已被判定为不做或后置（见下），其余只是登记。

| 项 | 判定 |
|---|---|
| 真实 UIA 事件订阅（`events.make_handler` + `UiaProvider.subscribe` + `Settler.wait` 的 `event_source`） | **降级为可选**。签名探测已把无变化路径做到 0 全量快照；事件只剩"去掉 40ms 静默"，且需要独立 COM 线程/泵 + PropertyChanged handler（`make_handler` 现在只有 structure）。成本 M，收益 ≤40ms。 |
| `_event_count` / `subscribe` / `NullSubscription` 死代码 | 若不接事件：直接删掉 `Session` 里假的 `event_source` 与 `UiaProvider.subscribe`，避免下一个人误以为它在工作。成本 S，收益 0（但防误判）。**本轮仍未清理。** |
| settle 的 `loading` 检查与 `_same_shape` 遗留 | `loading` 仍是死代码（normalize 只把 `busy→loading`，UIA 从不产生 busy）。成本 S。**本轮未清理。** |
| settle 增量 scope 快照（`UiaProvider.snapshot(scope=...)` 全库无人调用） | **延后**。增量子树合并没有参考实现，且 `registry.reconcile` 与 delta 是全树全局的，局部替换会引入 ref 漂移。本轮视口采集已覆盖大部分收益。成本 L。 |
| FIND 建 ref 索引（`CompileResult.find` 线性扫描） | **不做**。[算] 1470 节点整树索引+diff 才 1.1ms，收益被实测证伪。 |
| T0 局部校验替代 settle（C-1 #2） | **缩水采用**。它只能证明"这个元素变了"，不能提供 delta/FIND 需要的落地树；其价值已由签名探测覆盖。 |
| 把 `_route_memory` 学到的可用原语印到节点行/错误提示 | **可选，低收益**。能省 F3（菜单项 activate 被拒）那一次试错回合，但会增加每行 token；建议只在"同一 ref 第二次出现"时打印。成本 S。**本轮未做。** |
| Selector quality 标签（FlaUI 风格） | **不做**。真正的故障是对应关系（跨窗口重绑，已由 P2-6 修）；"质量标签"只是让事故更易读。成本 M。 |
| observe 的"可操作扁平清单"档（D-2 修正） | **延后**。对 L0 截断后的大树有用，但增加 token；P3-2 的桌面摘要已上线，先看回合是否真的降下来。成本 M。 |
| MCP `_call_tool` 丢进线程（`mcp_server._make_call_tool`） | 单 agent 串行下无收益；只在取消/并发时有意义。**本轮仍是同步调用。** 成本 S，收益 ≈0。 |
| 键盘输入按长度分流/超时缩放（`keyboard._PER_CHAR_TIMEOUT_S`、`executor` 的 `type_text`） | `paste_text` 已是第 2 条路由且 ~37ms 定长，逐字符验证只在"无 Value 且剪贴板失败"的第 3 条路径触发。建议：超时 `3+0.6×len`、验证每 4 字符一次、nudge 总数封顶。**本轮未改。** 成本 S，**收益低**。 |
| 元素遍历预算 500（A-2/C-2） | **不按原方案做**。Subtree CacheRequest 已取全量，走到 500 停不省 provider 成本；并且会让 FIND 对第 501+ 个节点永久 0 命中。若要做，必须配合 `TreeFilter`/子范围快照 + "FIND 0 命中时全量重抓一次"的回退，成本 L，收益未证实。 |
| 坐标点击兜底 / DPI / 截图 / OCR / VLM | **不做**。会破坏"零坐标、零视觉"不变量，且下拉/孪生/空壳三个真实失败都能在 UIA 内解决。**注意：`BoundingRectangle` 已作为视口采集的信号加进缓存（full +6.2%），这不等于坐标点击**。成本 L，收益不确定。 |
| delta 之外再附"受影响区域重投影"（D-1） | **不做**。delta 是差异化优势，别稀释。真实缺口是"可操作扁平清单"，若要加应作为 observe 的独立档位。 |
| 系统级快路径（open_app / shell / 文件） | 属新功能（benchmark #40/#41），不属本次"提速/提准"范围；单列需求。 |

---

## 4. 预期 vs 实测（本轮 `branch test`）

| 场景 | 计划预期（实施前） | 本轮实测 | 判定 |
|---|---|---|---|
| observe store（1467/1470 节点） | ≈360ms（viewport §2.1：44 + 194 + 120） | **594ms（−82.5% from 3385ms），tokens 292 不变** | **未达 360ms**；相对收益已达成 |
| 小窗 CALL（未区分有无变化） | 无变化 ~110–150ms；有变化 ~250–290ms | **215–231ms** | 高于"无变化"预期约 1.5–2×，仍是 200ms 级 |
| 大树 无变化 CALL | ~0.4–0.9s | **686–706ms** | 达成（区间内） |
| 大树 有变化 CALL | ~3.1–3.6s | **未测** | **未复核**（唯一未覆盖的主路径） |
| 大树 签名探测 | 150–300ms | **1622–1938 → 384–404ms** | **未达 150–300ms**；但相对 −76~79% |
| 无变化时的全量快照次数 | 0 | **0** | 达成 |
| ComboBox 选择 | 0% → 90%+ | 已实现 `+select`/首路由/回读/升级；**无真机成功率数字** | 未以数字确认 |
| FIND 等待（异步菜单） | 省 1 回合 | **140–172ms 命中**；`timeout=1.0` ≈ **1010ms**；大树 `timeout=3.0` ≈ **3.1s** | 达成（等待成本按 timeout 线性） |
| 菜单/对话框 4 步流程 | 押后到阶段 3，目标 1 回合 | `run` 已随 `30412a8` 实现：一次 run **453ms** vs 两次 CALL **428ms**，收益为**少一次模型推理** | 达成（运行时不变） |
| 桌面摘要 | 带 `ops` 的一行 | `WINDOWS` 行已默认出现；`ops` **未实现**（111ms > ~80ms 红线）；成本 0.002ms、+3 token | 部分 |
| 桌面签名 vs 枚举 | — | `top_level_signature` **p50 0.31–0.52ms / max ≤2.15ms**；`list_windows` p50 **70–78ms** | 达成（签名不变才跳过枚举） |
| 写入路径（标题不变） | — | 总 **81–104ms** / enum **0.6–0.9ms** | 达成（skips enumeration） |
| 写入路径（标题变化） | — | enum **78–104ms** / 总 **154–238ms** | 与旧实现同量级；修的是正确性 |
| `observe()` 的 enum | — | 仍 **78–102ms**（全量） | 如实记录 |
| 提示预算 | ≤2700 | **2639/2700**（确认门控文案移除后；`run` 刚进 prompt 时是 2695） | 达成（余量 61） |

**读法**：真正的收益来自"减少帧数 + 只采视口"，不是"缩 cache request"；后者（P0-1）被本轮实测否定。`sig`/`lean` 档保留为实验开关，不改变默认行为。

---

## 5. 验证与回归（已落地）

**本轮结果**：非 GUI（默认排除 `-m windows`）**733 passed**；真机 `pytest -m windows` **38 passed**（在子桌面会话内运行，约 **62–66s**，零 `window ... did not appear`）。 `30412a8` 新增 `test_run_script.py` / `test_observation_warnings.py` / `test_effect_retry.py`；后续提交新增 `test_read_capability.py` / `test_active_window.py` / `test_post_action_enum.py` / `test_peer_process.py` / `test_stale_relocation.py` / `test_find_escalation.py` / `test_small_window_adaptation.py`。提示预算测试通过，见 §9 的 2639 说明。

1. **单测**（无需真机）：`pytest -q`（默认排除 `-m windows`，见 `pyproject.toml:44`）
   - `tests/runtime/test_settle_frames.py`（512 行）：参数化快照耗时，断言帧数/快照次数，以及"无变化只探测、零全量"。
   - `tests/test_settings.py`（466 行）：默认值、优先级、非法值全有或全无、`reset`、MCP `instructions` 文案。
   - `tests/runtime/test_viewport_capture.py`（674 行）+ `test_viewport_projection.py`（229 行）：视口采集、回退全量、FIND 重试、`OBSERVE expand`。
   - `tests/runtime/test_precision_integration.py`（415 行）、`test_registry_window_scope.py`（113 行）、`test_verifier_status.py`、`test_dropdown_selection.py`、`test_executor_select_readback.py`、`test_twin_collapse.py`、`test_select_capability.py`。
   - `tests/projection/test_desktop_summary.py`、`tests/runtime/test_find_wait_and_desktop_summary.py`。
   - **约束仍在**：`test_prompt_budget.py` 总预算 2700、且"每个 tool 名必须出现在 system_prompt"；当前 **2639**，新增文案前仍应先减字。
2. **真机**：
   - `dotnet build apps\scenario-suite`（B 记录 5.7s 可过）。
   - `pytest -m windows tests/e2e/test_windows_scenarios.py`（store/form/chat/console）。
   - 新增：`test_windows_settle_probe.py`、`test_windows_find_wait.py`、`test_windows_menu_dialog.py`、`test_examples.py`。
   - 关键回归：`test_store_add_reaches_the_cart`（大树 + delta）、`test_form_submit_records`（状态翻转）、`test_chat_send_with_clipboard_lands_and_is_recorded`。
3. **计时**：`SOMNUS_PROFILE=1` 输出分段（`uia.py` 的 `provider.timing` + session 分项）；`bench/run.py` 已有墙钟列。**本轮没有做 observe 的分段拆分**——这正是 594 vs 360ms 无法归因的原因（见 §0 的未达成项 1）。
4. **停止条件（保留）**：若后续分段显示 `BuildUpdatedCache` 只占一小部分，则"cache request 瘦身"不再投入；本轮 `lean`/`sig` 与噪声同量级，已把这条判死。

---

## 6. 已实现：运行时设置项与门控移除

全部落地（本轮三笔后口径）。本节的"落点"用**函数名**而不是行号——本轮大改后行号已漂移，旧行号引用作废。

| 决策 | 结果 | 实际默认 |
|---|---|---|
| settle 探测参数 | 不写死，运行时可调 | `settle_quiet_ms=40`、`settle_poll_ms=10`、`settle_timeout_s=3.0` |
| 动作后窗口枚举 | 默认开启，可关 | `post_action_enum=true` |
| 脚本化 `run` | **提前落地** | `30412a8` |
| 确认门控 | **已全链路移除**：`!` 能力直接执行；错误码 / `--confirmed` / `check_action` / `CRITICAL_NAMES` 全删 | `30412a8` |

| 层 | 实际落点（符号） | 状态 |
|---|---|---|
| 默认值 | `settle.Settler.__init__`（quiet 40 / poll 10 / timeout 3.0） | ✅ |
| 启动读取（全局默认） | `config.Settings`、`_apply_file`、`_apply_env`、`render_toml`、`render`、`validate_settle_overrides`；键在 `_KNOWN_KEYS`；env 为 `SOMNUS_SETTLE_QUIET_MS` / `_POLL_MS` / `_TIMEOUT_S` / `SOMNUS_POST_ACTION_ENUM` | ✅ |
| 注入会话 | `mcp_server.build_session` 与 `cli.build_session`：用 `Settings` 构造 `Settler(...)`，再赋 `session.post_action_enum` | ✅ |
| 会话状态与原子应用 | `Session.__init__`（`_SETTINGS_SCHEMA`、`_startup_settings`）、`Session.apply_settings`、`Session._current_settings` | ✅ 全有或全无；立即对后续调用生效 |
| 持久化 | `Session._persist_settings` → `config.persist_session_settings` | ✅（`tools._configure` 仍保留 `NotImplementedError` 兜底） |
| 动作后枚举开关 | `Session.call` / `Session.call_batch`（`if self.post_action_enum: self.refresh_windows()`）；`KeyError` 恢复路径保持无条件枚举 | ✅ |
| 加载时提示（MCP） | `mcp_server.settings_brief` + `Server(..., instructions=...)` | ✅ 由当前生效值动态生成 |
| 加载时提示（文本宿主） | `system_prompt.md` 工具表 + `## Settings` | ✅ 预算 2639/2700 |
| 可改入口 | `tools.TOOL_SPECS` 的 `configure` + `tools._configure`；返回 `SETTINGS ... scope=session`（`persist` 时追加 `persisted=<path>`） | ✅ |

**边界（工具描述已写）**：`configure` 只改"等待多久 / 是否重枚举窗口"这类节奏参数；非法值一个都不改；`persist` 必须显式 `true` 才写盘；`reset=true` 回到本进程启动时的值。

**门控移除后保留的东西**：进程 allow-list、原生权限错误（`PERMISSION_DENIED`）、剪贴板/键盘通道开关、`via` 路由链、效果校验、批处理的全量预校验。`!` 只是能力的声明性元数据；`call`/`call_batch`/`run` 都不再要求确认。

---

## 7. 诚实标注

- **本轮真机实测（[实-本轮]，由实现方报告）**：observe store 3385→594ms、tokens 292；无变化 CALL ~6s→686–706ms；探测 1622–1938→384–404ms；小窗 CALL 215–231ms；无变化 0 全量；FIND 140–172ms / timeout 1.0≈1010ms / 大树 3.0≈3.1s；run 453ms vs 428ms；桌面摘要 0.002ms/+3 token、`ops` 111ms；`top_level_signature` p50 0.31–0.52ms（max ≤2.15ms），`list_windows` p50 70–78ms；写入路径（标题不变）81–104ms / enum 0.6–0.9ms，标题变化时 enum 78–104ms / 总 154–238ms；`observe()` enum 仍 78–102ms；计算器（UWP）在子桌面 `OBSERVE full` 读到 34 个按钮；探针 A 段 7/7、B 段 4/4。测试 **733 passed**（非 GUI）、**38 passed**（`-m windows`，子桌面，约 62–66s，零 `window ... did not appear`）。**我未独立复跑这些真机数字**；提示预算我复算为 **2639**（prompt 1641 + tools 998），与报告一致。
- **子桌面测量口径**：Win11 记事本的"恢复上次会话"会把旧标签页带回**子桌面**（用户配置级），所以 `observe` token 的判定用**相对基线**（同一会话前后对比），不假设"子桌面天然干净"。UWP 计算器在子桌面能起、能读，是 `94b6c77` 的验收样本。
- **亲自读代码确认（[码]，本轮工作区）**：签名探测/降级到全量的三条路径、六档 cache tier、`post_action_enum` 开关、`configure` 的原子校验与 `persist` 钩子、`settings_brief` 的 `instructions`、`_desktop_summary` 不取 `ops`、registry 的 `(window_id, fingerprint)`、`caps._is_text_entry` 的 `not node.children`、孪生重试、`EFFECT_NOT_CONFIRMED` → `_retry_unconfirmed` 换 route 一次（成功 `~ retried via <primitive>`）、`last_foreground_error` + `_observation_warnings`、确认门控移除（`policy.check_action`/`CRITICAL_NAMES`/`--confirmed` 已不存在）、键盘每字符 0.5s 未改、MCP `_call_tool` 仍同步。
- **后续三笔读码确认**：`94b6c77` 的 `process.start/status/kill` 与 `child_allow_spawn`/`allow_processes` 双开关；`326d2ec` 的 `_refresh_after_action`（`False` 才跳过枚举）、`_single_active_window_id`、`serialize.escape_text`（CR/TAB）、`executor._read_payload`（4096/200）；`eba3926` 的 `registry.relocate` 与 `_find_escalation_ready`（截断集合永不升级）。
- **实施前的实测（[实]，B 路）**：E/S/C/I 分项、旧 store 5.9s / observe 3.0s、ComboBox 失败、pywinauto 137ms、windows2text 9.5s。**B-1 与 B-2 对 pywinauto 的 1832/3172ms 自相矛盾**，因此"三家里最快"仍不做定论。
- **本机合成计时（[算]）**：1470 节点纯 Python 45ms；旧 settle 帧数 3/2、批量 10 步 = 单步墙钟。脚本在 `%TEMP%\somnus_frame_count.py`、`somnus_bench_synth.py`、`somnus_batch_cost.py`（未写入仓库）。
- **仍是推断（[推]）**：observe 594ms 与 360ms 之间的构成（未做分段）；大树"有变化" CALL 的现值；ComboBox 的真机成功率；`run` 跨窗口与 `$steps[N]` 的实际收益；宏的端到端收益；LLM 每回合 1–10s。

---

## 8. 本轮推翻/修正的旧结论

| 旧结论（实施前） | 本轮结果 |
|---|---|
| "cache request 瘦身（`lean` 去 4 个模式、`sig` 只留签名）能带来大树 −30%+" | **否定**：两档与噪声同量级；默认仍 `full`。收益来自帧数与视口采集，不是请求内容 |
| "observe 首屏 ≈0.36s" | **未达**：实测 594ms（−82.5%），固定成本未拆清 |
| "大树探测 150–300ms" | **未达**：384–404ms；provider 侧固有 |
| "脚本化 `run` 押后到阶段 3" | **提前落地**（`30412a8`），收益是一次模型推理而非运行时 |
| "静态的桌面摘要带 `ops`" | **未实现**：5 窗口浅读 111ms 超过 80ms 红线，只给窗口清单（渲染分支保留但无数据源） |
| "P2-7 = `EFFECT_NOT_CONFIRMED` 换 route 原地重试" | **已实现**：同一目标换下一条未试 route 一次；成功 `OK ~ retried via <primitive>` |
| "关键能力需要确认（`+submit!` 返回 `CONFIRMATION_REQUIRED`）" | **已推翻**：确认门控全链路移除，`!` 能力直接执行 |

---

## 9. 文档与代码不一致清单（本轮核对发现）

1. **`docs/viewport-capture-proposal.md` 顶部"未改任何生产代码"已过期**：`aa47ccd` 已实现视口采集与分层探测；该文件顶部的状态与"若批准/建议落地顺序"都已改成"已实现/未实现"。
2. **本文 §6 曾把 `run` 标为"押后、不排期"**：已改为"随 `30412a8` 落地"。
3. **提交口径（本轮已核对）**：`run`、P2-3、P2-7、确认门控移除在 `30412a8`；小窗口自适应在 `87030df`；陈旧 ref 自愈与 FIND 升级在 `eba3926`；子桌面进程启动在 `94b6c77`；`read()`/唯一 active/settle 后刷新/CR-TAB 转义在 `326d2ec`。早期"哈希尚未给出"的说法已过期。
4. **P2-7 与"孪生重试"是两条独立机制**：`_retry_unconfirmed` 处理效果确认失败后换 route；`_execute_with_twin` 处理目标接口不可用时换孪生。早期文档把两者混为一谈，已分开。
5. **`ops=` 是"有渲染分支、无数据源"**：`projection/engine.py` 的 `_desktop_segment` 支持 `ops=`，但 `Session._desktop_summary` 明确不产生该字段（111ms > 80ms 红线）。读文档容易误以为它已生效。
6. **P2-3 已实现（本条关闭）**：`uia.last_foreground_error` + `Session._observation_warnings`；触发条件是"有标题且根 `children==0`"，不是原计划的"节点数骤降 20%"。
7. **旧行号引用全部失效**：本轮 `session.py`/`uia.py`/`settle.py` 大改后，§3 各条目里的行号已不对应；阅读时请以函数名/符号为准（§6 已改为符号）。
8. **提示预算**：本轮三笔后为 **2639/2700**（prompt 1641 + tools 998，我用 `estimate_tokens` 复算一致）。中间状态曾出现 2695/2692，均已过时。
9. **`viewport-capture-proposal.md` 里有两个预测未复核**：§1.2 的"1000 行非虚拟化 ≈0.95s（−92%）"和 §2.4 的"探测 150–300ms"都没有真机验证（后者已被 384–404ms 否定）。
10. **`WINDOWS` 摘要的目标 ref 与正文不一致（已修复）**：早期 `Session._desktop_summary` 从 `_window_refs_by_id` 取 ref，而 `_window_header` 只更新正向映射 `_window_refs[target_ref]`，实测会输出 `WINDOWS w2` + `WINDOW w1`（同一窗口两个 ref）。`30412a8` 已修复；现在实测 `--demo observe` 输出 `WINDOWS w1` + `WINDOW w1`。两份 README 均按修复后的行为写。
11. **README 曾写"`WINDOWS` 行列出每个顶层窗口"**：实际最多列 **6 个**，超过部分折叠为 `+N more`；active 窗口若在 6 个之外会替换第 6 席以保证出现（`projection/engine.py` 的 `_MAX_DESKTOP_WINDOWS` / `_desktop_summary_line`）。两份 README 已改成"最多六个，active 始终在列"。
12. **README 曾写 `OBSERVE ... expand` "渲染下一页"**：实现只是把 ref 的路径交给采集层，**页大小/是否等价一页未经验证**（viewport §"未实现 6"）。两份 README 已改为"为容器请求一页"，不再承诺固定页语义。
13. **README 的测试数字未跟上**：旧文只写 `pytest`/`pytest -m windows`，没有数量；`optimization-plan` 曾写 658/31。现统一为 **733 passed**（非 GUI）与 **38 passed**（`-m windows`，子桌面）。
14. **`docs/child-session.md` 曾写"the four tools"**：模型面实际是 **七个工具**（`observe/find/inspect/call/run/switch/configure`）；"四个"是早期只剩 observe/call 时的旧口径。已改为 "the tool surface"。
