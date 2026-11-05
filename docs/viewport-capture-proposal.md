# 视口采集方案（viewport capture）— 测量结论与设计

> 状态：**已实现（`aa47ccd` 起，后续 `eba3926` / `326d2ec` 完成）+ 剩余项**。§1 的测量与 §2 的设计保留为原始依据；实现结果、实测数字与未实现项见下面的"实现状态"。`30412a8` 另实现了空壳/前台失败告警、效果重试与确认门控移除；`87030df`/`eba3926` 做了小窗口自适应与 FIND 升级；`326d2ec` 做了唯一 active、settle 后刷新与 CR/TAB 转义。本文件只描述视口采集与分层探测。
> 测量环境：本机 Windows，场景为项目自带 `apps/scenario-suite`（store：240 行 `ItemsControl`，**非虚拟化**，原始树 1467 节点）+ 一个临时 1000 行 WPF `ListBox`（默认虚拟化，验证用，位于 `e:\0down\somnus_work\perf\wpf1000`，未进仓库）。
> 所有耗时为同机中位数；测量期间机器有其它负载，绝对值偏慢，**只看相对比例**。
> 复现脚本：`e:\0down\somnus_work\perf\viewport_measure.py`、`layered_probe.py`、`wpf1000_measure.py`，原始数据在同目录 `*.json`。

---

## 实现状态（2026-11-05，branch `test`）

### 已实现部分

| 设计 | 落点 | 状态 |
|---|---|---|
| 可见性 = `rect ∩ 窗口`，`IsOffscreen` 不作判据 | `cache.CACHED_PROPERTIES` 加 `30001`；`uia` 的视口采集 | ✅ 实测 `IsOffscreen` 在 240 行里 0 行报 offscreen，矩形判定 12 行可见 |
| 骨架层 → 容器窄窥视 → 可见行完整读 | `UiaProvider.snapshot_viewport`（`uia.py`）+ `cache.PEEK_PROPERTIES` / `RECT_PROPERTIES` | ✅ |
| 容器元数据 `items_total/items_visible/items_truncated/scroll_pct` | `Session._apply_viewport_meta` + `compile._carry_viewport_fields` | ✅ |
| 折叠渲染 `rows/visible/+more`、样本默认 3 行、样本行结构完整 | `ProjectionEngine._plan_viewport` / `_viewport_more_text` | ✅ |
| 分层探测覆盖集（可见 ∪ 交互 ∪ 最近触碰及祖先），覆盖不足硬退回全量 | `Session._snapshot_viewport` / 探测集 + `Settler.wait(probe=, signature=)` | ✅ 实测无变化 0 次全量 |
| `OBSERVE ... expand` 传给采集层 | `Session._expand_paths` → `_snapshot_viewport(expanded_paths)` | ✅（扩大到多少样本未测，见"未实现 6"） |
| 六档 cache request | `cache.build_cache_request(tier)`，`SOMNUS_CACHE_TIER` | ✅（`lean`/`sig` 收益与噪声同量级） |
| 滚动/展开后的视口失效处理 | `_VIEWPORT_MOVING_PRIMITIVES` → 下一次探测回退全量 + 响应行 `~ viewport changed; OBSERVE again` | ✅（代价：这类动作之后的 CALL 会退到全量） |

### 实测数字（本轮）

| 指标 | 旧 | 新 |
|---|---|---|
| observe store（1467/1470 节点） | 3385ms | **594ms（−82.5%）**，tokens **292 不变** |
| 无变化 CALL（大树） | ~6s | **686–706ms** |
| 大树签名探测 | 1622–1938ms | **384–404ms** |
| 小窗 CALL | — | **215–231ms** |
| 无变化时的全量快照次数 | 2–4 | **0** |
| `BoundingRectangle` 代价 | — | full **+6.2%**、sig **+11.6%**（与 §1.1 一致） |
| 桌面签名 vs 枚举（`326d2ec`） | — | `top_level_signature` **p50 0.31–0.52ms / max ≤2.15ms**；`list_windows` p50 **70–78ms** |
| 写入路径（标题不变 / 变化，`326d2ec`） | — | 标题不变：总 **81–104ms** / enum **0.6–0.9ms**；标题变化：enum **78–104ms** / 总 **154–238ms** |
| `observe()` 的 enum | — | **78–102ms**（全量；观察本身不做签名短路） |
| 真机回归 | — | 非 GUI **733 passed**；`-m windows` **38 passed**（子桌面，约 **62–66s**，零 `window ... did not appear`） |

