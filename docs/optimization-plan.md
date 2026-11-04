# SomnusUI 性能与精准率优化方案（源码核对版）

> 输入：四路（A/B/C/D）各两份报告。本文件是核对后的**结论 + 可执行清单**，不是四份报告的摘要。
> 原则：只按「实测收益 / 改动成本」排序；安全/确认/权限/风险类建议按要求全部剔除，本文不涉及。
> 证据等级：**[实]** 本机真实实测（B 路，或本机可运行的真机数据）；**[码]** 我亲自读代码确认；**[算]** 本机合成的纯 Python 计时（不含 COM）；**[推]** 尚未实测的推断，已在文中标注。

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

**顺序总览**

```
阶段 0（先能测）      P0-0 仪表 → P0-1 cache request 变量实验
阶段 1（削固定税）    P1-1 签名探测 settle → P1-2 设置项/加载提示 → P1-3 窗口枚举开关
阶段 2（提准）        P2-1 下拉选择 → P2-2 孪生 → P2-3 空壳上浮 → P2-4 状态词
                      → P2-5 投影去噪 → P2-6 registry 隔离 → P2-7 原地重试
阶段 3（削回合）      押后：P3-1 FIND timeout、P3-2 桌面摘要、P3-3 脚本 run、P3-4 宏
                      （启动条件：阶段 1 真机验收通过，见下）
阶段 4（可选/不做）   事件订阅、死代码清理、键盘超时、MCP 线程、坐标/视觉、500 节点预算等
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
- **依赖**：无。**成本 S。**

#### P0-1 变量实验：cache request 瘦身能省多少
- **文件/函数**：`somnus/providers/windows/cache.py:16-55`（`CACHED_PROPERTIES`/`CACHED_PATTERNS`）、`cache.py:58-79 build_cache_request`。
- **改法**：用环境变量或构造参数支持三档探针：`full`（现状 14+16）、`lean`（去掉 4 个从未被消费的模式 `selection(10001)/grid(10006)/tableitem(10011)/table(10012)` → 14+12）、`sig`（只要 5 个属性 + 0 个模式，供 P0-2 用）。
- **预期**：`lean` 在 1466 节点上未知，可能 −15%~−35%（[推]）；`sig` 目标 0.1–0.3·S（280–840ms，[推]）。
- **验证**：P0-0 的分段计时 + 场景树同一个窗口重复 10 次取中位数。
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
- **验证**：`tests/e2e/test_windows_scenarios.py` 新增下拉用例（scenario-suite 已有 store/form/chat 用例可挂）；`pytest -m windows`。
- **依赖**：无（可与阶段 1 并行）。**成本 M。**

#### P2-2 孪生/歧义不再死路：按 ref 打，失败再试孪生
- **文件/函数**：`session.py:936-942`（提前 raise）、`session.py:306-308`（`_ambiguous_refs` 构造）、`runtime/registry.py:200-201`（`candidates_for`）、`uia.py:1057-1084`（`_find_by_native` 的 AutomationId 回退无校验）。
- **改法**：
  1) 删掉"ref 属于歧义集合就 raise"的前置判断——该 ref 在本次编译里指向**唯一节点**，`resolution` 里有它的 `NativeRef`，可以先打；
  2) 打不通（`ACTION_NOT_SUPPORTED` / `STALE_TARGET`）时，用 `registry.candidates_for(fingerprint)` 取另一个孪生 ref 重试一次，成功则在响应里注明 `healed ref→ref2`；
  3) `_find_by_native` 的 AutomationId 回退：命中后校验 `control_type` 与 `name` 至少一项与 ref 一致，否则视为未命中（避免拿到"第一个同名"）。
- **预期**：B 的 F1（下拉项孪生 `activate` 全拒）与"幻影孪生"从"必然 ERR + 一次重规划"变成"一次 CALL 命中"；每处失败省 1 个回合。
- **验证**：`tests/runtime/test_ambiguous_collapse.py` 扩展；真机 ComboBox/Explorer 双窗口。
- **依赖**：P2-1（同一个下拉场景）。**成本 S–M。**

#### P2-3 空壳/抬升失败上浮
- **文件/函数**：`uia.py:745`（丢弃返回值）、`uia.py:267`（peek 同样丢弃）、`session.py:539-658`（`observe` 输出）、`session.py:1102`（call 的 body）。
- **改法**：`_snapshot_sync`/`_peek_sync` 把 `ExecResult.ok=False` 存到 `RawTree` 的一个新字段（或 provider 的 `last_raise_error`）；会话按窗口记住上次节点数，若本次 `< 上次的 20%` 且窗口还在，输出一行 `~ tree may be incomplete: raise=FOREGROUND_DENIED nodes=5 (last=412); OBSERVE again or switch to child session`。
- **预期**：把"5 节点 = 没控件"的误判从"必然"降到"有提示"，直接减少 B-F6 类错误结论与随后的错误规划。无速度收益。
- **验证**：真机 Steam 后台窗口（B 的复现步骤）；单测伪造 raise 失败断言该行出现。
- **依赖**：无。**成本 S。**

#### P2-4 状态词归真（只改结果语义，不做任何策略）
- **文件/函数**：`session.py:1099-1171`（`status` 决策）、`verifier.py:45-63`。
- **改法**：`verify.status == OK_UNVERIFIED` 且 `settled=True` 且 `delta` 非空时 → `OK`（现状只有 delta 为空时才归真，所以"真的改了但没声明 effects"永远是 UNVERIFIED，B 的"语义通胀"由此而来）。
- **预期**：消除"已经成功却报 UNVERIFIED → agent 重试"的回合（B 的 F4/F5 与"set_text 值已变仍 UNVERIFIED"）。
- **验证**：`tests/runtime/test_batch_call.py`、`test_session.py` 的状态断言；golden 更新。
- **依赖**：无。**成本 S。**

#### P2-5 投影噪声：给"带子节点的 pane/group"不再发 `set_text`
- **文件/函数**：`caps.py:182-214`（`_is_text_entry`，尤其 212-213）。
- **改法**：`if "value" in node.patterns: return node.role in (...) and not node.children`（有子节点的 pane/group 是容器，不是输入面）。同时删掉死表 `ROLE_DEFAULT_CAPS`（`caps.py:21-38`，全库无引用）。
- **预期**：消灭 B 观察到的"每个窗口根部幻影 `pn1 +set_text(text)`"，减少 agent 误打根节点的一轮。
- **验证**：golden + 真机 observe 对比行数/token。
- **依赖**：无。**成本 S。**

#### P2-6 registry 按窗口隔离，杜绝跨窗口实例重绑
- **文件/函数**：`registry.py:52-165`（`SessionRegistry` 头部注释写着 "for one session/window"，但 `session.py:148` 建的实例在切窗口后继续用）、`session.py:223`、`session.py:585`（target 变更处）。
- **改法**：`by_fingerprint` / `records` 的键改成 `(window_id, fingerprint)`；或者 target 变化时清空 `by_fingerprint` 并把旧 ref 标记为 `last_seen=0`（保留记录用于 STALE 提示）。后者 10 行。
- **预期**：B-F5（关掉 form 重开，旧 `q1` 静默写到新实例）从"静默成功"变成"STALE_TARGET + 旧节点描述"，避免一次错误写入与后续重试。
- **验证**：`tests/runtime/test_window_switching.py` 构造"同进程同标题新 hwnd"；真机关窗重开。
- **依赖**：无。**成本 M。**

#### P2-7 失败原地重试一次（不新增回合）
- **文件/函数**：`session.py:1073-1079`（`EFFECT_NOT_CONFIRMED` 直接 raise）、`session.py:936-942`（`TARGET_AMBIGUOUS` 直接死路）、`session.py:1723-1755`（`_route_memory` 已有替代路由）。
- **改法**：捕获 `EFFECT_NOT_CONFIRMED` 后，按 fingerprint 重定位（AutomationId 优先，不走陈旧 `_element_cache`），换下一条 `via` 路由再执行一次；成功则 `OK recovered on retry (route=...)`，仍失败才 raise。仅对声明了 effects 的 semantic capability 生效（generic 模式几乎不触发，故排在 P2-6 之后）。
- **预期**：把"一次失败 = 一整轮 observe/find/call"压成"一次 CALL 内的一次重试"；命中率取决于 pack 覆盖率，[推] 中等。
- **验证**：`tests/runtime/test_capability_routing.py`；`tests/scenarios/test_store.py` 的加购效果校验。
- **依赖**：无。**成本 S。**

---

### 阶段 3｜削"回合数"（**押后：不排入当前实施顺序，等阶段 1 验收通过后再启动**）
> 启动条件：阶段 1 的三项（P1-1/P1-2/P1-3）全部通过真机回归，且至少 3 个真实多步任务复测确认单步成本已降到 0.2–3.6s；在此之前不动这一阶段的代码。

#### P3-1 FIND 带 timeout（把"等菜单出现"从一轮变零轮）
- **文件/函数**：`session.py:774-870`（`find`）、`sui/parse.py:267-274`（`parse_find`）、`tools.py:47-63`。
- **改法**：`FIND ... timeout=1.5s`：0 命中且给了 timeout 时，`refresh_windows + settle 一次 + 重新编译 + 再搜`，最多到 timeout。默认 0（保持现状 0–4ms 的快路径）。
- **预期**：菜单/对话框流程每次省 1 个"observe→找"回合（每回合 ≥1 次 LLM 推理 + 一次固定税）。[推] 端到端 −1–10s/流程。
- **验证**：`tests/runtime/test_session.py` + 真机菜单流程；断言默认路径没有额外快照。
- **依赖**：阶段 1（否则每次等待又是 3.5–5.6s）。**成本 M。**

#### P3-2 observe 默认带桌面摘要
- **文件/函数**：`session.py:539-658`（`include_other_windows` 默认 False）、`session.py:631-634`。
- **改法**：无 window 参数时默认输出一行 `WINDOWS w1 "…" ops=12 | w2 "…" active`（复用 `_render_window_list` 的紧凑版），正文仍是前台窗口 L0。token 预算从 header 里扣（现在已经有 `header_tokens` 扣减逻辑，`session.py:639-647`）。
- **预期**：冷启动从 3 回合（observe windows → observe wN → find）降到 1–2 回合；B 的 "420ms 冷启动" 之外主要省的是 LLM 回合。
- **验证**：golden（观察 token 是否仍 ≤300）；真机冷启动脚本。
- **依赖**：无。**成本 S。**

#### P3-3 脚本化 `run`（批量的"逐步解析"档）——**押后，暂不排入实施顺序**
- **状态**：**不在当前实施顺序内**。等阶段 1 验收通过、且复测确认固定税已降下来之后再启动；现在只登记设计，不排期。
- **文件/函数**：`sui/parse.py:177-222`（目前只有 `parse_call_block`，全预校验）、`session.py:1353-1640`（`call_batch`）、`tools.py:82-107`。
- **改法**：新增 `RUN` 语句块：`CALL a.activate()` / `AWAIT ref-or-name timeout=1s` / `CALL $it.select()`；逐步"执行→settle→重编译→解析下一步"，失败即停并报 `BATCH PARTIAL steps=2/4`。**保留现有 CALL 批作为"全预校验保守档"**（这是本项目独有优势，别丢）。
- **预期**：菜单/对话框类流程 2N 回合 → 1 回合（B 的菜单 expand→新建、F4 的 XAML 菜单、保存对话框）。运行时成本仍是 k 次固定税；因此**必须排在阶段 1 之后**。
- **验证**：`tests/runtime/test_batch_call.py` 新增 partial 语义；真机菜单流程 + benchmark 任务 #18/#20 类。
- **依赖**：阶段 1 + P2-1/P2-2。**成本 L。**

#### P3-4 宏录制/重放
- **文件/函数**：新增 `somnus/runtime/macro.py`；持久化到 `~/.somnus/macros/`；入口加在 `tools.py`。
- **改法**：录 CALL 序列 + 每步指纹链，重放零 LLM；失败步局部重规划。
- **预期**：命中宏时 [推] 端到端 −80%+（AWM 报 +51% 成功率，D-1 引用）。但工程最大、收益依赖"任务重复率"。
- **依赖**：P3-3、阶段 1。**成本 L。**

---

### 阶段 4｜低收益/可延后（做了也别排前面）

| 项 | 判定 |
|---|---|
| 真实 UIA 事件订阅（`events.py` + `uia.py:1094` + `settle.py:89`） | **降级为可选**。P1-1 稳定后再评估；收益只剩"去掉 40ms 静默"，且需要独立 COM 线程/泵 + PropertyChanged handler（`make_handler` 现在只有 structure）。成本 M，收益 ≤40ms。 |
| `_event_count` / `subscribe` / `NullSubscription` 死代码 | 若不接事件：直接删掉 `session.py:160,1008,1533` 的假 event_source 与 `uia.subscribe`，避免下一个人误以为它在工作。成本 S，收益 0（但防误判）。 |
| settle 的 `loading` 检查（`settle.py:102`） | 死代码（normalize 只把 `busy→loading`，UIA 从不产生 busy）。删掉或让 provider 真产出 busy。成本 S。 |
| settle 增量 scope 快照（`uia.py:489-495` 的 `scope` 参数全库无人调用） | **延后**。增量子树合并没有参考实现，且 `registry.reconcile` 与 delta 是全树全局的，局部替换会引入 ref 漂移。等 P1-1 落地后再评估。成本 L。 |
| FIND 建 ref 索引（`compile.py:126-130` 线性扫描） | **不做**。[算] 1470 节点整树索引+diff 才 1.1ms，收益被实测证伪；复杂度换不来可见收益。 |
| T0 局部校验替代 settle（C-1 #2） | **缩水采用**。它只能证明"这个元素变了"，不能提供 delta/FIND 需要的落地树，因此不能替代快照；其价值已由 P1-1 的探测帧覆盖。 |
| 把 `_route_memory` 学到的可用原语印到节点行/错误提示 | **可选，低收益**。能省 F3（菜单项 activate 被拒）那一次试错回合，但会增加每行 token；建议只在"同一 ref 第二次出现"时打印。成本 S。 |
| Selector quality 标签（FlaUI 风格） | **不做**。真正的故障是对应关系（跨窗口重绑，P2-6）；"质量标签"只是让事故更易读，不提升动作成功率。成本 M。 |
| observe 的"可操作扁平清单"档（D-2 修正） | **延后**。对 L0 截断后的大树确实有用，但它增加 token 且 L0 已按"消灭后续回合"定预算；先做 P3-2（桌面摘要）看回合是否真的降下来。成本 M。 |
| `MCP _call_tool` 丢进线程（`mcp_server.py:78-86`） | 单 agent 串行下无收益；只在取消/并发时有意义。成本 S，收益 ≈0。 |
| 键盘输入按长度分流/超时缩放（`keyboard.py:126-128,232-248`、`executor.py:139-157`） | `paste_text` 已是第 2 条路由且 37ms 定长（`caps.py:289-293`），逐字符验证只在"无 Value 且剪贴板失败"的第 3 条路径触发。建议：超时 `3+0.6×len`、验证每 4 字符一次、nudge 总数封顶；收益只覆盖 B 的 1.89s/3.41s 个案。成本 S，**收益低**。 |
| 元素遍历预算 500（A-2/C-2） | **不按原方案做**。Subtree CacheRequest 已取全量，走到 500 停不省 provider 成本；并且会让 FIND 对第 501+ 个节点永久 0 命中（精准率倒退）。若要做，必须配合 `TreeFilter`/子范围快照 + "FIND 0 命中时全量重抓一次"的回退，成本 L，收益未证实。 |
| 坐标点击兜底 / BoundingRectangle / DPI / 截图 / OCR / VLM | **不做**。会把"零坐标、零视觉"的设计不变量打破（bench 还断言坐标泄漏为 0），且 ComboBox/孪生/空壳三个真实失败都能在 UIA 内解决（P2-1/2/3）。成本 L，收益不确定。 |
| delta 之外再附"受影响区域重投影"（D-1） | **不做**。D-2 自己撤回是对的：delta 是差异化优势，别稀释。真实缺口是"可操作扁平清单"（winappCli `--interactive`），若要加应作为 observe 的独立档位，而不是塞进每次 call。 |
| 系统级快路径（open_app / shell / 文件） | 属新功能（benchmark #40/#41），不属本次"提速/提准"范围；单列需求。 |

---

## 4. 前后对比（预期，全部基于 §2 模型）

| 场景 | 现在 | 阶段 1（P1-1..3，`post_action_enum` 默认开） | 阶段 1+2（P3 押后） |
|---|---|---|---|
| 小窗口 无变化 CALL（设置/勾选已生效） | ~423ms（3 帧） | **~110–150ms**（`post_action_enum=false` 时 ~10–30ms） | 同 |
| 小窗口 有变化 CALL（set_text/activate） | ~423ms | **~250–290ms**（关枚举后 ~150–170ms） | 同 |
| 大树 无变化 CALL（滚动/已选中） | ~5.9s（2 帧） | **~0.4–0.9s** | 同 |
| 大树 有变化 CALL（加购/搜索） | ~5.9s | **~3.1–3.6s** | 同 |
| 大树 OBSERVE | ~3.0s | ~3.0s（观察仍需全量；P1-1 不覆盖 observe） | P0-1 若瘦身成功 → 1.5–2.5s |
| ComboBox 选择 | 失败（0/2 路径，多次回合） | — | **~90%+ 成功，1 回合** |
| 菜单/对话框 4 步流程 | 4 回合 × 5.9s 级 + 4 次推理 | — | **押后**：等 P3-3（阶段 3）启动后再做到 1 回合 |

**OBSERVE 的 3.0s 不在本方案主战场**：它本来只要 1 帧（不可再削帧），只能靠 P0-1 的 cache request 瘦身；而 L0 投影只输出 40 节点，说明"读 1470 个节点却只让 LLM 看 40 个"本身就是可议的——如果 P1-1 的签名探测证明 `sig` 档够快，可以进一步做"L0 只需要投影所需字段"的懒加载快照（成本 L，需另立方案）。

---

## 5. 验证与回归

1. **单测**（无需真机）：`pytest -q`（默认排除 `-m windows`，见 `pyproject.toml:44`）
   - 新增 `tests/runtime/test_settle_frames.py`：参数化 S，断言帧数与 snapshot 次数；断言"无变化只探测一次、零全量"。
   - 新增 `tests/protocol/test_configure_tool.py`：默认值回显、非法值一个都不改、`reset` 回启动值、`persist` 往返、改 `quiet_ms` 后下一次 CALL 的帧数变化；并断言 MCP `instructions` 里带当前生效默认值。
   - 扩展 `test_ambiguous_collapse.py`（P2-2）、`test_window_switching.py`（P2-6）、`test_batch_call.py`（P2-4）、`test_config.py`（P1-2 的四个键）。
   - **约束**：`test_prompt_budget.py:24` 的总预算 2700 token、`:38-41` 的"每个 tool 名必须出现在 system_prompt"——新增 `configure` 与说明后必须仍通过，不许抬高上限。
2. **真机**：
   - `dotnet build apps\scenario-suite`（B 记录 5.7s 可过）。
   - `pytest -m windows tests/e2e/test_windows_scenarios.py`（store/form/chat/console 全量）。
   - 关键回归用例：`test_store_add_reaches_the_cart`（大树 + delta）、`test_form_submit_records`（小树 + 状态翻转）、`test_chat_send_with_clipboard_lands_and_is_recorded`（剪贴板通道）。
   - 专项：Steam/QQ 类自绘窗口的前台锁场景（P2-3）、ComboBox（P2-1）、关闭重开窗口（P2-6）、`configure` 调参后连跑 20 次状态翻转用例。
3. **计时**：`SOMNUS_PROFILE=1` 输出分段；`bench/run.py` 增加墙钟；每次合并记录 `store_call_ms / form_call_ms / observe_ms` 三条基线。
4. **停止条件**：若 P0-0 显示 `BuildUpdatedCache` 只占 2.8s 的 20%，则 P0-1 的优先级下调，P1-1 仍照做（帧数是独立项）。

---

## 6. 已定：运行时设置项（替代原来的三个待拍板）

三个决策已定，全部落到 **P1-2 / P1-3**，本节只给摘要；实现细节见对应条目。

| 决策 | 结果 | 默认值 |
|---|---|---|
| settle 探测参数 | **不写死**，运行时可调；默认按建议值 | `settle_quiet_ms=40`、`settle_poll_ms=10`、`settle_timeout_s=3.0` |
| 动作后窗口枚举 | **默认开启**，可关 | `post_action_enum=true` |
| 脚本化 `run` | **押后到阶段 3**，阶段 1 验收通过后再启动 | 不排期 |

**设置项落点（最小改动一览）**

| 层 | 文件:行 | 改动 |
|---|---|---|
| 默认值 | `settle.py:33-49` `Settler.__init__` | quiet 40 / poll 10 |
| 启动读取（全局默认） | `config.py:78-86` `_KNOWN_KEYS`、`113-135` `Settings`、`177-204` `_apply_file`、`207-222` `_apply_env`、`240-273` `render_toml`、`137-150` `render` | 4 个键、范围校验、`SOMNUS_SETTLE_QUIET_MS` 等环境变量 |
| 注入会话 | `mcp_server.py:26-56` `build_session`；`cli.py` 的 `build_session` | 用读到的 `Settings` 构造 `Settler(...)`，传 `post_action_enum` |
| 会话状态与原子应用 | `session.py:145-147`、`156-184`；新增 `Session.apply_settings()` / `settings_brief()` | 直接改 `settler` 字段与开关；全有或全无校验；立即对后续调用生效 |
| 动作后枚举开关 | `session.py:999`、`1527` | `if self.post_action_enum: self.refresh_windows()`；`1015` 的恢复路径不变 |
| 加载时提示（protocol 宿主） | `mcp_server.py:89-105` `make_mcp_server` | `Server(..., instructions=settings_brief(session))`，由当前生效值动态生成 |
| 加载时提示（文本宿主） | `system_prompt.md:8-14` + 新增 `## Settings` | `configure` 行 + 默认值与取舍；受 `test_prompt_budget.py` 的 2700 token 上限 |
| 可改入口 | `tools.py:21-131` `TOOL_SPECS`、`134-184` `call_tool` | 新增 `configure` 工具；返回一行生效值；`persist=true` 写 `config.toml` |

