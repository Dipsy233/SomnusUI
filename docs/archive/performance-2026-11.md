# 性能与视口采集记录（2026-11）

本文是 2026-11-05 的性能改造记录，已合并原 `viewport-capture-proposal.md` 的内容。当时的工作分支 `test` 已不存在；当前仓库分支为 `play`、`main` 等。文中的行号引用已随代码改动失效，定位以函数名与符号为准。

测量环境：本机 Windows 11，i5-13500HX / 2560×1440@96DPI。主要场景为 `apps/scenario-suite` 的 store 界面（240 行非虚拟化 `ItemsControl`，原始树约 1467 节点）与一个临时 1000 行 WPF `ListBox`（默认虚拟化，验证用，未进入仓库）。所有耗时为同机中位数；测量期间机器存在其他负载，绝对值偏慢，主要用于比较相对比例。

## 1. 实现状态

| 项 | 状态 | 记录结果 | 原计划目标 |
| --- | --- | --- | --- |
| observe 视口采集 | 已实现 | store 3385 ms → 594 ms（−82.5%），tokens 292 不变 | 约 360 ms |
| 分层探测覆盖 | 已实现 | 无变化 CALL 约 6 s → 686–706 ms；探测 1622–1938 → 384–404 ms；无变化时 0 次全量快照；小窗 CALL 215–231 ms | 大树无变化 0.4–0.9 s；探测 150–300 ms |
| 计时仪表 | 已实现 | `tests/runtime/test_settle_frames.py`、`bench/run.py` 墙钟列、`SOMNUS_PROFILE` 分段 | — |
| cache request 瘦身 | 已实现，收益未证实 | 六档 `full/lean/sig/peek/rect/probe` 可用；`lean`/`sig` 与噪声同量级；默认 `full` | 大树 −30% 以上 |
| 签名探测 settle | 已实现 | 无变化 0 次全量；有变化 1 次全量加 1 次探测 | 1 次全量加 1 次探测 |
| 设置项与 `configure` | 已实现 | 40/10/3.0，优先级内置 < 文件 < 环境变量 < 运行时；MCP `instructions` 与工具名可用 | 默认 40/10、可调、加载可见 |
| 动作后窗口枚举 | 已实现 | 默认开启，`configure` 可关 | 默认开启、可关 |
| 下拉选择链 | 已实现 | `+select`、`SelectionItem` 首路由、选中值回读、`activate` 升级；有单元测试 | 0% → 90% 以上（未以真机成功率确认） |
| 孪生折叠 | 已实现 | 可证明同一元素时折叠，否则保留并标 `twin=true`；失败时原地重试孪生一次 | 一次 CALL 命中 |
| 空壳与前台失败上浮 | 已实现 | `uia.last_foreground_error` 记录原因；observe 头部输出告警 | 空壳有告警 |
| 状态词归真 | 已实现 | 有可见 delta 且 settled 时报告 `OK` | — |
| 投影去噪 | 已实现 | 带子节点的 pane/group 不再获得 `set_text` | — |
| registry 按窗口隔离 | 已实现 | 指纹键为 `(window_id, fingerprint)` | — |
| 效果失败原地重试 | 已实现 | `EFFECT_NOT_CONFIRMED` 时换下一条未试 route 重试一次 | 换 route 再试一次 |
| 确认门控移除 | 已实现 | `call`、`call_batch`、`run` 直接执行 `!` 能力 | 项目取向：不做安全机制 |
| `BoundingRectangle` 进缓存 | 已实现 | full +6.2%，sig +11.6% | 需要 |
| FIND 等待 | 已实现 | 异步菜单 140–172 ms 命中；`timeout=1.0` 约 1010 ms 返回；大树 `timeout=3.0` 约 3.1 s | 省 1 个回合 |
| 桌面摘要 | 部分实现 | `WINDOWS` 行默认出现；成本 0.002 ms、tokens +3；`ops=` 未实现 | 带 `ops` |
| 脚本化 `run` | 已实现 | 菜单流程一次 run 453 ms，两次 CALL 428 ms；节省一次模型推理 | 押后到阶段 3 |
| 宏、事件订阅、键盘超时、MCP 线程 | 未做 | — | 阶段 4 |
| 小窗口自适应 | 已实现 | 未截断的小窗口改走全量单请求；小窗 settle 不付全尺寸基线 | 小窗不退化 |
| 陈旧 ref 自愈 | 已实现 | 同窗口唯一匹配重定位一次，响应 `~ healed old→new` | 少一个重规划回合 |
| FIND 单次全量升级 | 已实现 | 零命中后，仅“部分但未截断”的小树可花一次全量；截断集合不升级 | — |
| `read()` 输出 | 已实现 | `~ ref value="…" len=N`；上限 4096，溢出报 `len>=4097 truncated=true`；内联 200 字符；Text 控件 50020 两表面为空时读 Name，空 Edit 50004 保持 `value=""` | — |
| 唯一 active 窗口 | 已实现 | 多个候选只输出一个 `active`，目标优先 | — |
| settle 后窗口刷新 | 已实现 | 标题在 settle 后才出现的情况被覆盖；桌面签名不变则跳过枚举，任何差异都全量枚举 | — |
| CR/TAB 转义 | 已实现 | 转义 `\\`、`"`、`\r`、`\n`、`\t`，保证一节点一行 | — |
| 子桌面启动进程 | 已实现 | peer 的 `process.start/status/kill` 与 CLI `child-session run`；模型面由 `launch` 启动 | — |
| 可移植测试 | 已实现 | 当时记录 788 passed；`pytest -m windows` 通过（最近记录 38 passed，子桌面，约 62–66 s） | — |