### 未实现 / 未达（含原因）

1. **observe 594ms vs 本文件 §2.1 的 ~360ms**：−82.5% 达成，但 360ms 未达。差距在"骨架 + 窄窥视 + 可见行"之后仍未消掉的固定成本；**本轮没有做分段拆分**（骨架/窥视/可见行/编译各占多少未知），因此无法归因，[推] 与容器行数/层数相关。
2. **探测 384–404ms vs §2.4 的 150–300ms**：provider 侧固有开销（请求物化/跨进程）。`lean`/`sig` 档与噪声同量级 → "缩 cache request"不是杠杆。
3. **虚拟化列表未 realized 行的 FIND 回退（R3）未做**：这些行没有节点/ref，`FIND` 仍只能 0 命中或 `STALE_TARGET`；没有"容器窥视 / 滚动 / provider 按名查询"的回退。`eba3926` 的"单次全量升级"只对**部分但未截断**的小树生效，**截断集合永不升级**，因此不覆盖这一场景。→ 剩余项第一梯队。
4. **大量 realized 行的 provider 侧"只取视口行"（R9）未做**：窄窥视仍按容器子项数线性（240 行 194ms）；§1.2 的"1 万行 ≈8s"外推**未复核**。
5. **`ops=` 不属于本方案，也未实现**：`projection/engine.py` 保留 `ops=` 的渲染分支，但**当前没有数据源**（桌面摘要需要每窗口浅读，5 窗口 111ms > 80ms 上限）；细节见 [optimization-plan.md](optimization-plan.md)。
6. **`expand` 的"下一页"语义未验证**：实现只是把该 ref 的路径传给采集层，扩到多少行、是否等价于"一页"没有实测。
7. **§1.2 的"1000 行非虚拟化 ≈0.95s（−92%）"仍是外推**，没有真机数据。
8. **§2.4 的"分层探测 ≈150–300ms"已实测否定**（384–404ms）；该数字应视为旧预期。

---

## 1. 关键数字

### 1.1 「可见性信号」的单价

| 依据 | 取数方式 | 单节点成本 | 单次快照增量 | 结论 |
|---|---|---|---|---|
| `IsOffscreen` (30022) | 已在 cache request 中 | **2.9 µs**（缓存读） | 0（本来就有） | 读取成本低，但**在本机 WPF 列表上不可用**：240 行里 0 行报 offscreen，实际只有 12 行在窗口内（228/240 与矩形判断不一致） |
| `BoundingRectangle` (30001) | **需要加进 cache request**（当前没有） | **9.5 µs**（缓存读） | full：**+6.2%**（3076→3267 ms）；sig：**+11.6%**（1622→1811 ms） | 单价可接受，且是唯一能正确判断「在不在视口」的信号 |
| `BoundingRectangle`（live） | `element.CurrentBoundingRectangle` | **≈0.2 ms/节点**（含遍历） | — | 1467 节点全读 ≈ 0.3 s/次探测，可用但开销较高；缓存读成本约为其 1/20 |
| 窗口矩形 | 窗口元素缓存 rect 或 `GetWindowRect` | 1 次读取，可忽略 | 0 | 作为裁剪基准 |
| 纯 Python 矩形相交 | 已取到 rect 后 | 1467 个节点 **0.04 ms** | — | 免费 |

