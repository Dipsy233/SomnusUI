# 文本输入通道

本文说明 SomnusUI 如何把文本写入控件、通道顺序与每条通道的代价。结论标注为实测或推断；原始探测脚本的输出不再保留。

## 1. 原则

优先使用应用自身发布的接口。应用没有暴露任何写入能力时，才使用合成输入。

两条原则按顺序生效：

1. 不在无焦点状态下写入。Value、`Ctrl+V` 与 `SendInput` 在执行前都会把目标窗口提升到前台（`executor._bring_to_foreground`）。窗口无法提升时，调用如实拒绝，不使用更隐蔽的替代路径。
2. 不干扰用户依靠子会话。无焦点写入的实际落点取决于当时的前台状态，副作用无法归因。需要“不打扰”的宿主应把 agent 放入子会话（同一账户的第二次交互式登录，有自己的前台窗口与输入队列），见 [child-session.md](child-session.md)。

合成输入的两项固定代价：

| 代价 | 说明 |
| --- | --- |
| 抢占前台焦点 | 用户正在使用的窗口会被切走；`Ctrl+V` 与 `SendInput` 只发给前台窗口 |
| 借用用户资源 | 粘贴通道会短暂占用剪贴板，随后恢复 |

`ValuePattern` 写入不产生合成输入，也不借用剪贴板，但同样要求目标窗口在前台。

## 2. 通道链

`set_text` 是模型看到的唯一名称。内部是一条有序条件链，由解析器按元素在运行时的实际能力逐条求值：

| 顺序 | primitive | 门控条件 | 前置要求 | 合成输入 | 借用剪贴板 |
| --- | --- | --- | --- | --- | --- |
| 1 | `set_text` | `has:value` | 控件实现 `ValuePattern` | 否 | 否 |
| 2 | `paste_text` | — | 需 `--allow-clipboard` | 是 | 是 |
| 3 | `type_text` | — | 需 `--allow-keyboard` | 是 | 否 |

链的执行规则（`runtime/session.py`）：

- 某一步成功：结束，并记录该路径，下次优先尝试。
- 某一步声明不支持（`unsupported`）：继续下一步。
- 某一步执行失败（拒绝、UIPI、COM 错误）：如实报错，不静默换路。
- 某一步未被授权（policy 拒绝）：跳过并记录，继续下一步。
- 整条链不可用：返回 `ACTION_NOT_SUPPORTED`，并列出尝试过的路径。

授权被拒时跳过而不终止，是为了支持只开启部分通道的宿主。若第一条被拒就整体失败，宿主会得到“没有可用接口”，而合法路径可能就在下一步。

三条通道都要求目标在前台。第一条不产生合成输入，也不借用剪贴板；后两条会。第一条因此始终排在最前。没有 `ValuePattern` 的真实自绘控件落在第 2 或第 3 条；未授权时被拒绝，不会静默写入。

### 已删除的 `set_text_hwnd` 通道

该通道位于第 1 条与第 2 条之间，在元素自身映射 HWND 时使用 `EM_REPLACESEL` / `WM_SETTEXT` 写入。其门控条件是“元素有 HWND”。实测中，能承载可编辑文本且有 HWND 的控件总是同时提供 `ValuePattern`（原生 `EDIT`、WinForms `TextBox`、多行 `TextBox`、`RichTextBox`），第一条因此总是命中，该通道从未被执行。构造“有 HWND 但无 Value 接口”的控件需要把文本面从 UIA 完全隐藏，真实应用没有这种形状。该通道覆盖的场景由第 2、3 条承担。

## 3. 各通道实现

### 3.1 `set_text`：UIA ValuePattern

`ValuePattern.SetValue`。应用在自己的线程上完成写入，原子执行，不产生合成输入，不借用剪贴板。目标窗口仍需在前台。

实测（Windows 11 24H2 记事本 RichEdit）：20/20 成功，包含 CJK 与长字符串。

### 3.2 `paste_text`：剪贴板通道

自绘控件（例如 Electron/Chromium 富文本）没有 `ValuePattern`，也没有元素级 HWND。可用的写入方式是 `Ctrl+V`：文本走剪贴板，由应用自己的 `WM_PASTE` 处理器读取。

该通道比逐字符键入可靠：`SendInput` 把 N 个字符变成 2N 个输入队列条目，目标消息泵一次只消费一小批，剩余条目滞留。粘贴固定发送 4 个按键事件，文本不进入输入队列。

实测：投递耗时与文本长度无关，10–3000 字符恒定为约 37 ms。

#### 剪贴板逐格式还原

该通道短暂借用用户剪贴板，使用后原样恢复。快照保存每一种格式的原始字节，恢复时逐个重建条目：

| 格式类型 | 捕获方式 | 重建方式 |
| --- | --- | --- |
| `HGLOBAL`（文本/RTF/HTML/`CF_HDROP`/自定义格式） | `GlobalLock` 拷贝字节 | `GlobalAlloc` + `SetClipboardData` |
| `CF_BITMAP` | `GetDIBits` 取 32bpp `BI_RGB` 像素，拼成 `CF_DIB` 块 | `CreateDIBitmap` |
| `CF_ENHMETAFILE` | `GetEnhMetaFileBits` | `SetEnhMetaFileBits` |

