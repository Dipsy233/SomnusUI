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
| **`WM_SETTEXT`** | 存在原生 HWND `Edit`/`RichEdit` | 否 | 否 | ✅ 可行，**零焦点要求** |
| **CDP `Input.insertText`** | Chromium 应用开了调试端口 | 否 | 否 | ✅ 可行，但需应用配合 |
| **UIA `LegacyIAccessible.SetValue`** | MSAA Provider 实现了它 | 否 | 否 | ❌ 通常 `E_NOTIMPL` |
| **UIA `TextPattern`** | 控件实现 `Text` pattern | 否 | 否 | ❌ **只读**，无写入方法 |
| **MSAA `IAccessible::put_accValue`** | 同上 | 否 | 否 | ❌ Chromium 只给无名 Pane |
| **`IAccessibleEx` 桥接** | Provider 实现它 | — | — | ❌ `E_NOINTERFACE` |
| **虚拟桌面隔离 + 键盘** | 能把窗口移走 | **是** | **是** | ❌ 移不动 + 切桌面更打扰 |
| **`SendInput` 键盘** | 目标在前台 | **是** | **是** | ⚠️ 可写但 0/20 丢字 |

**判断顺序**（从上到下，第一个可用的即是答案）：

```
1. 有没有原生 HWND Edit/RichEdit 子窗口？  → WM_SETTEXT
2. 有没有 UIA ValuePattern？                → SetValue
3. 是不是 Chromium 应用且开了调试端口？      → CDP
4. 都没有                                   → 应用没暴露写入能力，如实报错
```

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

`WM_SETTEXT` 需要真实的 Win32 窗口句柄。枚举：

```
QQ 顶层窗口 23 个，子窗口类型：
  'Chrome_RenderWidgetHostHWND'   ← Chromium 的渲染宿主
  'Intermediate D3D Window'       ← D3D 合成
原生 Edit 控件: 无
```

QQ 是纯 Electron，**没有原生 Edit 子窗口**，`WM_SETTEXT` 不适用。

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
| **原生 HWND** | ✅ `RichEditD2DPT` | ❌ 无 |
| **可写入** | **是** | **否** |

**决定性差别只有一行**：`ValuePattern` 存在与否。
`Text` pattern 两者都有——它**不能**用来判断可写性。

---

## 四、已经验证走不通的路线（不要再试）

以下每一条都做过实验，记录于此以免重复。

### 4.1 MSAA `IAccessible::put_accValue`
→ Chromium 只给 7 个无名 Pane，`set_accValue` 返回 `COMError -2147467259`。

### 4.2 `IAccessibleEx` / `IRawElementProviderSimple` 桥接
→ 全部 `E_NOINTERFACE`。Chromium 未实现 Provider 侧接口。

### 4.3 `ITextRangeProvider` / `ITextProvider` / `ITextEditProvider`
→ 全部 `E_NOINTERFACE`。拿不到 TextRange 对象，也就无法在范围上做任何事。

### 4.4 UIA `TextPattern` 写入
→ `TextPattern` 无写入方法，`range has SetText = False`。设计上就是只读。

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
  ├─ 1. 有原生 HWND Edit/RichEdit 子窗口吗？
  │      有 → WM_SETTEXT（零焦点，最干净）
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

### 判断可写性的三条铁律

1. **`IsXxxPatternAvailable = true` 不等于能用** —— 必须真的 `GetCurrentPattern` 一次。
2. **方法存在不等于有实现** —— `has SetValue = True` 但调用返回 `E_NOTIMPL`。必须真的调用。
3. **`TextPattern` 存在不代表可写** —— 它是只读接口。判据是 `ValuePattern`。

### 探测的顺序建议

```
ControlType → ValuePattern(含 IsReadOnly) → 实际 SetValue 调用 → 读回校验
```

前三步都是只读探测，最后一步才是写入。

---

## 七、结论

**没有可编程接口的自绘控件，不存在通用的无焦点写入方法。**

这不是库的选择问题（pywinauto / FlaUI / uiautomation 结果相同），
也不是调用方式问题，而是**应用没有暴露这个能力**。

具体来说，一个控件要能被外部写入，应用必须至少提供以下之一：

1. 原生 `Edit`/`RichEdit` 子窗口（→ `WM_SETTEXT`）
2. `ValuePattern` 实现（→ `SetValue`）
3. Chromium 调试端口（→ CDP）
4. 自定义 UIA Provider / IPC 接口（→ 需应用配合）

四者皆无时，可选方案只剩下：**应用专用集成** 或 **合成输入**（有上述固有问题）。

SomnusUI 的选择是：**如实报告这个事实**，而不是用合成输入掩盖它。

---

## 附录：复现实验

本报告所有数据可通过以下方式复现。核心是把 UIA 的两个问题分开问：
**"你声称支持吗"** 和 **"取出来真的能用吗"**。

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
