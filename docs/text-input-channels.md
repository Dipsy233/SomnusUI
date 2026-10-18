# Text input channels: how text reaches a control

本文档说明 SomnusUI **如何把文本写进一个控件**，为什么是这个顺序，
以及每条通道的代价。它是实现文档，不是实验日志——每条结论都注明了
它是**实测**还是**推断**，但不再保留原始探测脚本的输出记录。

---

## 一、核心原则

**优先使用应用自己发布的接口；只有在应用没有暴露任何写入能力时，
才退回到"替用户操作输入设备"。**

两条原则按此顺序生效，本文件讨论第一条，第二条见
[child-session.md](child-session.md)：

1. **不做无焦点写入。** 运行时不在用户不知情时碰触其他应用的窗口。写入通道
   （`ValuePattern`、`Ctrl+V`、`SendInput`）在动手之前都会先把目标窗口提到
   前台（`executor._bring_to_foreground`）；窗口提不到前台时，调用会**如实
   拒绝**，而不是换一条更隐蔽的路。
2. **不干扰用户，靠子会话。** 无焦点写入曾被当作"安静地工作"的办法，实践表明
   它引发的不可预期远多于它省下的东西：一次写入落在哪个窗口取决于当时的前台
   状态，副作用无法归因，用户也无法察觉发生了什么。真正把 agent 的操作从用户
   眼前移开的是**子会话**——同一账户的第二次交互式登录，有自己的前台窗口和
   输入队列。需要"不打扰"的宿主应当把 agent 放到那里，而不是要求运行时绕过
   前台。

合成输入（粘贴、键盘）因此有两个绕不开的代价：

| 代价 | 说明 |
|---|---|
| **抢占前台焦点** | 用户正在用的窗口会被切走。这不是副作用，是前提条件——`Ctrl+V` 和 `SendInput` 都只发给前台窗口。 |
| **借用用户资源** | 粘贴通道会占用用户的剪贴板（哪怕只是短暂地写入再恢复）。 |

`ValuePattern` 写入不产生合成输入，也不借用户的剪贴板，但它同样要求目标窗口
在前台——运行时选择**明确地**提窗口，而不是绕过前台去写。这是第四节的起点。

通道链的顺序因此不是"哪个先试哪个"，而是**按对用户的打扰程度排序**。

---

## 二、通道链

`set_text` 是 LLM 唯一看到的名字。它内部是一条**有序的条件链**，
由解析器按元素在运行时的真实能力逐条求值：

| 顺序 | primitive | 门控条件 | 前置要求 | 合成输入 | 借用剪贴板 |
|---|---|---|---|---|---|
| 1 | `set_text` | `has:value` | 控件实现 `ValuePattern` | 否 | 否 |
| 2 | `paste_text` | — | 需 `--allow-clipboard` | **是** | **是** |
| 3 | `type_text` | — | 需 `--allow-keyboard` | **是** | 否 |

链的执行规则（`runtime/session.py`）：

* 某一步**成功** → 结束，记住这条路线（下次优先试它）；
* 某一步**元素声明不支持**（`unsupported`）→ 继续试下一步；
* 某一步**执行失败**（拒绝、UIPI、COM 错误）→ 如实报错，不偷偷换路；
* 某一步**未被授权**（policy 拒绝）→ 跳过并记录，继续试下一步；
* 整条链都不可用 → `ACTION_NOT_SUPPORTED`，并列出试过哪些。

> "授权被拒"必须**跳过而非终止**：一个宿主可能只开了键盘没开剪贴板，
> 若第一条被拒就整个失败，它会拿到"没有可用接口"，而它合法的路线就在下一步。

三条通道都要求目标在前台（运行时统一在 `_bring_to_foreground` 里提窗口）。
差别在于代价：第一条不产生任何合成输入，也不借用户的剪贴板；后两条会。所以
第一条永远排在最前，真实自绘控件（无 `ValuePattern`）落在第 2/3 条，未授权时
被拒而不是被偷偷写入。

> **已删除的 `set_text_hwnd` 通道。** 曾经在 1 和 2 之间还有一条：当元素**自己**
> 映射了 HWND 时，用 `EM_REPLACESEL`/`WM_SETTEXT` 写入。它被删除了其门控是"元素有
> HWND"，而一个能承载可编辑文本且有 HWND 的控件**总是**同时提供 `ValuePattern`
> （在原生 `EDIT`、WinForms `TextBox`、多行 `TextBox`、`RichTextBox` 上实测），
> 于是第一条总是命中，这条从未被执行过。要造出"有 HWND 但无 Value 接口"的控件，
> 只能把文本面从 UIA 完全隐藏——真实应用没有这种形状。它被认为覆盖的场景
> （UIA 能看见但写不了）由第 2/3 条承担。