实测（真实剪贴板，同时含文本、位图与自定义格式）：

```
借用前 : CF_13, CF_2, SomnusTestFormat, CF_16, CF_1, CF_7, CF_8, CF_17
借用后 : CF_13, 四条 opt-out, CF_16, CF_1, CF_7
还原后 : CF_13, CF_2, SomnusTestFormat, CF_16, CF_1, CF_7, CF_8, CF_17

所有格式字节一致 : True
图片仍然存在     : image
```

端到端实测（QQ 输入框）：投递通过 `paste_text` 成功，用户粘贴前的图片（8 种格式）在投递后字节级无损保留。

实现细节：

- 自定义格式按名称重新注册（`RegisterClipboardFormat`）。`0xC000+` 的 id 是每会话动态分配的；直接复用旧 id 会把数据写到本会话中占用该 id 的另一个格式名下。
- 恢复时不写回四条 opt-out 标记。这些标记由运行时写入，不属于用户内容；写回会为用户的条目添加“不要进入历史”的标记。

仍然存在的代价：

1. 隐私：使用四条 opt-out 格式在写入前预防历史记录；实测历史记录从 `count 1` 变为 `0`。
2. 少数格式无法重建：`CF_PALETTE` 这类句柄背后没有字节块；OLE 承载格式（`DataObject`、`Ole Private Data`）属于 OLE 编组状态。两者都会通过 `read_failures` / `restore_failures` 列出名称，不会静默丢弃。
3. 恢复时序无法确认：延迟渲染本应提供“目标已读取”的信号，但实测在本环境中不可用（OLE 抢先实体化承诺，目标真正粘贴时不再产生第二次 `WM_RENDERFORMAT`）。实现依靠定时等待，并标注“未确认”。

未采用：延迟渲染（无额外优势，增加复杂度）；事后删除历史（存在 88–350 ms 时间窗且需要 STA/WinRT，写入前预防已覆盖该问题）。

授权：`allow_clipboard`，当前构建默认开启，可用 `--no-allow-clipboard` 或 `allow_clipboard = false` 关闭。

### 3.3 `type_text`：合成键盘

`SendInput` 配合 `KEYEVENTF_UNICODE`，与键盘布局无关。

实测的固有问题：

- 抢占焦点；
- 丢字：一次 burst 发送 10 字符只落 2 个，`SendInput` 仍报告 20/20 全部成功，因为它报告“事件被接受”，不报告“被处理”；
- 结果难以验证：目标可能滞留字符，等待下一次真实按键才消费。

缓解方式为逐字符发送、读回验证，必要时发送一个不输入字符的 SHIFT 唤醒目标输入泵。该方式缓解问题，不修复问题。

授权：`allow_keyboard`，当前构建默认开启，可用 `--no-allow-keyboard` 或 `allow_keyboard = false` 关闭。

## 4. 自绘控件的写入限制

没有可编程接口的自绘控件不存在通用写入方法，无论是否抢占前台。该结果与库的选择无关（pywinauto / FlaUI / uiautomation 结果相同）；原因是应用没有暴露该能力。

### 典型样本：QQ / 豆包的 ProseMirror 输入框

技术栈：Electron + Chromium + ProseMirror（contenteditable）。

| 字段 | 值 |
| --- | --- |
| ControlType | `Group` (50026) |
| ClassName | `ProseMirror ExEditor-qq-msg-editor`（QQ）；`tiptap ProseMirror`（豆包） |
| 元素级 HWND | 无，`CurrentNativeWindowHandle == 0` |
| 声称的 patterns | `legacyiaccessible`、`text`；没有 `value` |

实测中该控件被第一条通道过滤，直接从 `paste_text` 开始：

```
declared steps : [('set_text','has:value'),
                  ('paste_text',None), ('type_text',None)]
resolver steps : ['paste_text', 'type_text']
运行时实际尝试的第一条路线: paste_text
```

端到端投递实测：两个真实输入框都成功写入（`TEXT LANDED: True`），用户剪贴板被无损恢复。

### 根文档节点会声称实现 `value`

Chromium 应用的根 document 节点声称实现 `value`，其值是内部 URL（`chrome://doubao-chat/chat`、`app://./renderer/index.html`），`SetValue` 直接抛错。真正可编辑的 ProseMirror 子节点没有 `value`。判断必须针对具体元素，不能被祖先节点误导。

### 已验证不可行的路线