**「加回 BoundingRectangle 很贵」的担心不成立**：全量快照 +6%，探测 +12%；换来的是下面 80%+ 的采集缩减。`IsOffscreen` 单用不可行，必须 `rect ∩ 窗口 ∩ not offscreen`。

缓存遍历本身（`GetCachedChildren` + `GetCachedPropertyValue`）开销很小：1467 节点走一遍 **5.6 ms**；再读 1467 次 offscreen 也只到 9.9 ms。

### 1.2 列表：只读可见行 vs 整列表

store（240 行、非虚拟化、窗口内可见 **12 行**）：

| 读法 | 耗时 | 说明 |
|---|---|---|
| 整个列表 `Subtree` | **2868.8 ms** | ≈整棵树（列表拥有全部数据） |
| 列表一层 `Children`（全属性+模式） | **476.6 ms** | 240 行各取 14 属性 + 16 模式 |
| 列表一层 `Children`（窄探测：name/type/enabled/offscreen/rect） | **193.7 ms** | 设计里「行窥视」的请求形态 |
| 可见行（12 行）完整子树 | **≈138 ms**（实测前 10 行 120 ms，单行中位 11.5 ms） | 只有可见行需要完整语义 |
| 可见性扫描（240 行缓存 rect） | 3.2 ms（13 µs/行） | 过滤本身免费 |
| 只读「根 + 2 层」结构（19 节点） | 43.6 ms | 找容器用 |
| 只读「根 + 3 层」结构（266 节点） | 530.9 ms | 若不用窄探测而行级全读 |

**非虚拟化 1000 行的外推**：整列表 `Subtree` ≈ **11.5 s**（按 11.5 ms/行）；按本方案 ≈ 行窥视 0.8 s + 12 个可见行 0.14 s ≈ **0.95 s**（−92%）。

### 1.3 1000 行虚拟化列表（真实 WPF `ListBox`）

| 指标 | 值 |
|---|---|
| 数据 1000 行，树里实际 realized | **26 行** |
| 全量快照（整窗） | **141.3 ms**（+rect 146.1，**+3.4%**） |
| 列表一层 Children | 62.4 ms |
| 可见判定（rect ∩ 窗口） | 25/26 可见，1 行 offscreen |
| 单行子树 | 4.4 ms |
| 滚动 20 次后 | realized **75** 行（Item 431 起），快照 337 ms，Children 166.7 ms |

**结论**：虚拟化列表的树**本身就已经是"视口大小"**，本方案在这里的收益较低；收益主要来自非虚拟化/自绘容器（`ItemsControl`、Chromium 自绘列表、WPF 未开虚拟化的 `ListView`）。虚拟化的主要风险是未读到的行不存在（见 §4 R3）。

### 1.4 「一次 Descendants + 条件过滤」vs「先浅层再逐容器取」

| 方式 | 耗时 | 附加成本 |
|---|---|---|
| `FindAll(TreeScope_Descendants, TrueCondition)` | **852 ms**（1466 元素） | 返回的是 **live 元素**，没有缓存属性；逐元素读 name/type ≈ **0.34 ms/节点** → 1466 个 ≈ 0.5 s |
| `FindAll(…, IsOffscreen=false)` | 928 ms，命中仍 1466 | 该条件在本机**过滤不掉任何行** |
| 分层：根 children 19 ms + 2 层 44 ms + 列表窄窥视 194 ms | **≈257 ms** | 元素带缓存属性，可直接判可见、直接出语义 |

**结论**：一次性 Descendants 成本更高，且返回的是无缓存元素；"先浅层、再按容器窥视/按可见行取"的成本更低。

---

## 2. 设计

### 2.1 采集顺序（一次 observe / 一次探测都用同一套）