## 2. 当前真机性能

同一台机器，store 界面，真实 UIA provider。`CALL` 区间来自重复运行；`observe` 为单次。

| 场景 | 之前 | 之后 | 说明 |
| --- | --- | --- | --- |
| observe store | 3385 ms | 594 ms（−82.5%） | 视口采集：骨架、行窥视、可见行完整读取 |
| store 上树无变化的 `CALL` | 约 6 s | 686–706 ms | 签名探测；无变化时 0 次全量快照 |
| 签名探测本身 | 1622–1938 ms | 384–404 ms | 只读覆盖集上的签名字段 |
| 小窗口 `CALL` | 423 ms | 215–231 ms | settle 帧数减少（`quiet=40 ms`、`poll=10 ms`） |
| store 观察的 token | 292 | 292 | 不变 |
| 桌面变化检测 `top_level_signature` | — | p50 0.31–0.52 ms，max ≤2.15 ms | Win32 `(hwnd, title)` 走查；`list_windows` p50 70–78 ms |
| 写入后标题不变 | — | 总 81–104 ms，enum 0.6–0.9 ms | 桌面签名一致，跳过枚举 |
| 写入后标题变化 | — | enum 78–104 ms，总 154–238 ms | 标题变化改变窗口列表，执行全量枚举 |
| `observe()` 的窗口枚举 | — | 78–102 ms | 观察仍全量枚举 |

store 上产生变化的 `CALL` 仍需一次全量快照，本轮未复测。`SOMNUS_PROFILE=1` 输出枚举、快照、编译与 settle 的分段耗时。

## 3. 成本模型

```
OBSERVE(n) = E + S(n) + C(n)
CALL(n)    = E + F·S(n) + sleep + I + C(n) + P·(确认帧数)
BATCH(k,n)= E + F·S(n) + sleep + k·I + C(n)
F          = 1 + ceil(quiet_ms / (poll_ms + S(n)))
```

| 符号 | 含义 | 记录值 |
| --- | --- | --- |
| E | 一次窗口枚举 | 85–121 ms |
| S(小) | 小窗口一次全量快照 | 72–197 ms，取 90 |
| S(1466) | 大树一次全量快照 | 2786–2933 ms，取 2790 |
| C(小) | 编译与 reconcile | 0.8–5 ms |
| C(1470) | 编译与 reconcile | 34–41 ms |
| I | 一次注入 | 3–50 ms |
| F | settle 帧数 | 旧参数 150/50 下为 2/3/4；默认 40/10 后减少 |
| sleep | settle 纯睡眠 | 0–150 ms；默认后 0–40 ms |
| P | 一次签名探测 | 目标 0.1–0.3·S；实测 384–404 ms |

小窗口校验：100 + 3×90 + 100 + 5 ≈ 475 ms（实测 423 ms）。大树校验：95 + 2×2790 + 5 + 34 ≈ 5614 ms（实测 5867 ms）。

