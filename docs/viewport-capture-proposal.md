# 视口采集方案（viewport capture）— 测量结论与设计

> 状态：**设计 + 测量提案，未改任何生产代码**。
> 测量环境：本机 Windows，场景为项目自带 `apps/scenario-suite`（store：240 行 `ItemsControl`，**非虚拟化**，原始树 1467 节点）+ 一个临时 1000 行 WPF `ListBox`（默认虚拟化，验证用，位于 `e:\0down\somnus_work\perf\wpf1000`，未进仓库）。
> 所有耗时为同机中位数；测量期间机器有其它负载，绝对值偏慢，**只看相对比例**。
> 复现脚本：`e:\0down\somnus_work\perf\viewport_measure.py`、`layered_probe.py`、`wpf1000_measure.py`，原始数据在同目录 `*.json`。

---

## 1. 关键数字

### 1.1 「可见性信号」的单价

| 依据 | 取数方式 | 单节点成本 | 单次快照增量 | 结论 |
|---|---|---|---|---|
| `IsOffscreen` (30022) | 已在 cache request 中 | **2.9 µs**（缓存读） | 0（本来就有） | 便宜，但**在本机 WPF 列表上完全不可信**：240 行里 0 行报 offscreen，实际只有 12 行在窗口内（228/240 与矩形判断不一致） |
| `BoundingRectangle` (30001) | **需要加进 cache request**（当前没有） | **9.5 µs**（缓存读） | full：**+6.2%**（3076→3267 ms）；sig：**+11.6%**（1622→1811 ms） | 单价可接受，且是唯一能正确判断「在不在视口」的信号 |
| `BoundingRectangle`（live） | `element.CurrentBoundingRectangle` | **≈0.2 ms/节点**（含遍历） | — | 1467 节点全读 ≈ 0.3 s/次探测，可用但不划算；缓存读便宜 20 倍 |
| 窗口矩形 | 窗口元素缓存 rect 或 `GetWindowRect` | 1 次读取，可忽略 | 0 | 作为裁剪基准 |
| 纯 Python 矩形相交 | 已取到 rect 后 | 1467 个节点 **0.04 ms** | — | 免费 |

**「加回 BoundingRectangle 很贵」的担心不成立**：全量快照 +6%，探测 +12%；换来的是下面 80%+ 的采集缩减。`IsOffscreen` 单用不可行，必须 `rect ∩ 窗口 ∩ not offscreen`。

缓存遍历本身（`GetCachedChildren` + `GetCachedPropertyValue`）非常便宜：1467 节点走一遍 **5.6 ms**；再读 1467 次 offscreen 也只到 9.9 ms。

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

**结论**：虚拟化列表的树**本身就已经是"视口大小"**，本方案在这里几乎不省钱（也不需要省）；收益全部来自非虚拟化/自绘容器（`ItemsControl`、Chromium 自绘列表、WPF 未开虚拟化的 `ListView`）。虚拟化的额外风险不是"读太多"，而是"没读到的行不存在"（见 §4 R3）。

### 1.4 「一次 Descendants + 条件过滤」vs「先浅层再逐容器取」

| 方式 | 耗时 | 附加成本 |
|---|---|---|
| `FindAll(TreeScope_Descendants, TrueCondition)` | **852 ms**（1466 元素） | 返回的是 **live 元素**，没有缓存属性；逐元素读 name/type ≈ **0.34 ms/节点** → 1466 个 ≈ 0.5 s |
| `FindAll(…, IsOffscreen=false)` | 928 ms，命中仍 1466 | 该条件在本机**过滤不掉任何行** |
| 分层：根 children 19 ms + 2 层 44 ms + 列表窄窥视 194 ms | **≈257 ms** | 元素带缓存属性，可直接判可见、直接出语义 |

**结论**：一次性 Descendants **不划算**——更慢、且拿回来的是无缓存元素；"先浅层、再按容器窥视/按可见行取"才便宜。

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

预算：observe 首屏 ≈ 44 + 194 + 120 ≈ **0.36 s**（现状 3.07 s，**−88%**）；若骨架取 3 层（不做窄窥视）≈ 0.65 s（−79%）。

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
- 折叠中的行**不输出任何"压缩后的语义"**——只输出 `rows/visible/+more` 这种元信息，避免"减信息"。

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

预估：分层探测（骨架 2 层 + 窄窥视 + 可见行 sig 子树）≈ 150–300 ms，现状 sig 全量 1622 ms（**−80% 左右**）。

### 2.5 需要改哪些接口（若立项）