1. **骨架层**：根 `Children` → 每层 `Children`，直到「能看见容器」的深度（通常 2 层，19 节点 / 44 ms）。每个容器记录 `rect`。
2. **容器分类**：
   - 容器 `rect ∩ 窗口 rect` 为空 → 只留一行摘要（`LIST l3 "…" rows=240 offscreen`），不下钻。
   - 容器是**集合类**（`list/tree/table/listbox`，或子项 ≥ 阈值）→ 用**窄窥视**取子项（`name/type/enabled/offscreen/rect`，无模式，240 行 194 ms），按 `rect ∩ 窗口` 选可见行。
   - 容器是普通分组、且整体可见 → 直接完整读取（和今天一样）。
3. **可见行完整读**：只对可见行（12 行 / 120 ms）取完整属性 + 模式 + 子树；它们的深层子节点（含发送按钮这类状态控件）一并读到，**状态新鲜度不受深度截断影响**。
4. **不可见但"交互性未读"的行**：不读；只保留窄信息（名字、类型、可操作性由 type 推断）。模型显式展开时才读。

预算：observe 首屏 ≈ 44 + 194 + 120 ≈ **0.36 s**（旧状 3.07 s，**−88%**）；若骨架取 3 层（不做窄窥视）≈ 0.65 s（−79%）。
**实测（本轮）：594ms（−82.5%），360ms 未达**。差距是未拆分的固定成本，见顶部"未实现 1"。

### 2.2 observe 投影形态

沿用现有折叠渲染，只增加三样东西：可见样本、总量标记、展开入口。

```
SCREEN s1 "商品" +find
  LIST l1 "商品" rows=240 visible=12 scroll=5%
    ITEM li1 "Item 0" price=$1.00 stock=1 +add          ← 可见样本 1..3 行
    ITEM li2 "Item 1" price=$1.50 stock=2 +add
    ITEM li3 "Item 2" price=$2.00 stock=3 +add
    +more rows=240 visible=12 (first visible: "Item 0" .. "Item 11")
      use FIND "<name>" to reach a row, or OBSERVE w1 expand l1 for the next page
```

- `rows=` 总量（来自 `virtual_count` / 子项计数），`visible=` 视口内数量，`scroll=` 当前滚动比例（来自 ScrollPattern，取不到就不打印）。
- 样本行数默认 3，可配置；始终渲染**结构完整**的行（属性、能力、状态一个不少），只是行数少。
- 行外的兄弟控件（搜索框、购物车）照常完整渲染。
- 折叠中的行**不输出任何"压缩后的语义"**，只输出 `rows/visible/+more` 这种元信息，避免"减信息"。

### 2.3 模型如何主动展开

优先**复用现有语义**，不新增语法：

| 意图 | 现有入口 | 行为 |
|---|---|---|
| 看下一页 / 更多行 | `OBSERVE w1 expand l1`（`observe(..., expanded={ref})` 已有） | 在列表容器上等价于"扩大可见样本到一页（约一个视口高度）"，仍只读可见+页内行 |
| 精确找某行 | `FIND "<name>"`（已支持 `in ref` 作用域） | 部分树 0 命中时，回退"对该容器窄窥视一次 + 全名匹配"；虚拟化容器再回退 provider 侧 `FindAll` 按 name 查询 |
| 滚动 | `CALL l1.scroll(...)` / 行的 `scroll_into_view` | 动作后用同一套视口采集刷新，新进入视口的行自然出现并带 ref |

不建议新增 `OBSERVE ... rows=N` 这类语法（提示词预算和解析成本都要涨），把 `expand` 定义成"展开到一页"即可；程序化宿主已有 `observe(expanded=...)` 参数。

### 2.4 探测的覆盖规则（状态新鲜度的关键）

探测**不按深度**截断，而按「可见 + 交互 + 最近触碰」三者并集：

```
probe_set = visible_nodes                 # rect ∩ 窗口 ∩ 非 offscreen
          ∪ interactive_nodes(captured)   # 已捕获层里暴露 invoke/toggle/selectionitem/value/expandcollapse/...
          ∪ touched_refs ∪ ancestors(touched_refs)   # 最近一次 call/observe 用到的 ref 及其祖先
```

