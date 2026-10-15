# 接口能力实测报告

**目的**：记录 SomnusUI 在各种 UI 框架上真正能用的写入接口，以及**已经验证过走不通**的路线。

这份文档存在的意义是**避免重复劳动**：自绘控件（Electron / Chromium / 自绘 Win32）无法写入的问题，
看起来总是"再换个 API 就能解决"，因此被反复重新探索。下面每一条结论都附实测数据，
可以直接引用而不必重跑实验。

所有数据来自真实运行的应用（Windows 11 24H2，22631），程序化获取，
不用截图、不用人工点击，与 Accessibility Insights / Inspect.exe 读取的是同一批 UIA API。

---

## 一、总表

| 写入通道 | 需要的条件 | 是否抢焦点 | 是否合成输入 | 实测结论 |
|---|---|---|---|---|
| **UIA `ValuePattern.SetValue`** | 控件实现 `Value` pattern | 否 | 否 | ✅ **首选**，记事本 20/20 |
| **`WM_SETTEXT` / `EM_REPLACESEL`** | **元素自己**有非零 `CurrentNativeWindowHandle` | 否 | 否 | ✅ **已实现**：后台窗口写入 3/3，焦点未抢（2.10） |
| **CDP `Input.insertText`** | Chromium 应用开了调试端口 | 否 | 否 | ✅ 可行，但需应用配合 |
| **UIA `LegacyIAccessible.SetValue`** | MSAA Provider 实现了它 | 否 | 否 | ❌ 通常 `E_NOTIMPL` |
| **UIA `TextPattern`** | 控件实现 `Text` pattern | 否 | 否 | ❌ **只读**，无写入方法 |
| **UIA `TextEditPattern`** | 控件实现 `TextEdit` pattern（Win8.1+） | 否 | 否 | ❌ 取得到但**不可写**：range 恒 NULL 且无 `SetText`（见 2.9） |
| **MSAA `IAccessible::put_accValue`** | 同上 | 否 | 否 | ❌ Chromium 只给无名 Pane |
| **`IAccessibleEx` 桥接** | Provider 实现它 | — | — | ❌ `E_NOINTERFACE` |
| **虚拟桌面隔离 + 键盘** | 能把窗口移走 | **是** | **是** | ❌ 移不动 + 切桌面更打扰 |
| **`SendInput` 键盘** | 目标在前台 | **是** | **是** | ⚠️ 可写但 0/20 丢字 |

**判断顺序**（从上到下，第一个可用的即是答案）：

```
1. 元素自己有非零的 NativeWindowHandle？    → WM_SETTEXT / EM_REPLACESEL
2. 有没有 UIA ValuePattern？                → SetValue
3. 是不是 Chromium 应用且开了调试端口？      → CDP
4. 都没有                                   → 应用没暴露写入能力，如实报错
```

> 运行时不是按此顺序硬编码，而是同一能力的一条有条件通道链；实际顺序为
> **ValuePattern → 元素级 HWND → 键盘**（后两条互斥），详见 2.10.1。
>
> **第二轮探测（2.9）修正了两处判据**，并落地了一条新通道：
> 第 1 步的判据从“进程内有原生 Edit 子窗口”改为“**元素自己的 HWND**”（已实现，见 2.10）；
> 并补充实测：`TextEditPattern` 虽可取到但不可写，`ValuePattern2` 根本不存在。

---

## 二、核心案例：QQ 的 ProseMirror 输入框

这是"自绘控件无法写入"的典型样本，也是所有探索的起点。
技术栈：**Electron + Chromium + ProseMirror（contenteditable）**。

### 2.1 控件属性

| 字段 | 值 |
|---|---|
| **ControlType** | `Group` (50026) —— **不是 `Edit`** |
| **ClassName** | `ProseMirror ExEditor-qq-msg-editor is-empty` |
| **Name** | `我的手机`（会话名，非控件用途，且随聊天对象变化） |
| **AutomationId** | `""`（空） |
| **IsEnabled** | `1` |
| **IsKeyboardFocusable** | `1` |
| **IsControlElement** | `1` |
| **ProcessId** | `23444` (QQ.exe) |

**关键**：`ControlType = Group`。应用**没有把这个控件声明为可编辑**。
这是整个问题的根源——不是我们找不到接口，是应用没提供。

### 2.2 Pattern 可用性 vs 实际可取性

