# SomnusUI vs UFO：MCP 面与 GUI 语义对比

同机同任务实测（QQ 窗口，176/151 节点）：

> 任务：在 QQ 里找到联系人"宝宝"，打开聊天，发送消息。

两者都能完成。区别在于**给 LLM 看什么、让 LLM 怎么操作**。

---

## 1. 数据面（观察）

### UFO：原始 UIA 树，完整 JSON，带坐标

`get_ui_tree` 对 QQ 窗口输出：

```json
{ "id":"node_0", "name":"QQ", "control_type":"Window",
  "rectangle":{"left":1957,"top":236,"right":2341,"bottom":980},
  "adjusted_rectangle":{"left":0,"top":0,"right":384,"bottom":744},
  "relative_rectangle":{"left":0,"top":0,"right":1,"bottom":1},
  "level":0,
  "children":[ {"id":"node_1","name":"","control_type":"Pane","rectangle":{...},
                "adjusted_rectangle":{...},"relative_rectangle":{...},"level":1,"children":[]}, ... ] }
```

| 指标 | UFO | SomnusUI |
|---|---|---|
| 输出格式 | 原始 JSON | SUI-Text |
| 节点数 | 176 | 42 行 |
| **含坐标的节点** | **176（100%）** | **0** |
| 字符数 | 58,175 | 1,222 |
| 估算 tokens | ~14,544 | **393** |
| 压缩比 | 1×（原样） | **34.1×** |
| 无名空容器 | 全部保留（node_1~node_8 连续 8 层空 Pane） | flatten 掉 |
| 能力表达 | 无（靠 LLM 看 control_type 猜） | 行内 `+activate` / `+toggle` / `+submit!` |

### 语义化程度

**UFO** 给的是 `control_type: "Button"`、`name: "发送"`——LLM 要自己推断"这是个可点的按钮"。

**SomnusUI** 给的是 `SEND b24 "发送" +send!`——实体类型 + 能力 + 风险等级都直接标注。

### 实测：UFO 的控件列表工具在自绘界面上叫不出名字

`get_app_window_controls_info` 对同一个 QQ 窗口返回 91 个控件，但**每一个都只有
`control_type` 和一个数字 `label`**，`name` / `text_content` / `automation_id` 全为空：

```json
{"control_type": "Button", "label": "31"}
{"control_type": "Group",  "label": "34"}
{"control_type": "Text",   "label": "49"}
```

LLM 从这个列表里无法知道哪个是"发送"按钮。要拿到内容只能走 `get_ui_tree`
（那个 58k 字符 JSON），或依赖截图。

同一窗口，SomnusUI 输出的是带内容的行：

```
BUTTON b24 "发送"
TEXT  tx2 "傲慢-天线宝宝"
INPUT q1  "搜索"
```

原因在编译器：QQ 把可见文字放在深层 text 节点上，SomnusUI 的
`child_path` / 成员绑定把它提上来当作节点的 `name`，而 UFO 只报控件自身的
`name` 属性。

---

## 2. 动作面（操作）

### UFO：9 个原语工具，全部参数化

| 工具 | 参数 | 性质 |
|---|---|---|
| `click_input` | `id`, `name`, `button`, `double` | 鼠标点击 |
| `click_on_coordinates` | **`x`, `y`（相对坐标 0.0~1.0）** | 坐标点击 |
| `drag_on_coordinates` | 起点/终点坐标 | 坐标拖拽 |
| `set_edit_text` | `id`, `name`, `text`, `clear_current_text` | 设值 |
| `keyboard_input` | `id`, `keys`（虚拟键码 `{VK_CONTROL}c`） | 键盘 |
| `wheel_mouse_input` | `id`, 滚动量 | 滚轮 |
| `texts` | `id` | 读文本 |
| `wait` | 秒数 | 等待 |
| `summary` | 子任务描述 | 让 LLM 自述 |

关键：**这些是原语动词**——`click`、`drag`、`keys`。LLM 直接指定 `id`+`name`，或干脆给坐标。

### SomnusUI：1 个动作工具 `call`

```
CALL b24.send()
CALL g7.set_text(text="你好")
CALL li1.open()
```

- 无 `click`、无坐标、无按键码
- 能力**附着在节点上**（`+send!`），不是全局工具
- 动作语义由 pack/编译器决定解析成什么原生调用

---

## 3. 核心差异