---

## 三、各通道的实现要点

### 3.1 `set_text` — UIA ValuePattern（首选）

`ValuePattern.SetValue`。应用在自己的线程上完成写入：**原子、不产生任何合成
输入、不借用户的剪贴板**。它仍然要求目标窗口在前台（运行时会把窗口提上来），
但在四条通道里代价最低，只要控件实现了它就一定用它。

**实测**（Windows 11 24H2 记事本 RichEdit）：20/20 成功，含 CJK 与长字符串。

### 3.2 `paste_text` — 剪贴板通道

自绘控件（Electron/Chromium 的富文本）既没有 `ValuePattern`，也没有元素级
HWND。够得着它们的是 `Ctrl+V`：文本走**剪贴板**，由应用自己的 `WM_PASTE`
处理器一次性读走。

**为什么它比打字可靠**：`SendInput` 把 N 个字符变成 2N 个输入队列条目，
而目标的消息泵一次只消费一小批，剩下的会滞留。粘贴恒定只发 4 个按键事件，
文本从不进入队列。

**实测**：投递耗时与文本长度无关，10~3000 字符恒定 **~37 ms**。

#### 对用户剪贴板的保护：逐格式无损还原

这条通道会短暂借用用户的剪贴板，但**用完原样还回去**。关键在于快照不是
只保存文本，而是保存**每一种格式的原始字节**，还原时逐个重建整个条目：

| 格式类别 | 捕获方式 | 重建方式 |
|---|---|---|
| `HGLOBAL`（文本/RTF/HTML/`CF_HDROP`/自定义格式） | `GlobalLock` 直接拷字节 | `GlobalAlloc` + `SetClipboardData` |
| `CF_BITMAP` | `GetDIBits` 取 32bpp `BI_RGB` 像素（拼成 `CF_DIB` 块） | `CreateDIBitmap` |
| `CF_ENHMETAFILE` | `GetEnhMetaFileBits` | `SetEnhMetaFileBits` |

**实测**（真实剪贴板，同时含文本 + 位图 + 自定义格式）：

```
借用前 : CF_13, CF_2, SomnusTestFormat, CF_16, CF_1, CF_7, CF_8, CF_17
借用后 : CF_13, 四条 opt-out, CF_16, CF_1, CF_7
还原后 : CF_13, CF_2, SomnusTestFormat, CF_16, CF_1, CF_7, CF_8, CF_17

所有格式字节一致 : True      ← 含 12328 字节的位图
图片仍然存在     : image
```

**端到端实测**（QQ 输入框）：投递走 `paste_text` 成功，用户粘贴前的图片
（8 种格式）在投递后**字节级无损保留**。

两个实现细节：

* **自定义格式按名字重新注册**（`RegisterClipboardFormat`）。`0xC000+` 的 id
  是每会话动态分配的，直接复用旧 id 会把数据写到本会话里恰好占了该 id 的
  另一个格式名下——名字对、内容错。
* **还原时不写回四条 opt-out 标记**。它们是我们写入时的标记，不属于用户的
  内容；若一并写回，等于给用户的条目偷偷加了“不要进历史”的标记。

#### 仍然存在的代价

1. **隐私泄漏** —— 用四条 opt-out 格式**预防**（不是事后删除），
   实测历史记录 `count 1 → 0`；
2. **少数格式无法重建** —— `CF_PALETTE` 这类句柄背后没有字节块（读不出也写不回），
   以及 OLE 承载格式（`DataObject`、`Ole Private Data`，它们是 OLE 编组状态而非
   内存块）。二者都不会静默丢弃：`read_failures` / `restore_failures` 会**列出名字**；
3. **恢复时序无法确认** —— 延迟渲染（promise）本该提供“目标已读取”的信号，
   但实测在本环境不可用（OLE 抢先实体化承诺，目标真正粘贴时不再产生第二次
   `WM_RENDERFORMAT`），所以只能靠定时等待，并**如实标注“未确认”**。

**不做**：延迟渲染（无优势、纯增复杂度）、事后删除历史（有时间窗 88~350 ms
且需 STA/WinRT，既然能预防就不需要）。

