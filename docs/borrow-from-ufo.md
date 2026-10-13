# 从 UFO 借鉴什么（非视觉部分）

SomnusUI 的目标是**不依赖视觉**。读完 UFO 的实现后，把它的能力分成三类：

| 类别 | 例子 | 是否借鉴 |
|---|---|---|
| **视觉兜底** | `capture_window_screenshot`、`IconControlFilter`、坐标点击 | ❌ 非目标 |
| **应用 API** | Excel/Word/PPT 的 COM 客户端、Shell CLI | ✅ **已实现** |
| **检索与分层** | 控件过滤、控件字典复用、加载/卸载 app api | ✅ 已借鉴/部分 |

---

## 1. 应用 API 通道（已实现）

**UFO 的做法**：`ufo/automator/app_apis/` 下有 2790 行 COM/脚本客户端。

```python
# UFO: Excel 客户端
def get_range_values(self, sheet_name, range) -> list   # 直接读数据
def table2markdown(self, sheet_name) -> str             # 表格 → markdown
def save_as(self, file_dir, file_name, ...)             # 直接存盘
```

这些**完全不碰像素**——它们调用 Excel 自己的 COM 接口，直接读写单元格。

**为什么比驱动 UI 更好**：

- **准确**：`get_range_values` 返回单元格值；点"复制"再读剪贴板是猜测
- **能表达 UIA 表达不了的**：设置文档作者、重命名工作表、跑 CLI
- **可验证**：API 返回真实数据，`Effect` 能拿它校验，而不是靠观察 delta

**SomnusUI 的实现**（`somnus/appapi/`）：

能力的一个 `via` 步骤可以声明为 `app_api`，runtime 按进程名路由到处理器：

```yaml
caps:
  list_files:
    via:
      - { primitive: app_api, target: list, value: { path: "$path" } }
    params:
      path: { type: string, required: true }
```

LLM 侧完全不变，还是 `CALL x.list_files(path=".")`，返回：

```
OK_UNVERIFIED rev=2
= [".git", "apps", "bench", "docs", "pyproject.toml", "somnus", "tests"]
```

**关键设计**：处理器按**进程名**注册（和 Pack 一样），runtime 不暴露处理器的
参数给 LLM（只暴露 pack 声明的），所以同样的 allow-list 纪律适用。

**与 UFO 的差别**：UFO 在 `mcp.yaml` 里为 Word/Excel/PPT 各自绑定一个 COM MCP
server；SomnusUI 用同一套 capability + registry 机制，不需要额外的 MCP 连接。

---

## 2. 控件字典复用（部分借鉴）

**UFO 的做法**：`UIServerState` 维护一个 `control_dict`，观察一次后每个控件有个
稳定的数字 `label`，后续动作只传 `id`/`name`，LLM 不用重复看整个树。

**SomnusUI 的对应**：Session Registry 的指纹 ref。差别是：

| | UFO | SomnusUI |
|---|---|---|
| 稳定性 | 每次观察重新分配 | 指纹匹配，跨重绘存活 |
| 失效处理 | 需重新 `get_ui_tree` | `STALE_TARGET` 自动提示 re-find |
| 身份依据 | 观察序号 | entity + automation_id + name（非易变） |

SomnusUI 已经做得更稳，**无需改动**。但 UFO 的 `annotation_id` 机制启发了
`UNKNOWN` 节点编号（`? n1 canvas reason=visual`）。

---

## 3. 控件过滤（可借鉴，尚未实现）

**UFO 的做法**：`control_filter.py` 提供两种非视觉过滤：

```python
TextControlFilter      # 按计划关键词筛控件名
SemanticControlFilter  # 句子嵌入相似度筛控件
```

把 100+ 个控件缩到和当前子任务相关的几个，再给 LLM。

**SomnusUI 的现状**：`find` 已经做关键词匹配，`projection` 做结构压缩。
但**没有按任务相关度筛**——`observe` 始终给全量（压缩后的）树。

**可借鉴点**：给 `observe` 加一个可选的 `focus` 参数（LLM 传当前子目标），
让 runtime 只输出相关子树。这符合文档 §5 的 L1「inspect 某区域」，
但目前的 `inspect` 需要 LLM 先知道 ref。

> 注：UFO 的 `SemanticControlFilter` 用**局部嵌入模型**做相似度，不调 LLM，
> 所以仍属确定性范畴，不违反「编译器不调用 LLM」。

---

## 4. 应用加载/卸载生命周期（可借鉴）

**UFO 的做法**：`mcp.yaml` 里每个 app 有 `reset: true`（切窗口时重建 COM 连接），
`mcp_server_manager.py` 管理连接池。

**SomnusUI 的现状**：`AppApiRegistry` 是常驻的，`close()` 在 session 结束时调。

**可借鉴点**：COM 连接长时间不用会失效（Excel 被用户关掉）。应该在
`invoke` 失败时**自动重连一次**，而不是直接把错误抛给 LLM。

---

## 5. 读/写工具分离（不适用）

**UFO 的做法**：MCP 工具分 `data_collection`（只读）和 `action`（有副作用）。

**为什么 SomnusUI 不需要**：能力本身就带 `risk` 分级
（`safe`/`normal`/`critical`），且 `+submit!` 会在 SUI-Text 里标出来。
比工具级分离更细。

---

## 6. 分层 Agent（不适用）

**UFO 的做法**：HostAgent（选应用、拆任务）+ AppAgent（操作单个应用）。

**为什么 SomnusUI 不做**：文档 §1.2 明确「任务规划属于 LLM 侧，不属于 runtime」。
SomnusUI 只提供工具，agent loop 在 `examples/`。

---

## 总结：只借鉴一条，但这条很重要

**App API 通道**是唯一真正需要补的，因为它把「GUI 中间件的触达面」从
"只能像素/控件级操作" 扩展到 "能调用应用的原生数据接口"。

这也正是文档 §20 写的那句：

> 借鉴点：**统一 GUI–API 动作层**

实现后，同一个 `CALL` 语可以落到三种通道：

```
CALL → Native Pattern（UIA）      ← 通用，主力
     → app_api（COM/CLI）         ← 精确，结构化
     → double_click（合成输入）    ← 兜底，显式声明
```

LLM 侧**完全无感**——它只知道调用了一个能力，不知道底下走哪条路。
这正是「GUI = Dynamic Semantic API」的完整形态。