三条主要成本来源：

1. 每次 CALL 的固定全树快照次数。settle 以“连续帧形状一致 + 静默窗口”判定稳定，帧数由 `quiet_ms`、`poll_ms` 与单帧快照耗时决定。
2. 快照单价由元素数与每元素请求的属性/模式数决定。`BuildUpdatedCache(TreeScope_Subtree)` 在窗口根一次取回整棵子树的属性与模式；Python 侧遍历截断不减少 provider 侧工作量。1470 节点上纯 Python 编译、reconcile、索引与 diff 合计约 45 ms，占总耗时约 1.5%。
3. 精准率问题来自静默失败：前台提升失败未被上报、空壳树被当作正常结果、同一指纹的多个节点直接返回 `TARGET_AMBIGUOUS`、下拉项缺少 `select` 通路与当前值回读。

## 4. 已实现的改造

### 4.1 视口采集

- 可见性判定为 `rect ∩ 窗口`。`IsOffscreen` 不作判据：240 行列表实测 0 行报告 offscreen，矩形判定 12 行可见。
- 采集顺序：骨架层 → 容器窄窥视 → 可见行完整读取。
- 容器元数据：`items_total`、`items_visible`、`items_truncated`、`scroll_pct`。
- 折叠渲染：`rows=`、`visible=`、`+more`，样本默认 3 行，样本行结构完整。
- `OBSERVE ... expand` 把 ref 路径传给采集层；页大小未固定。
- 滚动或展开后视口失效：下一次探测退回全量，响应加 `~ viewport changed; OBSERVE again`。
- 六档 cache request：`full`、`lean`、`sig`、`peek`、`rect`、`probe`，由 `SOMNUS_CACHE_TIER` 选择。

### 4.2 分层探测

探测集为三部分并集：

```
probe_set = visible_nodes
          ∪ interactive_nodes(captured)
          ∪ touched_refs ∪ ancestors(touched_refs)
```

- `visible_nodes` 包含可见行的完整子树，因此可见行内部的深层状态在探测范围内。
- `interactive_nodes` 覆盖可见但不在上述子树里的工具栏与输入框。
- `touched_refs` 覆盖动作目标在折叠区或屏幕外的情形。
- 探测签名无法求值时硬退回一次全量快照，不做概率判断。

### 4.3 settle

- 签名探测使用与 `_same_shape` 一致的字段契约：role、name、value、子节点数、状态；状态包含 `disabled`。
- `previous_signature` 必须是编译前读到的签名；编译会原地规范化节点。
- 默认参数 `settle_quiet_ms=40`、`settle_poll_ms=10`、`settle_timeout_s=3.0`，运行时可调。
- 动作后重新枚举窗口默认开启；关闭后每次 CALL 约省 100 ms，可能漏报动作打开的窗口。

### 4.4 精准率

- 下拉与列表选择：授予 `select`；快照时对 select 节点读取当前选择；`via` 链为 expand → 子项 select → 回读 → 不符则 invoke。没有引入坐标点击。
- 孪生：只有可证明同一元素时折叠；否则保留并标 `twin=true`；失败时原地重试孪生一次。
- 空壳与前台失败：`UiaProvider` 记录 `last_foreground_error`；observe 头部输出 `~ foreground denied (…)` 与 `~ tree looks empty; …`。空树判据为“窗口有标题且根节点 `children == 0`”。
- 状态词：有可见 delta 且 settled 时由 `OK_UNVERIFIED` 归真为 `OK`。
- 投影：带子节点的 pane/group 不再获得 `set_text`；删除未引用的 `ROLE_DEFAULT_CAPS`。
- registry：指纹键改为 `(window_id, fingerprint)`。
- 效果失败：`EFFECT_NOT_CONFIRMED` 时对同一目标换下一条未试 route 重试一次；成功返回 `OK ~ retried via <primitive>`。
- 陈旧 ref：同窗口唯一匹配重定位一次，响应 `~ healed old→new (via …)`。

### 4.5 回合削减