**边界（写进工具描述，避免误用）**：`configure` 只改"等待多久 / 是否重枚举窗口"这类节奏参数，不改任务语义；它不是"跳过校验"的开关（那个由 P1-1 的签名探测本身保证正确性）。非法值一律不改并报错；`persist` 必须显式传 `true` 才写盘。

---

## 7. 诚实标注

- **亲自读代码确认（[码]）**：帧数逻辑与公式、`_peek` 不枚举、`_event_count`/`subscribe` 死线、`make_handler` 只有 StructureChanged、`loading` 死检查、帧数 2–4、`TARGET_AMBIGUOUS` 前置死路、`_ambiguous_refs` 由 ref 唯一但被拦、raise 返回值丢弃、cache request 无 BoundingRectangle、缓存按 hwnd 分配 wid、registry 只建一次、caps 的 `select`/`set_text` 授予条件、ComboBox 无当前值回读、`_is_text_entry` 对有子节点的 pane 也发 set_text、`ROLE_DEFAULT_CAPS` 无引用、MCP handler 同步阻塞、bench 无墙钟、settle 参数**当前**不可配（本方案 P1-2 会改）、`mcp 2.x` 的 `Server.__init__` 支持 `instructions=`（`.venv` 内核对）、`system_prompt.md` 受 2700 token 预算测试约束。
- **实测数字来自 B 路（[实]）**：E/S/C/I 的分项、store 5.9s、observe 3.0s、ComboBox 失败、pywinauto 137ms、windows2text 9.5s。**注意 B-1 与 B-2 对 pywinauto 的 1832ms/3172ms 自相矛盾**，因此"三家里最快"不做定论。
- **本机合成计时（[算]）**：1470 节点纯 Python 45ms；settle 帧数 3/2、批量 10 步 = 单步墙钟。脚本在 `%TEMP%\somnus_frame_count.py`、`somnus_bench_synth.py`、`somnus_batch_cost.py`（未写入仓库）。
- **仍是推断（[推]）**：`BuildUpdatedCache` 与逐节点缓存读的 2.8s 切分；16 个 pattern 的 provider 侧物化成本；`sig` 档的 0.1–0.3·S；事件接线后"零事件时省一帧"的概率；LLM 每回合 1–10s；宏的端到端收益；`scan` 类 DPI 相关成本（不适用）。
- **可能互相冲突、需实测裁决**：P0-1 若证明 pattern 只占 10%，则"大树 CALL 降到 3.1s"要改成"3.6s"；P1-1 的"无变化零全量"依赖签名实现与 `_same_shape` 一致，真机上若漂移会出现"提前 settled"（回归用例必须覆盖状态翻转）。