UIA 有两个独立的问题要问：**"你声称支持吗"** 和 **"取出来真的能用吗"**。
它们**不总是一致**，所以两者都要测。

> **⚠️ 第二轮探测补充**：本节表格只覆盖了**第一代** pattern id，遗漏了文本家族里的
> **`TextEditPattern`**（`UIA_TextEditPatternId` = 10032，Win8.1+）。它与
> `TextPattern`（10014）是**两个不同的接口**，而不是同一个接口的别名。
> 只探测 `TextPattern` 就得出"文本接口都是只读的"，覆盖的只是该家族的只读成员。
> 已补测，结论见 **2.9**（结论是"仍然写不进去"，但**理由和原文不同**）。

| Pattern | `IsXxxPatternAvailable` | `GetCurrentPattern` |
|---|---|---|
| `Invoke` | `false` | `ValueError: NULL COM pointer` |
| `Value` | `false` | `ValueError: NULL COM pointer` |
| **`Text`** | **`true`** | **成功取到** |
| `Text2` | `false` | `ValueError` |
| **`ScrollItem`** | **`true`** | **成功取到** |
| **`LegacyIAccessible`** | **`true`** | **成功取到** |
| `SelectionItem` | `false` | `ValueError` |
| `Scroll` | `false` | `ValueError` |

这个控件**报告是诚实的**（声称可用的都真的取得到）。
但反过来不成立：**声称可用 ≠ 方法有实现**（见 2.5）。

### 2.3 Provider 侧接口探测（最关键的一步）

`IsXxxAvailable` 只说明"UIA 映射层有这个东西"。
真正决定有没有**写入入口**的，是 Provider 侧实现了哪些接口：

| 接口 | QueryInterface 结果 |
|---|---|
| `IUIAutomationElement` | 支持 |
| `IUIAutomationElement3` | 支持（所以 `ShowContextMenu` 可用） |
| `IAccessibleEx` | **`E_NOINTERFACE`** |
| `IRawElementProviderSimple` | **`E_NOINTERFACE`** |
| `ITextRangeProvider` | **`E_NOINTERFACE`** |
| `ITextProvider` | **`E_NOINTERFACE`** |
| `IValueProvider` | **`E_NOINTERFACE`** |
| `ITextEditProvider` | **`E_NOINTERFACE`** |
| `IAccessible` | **`E_NOINTERFACE`** |

**全部 `E_NOINTERFACE` (0x80004002)。**

这说明 Chromium 只做了**单向映射**：把自己的无障碍树映射成 UIA 元素供读取，
但**没有实现任何 Provider 侧的接口**，因此不存在反向操作的通路。

### 2.4 子树枚举：没有隐藏的 Edit

"也许真正的编辑节点在更深处"是一个常见假设。用 UIA 原生
`FindAll(TreeScope_Subtree)` 枚举（绕开我们自己的树构建）：

```
返回 2 个节点:
  [Group] name='我的手机'   cls='ProseMirror ExEditor-qq-msg-editor is-empty'
  [Text ] name='\n'        cls='ProseMirror-trailingBreak'
含 Edit 节点: 否
```

**整棵子树只有 2 个节点。** 假设不成立。

### 2.5 TextPattern：只读，且没有 TextRange 对象

`TextPattern` 确实存在，能读文本。但：

```
DocumentRange.GetText(-1)    = '\n'
SupportedTextSelection       = 1
GetSelection.Length          = 1
  selection[0].GetText(-1)   = ''
GetVisibleRanges.Length      = 0
range has SetText?           = False        ← 没有任何写入方法
ITextRangeProvider           = E_NOINTERFACE ← 连 range 对象都拿不到
```

**`TextPattern` 是读取接口**。它能告诉你编辑器里有什么文字，
但 UIA 在设计上就不提供通过它写入的能力。

### 2.6 LegacyIAccessible：方法存在，但未实现

```
CurrentName          = '我的手机'
CurrentValue         = ''
CurrentRole          = 20         ← ROLE_SYSTEM_GROUPING（不是 TEXT/EDIT！）
CurrentState         = 1048576    ← STATE_SYSTEM_FOCUSABLE
CurrentDefaultAction = ''
CurrentDescription   = ''

方法存在性：
  has SetValue          = True     ← 名字在
  has DoDefaultAction   = True
  has Select            = True
```

