# 粘贴板通道实测报告

**目的**：记录**用粘贴板向无权接口的控件写入文本**这条通道的全部实测事实，
包括它为什么在"UIA 控件写不进去"时仍然可用、它的代价是什么、以及
哪些围绕它的常见方案（延迟渲染、粘贴板历史删除）在本环境实际不成立。

这份文档与 [interface-capability-report.md](interface-capability-report.md) 互补：
那一份回答"能不能通过控件自己的接口写"，这一份回答"控件没有可写接口时，粘贴板这条路怎么样"。

所有数据来自 Windows 11 24H2（22631），程序化获取，不用截图、不用人工操作。

---

## 一、测量状态（阅读前必读）

本报告的文字整理时，桌面会话处于**锁屏**状态，因此**部分结论无法当场重测**。
为避免把旧数字当成新验证，每条结论标注状态：

| 状态 | 含义 | 涉及条目 |
|---|---|---|
| ✅ **已重测** | 写文档期间当场跑通 | 4.4（无回滚）、5.3（promise 信号不可用）、锁屏行为 |
| 📌 **已验证，待二次确认** | 前几轮实测有效，但本次因锁屏未能重跑 | 3.1（粘贴耗时）、3.2 |
| 📌 **已验证（引用第三方实现）** | 本机实测 + 独立开源实现交叉验证 | 4.1（opt-out 格式）、4.2（WinRT 删除）、4.3（恢复代价） |

**锁屏的限制是真实的，不是借口**（实测）：

```
LogonUI 在运行  →  会话已锁定
OpenClipboard()  →  False，错误“拒绝访问”
WinRT ClearHistory() →  CLIPBRD_E_CANT_OPEN (0x800401D0)
```

**对粘贴通道本身也是一个重要发现**：粘贴需要前台窗口且需要能打开粘贴板，
**锁屏时整个通道不可用**。这是它相对 ValuePattern / 元素级 HWND 的又一个前提条件。

---

## 二、总表

| 结论 | 实测结果 |
|---|---|
| **粘贴写入：投递量与文本长度无关** | ✅ 10~3000 字符耗时恒定 **~37 ms** |
| **粘贴写入：不需要 UIA 接口** | ✅ 对 ProseMirror 等不可写控件有效 |
| **对比：`SendInput` 逐字符打字** | ❌ 100 字符 → 3098 ms，实际只落 14 个（见 3.1） |
| **粘贴板历史会记录我们的文本** | ⚠️ 默认会；**加四条 opt-out 格式可预防** |
| **opt-out 格式有效性** | ✅ **实测 count 1 → 0**（4.1） |
| **延迟渲染（promise）读取信号** | ❌ **在本环境不可用**（OLE 抢先实体化，5.3） |
| **WinRT 历史删除 API** | ✅ 可用，**5/5 干净删除**（4.2） |
| **恢复原粘贴板（纯文本）** | ✅ 无损（4.3） |
| **恢复原粘贴板（图片/文件）** | ❌ **会永久丢失**（4.3） |

---

## 三、为什么粘贴能写进 UIA 写不进的控件

### 3.1 核心区别：文本走哪条路

这是整份报告最重要的一条。粘贴与打字**都在用 `SendInput`**，
但**文本载体不同**，因此命运完全不同：

| | 按键事件数 | 文本载体 | 实测结果 |
|---|---|---|---|
| **打字** | N 个字符 = **2N 个队列条目** | 输入队列 | ❌ 队列堆积，目标一次只消费一小批 |
| **粘贴** | `Ctrl+V` = **固定 4 个事件** | **粘贴板** | ✅ 目标一次读走全文 |

**实测（Notepad RichEdit，一次 `SendInput` 发 N 个字符）**：

```
  10 字符:  35.9 ms  exact=True
 100 字符:  37.8 ms  exact=True
1000 字符:  37.0 ms  exact=True
3000 字符:  37.3 ms  exact=True     ← 文本量 ×300，耗时不变
```

**粘贴耗时与文本量完全无关。** 因为队列里始终只有 4 个按键事件，
文本是目标**自己**在 `WM_PASTE` 处理里一次性取走的，不经过输入队列。

> **数据状态**：上表为首次实测结果。事后复测时桌面会话处于**锁屏**状态
> （`LogonUI` 进程在运行，`GetForegroundWindow()` 返回空），而粘贴通道
> **必须目标在前台**才能测，因此未能在锁屏下重测。此条属于**已验证但未二次确认**，
> 重新解锁后建议重跑一次（复现脚本见附录）。