- `FIND` 带 `timeout`：每轮重读视口采集，命中即返回。`timeout_s=None/0` 保持一次内存搜索。
- observe 默认带桌面摘要：首行输出 `WINDOWS` 行；`ops=` 未实现。
- `run` 脚本：纯 `CALL` 脚本保留全量预校验；含 `AWAIT` 时逐步执行，失败返回 `RUN PARTIAL steps=k/N failed-at=i`。
- `read`：读取上限 4096，内联 200 字符；Text 控件空表面回退 accessible Name。
- 唯一 active 窗口：`_single_active_window_id` 只输出一个 `active`，目标窗口优先。
- settle 后刷新：标题可能在动作返回后几毫秒出现；刷新移到 settle 之后。
- CR/TAB 转义：保证 SUI-Text 一节点一行。

## 5. 视口采集的测量依据

### 5.1 可见性信号的单价

| 依据 | 取数方式 | 单节点成本 | 单次快照增量 | 结论 |
| --- | --- | --- | --- | --- |
| `IsOffscreen` (30022) | 已在 cache request 中 | 2.9 µs（缓存读） | 0 | 读取成本低，但在本机 WPF 列表上不可用：240 行中 0 行报告 offscreen，实际 12 行在窗口内 |
| `BoundingRectangle` (30001) | 加入 cache request | 9.5 µs（缓存读） | full +6.2%（3076→3267 ms）；sig +11.6%（1622→1811 ms） | 可正确判断是否在视口内 |
| `BoundingRectangle`（live） | `element.CurrentBoundingRectangle` | 约 0.2 ms/节点（含遍历） | — | 1467 节点全读约 0.3 s/次探测；缓存读成本约为其 1/20 |
| 窗口矩形 | 窗口元素缓存 rect 或 `GetWindowRect` | 1 次读取 | 0 | 裁剪基准 |
| 纯 Python 矩形相交 | 已取得 rect 后 | 1467 节点 0.04 ms | — | 可忽略 |

缓存遍历本身开销较小：1467 节点走一遍 5.6 ms；再读 1467 次 offscreen 也只到 9.9 ms。

### 5.2 列表读取方式

store（240 行，非虚拟化，窗口内可见 12 行）：

| 读法 | 耗时 | 说明 |
| --- | --- | --- |
| 整个列表 `Subtree` | 2868.8 ms | 约等于整棵树 |
| 列表一层 `Children`（全属性+模式） | 476.6 ms | 240 行各取 14 属性与 16 模式 |
| 列表一层 `Children`（窄探测：name/type/enabled/offscreen/rect） | 193.7 ms | 行窥视的请求形态 |
| 可见行（12 行）完整子树 | 约 138 ms | 实测前 10 行 120 ms，单行中位 11.5 ms |
| 可见性扫描（240 行缓存 rect） | 3.2 ms | 过滤本身可忽略 |
| 根 + 2 层结构（19 节点） | 43.6 ms | 找容器 |
| 根 + 3 层结构（266 节点） | 530.9 ms | 行级全读时的成本 |

非虚拟化 1000 行的外推：整列表 `Subtree` 约 11.5 s；按本方案约 0.95 s（−92%）。该外推未在真机复核。

### 5.3 1000 行虚拟化列表

| 指标 | 值 |
| --- | --- |
| 数据 1000 行，树中 realized | 26 行 |
| 全量快照（整窗） | 141.3 ms（加 rect 146.1 ms，+3.4%） |
| 列表一层 Children | 62.4 ms |
| 可见判定（rect ∩ 窗口） | 25/26 可见，1 行 offscreen |
| 单行子树 | 4.4 ms |
| 滚动 20 次后 | realized 75 行（Item 431 起），快照 337 ms，Children 166.7 ms |

虚拟化列表的树本身接近视口大小，视口采集在此类容器上的收益较低；收益主要来自非虚拟化或自绘容器。虚拟化的主要风险是未读到的行不存在，见第 8 节 R3。

### 5.4 一次 Descendants 与分层读取

| 方式 | 耗时 | 附加成本 |
| --- | --- | --- |
| `FindAll(TreeScope_Descendants, TrueCondition)` | 852 ms（1466 元素） | 返回 live 元素，无缓存属性；逐元素读 name/type 约 0.34 ms/节点，1466 个约 0.5 s |
| `FindAll(…, IsOffscreen=false)` | 928 ms，命中仍 1466 | 该条件在本机过滤不掉任何行 |
| 分层：根 children 19 ms + 2 层 44 ms + 列表窄窥视 194 ms | 约 257 ms | 元素带缓存属性，可直接判可见并出语义 |

