# SomnusUIm (SomnusDroid)

**语言模型会梦见像素吗？**

[English](README.md) | 中文

SomnusUIm 把安卓手机的无障碍树编译成纯文本语义 API，让语言模型通过**能力调用**操作设备，而不是像素点击。模型看到的是每个控件的紧凑描述——它是什么、能做什么；永远看不到截图、坐标或原生标识。

本仓库是 Android 版本，由 Windows UIA 版本 [SomnusUI](../SomnusUI) 演化而来；共享的编译器 / 运行时 / SUI-Text 协议是同一套设计。

```
用户意图
   ↓
LLM 规划器（五个工具）
   ↓
Somnus 运行时 ─ observe / find / inspect / call / switch
   ├─ 会话注册表     ref ↔ fingerprint ↔ 原生节点
   ├─ 解析器         语义能力 → 原生原语
   ├─ 沉降器 + 校验器 静默窗口 + 效果 + delta
   └─ 策略           风险、确认
   ↓
语义图
   ↑
语义编译器（确定性：normalize → infer → match → project → compress）
   ↑
AndroidProvider ── Portal 应用（无障碍服务，JSON-RPC over WebSocket）
                    经 `adb forward` 连接（即信任边界）
```

## 原则

- **无视觉。** 无障碍树是唯一的感知通道。
- **能力即证据。** 只有平台真正提供了背后接口时才广告能力：`scroll` 来自节点自身的 SCROLL 动作，`select` 绝不出现在惰性标签上，Android 的 `context_menu` 只在节点真有 LONG_CLICK 时授予。无可用的控件会被如实报告。
- **读取跟随屏幕。** 手机只有一个屏幕；observe / find / inspect / call 永远描述*当前*的活动窗口，且服务前先重新快照。
- **低 token 成本。** 观察结果经投影和预算控制：同质列表只预览几项，匿名叶子折叠成一行。L0 概览通常只要几十到几百 token。
- **语义优先。** 模型调用 `CALL b9.activate()`，而不是 `tap(312, 528)`。树无法表达的少数手势（桌面翻页）在 Portal 内部合成——屏幕尺寸只有它知道。
- **确定性编译器。** 运行时和编译器内部没有任何语言模型。
- **大声失败。** 无障碍 API 表达不了的东西报告为 `UNKNOWN`，绝不猜。

## 快速开始

```bash
pip install -e ".[dev,mcp]"

# 手机端：安装 Portal APK（portal/）并开启无障碍服务，然后转发 RPC 端口：
adb forward tcp:18677 tcp:8677

# 观察当前屏幕：
somnus --provider android observe

# 执行一个动作：
somnus --provider android call 'CALL b1.activate()'

# 交互循环：
somnus --provider android repl
```

### 作为 MCP 服务器

```bash
somnus-mcp android
```

向任意 MCP 宿主暴露五个工具：`observe`、`find`、`inspect`、`call`、`switch`。现成的客户端配置见 `.mcp.json`。

## 模型看到什么

每次观察都是 SUI-Text：每个控件一行，带角色、ref 和它支持的能力。

```
@rev 12
WINDOW w3 "com.tencent.mobileqq" active
  GROUP g1 +scroll_into_view +select +tap
    LIST l5 +context_menu +scroll_into_view +select
    TEXT tx147 "我的电脑" +activate +focus +read +scroll_into_view
    INPUT q1 +activate +context_menu +set_text(text)
    BUTTON b4 "发送" disabled +focus +read +scroll_into_view +select +tap
```

动作就是以 ref 为主语的普通句子：

```
CALL q1.set_text(text:"你好")
CALL b4.activate()
SWITCH home          # 回桌面（平台全局动作）
SWITCH next          # 桌面翻一页（合成的滑动手势）
```

调用的返回是 **delta** —— 变了什么，而不是整个重渲染：

```
OK rev=11
~ q1 name →""            # 输入框清空
~ b4 disabled false→true # 发送按钮重新锁定
+ TEXT tx167 "你好…"      # 出现的消息气泡
```

## 屏幕导航

用户在手机上移动于"屏幕"之间，而其中一些根本不存在于任何树里：应用背后的桌面、上一个应用、尚未实例化的启动器页面。`switch` 通过平台自身的 API 触达它们——home/back 用 `performGlobalAction`，分页用 Portal 内合成的滑动手势——绝不使用模型提供的坐标。

## 五个工具

| 工具 | 读写 | 语法 |
| --- | --- | --- |
| `observe` | 当前屏幕，L0 | `OBSERVE [window] [full]` |
| `find` | 搜索，先重新快照 | `FIND "text" [in ref]` |
| `inspect` | 单节点全貌 | `INSPECT ref [depth=N]` |
| `call` | 动作 | `CALL ref.cap(args)` |
| `switch` | 屏幕导航 | `SWITCH home\|back\|next\|prev` |

## Portal 应用

`portal/` 是设备侧的一半：一个无障碍服务，一次遍历快照整屏（key 是索引路径；密码文本永不离开手机）、按键执行动作、持有手势通道。RPC 服务器只绑定 loopback——adb forward 本身就是信任边界。

构建：`gradle assembleDebug`（产物在 `portal/app/build/outputs/apk/debug/`）。

## 仓库结构

```
somnus/
  compiler/        原始树 → 语义图（normalize、caps、match、packs）
  projection/      L0/L1/L2 的预算与折叠规则
  runtime/         session、registry（稳定 ref）、resolver、settler、delta
  providers/
    android/       Portal 传输、原始树转换、Android mock
    mock.py        平台中立的回放 provider（测试、离线运行）
  protocol/        MCP 服务器、JSON-RPC 服务器、SUI-Text 解析、提示词
  sui/             SUI-Text 语法（解析 + 渲染）
portal/            Android 应用（Kotlin）：无障碍服务 + RPC 服务器
tests/             pytest 套件（离线：fixture 回放手机路径）
docs/              设计与进度笔记
```

## 测试

```bash
python -m pytest tests -q
```

全部离线运行：录制的 Portal 快照经真实的转换、编译器和运行时回放，CI 里通过的就是对手机路径的证据。`tests/android/smoke_real_device.py` 针对真机。

## 许可

MIT