**授权**：`allow_clipboard`，本构建中**默认开启**，可用 `--no-allow-clipboard`
或 `allow_clipboard = false` 关闭。

### 3.4 `type_text` — 合成键盘（最后手段）

`SendInput` + `KEYEVENTF_UNICODE`（与键盘布局无关）。

**固有问题**（实测）：

* **必然抢焦点**；
* **必然丢字** —— 一次 burst 发 10 字符只落 2 个，且计数返回 20/20 全成功
  （`SendInput` 只报告"事件被接受"，不报告"被处理"）；
* **结果难以验证** —— 目标可能把字符滞留，等下一次真实按键才消费。

缓解手段是逐字符发送 + 读回验证 + 必要时发一个不输入任何字符的 SHIFT
去"唤醒"目标的输入泵。但这不是修复，只是缓解。

**授权**：`allow_keyboard`，本构建中**默认开启**，可用 `--no-allow-keyboard`
或 `allow_keyboard = false` 关闭。

---

## 四、为什么自绘控件写不进去

**结论：没有可编程接口的自绘控件，不存在通用的写入方法——无论是否抢占前台。**
这不是库的选择问题（pywinauto / FlaUI / uiautomation 结果相同），
而是**应用没有暴露这个能力**。

### 4.1 典型样本：QQ / 豆包的 ProseMirror 输入框

技术栈：Electron + Chromium + ProseMirror（contenteditable）。

| 字段 | 值 |
|---|---|
| ControlType | `Group` (50026) —— **不是 `Edit`** |
| ClassName | `ProseMirror ExEditor-qq-msg-editor`（QQ）<br>`tiptap ProseMirror`（豆包） |
| 元素级 HWND | **无**（`CurrentNativeWindowHandle == 0`） |
| 声称的 patterns | `legacyiaccessible`, `text` —— **没有 `value`** |

实测中它被第一条通道过滤掉，直接从 `paste_text` 开始：

```
declared steps : [('set_text','has:value'),
                  ('paste_text',None), ('type_text',None)]
resolver steps : ['paste_text', 'type_text']
>>> 运行时实际尝试的第一条路线: paste_text
```

**端到端投递实测**：两个真实输入框都成功写入（`TEXT LANDED: True`），
且用户剪贴板被无损恢复。

### 4.2 一个必须知道的陷阱：根文档节点会谎报 `value`

Chromium 应用的**根 document 节点**会声称自己实现了 `value`，
但它的"值"是内部 URL（`chrome://doubao-chat/chat`、
`app://./renderer/index.html`），且 `SetValue` 直接抛错。

真正可编辑的 ProseMirror 子节点反而是**干净**的（没有 `value`）。
所以判据必须问**具体的元素**，不能被祖先节点误导。

### 4.3 已经验证走不通的路线（不要重试）

| 路线 | 实测结论 |
|---|---|
| UIA `TextPattern` 写入 | **只读**，根本没有写入方法 |
| UIA `TextEditPattern` | 取得到但**不可写**：range 恒为 NULL，且无 `SetText` |
| UIA `LegacyIAccessible.SetValue` | 通常返回 `E_NOTIMPL` |
| MSAA `IAccessible::put_accValue` | Chromium 只给无名 Pane |
| `IAccessibleEx` / `IRawElementProviderSimple` 桥接 | `E_NOINTERFACE` |
| `PostMessage(WM_CHAR)` | 消息发到了，但**无人读** |
| CDP `Input.insertText` | 机制上可行且无感，但要求应用**启动时**就带 `--remote-debugging-port`；对**已运行**的应用实测不可达 |
| 虚拟桌面隔离 + 键盘 | 窗口移不动，且切桌面比抢焦点更打扰 |
| `ValuePattern2` | **不存在**（避免一个伪造结论） |

### 4.4 判断一个控件能否写入，只需问三个问题

```
1. 元素实现了 UIA ValuePattern？           → set_text（首选）
2. 应用启动时带了 Chromium 调试端口？      → CDP（已运行的应用不要期待）
3. 以上皆无                               → 应用没暴露写入能力
```

**三者皆无时，可选方案只剩**：粘贴（需前台+剪贴板）、合成键盘（需前台）、
或应用专用集成。

SomnusUI 的选择是：**如实报告这个事实**，而不是用合成输入掩盖它。

---

## 五、验证信号：读回哪个面

链上每一步都需要知道"写进去了没有"。**不是所有控件都能读回自己的内容**，
而且读错地方会静默地把"成功"判成"失败"。