⚠️ **`has SetValue = True` 是一个陷阱。** 调用它返回：

```
COMError: (-2147467263, '尚未实现')     ← E_NOTIMPL
```

**COM 接口声明里方法总是存在，Provider 可以合法地返回 `E_NOTIMPL`。**
所以"方法存在"永远不能当作"能力存在"，必须真的调用一次。

### 2.7 MSAA 独立路径：更差，不是更好

有人会建议"UIA 不行就试 MSAA"。实测：**MSAA 比 UIA 弱得多。**

```
通过 AccessibleObjectFromWindow(OBJID_CLIENT) 得到 IAccessible
遍历结果：7 个节点，全部是 ROLE_SYSTEM_PANE (0x10)
有名字的：只有窗口标题 '我的手机'
```

```
accValue(child)   → TypeError（读值就异常）
set_accValue(0)   → COMError -2147467259 '未指定的错误'
```

**原因**：Chromium 已基本放弃 MSAA。它保留 MSAA 入口（避免老程序崩溃），
但只导出极浅的窗口层级。**MSAA 不是 UIA 的替代品，是它的前身。**

### 2.8 原生 HWND 子窗口：不存在

> **判据已被 2.9.3 修正**：本节问的是“进程里有原生 `Edit` 子窗口吗”，而 `WM_SETTEXT`
> 真正需要的是“**元素自己有非零 `CurrentNativeWindowHandle`**”。本节的实测数据仍然有效，
> 结论（QQ 不适用）也恰好相同，但**推理过程不可推广到其他控件**。

`WM_SETTEXT` 需要真实的 Win32 窗口句柄。枚举：

```
QQ 顶层窗口 23 个，子窗口类型：
  'Chrome_RenderWidgetHostHWND'   ← Chromium 的渲染宿主
  'Intermediate D3D Window'       ← D3D 合成
原生 Edit 控件: 无
```

QQ 是纯 Electron，**没有原生 Edit 子窗口**，`WM_SETTEXT` 不适用。

---

### 2.9 第二轮探测：`TextEditPattern` 与元素级 HWND（新结论）

> 本节由 `tools/probe_write_routes.py` 与 `tools/probe_textedit_state.py` 产出（均可在本仓库直接重跑）。

第一轮探测留下了两个真空白，已补测。**结论：QQ 输入框依然写不进去，但原文给出的
理由有两处不准确**，若不修正，后来者会按错误的前提去评估其他控件。

复现工具：`tools/probe_write_routes.py`、`tools/probe_textedit_state.py`（本轮新增）。

### 2.9.1 `TextEditPattern`：**真的存在，且真的取得到**

在同一个 QQ 输入框（`class='ProseMirror is-empty ExEditor-qq-msg-editor'`）上：

```
-- does it CLAIM support? --
  Value              [1st] available=False
  Text               [1st] available=True
  LegacyIAccessible  [1st] available=True
  Invoke             [1st] available=False
  TextEdit           [8.1] available=True      ← 原文未测
  Text2              [8.1] available=False

-- can the pattern ACTUALLY be fetched? --
  Text               [1st] FETCHED  queried
  TextEdit           [8.1] FETCHED  queried      ← 原文未测
```

**所以原文 2.3 节"全部 `E_NOINTERFACE`"的说法只对 *Provider 侧* 接口成立。**
`GetCurrentPattern(UIA_TextEditPatternId)` 是**成功**的，取到的对象
`QueryInterface(IUIAutomationTextEditPattern)` 也**成功**。
这是原文漏掉的一条真实通道。

### 2.9.2 但它依然不能写：range 是空指针，且没有 `SetText`

`TextEditPattern` 的两个 range 都取得到，但**底层指针为 NULL**，且**没有写入方法**：

```
GetActiveComposition   range obtained
    has SetText = False
    GetText -> ValueError (NULL COM pointer access)
GetConversionTarget    range obtained
    has SetText = False
    GetText -> ValueError (NULL COM pointer access)
```

"空指针"这个结果本身是有歧义的，所以又做了**状态对照实验**——因为这两个 range 的语义是
"当前输入法组合串"，在**未聚焦 / 空编辑器**下返回 NULL 是设计行为，不能据此判死：