一次性 Descendants 成本更高，且返回无缓存元素；分层读取成本更低。

## 6. 设计与接口落点

采集顺序：

1. 骨架层：根 `Children`，逐层向下，直到能看见容器的深度（通常 2 层，19 节点，44 ms）。每个容器记录 `rect`。
2. 容器分类：
   - 容器 `rect ∩ 窗口 rect` 为空：只留一行摘要（`LIST l3 "…" rows=240 offscreen`），不下钻。
   - 集合类容器（`list/tree/table/listbox`，或子项数达到阈值）：窄窥视取子项（`name/type/enabled/offscreen/rect`，无模式，240 行 194 ms），按 `rect ∩ 窗口` 选择可见行。
   - 普通分组且整体可见：完整读取。
3. 可见行完整读：只对可见行取完整属性、模式与子树；深层子节点（包括发送按钮这类状态控件）一并读到。
4. 不可见且交互性未读的行：不读；保留窄信息，模型显式展开时再读。

投影形态沿用折叠渲染，增加可见样本、总量标记与展开入口。样本行默认 3 行，结构完整。折叠行只输出 `rows/visible/+more` 元信息。

接口落点：

| 文件 | 内容 |
| --- | --- |
| `cache.py` | `CACHED_PROPERTIES` 加入 30001；新增 `peek`/`rect`/`sig`/`probe` 档 |
| `uia.py` | `snapshot_viewport(window_id, ...)`；保留全量 `snapshot` 作为回退 |
| `ir/node.py` | `RawNode` 携带 rect 与可见性；`RawTree` / 容器携带 `items_*` 元数据 |
| `compiler` / `projection` | 集合折叠读取 `rows/visible`；`expand` 对列表容器生效 |
| `runtime/session.py` | 保存视口集合与 touched，`observe` / `call` 后更新 |
| `runtime/settle.py` | 探测支持部分覆盖；覆盖不足时退回全量 |
| `find` | 部分树 0 命中的回退尚未实现 |

## 7. 未达成项与剩余工作

1. observe 594 ms 未达到 360 ms。−82.5% 已达成，剩余固定成本未做分段拆分，构成未知；推断与容器行数、层数相关。
2. 大树探测 384–404 ms 未进入 150–300 ms。该开销来自 provider 侧请求物化与跨进程调用；`lean`/`sig` 档与噪声同量级，cache request 瘦身不是该路径的杠杆。
3. 虚拟化列表未 realized 行的 `FIND` 回退未实现（R3）。这些行没有节点与 ref，`FIND` 只能 0 命中或 `STALE_TARGET`。“单次全量升级”只对部分但未截断的小树生效，截断集合不升级。需要在 provider 侧按名查询或滚动分页。
4. 大量 realized 行的 provider 侧“只取视口行”（R9）未实现。窄窥视成本仍按容器子项数线性增长（240 行 194 ms）；1 万行约 8 s 的外推未复核。
5. `ops=` 未实现。`projection/engine.py` 保留渲染分支，`Session._desktop_summary` 不产生该字段。统计可操作元素需要每窗口一次浅读：5 个窗口合计 111 ms（form 窗口 43 ms），超过 observe 约 80 ms 的上限。
6. `expand` 的“下一页”语义未验证。实现只是把 ref 路径传给采集层，扩到多少行、是否等价于“一页”没有实测。
7. `run` 跨窗口支持未实现。跨窗口流程需要显式窗口 ref，`delta` 是脚本首尾净差，`$it` 只绑定首个命中。
8. 宏录制与重放未实现。
9. 事件订阅未接入。签名探测已把无变化路径做到 0 次全量快照；事件订阅只能去掉 40 ms 静默，且需要独立 COM 线程与 PropertyChanged handler。
10. 键盘输入超时缩放未改。`keyboard.py` 仍为每字符 0.5 s 固定预算，只在没有 Value 且剪贴板失败的第 3 条路径触发。
11. MCP handler 线程化未做。`mcp_server._make_call_tool` 仍为同步调用；单 agent 串行下没有收益。
12. 下拉链没有真机成功率数字。实现与单元测试已存在，“0% → 90%+”仍是旧目标。
13. 大树“有变化”的 `CALL` 未复测。只测了无变化路径 686–706 ms；旧预期 3.1–3.6 s 未复核。