### 5.1 Value 属性可能是"活的空串"

实测 QQ 的 ProseMirror 编辑器：

```
GetCurrentPropertyValue(30045)  ->  ''        ← 成功返回空串，不抛异常
Text pattern 的 DocumentRange   ->  真实内容   ← 内容在这里
```

它**既不抛异常，也不返回非字符串**——只是一个合法的、永远为空的字符串。
如果校验器只看 Value，`len('') - 0 >= count` 恒为 `False`。

**实测后果**（修复前）：每个字符都跑满重试预算并在其间狂发 SHIFT 键：

| 字符数 | 修复前 | 修复后 |
|---|---|---|
| 3 | 1.89 s | 0.05 s |
| 6 | 3.41 s | 0.08 s |
| 10 | **超时失败** | 0.14 s |
| 20 | — | 0.28 s |

**630 ms/字符 → 14 ms/字符**。修的是**信号**，不是超时。

### 5.2 正确的做法

按顺序选一个**真的会动**的面：

1. `Value` 属性**有内容** → 用它；
2. 否则 `Text` pattern 的 `DocumentRange.GetText()` → 用它；
3. 两者都读不到 → **返回"没有校验器"**，让调用方退回定速投递并如实说明。

**绝不要**返回一个恒假的校验器——那是在每个字符上白等重试预算。

---

## 六、授权模型

两条合成输入通道**各自独立授权**，本构建中默认都开启，可以分别关闭：

| 开关 | 授权通道 | 理由 |
|---|---|---|
| `allow_clipboard`（`--no-allow-clipboard` 关闭） | `paste_text` | 借用用户剪贴板，需要前台 |
| `allow_keyboard`（`--no-allow-keyboard` 关闭） | `type_text` | 抢焦点、丢字、难以验证 |

**按 step 检查**：一个能力可以把多条路线声明成互斥选项，宿主可能只开了其中一个。
被拒的 step 只是跳过，只有整条链都被拒才报错——且错误里会列出每条被拒路线
**及其对应的开关**，让运维一眼看出该开哪个。

### 6.1 持久化：设置文件

**MCP 客户端是自己启动这个进程的，用户根本没有地方传 flag**，所以每个开关都
有一个持久形式（`somnus/config.py`），命令行参数只用于单次覆盖：

```bash
somnus config --path                 # 文件位置
somnus config --no-allow-clipboard   # 关闭剪贴板通道，持久生效
somnus config --no-allow-keyboard    # 关闭键盘通道
somnus config                        # 查看当前状态（首次会写出默认文件）
```

文件是普通 TOML，**带注释、可直接手改，也小到可以交给 LLM 改**：

```toml
# Both synthetic-input channels are ON unless set to false below.
# They seize the screen's foreground (and the clipboard channel borrows
# your clipboard), so turn one off if you do not accept that a window
# the agent writes into may come to the front. ...
allow_clipboard = true
allow_keyboard  = true
allow_processes = []
```

位置（按优先级）：`$SOMNUS_CONFIG` → 平台配置目录
（Windows 为 `%APPDATA%\somnusui\config.toml`）。

优先级：**命令行 flag > 环境变量（`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`）> 文件 > 内置默认（全开）**。
环境覆盖支持**双向**：既能打开文件里关的，也能关掉文件里开的（CI/临时调试用）。

两个设计约束：

* **降级方向必须安全** —— 文件损坏/读不动/类型不对时，回到内置默认并把问题
  记在 `problems` 里报出来，绝不静默按用户没选择过的值运行；
* **不洗掉注释** —— 保存是重新渲染带注释的模板，而不是覆写裸键值对；
  否则每保存一次，文件就更难改一次。

---

## 七、复现与回归

以下行为都有自动化测试覆盖（`pytest -m windows` 需要真实 Windows 环境）：

| 测试文件 | 覆盖内容 |
|---|---|
| `tests/runtime/test_capability_routing.py` | 链的推进规则：缺失接口继续、失败终止、授权被拒跳过、路线记忆 |
| `tests/runtime/test_clipboard_channel.py` | opt-out 格式、非文本拒用、恢复如实上报、通道顺序、授权默认值 |
| `tests/runtime/test_text_verifier.py` | 校验器选面：Value 真能读时用它，否则退到 Text pattern |

手工复现某条通道是否真的可用，最直接的方式是开 `SOMNUS_TRACE_STATES=1`
跑一次 `CALL`，它会打出每一步 primitive 的结果与目标的前后状态。