| 状态 | 操作 | `GetActiveComposition` | `HasKeyboardFocus` |
|---|---|---|---|
| A（原状，`is-empty`） | — | NULL，无 `SetText` | 0 |
| B（`SetFocus()` 之后） | 仅 UIA `SetFocus`，无任何按键 | **仍然** NULL，无 `SetText` | **1** |

聚焦后状态确实变了（`HasKeyboardFocus = 1`，class 里出现 `ProseMirror-focused`），
但两个 range **依然是 NULL**。同时 `TextPattern` 读到：

```
TextPattern document text = '\n'
TextPattern selection len = 1
TextPattern selection text = ''
selection range has SetText = False
```

**结论**：`TextEditPattern` 在 Chromium 里只用于输入法组合串上报，**它不是一个通用写入接口**。
编辑区为空时，两个 range 都没有可操作对象，因此无字可写。

> **这一条值得单独记住**：原文第二条铁律是"方法存在 ≠ 有实现"。
> 与其对称的另一半同样重要：**"方法返回 NULL 一次 ≠ 永远不可用"**。
> range 类接口必须在它被定义的那个状态（此处是输入法组合进行中）下测，否则会把
> "当前无数据"误判为"接口不存在"。本轮没有用合成输入去构造组合态，因为那正是项目
> 拒绝的路线；因此 2.9.2 的结论限定为**"在无合成输入的前提下不可用"**。

### 2.9.3 元素级 HWND：原文的判据写错了（真实缺口）

> **✅ 已修复并落地**（v0.2）：本节描述的缺口已实现并端到端验证通过，见 **2.10**。

原文 2.8 节用"枚举进程顶层窗口，找 `Edit`/`RichEdit` 子窗口"来判定 `WM_SETTEXT` 是否适用，
并据此说"QQ 没有原生 Edit 子窗口，`WM_SETTEXT` 不适用"。

**这个判据是错的。** `WM_SETTEXT` 的正确前置条件不是"存在原生 Edit 子窗口"，
而是"**目标元素自己有非零的 `CurrentNativeWindowHandle`**"。实测该控件：

```
NativeWindowHandle = 0      ← 该元素不映射任何 HWND
```

**结论恰好相同（不适用），但原因不同。** 差别在于判据：按元素级 HWND 判，
每一个控件都能各自给出确定的答案；按"进程里有没有 Edit 子窗口"判，会
**把同一进程内本来可写的原生控件也一起误杀**（一个 Electron 应用完全可能同时
自带可写的原生 Edit 子窗口和自绘编辑器）。

**同时暴露一个实现缺口**：`NativeRef`（`somnus/ir/node.py`）当前只有
`window_id / runtime_id / automation_id / class_name / control_type / path`，
**没有保存元素级 HWND**。因此原文第六节流程图第 1 步在当前代码里**无从执行**。
若要启用 `WM_SETTEXT` / `EM_REPLACESEL` 通道，必须先把
`CurrentNativeWindowHandle` 存进 `NativeRef`。

### 2.9.4 `ValuePattern2` 不存在（避免一个伪造结论）

本轮曾把 `ValuePattern2` / `IUIAutomationValuePattern2` 当作"第二代 pattern"列入探测表。
查本机 typelib 全部 `UIA_*PatternId` 常量后确认：

```
UIA_ValuePatternId  10002        ← 只有这一个
（无 UIA_ValuePattern2Id，无 IUIAutomationValuePattern2）
```

**UIA 从未定义过 `ValuePattern2`。** `ValuePattern.SetValue` 是 UIA 唯一的取值写入接口。
第一轮探测把它漏掉无所谓；但如果像本轮初稿那样去"探测"一个不存在的常量，
输出会是一行看起来像实测数据的 `MISSING`，**实际上什么也没测**。
值家族里真正属于新代的是 `TextPattern2` / `TransformPattern2` / `SelectionPattern2`（均 8.1+）。

---

## 2.10 已实现的第三条写入通道：元素级 HWND（实测数据）

本节记录 2.9.3 指出的缺口**已修复**后的实测结果。工具：直接跑 runtime。

### 2.10.1 通道链（现在的实际顺序）

`set_text` 现在是一条**有序通道链**，由解析器按条件的实时求值决定跑哪一条：