## 8. 残留风险

| # | 场景 | 表现 | 处理 |
| --- | --- | --- | --- |
| R1 | 不可见且从未被触碰的控件发生状态翻转 | 直到进入视口或被 FIND/展开/触碰时才更新 | 可接受；模型操作时会重新读取 |
| R2 | 可见区域内但位于未完整读取的容器 | 可见行完整读取；不可见子项不读 | 可接受 |
| R3 | 虚拟化列表未 realized 的行没有节点与 ref | `FIND "Item 600"` 0 命中；旧 ref 返回 `STALE_TARGET` | 未实现回退；剩余项第一梯队 |
| R4 | 滚动后新进入视口的行 | 下一次采集可见；`CALL scroll(...)` 的 delta 不含新行 | 滚动/展开等动作后视口集合失效，下一次探测退回全量，并输出 `~ viewport changed; OBSERVE again` |
| R5 | 被其他窗口或模态对话框遮挡 | rect 判定仍算可见，会读到被遮挡内容 | 可接受；精确判定需要 `WindowFromPoint` 逐点检测 |
| R6 | 同一 rect 上的重叠控件 | 两个都判为可见 | 可接受 |
| R7 | 高 DPI、多显示器、窗口部分在屏外 | rect 与窗口 rect 使用物理像素与同一坐标系 | 可接受 |
| R8 | 探测覆盖不足却误判 settled | 探测集不含 touched 目标时可能把“没看到变化”当作“没有变化” | `Settler.wait` 在签名无法求值时退回全量 |
| R9 | 列表子项数量巨大且全部 realized | 窄窥视成本按行数线性增长 | 未实现 provider 侧只取视口行 |
| R10 | 多个窗口同时标 `active` | 窗口列表出现两个 `active` | 已修：`_single_active_window_id`，目标窗口优先 |
| R11 | 窗口标题在动作返回后几毫秒才出现 | settle 前比较签名会把重命名判为未变化 | 已修：刷新移到 settle 之后；签名不变才跳过枚举 |
| R12 | 文本值中的裸 CR/TAB 把节点行拆成两行 | 下一轮解析错位 | 已修：`escape_text` 与 `_escape_read_text` 转义控制字符 |

## 9. 预期与实测对照

| 场景 | 实施前预期 | 实测 | 判定 |
| --- | --- | --- | --- |
| observe store | 约 360 ms | 594 ms（−82.5%），tokens 292 不变 | 未达到 360 ms；相对收益达成 |
| 小窗 CALL | 无变化 110–150 ms；有变化 250–290 ms | 215–231 ms | 高于无变化预期约 1.5–2 倍，仍在 200 ms 级 |
| 大树无变化 CALL | 0.4–0.9 s | 686–706 ms | 达成 |
| 大树有变化 CALL | 3.1–3.6 s | 未测 | 未复核 |
| 大树签名探测 | 150–300 ms | 384–404 ms | 未达到；相对降低 76–79% |
| 无变化时全量快照次数 | 0 | 0 | 达成 |
| ComboBox 选择 | 0% → 90% 以上 | 实现与单元测试存在，无真机成功率 | 未以数字确认 |
| FIND 等待（异步菜单） | 省 1 回合 | 140–172 ms 命中；`timeout=1.0` 约 1010 ms；大树 `timeout=3.0` 约 3.1 s | 达成；等待成本随超时线性 |
| 菜单/对话框 4 步流程 | 押后到阶段 3 | 一次 run 453 ms，两次 CALL 428 ms | 运行时成本不变，节省一次模型推理 |
| 桌面摘要 | 带 `ops` 的一行 | `WINDOWS` 行默认出现；`ops` 未实现；成本 0.002 ms、+3 token | 部分 |
| 桌面签名与枚举 | — | 签名 p50 0.31–0.52 ms，max ≤2.15 ms；`list_windows` p50 70–78 ms | 达成 |
| 写入路径（标题不变） | — | 总 81–104 ms，enum 0.6–0.9 ms | 达成；跳过枚举 |
| 写入路径（标题变化） | — | enum 78–104 ms，总 154–238 ms | 与旧实现同量级；修正重命名延迟 |
| `observe()` 的 enum | — | 78–102 ms | 观察仍全量枚举 |