| 路线 | 实测结论 |
| --- | --- |
| UIA `TextPattern` 写入 | 只读，没有写入方法 |
| UIA `TextEditPattern` | 可取得但不可写：range 恒为 NULL，且无 `SetText` |
| UIA `LegacyIAccessible.SetValue` | 通常返回 `E_NOTIMPL` |
| MSAA `IAccessible::put_accValue` | Chromium 只提供无名 Pane |
| `IAccessibleEx` / `IRawElementProviderSimple` 桥接 | `E_NOINTERFACE` |
| `PostMessage(WM_CHAR)` | 消息发送成功，目标未读取 |
| CDP `Input.insertText` | 机制可行且无感，但要求应用启动时带 `--remote-debugging-port`；对已运行的应用实测不可达 |
| 虚拟桌面隔离加键盘 | 窗口无法移动，切换桌面的打扰高于抢占焦点 |
| `ValuePattern2` | 不存在 |

### 判断控件能否写入

```
1. 元素实现 UIA ValuePattern           -> set_text（首选）
2. 应用启动时带 Chromium 调试端口       -> CDP（已运行的应用不保证可用）
3. 以上皆无                            -> 应用没有暴露写入能力
```

三者皆无时，可选方案为粘贴（需前台与剪贴板）、合成键盘（需前台），或应用专用集成。SomnusUI 如实报告该状态。

## 5. 验证信号

链上每一步都需要判断写入是否成功。并非所有控件都能读回自身内容，读取位置错误会把成功判为失败。

### Value 属性可能返回空串

实测 QQ 的 ProseMirror 编辑器：

```
GetCurrentPropertyValue(30045)  ->  ''        # 成功返回空串，不抛异常
Text pattern 的 DocumentRange   ->  真实内容    # 内容在这里
```

返回值是合法的空字符串，不抛异常。校验器只读 Value 时，`len('') - 0 >= count` 恒为 `False`。

实测后果（修复前）：每个字符都跑满重试预算，并在此期间发送 SHIFT 键。

| 字符数 | 修复前 | 修复后 |
| --- | --- | --- |
| 3 | 1.89 s | 0.05 s |
| 6 | 3.41 s | 0.08 s |
| 10 | 超时失败 | 0.14 s |
| 20 | — | 0.28 s |

从 630 ms/字符降至 14 ms/字符。修复的是验证信号，未修改超时。

### 正确的做法

按顺序选择一个确实会变化的面：

1. `Value` 属性有内容：使用它。
2. 否则使用 `Text` pattern 的 `DocumentRange.GetText()`。
3. 两者都读不到：返回“没有校验器”，调用方退回定速投递并说明情况。

校验器不返回恒假结果，避免每个字符都等待重试预算。

## 6. 授权模型

两条合成输入通道各自独立授权，当前构建默认开启，可逐项关闭：

| 开关 | 授权通道 | 理由 |
| --- | --- | --- |
| `allow_clipboard`（`--no-allow-clipboard` 关闭） | `paste_text` | 借用用户剪贴板，需要前台 |
| `allow_keyboard`（`--no-allow-keyboard` 关闭） | `type_text` | 抢占焦点、可能丢字、难以验证 |

授权按步骤检查。一个能力可以声明多条互斥路线，宿主可能只开启其中一个。被拒的步骤跳过；整条链都被拒时才报错，错误中列出每条被拒路线及其对应开关。

### 持久化

MCP 客户端自行启动进程，用户没有位置传递命令行参数，因此每个开关都有持久形式（`somnus/config.py`）。命令行参数只用于单次覆盖。

```bash
somnus config --path                 # 文件位置
somnus config --no-allow-clipboard   # 关闭剪贴板通道，持久生效
somnus config --no-allow-keyboard    # 关闭键盘通道
somnus config                        # 查看当前状态；首次运行写出默认文件
```

设置文件为普通 TOML，带注释，可手工编辑：

```toml
# 两条合成输入通道默认开启，设为 false 关闭。
allow_clipboard = true
allow_keyboard  = true
allow_processes = []
```

位置：`$SOMNUS_CONFIG` 优先，否则使用平台配置目录（Windows 为 `%APPDATA%\somnusui\config.toml`）。

优先级：

```
命令行参数 > 环境变量（SOMNUS_ALLOW_CLIPBOARD / SOMNUS_ALLOW_KEYBOARD）> 文件 > 内置默认（全开）
```

环境变量支持双向覆盖：可以打开文件中关闭的开关，也可以关闭文件中打开的开关。

两项设计约束：

- 降级方向安全：文件损坏、不可读或类型错误时回到内置默认，并把问题记录在 `problems` 中报告。
- 保存保留注释：保存时重新渲染带注释的模板，不覆写为裸键值对。

## 7. 测试与回归

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/runtime/test_capability_routing.py` | 链的推进规则：缺失接口继续、失败终止、授权被拒跳过、路线记忆 |
| `tests/runtime/test_clipboard_channel.py` | opt-out 格式、非文本拒用、恢复如实上报、通道顺序、授权默认值 |
| `tests/runtime/test_text_verifier.py` | 校验器选面：Value 可读时使用，否则退回 Text pattern |

手工复现某条通道是否可用时，设置 `SOMNUS_TRACE_STATES=1` 运行一次 `CALL`，输出每一步 primitive 的结果与目标前后状态。