| 顺序 | primitive | 条件 | 前提 | 抢焦点 |
|---|---|---|---|---|
| 1 | `set_text` | `has:value` | `ValuePattern` | 否 |
| 2 | **`set_text_hwnd`** | **`hwnd`** | **元素级 `NativeWindowHandle != 0`** | **否** |
| 3 | `type_text` | `no-hwnd` | 需 `--allow-keyboard` | **是** |

链在 `set_text` 能力内部完成，**LLM 依然只看到 `set_text` 一个名字**，
看不到哪条通道在跑——与 `type_text` 既有的处理方式一致。

### 2.10.2 端到端实测（记事本，窗口置后台）

通过 runtime 真实调用，目标窗口**故意置为后台**，前景始终是终端：

```
foreground before = 'π - SomnusUI'
notepad is foreground? False        ← 确实是后台

-- dispatch with notepad in the BACKGROUND --
  sent_len=3   got='后台一'          exact=True fg_still_background=True
  sent_len=14  got='background-two' exact=True fg_still_background=True
  sent_len=50  got='中中中…中'        exact=True fg_still_background=True
```

**3/3 精确写入，焦点一次未被抢走**，含 CJK 与 50 字符长串。
对比键盘路径（第五节的实测）：同样条件下 `SendInput` 是 **0/20 完整**，
且必然抢焦点。这就是这条通道值得存在的全部理由。

### 2.10.3 两个反例仍然正确

通道链必须有条件地失败，否则就是把"能写"当成"总能写"：

```
QQ 输入框（hwnd=0，无 ValuePattern）
→ ERR ACTION_NOT_SUPPORTED msg="...synthetic keyboard input is disabled"

记事本（hwnd=0x940B7E）
→ OK_UNVERIFIED rev=3
```

同一个 runtime、同一次会话，两个控件走不同通道，QQ 依然**如实报错**。

### 2.10.4 实现细节（都有理由，不是任意选择）

- **`native_window_handle` 入 `NativeRef`**，由缓存请求一并拉取（属性 id 30020）。
  一次元素属性单读会破坏批量化，这是缓存存在的意义。
- **`SendMessageTimeout`（2s，`SMTO_ABORTIFHUNG`）而非 `SendMessage`**：
  后者会阻塞到目标消息泵响应，一个模态对话框就能把 agent 永久卡死。
- **先 `EM_REPLACESEL`（含 `EM_SETSEL` 全选），才退到 `WM_SETTEXT`**：
  前者是"替换选区"语义、贴近用户编辑且保留撤销栈；后者是通用回退。
- **句柄为空时不发送**：发给 `HWND 0` 会直送桌面，因此该判断是**安全属性**，不是优化。
- **句柄从活元素上现读**（`_element_hwnd`），而非用快照时的旧值：
  元素重建后报的是它当下的句柄。

### 2.10.5 覆盖面（不要高估它）

实测全部已开窗口：**357 个元素中仅 25 个（7%）有非零 HWND**，
且多为窗口级骨架（任务栏、拖拽栏、树视图）。也就是说这条通道
**只对一部分原生控件有效**，自绘控件（QQ/Steam/VS Code）依旧不在其内。

它解决的是"原生控件因判据错误而被误杀"这一类，而不是"自绘控件可以写了"。

---

## 三、对照组：能写入的控件长什么样

| | 记事本编辑器 | QQ 输入框 |
|---|---|---|
| **ClassName** | `RichEditD2DPT` | `ProseMirror ExEditor-qq-msg-editor` |
| **ControlType** | `50030 Document` | `50026 Group` |
| **Value pattern** | ✅ 可用 | ❌ 不存在 |
| **IsReadOnly** | `0` | — |
| **`SetValue` ** | **✅ 实测生效 20/20** | — |
| **Text pattern** | 可用（只读） | 可用（只读） |
| **TextEdit pattern** | — | ✅ 存在且可取到，但 range 为 NULL 且无 `SetText`（见 2.9） |
| **元素级 HWND** | ✅ `RichEditD2DPT` | ❌ `0`（不是"进程里无 Edit 子窗口"） |
| **可写入** | **是** | **否** |

**决定性差别只有一行**：`ValuePattern` 存在与否。
`Text` pattern 两者都有——它**不能**用来判断可写性。
（`TextEdit` pattern 也同样不能：两列都取得到，但只有一列真的写得进去。）

---

## 四、已经验证走不通的路线（不要再试）

以下每一条都做过实验，记录于此以免重复。