- `visible_nodes` 包含可见行的**完整子树**（可见行是完整读的），所以 QQ 发送键这种"可见行内部深层状态"一定在探测范围内。
- `interactive_nodes` 覆盖"可见但不在上述子树里的工具栏/输入框"。
- `touched_refs` 覆盖"动作目标在折叠区/屏幕外"的情形：动作刚碰过它，就必须复读它和它的祖先，直到下一次 observe 重新划视口。
- 实现落点（阶段 1 版本，低成本）：`viewport` 集合在每次 `observe`/`call` 的采集里算出并存进 `Session`（`dict[ref→rect]` + `visible:set` + `touched:set`）；`Settler.wait` 的探测走"同一套分层采集"，只对 `probe_set` 比较签名；签名不覆盖的区域**不参与 settled 判定**，一旦发现探测覆盖不到 touched 目标就退回一次全量快照。

预估：分层探测（骨架 2 层 + 窄窥视 + 可见行 sig 子树）≈ 150–300 ms，旧状 sig 全量 1622 ms（**−80% 左右**）。
**实测（本轮）：384–404ms（−76~79%），未进 150–300ms**。这是 provider 侧固有开销，见顶部"未实现 2"。

### 2.5 接口落点（实现对照）
> 1–6 已实现（`UiaProvider.snapshot_viewport` / `viewport_meta` / `cache` 六档 / `Settler` 探测 / `Session` 视口集合）；**7（FIND 在部分树上 0 命中时的回退）未做**。

1. `cache.py`：`CACHED_PROPERTIES` 加 30001；新增 `peek`/`rect`/`sig`/`probe` 档（`SOMNUS_CACHE_TIER`）。
2. `uia.py`：`snapshot_viewport(window_id, *, ...)`；保留全量 `snapshot` 作回退。
3. `ir/node.py`：`RawNode` 带 rect/可见性；`RawTree`/容器带 `items_*` 元数据；`truncated` 扩展到"超出视口"。
4. `compiler`/`projection`：集合折叠读 `rows/visible`；`expand` 对列表容器生效。
5. `runtime/session.py`：`Session` 保存视口集合与 touched；`observe`/`call` 后更新。
6. `runtime/settle.py`：探测支持部分覆盖；覆盖不足 → 硬退回全量。
7. **未实现**：`find` 的部分树 0 命中回退（容器窥视 / provider 按名查询 / 滚动分页）。

---

## 3. 收益判定（做 / 不做）

- **做**：`observe` 首屏 + 探测覆盖 = 非虚拟化大容器上 −80% 以上；数据来自同一套采集，语义不降级（可见内容完整）。
- **不做**：一次性 `Descendants + IsOffscreen` 过滤（更慢且条件无效）；按深度硬截断（会漏深层状态翻转）；"压缩行内容"（违反"减冗余不减信息"）。
- **虚拟化列表**：方案本身的收益较低，风险也较低；树已经是视口大小；只需补"FIND 0 命中 → provider 侧查询/分页"。

---

## 4. 残留风险清单（可见+交互覆盖下会漏什么）