| 维度 | UFO | SomnusUI |
|---|---|---|
| **MCP 工具数** | 16 个（9 动作 + 7 数据） | **4 个**（observe/find/inspect/call） |
| **观察载体** | 原始 UIA 树 JSON | SUI-Text（投影+压缩后） |
| **坐标暴露** | 是（每个节点 3 组矩形 + 坐标点击 API） | **否**（信息隔离指标要求 0） |
| **截图** | `capture_window_screenshot`（视觉通道） | 无（No Vision） |
| **动作粒度** | 原语（click/drag/keys） | 能力（`p1.add()`） |
| **定位方式** | `id` + `name`（每次观察重新分配） | 指纹稳定 ref（跨重绘存活） |
| **结果反馈** | 返回值/截图，需自己再观察对比 | `OK` + **delta**（`~ c1 count 0→1`） |
| **危险动作** | 无内建分级 | `!` critical → `CONFIRMATION_REQUIRED` |
| **领域语义** | 无（除非写 COM executor） | Pack（`PRODUCT add()`） |
| **断言/校验** | 无 | `effects` + Verifier |

---

## 4. 两者的设计取舍

### UFO 强在

- **覆盖面**：COM executor（Word/Excel/PPT 直接调 API）、MCP 生态（硬件/移动/Linux agent）、多设备银河
- **兜底**：坐标点击 + 截图，能处理 UIA 完全暴露不了的界面
- **成熟度**：微软出品，完整 agent 框架（HostAgent/AppAgent 分层）

### SomnusUI 强在

- **Token 效率**：34× 压缩，L0 概览 ≤ 200 tokens（UFO 单窗口就 14k tokens）
- **信息隔离**：LLM 上下文里 0 坐标、0 句柄、0 AutomationId
- **稳定性**：指纹 ref 跨重绘存活（UFO 的 id 每次观察重分配）
- **可验证**：`effects` + delta + `OK_UNVERIFIED` 区分"真成功"和"不知道"
- **安全性**：原生 critical 分级 + 确认门槛；动作经白名单能力，无裸坐标/按键

### 取舍本质

```
UFO:      LLM 看到完整现实（坐标、截图），自己决定怎么点
SomnusUI: 编译器先理解 GUI，只把"可调用的能力"给 LLM
```

UFO 把**理解成本**放在 LLM（每次推理都要处理 14k tokens 和坐标）；
SomnusUI 把**理解成本**放在确定性的编译器（一次编译，多次复用，可测试）。

代价：SomnusUI 依赖可访问性 API 的质量。UIA 完全暴露不了的界面（纯自绘、canvas），
SomnusUI 输出 `UNKNOWN`，而 UFO 可以用坐标+截图兜底。

---

## 5. 实测数据

同一台机器、同一个 QQ 窗口：

| | UFO | SomnusUI |
|---|---|---|
| 观察一次（`get_ui_tree`） | 58,175 字符 / ~14.5k tok | 1,222 字符 / 393 tok |
| 观察一次（控件列表） | 91 条，**0 条有名字** | 42 行，**每行都有名字/能力** |
| 工具调用数（完成同一任务） | 至少 6-8 次（列窗口→选中→取树→点→取树→输入→点发送） | 5 次 |
| 坐标泄漏 | 是 | 否 |
| 任务结果 | 可完成（需 LLM 处理坐标/大 JSON） | 已实测完成（消息 17:31 送达） |

---

## 6. 结论

两者不是替代关系，是**两个抽象层级**：

- **UFO** = "让 LLM 直接看和摸真实 GUI"（含视觉兜底），通用但昂贵
- **SomnusUI** = "把 GUI 编译成 API 再给 LLM"，省 token、可验证，但受限于可访问性质量

SomnusUI 的路线正是开发文档 §1.3 的命题：

> GUI = Dynamic Semantic API。真正的产物是"当前屏幕上可调用的能力集合"。

而 UFO 的路线是 §1.2 明确的非目标：

> Vision / OCR / 截图理解 / 坐标定位 / 像素点击

**如果要做取舍**：SomnusUI 可以借鉴 UFO 的 `/v1.0 Vision Adapter`——文档 §16 已规划：
Vision 只处理 `UNKNOWN` 节点，输出带 `confidence` 的语义候选，**核心 Agent 与 SUI-Text 不变**。
这样既保留 token 效率和可验证性，又补上自绘界面的兜底。