而打字：

```
打字 100 字符 → 3098 ms，只落 14 个
   单批 20 个事件 → 落 2 个
```

> **推论**：键盘通道的"滞留"病根是"把 N 个字符切碎成 2N 个事件塞进队列"。
> **粘贴路径上根本不存在这个问题**，因为文本从不进入队列。
> 这也是粘贴在"控件没有任何可写接口"时仍然可用的原因——它靠的是
> **应用自己的 `WM_PASTE` 实现**，而不是某个 UIA 接口。

### 3.2 与元素级 HWND 通道的关系

粘贴和 `EM_REPLACESEL` 属于**同一类机制**（应用自己的接口），
但前提不同：

| 通道 | 前提 | 抢焦点 |
|---|---|---|
| `ValuePattern.SetValue` | 控件发布 Value 模式 | 否 |
| `EM_REPLACESEL` / `WM_SETTEXT` | **元素自己有 HWND** | 否 |
| **粘贴（`Ctrl+V`）** | **目标在前台 + 会处理 `WM_PASTE`** | **是** |
| `SendInput` 逐字符 | 目标在前台 | 是 |

**注意粘贴需要前台焦点**——这是它相对 HWND 通道的劣势，
但相对逐字符打字，它避免了队列滞留。

---

## 四、粘贴板的代价与应对

### 4.1 隐私泄漏：opt-out 格式**可以预防**（最强结论）

写 `CF_UNICODETEXT` 而不加任何标记，内容会进入 **Windows 粘贴板历史**
与 **云同步**（若开启）。实测：

```
A. 普通 Win32 写入                    → 历史 count = 1   （WIN32_PLAIN 被记录）
B. 同一次写入 + 四条 opt-out 格式      → 历史 count = 0   （根本没被记录）
```

> **数据状态**：首次实测如上。事后复测遇到**锁屏**：
> 会话锁定时 `OpenClipboard` 直接返回**“拒绝访问”**（实测），
> WinRT `ClearHistory` 报 `CLIPBRD_E_CANT_OPEN (0x800401D0)`，
> 因此无法在锁屏下重跑。本条属于**已验证但未二次确认**。
> 重新解锁后建议重跑（复现脚本见附录）。


四条格式（Windows 官方定义的协议）：

```
ExcludeClipboardContentFromMonitorProcessing    存在即可
CanIncludeInClipboardHistory        = DWORD 0   ← 必须是 0
CanUploadToCloudClipboard           = DWORD 0   ← 必须是 0
Clipboard Viewer Ignore                        存在即可
```

**独立的第三方实现验证了这套协议**（不是单一来源）：

- **CopyQ**（`src/platform/win/winplatformclipboard.cpp:30-48`）：四条全处理，
  且额外识别 `ExcludeClipboardContentFromMonitorProcessing == "1"` 这种 Qt 应用形态
- **CoPanda**（`crates/cp-win/src/formats.rs`）：有 `concealed` / `denied_when_zero`
  的显式模型，并多识别一个 `ExcludeClipboardDataFromMonitorProcessing`

> ⚠️ **这是"合作协议"，不是强制机制。** 它挡住 Windows 历史服务和
> **守规矩**的管理器，挡不住无视协议的程序。`win-text-inject` 的 README
> 对此说得很直白，不要夸大成效。

### 4.2 事后删除：**可行，但不如预防**

WinRT 提供官方删除 API：

```
Windows.ApplicationModel.DataTransfer.Clipboard
  DeleteItemFromHistory(item)    ← 删除单条
  ClearHistory()                 ← 清空全部
  GetHistoryItemsAsync()         ← 枚举（Id / Timestamp / Content）
  IsHistoryEnabled()
  SetHistoryItemAsContent(item)
```

**实测（写 5 次，删 5 次）**：

```
trial 0: 出现+321ms  删除+328ms  api=True  gone=True
trial 1: 出现+88ms   删除+110ms  api=True  gone=True
trial 2: 出现+148ms  删除+156ms  api=True  gone=True
trial 3: 出现+300ms  删除+306ms  api=True  gone=True
trial 4: 出现+112ms  删除+125ms  api=True  gone=True

cleanly removed: 5/5     leftover entries: 0
```

**选择性删除正确**（不误删其他条目）：

```
seeded, count = 2                        ← KEEP_ME_PLEASE + DELETE_ME_NOW
delete returned True
count after delete = 1
   survivor: KEEP_ME_PLEASE              ← 未受影响
```