| # | 场景 | 表现 | 是否可接受 |
|---|---|---|---|
| R1 | 不可见、且从未被触碰的控件发生状态翻转（如折叠区里的按钮从禁用变可用） | 直到它进入视口或被 `FIND`/展开/触碰才更新；当前帧可能显示旧状态 | **可接受**。它本来就不可见，用户看不到；模型要操作它时会 `FIND`/展开，届时重读。代价是一次额外回合（可被 `FIND` 的"命中即读子树"抵消） |
| R2 | 可见区域内部、但位于**未完整读**的容器里（容器子项多到只窥视） | 不存在的现象：可见行是完整读的；只有"可见容器但有大量子项且判定为不可见"才不读，那些子项也不可见 | 可接受 |
| R3 | 虚拟化列表：未 realized 的行没有节点/ref | `FIND "Item 600"` 在部分树 0 命中；`CALL` 旧 ref 直接 STALE | **未实现回退**（本轮仍未做）。不可接受"静默 0 命中"，必须带提示。→ 剩余项第一梯队 |
| R4 | 滚动后新进入视口的行 | 下一次采集可见；如果这次是 `CALL scroll(...)`，响应里的 delta 不会包含新行 | **已按"降级 + 声明"处理**：`_VIEWPORT_MOVING_PRIMITIVES`（scroll/scroll_into_view/expand/collapse）之后，watched set 失效 → 下一次探测退回全量，并在响应里加 `~ viewport changed; OBSERVE again`。代价是这类动作之后的 CALL 变慢。 |
| R5 | 被其它窗口/模态对话框遮挡 | rect 判断仍算"可见"，会读到并展示被遮住的内容 | 可接受（多给信息、不漏信息）；若要精确需要 `WindowFromPoint` 逐点检测，成本高，不建议 |
| R6 | 同一 rect 上的重叠控件（透明覆盖层、拖拽层） | 两个都会被判可见，多读一点 | 可接受 |
| R7 | 高 DPI / 多显示器 / 窗口部分在屏外 | rect 与窗口 rect 都是物理像素、同一坐标系，裁剪正确；窗口自身部分在屏外时以窗口 rect 为准 | 可接受 |
| R8 | 探测覆盖不足却误判 settled | 见 §2.4：探测集不含 touched 目标时必须退回全量，否则会把"没看到变化"当"没变化" | **已守住**：`Settler.wait` 在探测签名无法求值（watched 节点缺失）时硬退回全量，不做概率判断 |
| R9 | 列表子项数量巨大且全部 realized（自绘无虚拟化，如 1 万行） | 窄窥视成本按行数线性（240 行 194 ms → 1 万行 ≈ 8 s） | **未做 provider 侧"只取视口行"**；本轮未复核 1 万行外推。→ 剩余项 |
| R10 | 多个窗口同时标 `active`（provider 或 rotate 留下的陈旧标记） | 桌面摘要/窗口列表出现两个 `active`，agent 无法判断在哪 | **已修（`326d2ec`）**：`Session._single_active_window_id` 只允许一个，目标窗口优先；`test_active_window.py` 覆盖 |
| R11 | 窗口标题在动作返回后几毫秒才出现（如记事本标题的 `*`） | 若在 settle 前比较签名，重命名会被判为"没变化"，delta 晚一 CALL 才出现，两次重命名还可能合并 | **已修（`326d2ec`）**：刷新移到 settle 之后；签名不变才跳过枚举，**任何差异都全量枚举** |
| R12 | Windows 文本值里的裸 CR/TAB 把一条节点行拆成两行 | `splitlines()` 把一个节点拆开，下一轮解析错位 | **已修（`326d2ec`）**：`escape_text` 与 `executor._escape_read_text` 转义 `\\` `"` `\r` `\n` `\t`；previews 只用于阅读，不用于回写 |

**本轮总评**：R3/R9 仍开放（大列表的剩余缺口）；R4 用"回退全量 + 提示"处理；R8 守住；R10–R12 已在 `326d2ec` 修复；R1/R2/R5–R7 维持原判断。

---

## 5. 实施顺序（实际）

1. **只做 observe**：✅ 已完成（`aa47ccd` 起，实测 3385→594ms，tokens 292 不变）。
2. **再做探测**：✅ 已完成（同提交，实测无变化 CALL ~6s→686–706ms；探测 1622–1938→384–404ms）。
3. **最后做 FIND 回退**：❌ 未做（R3，剩余项第一梯队）。`eba3926` 只加了"未截断小树的一次全量升级"，不覆盖未 realized 行。

原判断"在阶段 3 的批量/脚本化之前插入这一步"成立：observe 与探测与阶段 1 同轮实现，`run`（阶段 3）在其后实现；运行时收益来自前两项，脚本化的运行时收益较小。