### 4.1 MSAA `IAccessible::put_accValue`
→ Chromium 只给 7 个无名 Pane，`set_accValue` 返回 `COMError -2147467259`。

### 4.2 `IAccessibleEx` / `IRawElementProviderSimple` 桥接
→ 全部 `E_NOINTERFACE`。Chromium 未实现 Provider 侧接口。

### 4.3 `ITextRangeProvider` / `ITextProvider` / `ITextEditProvider`
→ 全部 `E_NOINTERFACE`。拿不到 TextRange 对象，也就无法在范围上做任何事。

> **⚠️ 这一条容易被误读，补充澄清（见 2.9）**：上面说的是向**元素**查询
> *Provider 侧*接口时全部 `E_NOINTERFACE`。但这**不等于** UIA 的
> `TextEditPattern` 不可用——实测 `GetCurrentPattern(UIA_TextEditPatternId)`
> 是**成功**的。两者不矛盾：Chromium 自己实现了 UIA 的 pattern 映射，
> 但没把底层 Provider 接口暴露给外部。**因此本节结论仅限于
> “不能绕过 UIA 直接用 Provider 接口”，不能引申为“TextEditPattern 不存在”。**
> 而 TextEditPattern 虽可取到，也依然不能写——原因见 2.9.2（range 为 NULL 且无 `SetText`），
> **不是因为 `E_NOINTERFACE`**。

### 4.4 UIA `TextPattern` 写入
→ `TextPattern` 无写入方法，`range has SetText = False`。设计上就是只读。

### 4.4b UIA `TextEditPattern` 写入（2.9 新增实测）
→ pattern 本身**取得到**（`available=True`，`GetCurrentPattern` 成功），
但两个 range（`GetActiveComposition` / `GetConversionTarget`）在聚焦前后
**均为 NULL 且无 `SetText`**。它是输入法组合串的上报接口，不是通用写入通道。

### 4.5 `LegacyIAccessible.SetValue`
→ 方法存在但 `E_NOTIMPL`。

### 4.6 虚拟桌面隔离 + 键盘
`MoveWindowToDesktop` 跨进程移动被**拒绝**：

```
COMError (-2147024891, '拒绝访问')
```

即使能移动，原理上也不成立：

- `SendInput` 送给**当前前台桌面的前台窗口**。目标在另一个桌面，键盘事件根本到不了它。
- 要输入就必须**切到那个桌面**，而切桌面会隐藏用户所有窗口，**比抢焦点打扰得多**。

### 4.7 单纯加大键盘输入的间隔
曾假设"丢字是因为发送太快"。实测否定了这个假设：

| 分块大小 | 间隔 | 完整写入率 |
|---|---|---|
| 8 字符 | 10ms | 6/30 |
| 4 字符 | 10ms | 7/30 |
| 1 字符 | 5ms | 6/30 |
| 8 字符 | **30ms** | 11/30 |
| 8 字符 | **60ms** | 8/30 |

**加大间隔没有本质改善**。原因：`SendInput` 只把事件排进目标线程的输入队列就返回，
字符是在目标的消息泵处理时被丢弃的，与发送速度无关。

---

## 五、键盘路径的固有问题

即使 `--allow-keyboard` 允许使用，键盘路径也有三个结构性问题。

### 5.1 必然抢焦点

实测（用户正在终端工作时执行 `set_text`）：

```
执行前前台: 'π - SomnusUI'（终端）
执行后前台: '我的手机'（QQ 被抢到前台）
```

元凶是 `element.SetFocus()`——这是 UIA 的语义，不是可选项。
而 `SendInput` 要求目标是前台，所以**抢焦点是键盘路径的前置条件**，无法绕过。

### 5.2 必然丢字

干净基线下写入 10 字符，重复 20 次：

```
完整 0/20    实际长度分布 = [0, 4, 5, 6, 7]
```

**不是偶发，是必然。** 丢的是**尾部连续字符**，读回值总是正确前缀。

### 5.3 结果无法验证

`SendInput` 返回成功只表示"事件已投递"，不代表"字符已落地"。
实测 `SendInput` 返回 `16/16` 时，目标实际只收到了 6 个字符。

---

## 六、探索新控件时的判断流程