**但事后删除有三个代价**：

1. **存在可见时间窗**：实测文本进入历史到被删除之间 **88~350 ms**，
   这期间它在历史 UI 里是可见的
2. **需要 STA 线程 + WinRT**：控制台默认 MTA，`SetContent` 直接报
   `CO_E_NOTINITIALIZED (0x800401F0)`；且 `await` 的续体会掉回线程池导致再次失败，
   必须自建消息泵（`SynchronizationContext` 实现）
3. **不能靠改文件**：`%LOCALAPPDATA%\Microsoft\Windows\Clipboard\HistoryData`
   **拒绝访问**（系统保护，实测 `Access to the path ... is denied`），
   只能用 API

**结论**：既然 opt-out 格式已经能预防（4.1），**没有理由选择事后删除**——
前者是零时间窗、零额外依赖。

### 4.3 恢复原粘贴板：纯文本无损，**图片/文件会永久丢失**

| 原内容 | 保存→写入→恢复 | 结果 |
|---|---|---|
| **纯文本** | ✅ **无损** | 文本一致，格式列表前后一致（`CF_UNICODETEXT`/`CF_LOCALE`/`CF_TEXT`/`CF_OEMTEXT`），丢失 0、新增 0 |
| **图片** | ❌ **被销毁且无法恢复** | 实测 `{image: True}` → `{image: False}` |

实测图片那一组：

```
放入图片        {text: None, image: True, bitmap: True}
跑 save/write/restore  {text: 'PAYLOAD', image: False, bitmap: False}
```

**所以实现必须判断**：粘贴板不是纯文本时**拒绝使用该通道**，
否则会静默毁掉用户的截图，比抢焦点更糟（静默且持久）。

### 4.4 粘贴板**没有**回滚/撤销

写两个值，序列号每次递增，任意时刻只能读到一个：

```
写 FIRST_VALUE_AAA   seq=3284
写 SECOND_VALUE_BBB  seq=3289
可读内容 = 'SECOND_VALUE_BBB'      ← 前一个已经没了
```

**没有 API 层的"回滚到上一条"。** Win+Ctrl+V 的历史 UI 是独立系统功能，
程序无法把它当回滚接口用。因此**必须先读出来、用完再写回去**。

---

## 五、已实测否定：延迟渲染（delayed render / promise）

### 5.1 这套机制是什么

`win-text-inject`（Rust crate，被 dictation 工具使用）提出的方案：
不发布文本，而是**发布承诺**——`SetClipboardData(CF_UNICODETEXT, NULL)`
配一个隐藏的所有者窗口。Windows 在**消费者真正索取时**向所有者发
`WM_RENDERFORMAT`，所有者再现场提供数据。

**该消息就是"目标已读取"的信号**，理论上是解决"恢复粘贴板与目标读取赛跑"
的优雅办法（所有固定延时都是猜测）。

### 5.2 机制本身**可用**

实测（发布 promise，让 Notepad 粘贴）：

```
advertised = True    owner = True          ← 承诺已发布，不含文本
notepad content = 'PROMISE_ONLY_PAYLOAD_xyz'   ← 目标确实拿到了
```

### 5.3 但**读取信号在本环境不可用**（决定性）

实测：promise 发布后 **~3-5 ms 就出现一次 `WM_RENDERFORMAT`**，
**且无人索取**：

```
=== 粘贴板历史 OFF，无监听器、无观察窗口 ===
trial 0: 1 次主动 render，+4.8ms
trial 1: 1 次主动 render，+3.1ms
trial 2: 1 次主动 render，+3.6ms

空闲 1 秒的粘贴板序列号变化 = 0     ← 不是其他进程在写
```

**归因过程**（先后排除）：

| 假设 | 实测 | 结论 |
|---|---|---|
| 粘贴板历史服务 | 关闭 `EnableClipboardHistory` 后**仍然发生** | ❌ 不是 |
| 我们自己的监听器 | 完全不注册监听器后**仍然发生** | ❌ 不是 |
| **OLE 剪贴板层** | 无其他解释，且与格式无关 | ✅ 是 |

**后果**：一旦 promise 被实体化，粘贴板就持有真实数据，
Windows **不再发后续的 `WM_RENDERFORMAT`**，目标的读取**从"可观测"变成"无法观测"**——

```
=== 渲染相对"发送粘贴按键"的时机（5 次试验）===
trial 0: baseline=1  粘贴后 render=0  投递成功
trial 1: baseline=1  粘贴后 render=0  投递成功
trial 2: baseline=1  粘贴后 render=0  投递成功
```