1. `cache.py`：新增 `VIEWPORT_PROPERTIES`（含 30001）与 `peek` 档；`SIGNATURE` 档加 30001（探测要判可见）。
2. `uia.py`：新增 `snapshot_viewport(window_id, *, max_depth=2)`：骨架层 + 容器窄窥视 + 可见行完整读，返回带 `visible/rows/scroll` 的 `RawTree` 与"未读容器"清单；保留现有 `snapshot` 作全量回退。
3. `ir/node.py`：`RawNode` 增加 `rect`/`visible`（或放进 attrs），`RawTree` 增加 `unread: list[...]` 标记；`truncated` 语义从"超深"扩展为"超出视口"。
4. `compiler`/`projection`：集合折叠改为读 `rows/visible`；`expand` 参数对列表容器生效。
5. `runtime/session.py`：`Session` 保存视口集合与 `touched`，settle 探测按 §2.4 组装比较集；`observe`/`call` 后更新。
6. `runtime/settle.py`：探测支持"部分覆盖"——只比较覆盖集合；覆盖不足 → 退回全量。
7. `find`：部分树 0 命中时的回退（容器窥视 / provider 侧按名查询）。

---

## 3. 收益判定（做 / 不做）

- **做**：`observe` 首屏 + 探测覆盖 = 非虚拟化大容器上 −80% 以上；数据来自同一套采集，语义不降级（可见内容完整）。
- **不做**：一次性 `Descendants + IsOffscreen` 过滤（更慢且条件无效）；按深度硬截断（会漏深层状态翻转）；"压缩行内容"（违反"减冗余不减信息"）。
- **虚拟化列表**：方案本身几乎无收益，但它也几乎无风险——树已经是视口大小；只需补"FIND 0 命中 → provider 侧查询/分页"。

---

## 4. 残留风险清单（可见+交互覆盖下会漏什么）

| # | 场景 | 表现 | 是否可接受 |
|---|---|---|---|
| R1 | 不可见、且从未被触碰的控件发生状态翻转（如折叠区里的按钮从禁用变可用） | 直到它进入视口或被 `FIND`/展开/触碰才更新；当前帧可能显示旧状态 | **可接受**。它本来就不可见，用户看不到；模型要操作它时会 `FIND`/展开，届时重读。代价是一次额外回合（可被 `FIND` 的"命中即读子树"抵消） |
| R2 | 可见区域内部、但位于**未完整读**的容器里（容器子项多到只窥视） | 不存在的现象：可见行是完整读的；只有"可见容器但有大量子项且判定为不可见"才不读，那些子项也不可见 | 可接受 |
| R3 | 虚拟化列表：未 realized 的行没有节点/ref | `FIND "Item 600"` 在部分树 0 命中；`CALL` 旧 ref 直接 STALE | **需要补回退**（FIND 0 命中 → 容器窥视/滚动/按名查询）。不可接受"静默 0 命中"，必须带 `~ not in current viewport; try FIND or scroll` 提示 |
| R4 | 滚动后新进入视口的行 | 下一次采集可见；如果这次是 `CALL scroll(...)`，响应里的 delta 不会包含新行 | **可接受但要在响应里声明**：滚动/展开类动作强制附带一次视口刷新（成本 ≈194 ms），否则加一行 `~ viewport changed; OBSERVE again` |
| R5 | 被其它窗口/模态对话框遮挡 | rect 判断仍算"可见"，会读到并展示被遮住的内容 | 可接受（多给信息、不漏信息）；若要精确需要 `WindowFromPoint` 逐点检测，成本高，不建议 |
| R6 | 同一 rect 上的重叠控件（透明覆盖层、拖拽层） | 两个都会被判可见，多读一点 | 可接受 |
| R7 | 高 DPI / 多显示器 / 窗口部分在屏外 | rect 与窗口 rect 都是物理像素、同一坐标系，裁剪正确；窗口自身部分在屏外时以窗口 rect 为准 | 可接受 |
| R8 | 探测覆盖不足却误判 settled | 见 §2.4：探测集不含 touched 目标时必须退回全量，否则会把"没看到变化"当"没变化" | **必须守住**，实现时用"覆盖不足 → 全量"的硬规则，不做概率判断 |
| R9 | 列表子项数量巨大且全部 realized（自绘无虚拟化，如 1 万行） | 窄窥视成本按行数线性（240 行 194 ms → 1 万行 ≈ 8 s） | 需要在窥视请求上加"只取当前视口行"的 provider 侧支持；拿不到时**降级为摘要 + 首 3 行**并声明 `rows=N unread` |

---

## 5. 建议的落地顺序（若批准）

1. **只做 observe**：`snapshot_viewport` + 集合摘要（`rows/visible/+more`）+ `expand` 语义；不做探测改造。风险最低，收益 ≈ −80~88%。
2. **再做探测**：视口集合 + touched 集合 + "覆盖不足退回全量"；这一步才需要动 `settle`/`session`。
3. **最后做 FIND 回退**：部分树 0 命中 → 容器窥视/按名查询/滚动，配套 R3 的提示语。

在阶段 3 的批量/脚本化之前插这一步是值得的：它同时降低 observe 的固定税和 call 的探测税，且不引入新的"语义压缩"。