```
目标控件
  │
  ├─ 1. 该元素自己有非零的 NativeWindowHandle 吗？
  │      有 → WM_SETTEXT / EM_REPLACESEL（零焦点，最干净）
  │      ★ 判据是【元素级 HWND】，不是“进程里有没有 Edit 子窗口”。
  │        后者会把同进程中本来可写的原生控件一起误杀（见 2.9.3）。
  │      ✅ 已实现：`native_window_handle` 已入 `NativeRef`，
  │        运行时通过 `set_text_hwnd` primitive 自动选路（见 2.10）。
  │
  ├─ 2. 有 UIA ValuePattern 吗？
  │      ① IsValuePatternAvailable = true
  │      ② GetCurrentPattern 真的取得到（不是 NULL）
  │      ③ CurrentIsReadOnly = false
  │      有 → SetValue（首选）
  │
  ├─ 3. 是 Chromium/Electron 且开了调试端口吗？
  │      有 → CDP Input.insertText
  │
  └─ 4. 都没有
          → 应用没有暴露写入能力。如实报 ACTION_NOT_SUPPORTED，
            不要用键盘偷偷改（除非显式 --allow-keyboard）
```

> 已实测排除、**不必再试**的中间选项（详见 2.9）：`TextEditPattern` 在 Chromium 上
> 虽然存在且取得到，但只用于输入法组合串上报，range 恒为 NULL 且无 `SetText`；
> `TextPattern` / `TextPattern2` 均为只读；`ValuePattern2` 不存在。

### 判断可写性的四条铁律

1. **`IsXxxPatternAvailable = true` 不等于能用** —— 必须真的 `GetCurrentPattern` 一次。
2. **方法存在不等于有实现** —— `has SetValue = True` 但调用返回 `E_NOTIMPL`。必须真的调用。
3. **`TextPattern` 存在不代表可写** —— 它是只读接口。**唯一的可写判据是 `ValuePattern`。**
   同样适用于 `TextEditPattern`：它与 `TextPattern` 是两个接口，可取到，但**不是通用写入入口**。
4. **返回 NULL 一次不等于永远不可用** —— 与第 2 条对称。像 `TextEditPattern` 的
   range 这类接口，只在特定状态（输入法组合进行中）才有值；必须在**它被定义的那个状态**下
   测，否则会把“当前无数据”误判成“接口不存在”。

### 探测的顺序建议

```
ControlType → ValuePattern(含 IsReadOnly) → 实际 SetValue 调用 → 读回校验
```

前三步都是只读探测，最后一步才是写入。

### 第二轮探测总览

| 条目 | 第二轮实测结果 |
|---|---|
| **`TextEditPattern` 存在且可取到** | ✅ 真（原文未测） |
| **`TextEditPattern` 能写入** | ❌ 否（range 恒 NULL，无 `SetText`）——**结论不变，但理由不同** |
| **`ValuePattern2` 存在** | ❌ 不存在（UIA 从未定义） |
| **原文 `WM_SETTEXT` 判据** | ❌ 判据写错（应为元素级 HWND，非“进程内有 Edit 子窗口”） |
| **元素级 HWND 通道已实现** | ✅ 是（记事本后台写入 3/3，焦点未抢，见 2.10） |
| **QQ 输入框可写** | ❌ 否（两个独立机制均已实测排除） |

---

## 七、结论

**没有可编程接口的自绘控件，不存在通用的无焦点写入方法。**

这不是库的选择问题（pywinauto / FlaUI / uiautomation 结果相同），
也不是调用方式问题，而是**应用没有暴露这个能力**。

具体来说，一个控件要能被外部写入，应用必须至少提供以下之一：

1. 该元素自己的原生 HWND（`CurrentNativeWindowHandle != 0`）→ `WM_SETTEXT` / `EM_REPLACESEL`
2. `ValuePattern` 实现（→ `SetValue`）
3. Chromium 调试端口（→ CDP）
4. 自定义 UIA Provider / IPC 接口（→ 需应用配合）

> 第 1 项已修正：原文写作“原生 `Edit`/`RichEdit` **子窗口**”，判据过宽且会误杀
> 同进程内可写的原生控件。正确的判据是**元素自己映射的 HWND**。
>
> 另：**`TextEditPattern` 不属于上面四条中的任何一条**。它看上去像第 4 条
> （一个真正的 UIA 接口），但实测只能上报输入法组合串，不能写入。

四者皆无时，可选方案只剩下：**应用专用集成** 或 **合成输入**（有上述固有问题）。