**目标粘贴时完全不产生第二次 render。** 因此 `win-text-inject` 的
`mark_paste_sent` + baseline 方案**在本环境无法救回信号**，
其 `wait_for_target_read` 会**永远超时**，只能退回到它自己文档里写的
"退回定时器 + 报告 `read_confirmed: false`"。

> **这不是该 crate 的缺陷**——它在 README 里明确预见并如实报告了这一情形
> （"promise was already consumed → 退回定时器"）。只是本机环境正好落在那个分支上。

### 5.4 旁证：promise 一样会泄漏进历史

测试期间写入的 payload（`BASELINE_TEST_PAYLOAD`、`FINAL_CHECK_PAYLOAD` 等）
**确实出现在粘贴板历史里**——说明 promise 被 OLE 实体化后，
与普通写入在隐私上没有区别。

### 5.5 结论

**promise 相对"直接写普通粘贴板"没有任何实际优势。**
它多出来的唯一东西（可观测的读取信号）在本环境拿不到，
却要付出隐藏窗口 + 消息泵线程 + 单次 render 语义的复杂度。

---

## 六、实现建议

若要启用粘贴通道，按以下顺序，**前三条都是实测支撑的硬要求**：

1. **写入时附带四条 opt-out 格式**（预防，而非事后删除）
   — 实测 count 1 → 0；成本极低
2. **粘贴板非纯文本时拒绝使用该通道**
   — 实测图片会被永久销毁且无法恢复
3. **恢复原文本，并如实报告是否确认**
   — 纯文本无损；但恢复时序**只能靠"等一段安静时间"**，
   因为 promise 信号不可用（第五节），必须诚实标注"未确认"
4. **不用 delayed render** — 无优势，纯增复杂度
5. **不做事后删除** — 有时间窗（88~350ms）+ STA/WinRT 依赖；
   既然 opt-out 能预防就不需要它

### 通道定位（相对其他三档）

```
set_text 通道链：
  1. ValuePattern.SetValue       应用自己的接口，无焦点
  2. EM_REPLACESEL / WM_SETTEXT  元素自己映射的 HWND，无焦点
  3. 粘贴（Ctrl+V）              ← 本文档：需要前台，但不受队列长度限制
  4. SendInput 逐字符            最后手段，有队列滞留问题
```

粘贴应排在键盘**之前**（它没有滞留问题，且实测 3000 字符 37ms），
但排在两类无焦点通道**之后**（它需要前台，且会扰动用户粘贴板）。

---

## 附录：复现方式

```powershell
# 检查历史是否开启
Get-ItemProperty HKCU:\Software\Microsoft\Windows\Clipboard | Select EnableClipboardHistory

# 历史存储（系统保护，只读都拒绝）
# %LOCALAPPDATA%\Microsoft\Windows\Clipboard\HistoryData

# WinRT 枚举/删除需要 STA 线程，控制台默认 MTA 会报 0x800401F0
# 且 await 续体会掉回 MTP 线程池，必须自建消息泵
```

### 锁屏时的行为（实测）

```powershell
# 会话锁定时（LogonUI 进程存在）：
#   OpenClipboard()      -> False, 错误 = 拒绝访问
#   WinRT ClearHistory() -> CLIPBRD_E_CANT_OPEN (0x800401D0)
# 因此粘贴通道在锁屏下完全不可用；无焦点通道（ValuePattern / HWND）不受影响。
```

### 待重测项

解锁后建议重跑（本次因锁屏未能执行）：

1. 粘贴耗时 vs 长度（3.1）——需目标在前台
2. opt-out 格式的预防效果（4.1）——需能打开粘贴板

参考项目（`E:\dao\ui\`）：

| 项目 | 价值 |
|---|---|
| `win-text-inject-main` | 唯一完整记录 promise / OLE / 多读消费者 / 静默失败（UIPI、修饰键污染、`SetClipboardData` 假失败）的工程细节 |
| `CopyQ-master` | opt-out 格式的权威参考实现（含 DWORD=0 判定与两种形态） |
| `CopyPaste-main`（CoPanda） | 同上，`refusal` 抽象更清晰，多一个格式名 |
| `QT4C-master` | `PostMessage(WM_CHAR)` 技术来源（已实测否定，见另一份报告 4.8） |

三者的共同点：**都不提供写入通道**，但合计给出了"把粘贴做对"所需的全部知识。