旧预期中未复现的部分：

- “cache request 瘦身带来大树 −30% 以上”：`lean`/`sig` 与噪声同量级；收益来自帧数与视口采集。
- “observe 首屏约 0.36 s”：实测 594 ms。
- “大树探测 150–300 ms”：实测 384–404 ms。
- “桌面摘要带 `ops`”：未实现。
- “1000 行非虚拟化约 0.95 s”：仍为外推。

## 10. 提示词预算

拆分前记录：

| 项目 | 数值 |
| --- | ---: |
| `system_prompt.md` + 工具面 | 2669 |
| 软检查上限 | 2670 |
| 硬上限 | 2700 |
| MCP `instructions` | 798 |

拆分后的当前口径（`estimate_tokens`，2026-11-07 复核）：

| 项目 | token |
| --- | ---: |
| 核心提示词 `system_prompt.md` | 715 |
| core 档工具 schema | 596 |
| core 档合计 | 1311 |
| 九工具 schema | 981 |
| 九工具合计 | 1696 |
| MCP `instructions`（core 档，默认设置） | 279 |

`tests/protocol/test_prompt_budget.py` 约束核心提示词不超过 1000 token，并要求 core 与完整工具面低于 2669。`instructions` 在 MCP `initialize` 时发送一次，不参与每轮预算。

## 11. 验证

```bash
pytest -q
pytest tests/runtime/test_settle_frames.py
pytest tests/runtime/test_viewport_capture.py
pytest tests/runtime/test_precision_integration.py
pytest tests/protocol/test_prompt_budget.py
pytest -m windows tests/e2e/test_windows_scenarios.py
```

当时记录：

- 可移植套件 788 passed。
- `pytest -m windows` 通过，最近记录 38 passed，在子桌面会话内运行约 62–66 s，未出现 `window ... did not appear`。
- 子桌面 UWP 计算器能启动并读取，`OBSERVE full` 读到 34 个按钮。
- 探针验收：A 段 7/7、B 段 4/4。A 段包括 read 85 ms、命中 4096 字符上限、预览 199/79 字符、按钮报 `CAPABILITY_NOT_FOUND`；B 段包括 active 唯一、`focus()` 跟随、窗口关闭后无残留。
- `SOMNUS_PROFILE=1` 输出分段耗时；本轮未做 observe 分段拆分，594 ms 与 360 ms 的差距因此无法归因。
- 若后续分段显示 `BuildUpdatedCache` 只占小部分，cache request 瘦身不再投入。
- 子桌面测量口径：Windows 11 记事本的“恢复上次会话”会把旧标签页带回子桌面，因此 observe token 使用相对基线，不假设子桌面天然干净。

## 12. 评估过但未采用的方案

| 方案 | 内容 | 现状 |
| --- | --- | --- |
| 坐标点击回退 | 在 UIA 动作失败时使用屏幕坐标 | 未采用；会破坏零坐标、零视觉约束，且下拉、孪生、空壳问题均在 UIA 内解决 |
| 截图、OCR、视觉模型 | 作为感知补充 | 未采用 |
| 事件订阅优先 | 用 PropertyChanged 事件替代探针 | 未优先采用；签名探测已消除无变化路径的全量快照，事件订阅最多省 40 ms 静默 |
| 元素遍历预算 500 | 遍历到 500 节点停止 | 未按原方案采用；Subtree CacheRequest 在遍历前已取全量，停止不会减少 provider 成本，且会让 FIND 对第 501 个之后的节点永久 0 命中 |
| 快照级增量 scope | 只重读子树 | 延后；registry reconcile 与 delta 为全树全局，局部替换会引入 ref 漂移 |
| FIND 建索引 | 为 FIND 建 ref 索引 | 未采用；1470 节点整树索引与 diff 约 1.1 ms，收益不足 |
| observe 可操作扁平清单 | 额外输出扁平操作列表 | 延后；增加 token，先观察桌面摘要的实际回合收益 |
| 系统级快路径 | open_app、shell、文件 API | 属新功能，未纳入性能改造 |