SomnusUI 的选择是：**如实报告这个事实**，而不是用合成输入掩盖它。

---

## 附录：复现实验

本报告所有数据可通过以下方式复现。核心是把 UIA 的两个问题分开问：
**"你声称支持吗"** 和 **"取出来真的能用吗"**。

**第二轮探测（2.9 节）已封装为可直接运行的工具**，无需手写脚本：

```bash
python tools/probe_write_routes.py --list                 # 列出候选输入控件
python tools/probe_write_routes.py --window w4 --match ExEditor-qq-msg-editor
python tools/probe_write_routes.py --window w4 --match ExEditor-qq-msg-editor --write
python tools/probe_textedit_state.py                      # 聚焦前后状态对照（2.9.2）
```

这两个工具已覆盖完整 pattern 矩阵（含 `TextEditPattern` / `TextPattern2`）、
元素级 HWND、以及"声称 vs 真取到 vs 真写入"的三级验证。

下面是原始复现方式（已保留）。注意 `PROBE` 列表已在 2.9 中被扩展：

```python
from somnus.providers.windows.uia import UiaProvider

provider = UiaProvider()
UIA = provider._UIA
tree = provider.snapshot(window_id)
node = next(n for n in tree.walk() if "目标class名" in (n.native.class_name or ""))

# 要探测的 pattern：显式列出，不要用 getattr(..., 0) 拼名字，
# 一个不存在的属性 id (0) 会让 GetCurrentPropertyValue 直接抛 COMError。
PROBE = [
    ("Value", "UIA_IsValuePatternAvailablePropertyId",
     "UIA_ValuePatternId", "IUIAutomationValuePattern"),
    ("Text", "UIA_IsTextPatternAvailablePropertyId",
     "UIA_TextPatternId", "IUIAutomationTextPattern"),
    ("LegacyIAccessible", "UIA_IsLegacyIAccessiblePatternAvailablePropertyId",
     "UIA_LegacyIAccessiblePatternId", "IUIAutomationLegacyIAccessiblePattern"),
    ("Invoke", "UIA_IsInvokePatternAvailablePropertyId",
     "UIA_InvokePatternId", "IUIAutomationInvokePattern"),
    # ↓ 第二轮补充（见 2.9）。注意：不要写 ValuePattern2——它不存在。
    ("TextEdit", "UIA_IsTextEditPatternAvailablePropertyId",
     "UIA_TextEditPatternId", "IUIAutomationTextEditPattern"),
    ("Text2", "UIA_IsTextPattern2AvailablePropertyId",
     "UIA_TextPattern2Id", "IUIAutomationTextPattern2"),
]


def probe():
    el = provider._locate(node.native)

    print("--- 它声称支持吗 ---")
    for name, avail_attr, _pid, _iface in PROBE:
        try:
            avail = bool(el.GetCurrentPropertyValue(getattr(UIA, avail_attr)))
        except Exception:
            avail = "?"
        print(f"  {name}: available={avail}")

    # 关键：真的取一次。这一步才能区分"声称可用"和"真的可用"。
    print("--- 真的能用吗（决定性）---")
    for name, _avail, pid_attr, iface_attr in PROBE:
        try:
            p = el.GetCurrentPattern(getattr(UIA, pid_attr)).QueryInterface(
                getattr(UIA, iface_attr))
            print(f"  {name}: 取到")
            if name == "Value":
                print(f"     IsReadOnly = {p.CurrentIsReadOnly}")
                # 要真正确认可写，还需实调 SetValue 并读回校验，见正文 2.5
        except Exception as e:
            print(f"  {name}: {type(e).__name__}")


provider._executor.submit(probe).result(timeout=10)
```

要把"声称可用"和"真的可用"分开看 —— 后者才是决定性的。
再进一步：连"真的取到"也不够（`LegacyIAccessible.SetValue` 取得到但调用返回 `E_NOTIMPL`），
只有**实际写入并读回校验**才是终极判据。

### 实测输出（QQ ProseMirror 输入框）

```
--- 它声称支持吗 ---
  Value: available=False
  Text: available=True
  LegacyIAccessible: available=True
  Invoke: available=False
--- 真的能用吗（决定性）---
  Value: ValueError
  Text: 取到
  LegacyIAccessible: 取到
  Invoke: ValueError
```
