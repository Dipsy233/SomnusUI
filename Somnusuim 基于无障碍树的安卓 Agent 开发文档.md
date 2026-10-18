# SomnusDroid:基于无障碍树的安卓 Agent 开发文档

<aside>
💡

一句话定位:把 SomnusUI 的「语义编译器 + 五个工具 + SUI-Text」原样搬到 Android,只替换感知/执行层为 **AccessibilityService**。模型看到的是「控件是什么、能做什么」,永远看不到截图、坐标、resource-id。

</aside>

## 1. 背景与目标

### 1.1 为什么不用视觉

- **Token 成本**:一张截图通常上千 token,一份投影后的 L0 概览只要几十到几百 token。
- **确定性**:视觉方案要靠模型「猜」点哪里;无障碍树直接给出「这个节点能 click / set_text / scroll」,能力是被平台证明的。
- **可验证**:动作执行后可以通过树的差分(delta)和无障碍事件确认效果,而不是再截一张图让模型自己看。
- **隐私**:密码框、敏感内容可以在编译层直接屏蔽,不需要把整屏像素发给模型。

### 1.2 目标

1. 支持任意已安装 App 的通用操作(Generic mode),无需为每个 App 写适配。
2. 模型侧只暴露五个工具,输入输出全部为 SUI-Text。
3. 编译器、投影、运行时与 SomnusUI **共用代码**,Android 只是一个新的 `AccessibilityProvider`。
4. 失败必须「大声」:树里表达不了的东西(游戏、Canvas 自绘)报 `UNKNOWN`,绝不猜坐标。

### 1.3 非目标

- 不做像素识别、OCR、Set-of-Mark 标注。
- 不承诺能操作 Unity / 游戏 / 纯 Canvas 自绘界面。
- 不绕过 App 的反无障碍检测、不绕过安全键盘与支付验证。

## 2. 参考项目调研

| 项目 | 感知方式 | 值得借鉴 | 不同点 |
| --- | --- | --- | --- |
| SomnusUI(自有) | Windows UIA 树 | 语义编译器、SUI-Text、五工具、delta、Fail loud、子会话 | 平台不同,其余架构直接复用 |
| DroidBot / DroidBot-GPT | UI hierarchy(uiautomator/a11y) | 状态抽象、UI 转移图、自动探索 | 早期方案,文本表示较原始 |
| AutoDroid(MobiCom'24) | UI 树转为类 HTML 文本 | 离线探索 App 生成「功能记忆」,相当于自动生成 App Pack | 输出给模型的是 HTML,token 偏多 |
| AutoDroid-V2 | UI 文档 + 代码生成 | 让模型一次生成多步脚本,适合小模型 | 可作为后期「批量 CALL」的参考 |
| AppAgent(腾讯) | XML + 带编号截图 | 探索阶段生成 App 文档 | 依赖视觉,仅借鉴文档生成思路 |
| DroidRun / mobilerun + Portal | 手机端 AccessibilityService App 导出树,截图可选 | 「手机端 Portal App + 主机端框架」的部署形态 | 树直接给模型,没有语义编译 |
| mobile-mcp | 结构化无障碍快照 / 坐标点击 | MCP 形态、多设备、iOS/Android 统一 | 仍暴露坐标 |
| AndroidWorld(Google) | 无障碍树 + 截图 | 116 个可复现任务的基准环境,带成功判定 | 用作评测,不用作框架 |
| uiautomator2 / Appium / Maestro | UiAutomator | 成熟的设备连接、安装、按键工具链 | 只作调试与兜底,不作主感知通道 |

**结论**:业界大多数方案要么把原始树/HTML 直接扔给模型,要么混用截图。SomnusUI 的「确定性语义编译 + 能力证明 + 差分回包」在移动端几乎没有同类,这是本项目的核心差异点。

## 3. 总体架构

```
User intent
   ↓
LLM planner(五个工具)
   ↓
Somnus runtime ─ observe / find / inspect / call / system
   ├─ Session registry   ref ↔ fingerprint ↔ (displayId, windowId, nodePath)
   ├─ Resolver           语义能力 → AccessibilityAction / 全局动作 / 手势(可选)
   ├─ Verifier           settle(事件静默)+ effects + delta
   └─ Policy             风险、确认、包名白名单、敏感字段屏蔽
   ↓
Semantic graph
   ↑
Semantic compiler(normalize → infer → match → project → compress,与 SomnusUI 共用)
   ↑
AndroidProvider(主机端,Python)
   ↑  adb forward / WebSocket(JSON-RPC)
Somnus Portal(手机端 App,Kotlin)
   ├─ AccessibilityService   树快照 / 事件流 / performAction / performGlobalAction
   ├─ IME(可选)              commitText 文本通道
   └─ VirtualDisplay(可选)   后台工作屏,对应 Windows 的 child session
```

### 3.1 部署形态选择

| 方案 | 说明 | 结论 |
| --- | --- | --- |
| A. 主机端运行时 + 手机端 Portal | Portal 只负责导出 RawTree 和执行原语;编译器/运行时在电脑上跑 | **MVP 采用**,可直接复用 SomnusUI 的 Python 代码 |
| B. 全部跑在手机上 | Kotlin 重写编译器,手机直接调 LLM API | 后期做「无电脑」版本,编译器需移植 |
| C. 纯 adb `uiautomator dump` | 不装 App,dump XML | 只作调试兜底:慢、动画中容易 dump 失败、没有事件流 |

依赖方向不变:`providers → compiler → projection → serialization → protocol`;runtime 依赖全部;Portal 和编译器都不引入 LLM。

## 4. 手机端 Portal 设计

### 4.1 AccessibilityService 配置要点

```xml
<accessibility-service
    android:accessibilityEventTypes="typeWindowStateChanged|typeWindowContentChanged|typeWindowsChanged|typeViewTextChanged|typeViewScrolled|typeViewFocused"
    android:accessibilityFeedbackType="feedbackGeneric"
    android:accessibilityFlags="flagRetrieveInteractiveWindows|flagReportViewIds|flagIncludeNotImportantViews"
    android:canRetrieveWindowContent="true"
    android:canPerformGestures="true"
    android:notificationTimeout="50" />
```

- `flagRetrieveInteractiveWindows`:拿到所有窗口(App、弹窗、输入法、系统 UI),而不仅仅是活动窗口。
- `flagIncludeNotImportantViews`:很多 App 的可点击容器被标记为不重要,不开会漏节点;由编译器负责折叠噪声。
- `flagReportViewIds`:resource-id 只用于内部指纹,**绝不发给模型**。
- Android 13+ 侧载 App 开启无障碍需要先在应用信息里「允许受限设置」,首次引导流程要写清楚。
- Android 14+ 的 `accessibilityDataSensitive` 视图对非无障碍工具类服务不可见,这类节点按 `UNKNOWN` 报告。

### 4.2 Portal 对外接口(JSON-RPC over WebSocket)

| 方法 | 作用 |
| --- | --- |
| `snapshot(displayId?)` | 返回所有可交互窗口的 RawTree |
| `perform(nodeKey, action, args)` | 对节点执行 AccessibilityAction |
| `global(action)` | BACK / HOME / RECENTS / NOTIFICATIONS / QUICK_SETTINGS |
| `gesture(nodeKey, kind)` | 可选通道:按节点 bounds 合成 tap / long_press / swipe,坐标只在 Portal 内部使用 |
| `launch(package)` / `apps()` | 启动 App、列出可启动 App |
| `events.subscribe()` | 推送事件流,供 settle 和 delta 使用 |
| `clipboard.set(text)` | 可选通道,写剪贴板后用于 `ACTION_PASTE` |

### 4.3 RawTree 格式(与 SomnusUI 的 RawTree 对齐)

```json
{
  "display": 0,
  "windows": [
    {"id": 12, "type": "application", "pkg": "com.android.settings", "title": "设置", "active": true,
     "root": {
       "key": "12/0", "cls": "android.widget.FrameLayout",
       "children": [
         {"key": "12/0.3.1", "cls": "android.widget.LinearLayout", "text": null, "desc": null,
          "rid": "android:id/title_container",
          "actions": ["CLICK", "LONG_CLICK"], "enabled": true,
          "bounds": [0, 420, 1080, 600],
          "children": [{"key": "12/0.3.1.0", "cls": "android.widget.TextView", "text": "WLAN"},
                       {"key": "12/0.3.1.1", "cls": "android.widget.TextView", "text": "已连接到 Home-5G"}]}
       ]}}
  ]
}
```

需要采集的字段:`className`、`text`、`contentDescription`、`hintText`、`stateDescription`、`paneTitle`、`viewIdResourceName`、`actionList`、`isEnabled / isChecked / isCheckable / isSelected / isFocused / isEditable / isPassword`、`collectionInfo / collectionItemInfo`、`rangeInfo`、`bounds`(仅内部)、`drawingOrder`。

### 4.4 性能注意

- 遍历在 Portal 进程内完成,一次 snapshot 只做一次跨进程往返给主机;每个 `getChild` 都是一次 IPC,优先使用批量预取(API 33+ 的预取策略)。
- 事件节流:`TYPE_WINDOW_CONTENT_CHANGED` 非常频繁,Portal 只维护「脏标记 + 最后变化时间」,不逐条转发。
- 节点对象及时回收,避免长列表滚动时内存上涨。
- 目标:L0 snapshot 端到端 < 300ms(在 bench 里实测,作为门禁)。

## 5. 语义编译器:Android 规则

编译流水线与 SomnusUI 相同,只是新增 Android 的 normalize / caps 规则。

### 5.1 Normalize(去噪)

- **标签上提**:Android 最常见的问题是「可点击的是外层 LinearLayout,文字在里面的 TextView」。规则:可点击且无名的容器,从后代 TextView 合成名称(主标题 + `sub=` 副标题),后代文本节点折叠。
- **无名包装折叠**:无文本、无动作、只有一个子节点的 `FrameLayout / LinearLayout / ViewGroup / View` 一律折叠。
- **Compose**:类名多为 `android.view.View`,以 actionList、role(stateDescription / roleDescription)和语义属性判断,不依赖类名。
- **WebView**:WebView 内部节点照常编译;节点过多时整体作为 `WEB` 区块并折叠,需 `inspect` 展开。
- **Flutter / RN**:Flutter 在无障碍开启时才导出语义树;RN 依赖开发者是否设置 accessibilityLabel,缺失时报 `UNKNOWN`。
- **不可见与遮挡**:bounds 为空、完全在屏幕外、被上层窗口完全遮挡的节点移出 L0。
- **系统窗口**:状态栏、导航栏只保留有意义的入口;输入法窗口单独作为 `IME` 节点出现。

### 5.2 Caps(能力只由平台证明)

| AccessibilityAction / 属性 | SUI 能力 | 说明 |
| --- | --- | --- |
| `ACTION_CLICK` | `activate`(隐式) | 只有 actionList 里出现才给,不看 isClickable |
| `ACTION_LONG_CLICK` | `+long_press` |  |
| `ACTION_SET_TEXT` / isEditable | `+set_text(text)` | 路由见第 7 节 |
| `ACTION_SCROLL_FORWARD / BACKWARD` | `+scroll(down|up)` | 横向列表映射为 `left|right` |
| `ACTION_SHOW_ON_SCREEN` / `SCROLL_TO_POSITION` | `+scroll_into_view` | 用于 FIND 命中折叠项后定位 |
| isCheckable | `+toggle` | 状态 `checked` 写在行上 |
| `ACTION_SELECT` / isSelected | `+select` | Tab、单选项 |
| `ACTION_EXPAND / COLLAPSE` | `+expand +collapse` |  |
| `ACTION_SET_PROGRESS`  • rangeInfo | `+set_value(n)` | 滑块、音量 |
| `ACTION_DISMISS` | `+dismiss` | 通知、Snackbar、可关闭弹窗 |
| `ACTION_IME_ENTER` | `+submit` | 搜索框回车 |
| 无任何动作 | 无能力 | 不给一个注定失败的能力 |

### 5.3 Infer / Match(实体识别)

- `collectionInfo` 存在 → `LIST count=N`(N 未知时写 `count=?`),`collectionItemInfo` → `ITEM`。
- 同一容器下 ≥3 个同构可点击行 → 实体列表,L0 只预览前几项,其余 `… N more`。
- 底部 3–5 个同构可选项 → `TABBAR`;带返回语义的左上角按钮 → `BACK`。
- 对话框窗口(`TYPE_APPLICATION` 且小于全屏、含 paneTitle)→ `DIALOG`,并在 L0 置顶显示。

### 5.4 Ref 稳定性(指纹)

Android 节点没有持久 ID,RecyclerView 还会复用 View。指纹由以下字段组合:
`pkg + windowType + rid + 角色 + 合成名称 + collectionItemInfo 索引 + 祖先路径摘要`。

- 不把 bounds 放进指纹(滚动会变),只作为同名冲突时的消歧。
- 刷新后指纹相同 → 复用同一 ref;消失 → 下一次 CALL 返回 `ERR TARGET_GONE hint=observe`。

## 6. 模型侧协议:五个工具 + SUI-Text

### 6.1 工具

| 工具 | 语法 | 说明 |
| --- | --- | --- |
| `observe` | `OBSERVE [ref]` | 当前屏幕 L0 概览;`OBSERVE apps` 列出可启动 App |
| `find` | `FIND [type] "query" [in ref] [scroll]` | 关键词定位;带 `scroll` 时在虚拟化列表中自动滚动查找 |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 详情 |
| `call` | `CALL ref.cap(args)` | 执行能力,返回 OK/ERR + delta |
| `system` | `SYSTEM back|home|recents|notifications`、`APP open "设置"`、`DISPLAY main|work` | 替代 SomnusUI 的 `switch`,承载 Android 的全局动作与工作屏切换 |

### 6.2 模型看到的样子

```
@rev 4
DEVICE d1 android=14 display=main ime=hidden
APP a1 "设置" active
  SCREEN s1 "设置"
    INPUT q1 "搜索设置" +set_text(text) +submit
    LIST l1 count=? visible=8 +scroll(up|down) +find
      ITEM i1 "WLAN" sub="已连接到 Home-5G"
      ITEM i2 "蓝牙" sub="已关闭"
      ITEM i3 "显示"
      … 5 more
```

```
CALL i2.activate()
→ OK rev=5
  ~ s1 title 设置→蓝牙
  + SWITCH sw1 "蓝牙" +toggle
  - l1

CALL sw1.toggle()
→ OK rev=6
  ~ sw1 checked no→yes
```

```
FIND "开发者选项" in l1 scroll
→ FOUND 1 scrolled=3
ITEM i14 "开发者选项"
```

### 6.3 错误码(在 SomnusUI 基础上新增)

| 错误 | 含义 | hint |
| --- | --- | --- |
| `ERR TARGET_GONE` | ref 对应节点已消失 | `observe` |
| `ERR TARGET_DISABLED` | 节点不可用 | `inspect` |
| `ERR CAPABILITY_NOT_FOUND` | 节点没有该能力 | `inspect` |
| `ERR SURFACE_OPAQUE` | 自绘/游戏界面,树中无内容 | `report-user` |
| `ERR SERVICE_DISCONNECTED` | 无障碍服务被系统杀掉或关闭 | `report-user` |
| `ERR KEYGUARD_LOCKED` | 锁屏中 | `ask-user` |
| `ERR APP_NOT_FOUND` | 包未安装或不可启动 | `observe apps` |
| `ERR CONFIRMATION_REQUIRED` | 高风险动作需用户确认 | `ask-user` |

## 7. 动作执行与输入通道

### 7.1 点击类动作的路由

1. `performAction(ACTION_CLICK)` 作用于证明了该能力的节点。
2. 节点自身没有、但最近可点击祖先有 → 在编译阶段就把能力挂到祖先上,运行时不做猜测。
3. **手势通道**(`--allow-gesture`,默认关闭):`dispatchGesture` 在节点 bounds 中心合成点击。用于只监听 onTouch 不暴露 click 的控件。坐标只存在于 Portal 内部,回包标注 `via=gesture`,结果为 `OK_UNVERIFIED` 除非 delta 证明生效。

### 7.2 set_text 路由(对应 SomnusUI 的 ValuePattern → 剪贴板 → 键盘)

| # | 通道 | 需要 | 副作用 |
| --- | --- | --- | --- |
| 1 | `ACTION_SET_TEXT` | 节点可编辑 | 无 |
| 2 | 剪贴板 + `ACTION_PASTE` | `--allow-clipboard` | 占用剪贴板,执行后恢复 |
| 3 | Somnus IME `commitText` | `--allow-ime`,需切换输入法 | 临时替换用户输入法,结束后恢复 |
| 4 | `adb shell input text` | 仅调试 | 不支持中文,不进入正式路由 |
- 路由在执行时决定,并在会话内记住成功的通道。
- 验证:等待 `TYPE_VIEW_TEXT_CHANGED` 并回读节点 text;密码框无法回读 → `OK_UNVERIFIED`,且值永不进入回包。

### 7.3 滚动与虚拟化列表

- `FIND ... scroll`:在目标列表上循环 `SCROLL_FORWARD`,每步重新编译局部子树匹配;连续两次内容不变或无 `TYPE_VIEW_SCROLLED` 视为到底;默认最多 15 步。
- 命中后用 `SHOW_ON_SCREEN` 确保可见,再分配 ref。

### 7.4 Settle 与 Verify

- Settle:动作后等待事件静默(默认 300ms 内无内容变化,超时 3s),窗口切换类动作额外等待 `TYPE_WINDOW_STATE_CHANGED`。
- Verify:对比前后语义图,输出 `~ + -` 三类 delta;预期效果(如 toggle 后 checked 变化)未出现 → `OK_UNVERIFIED`。
- 长加载(网络页面):delta 中出现 `LOADING` 节点时,回包附 `hint=wait`,由模型决定是否再次 observe。

## 8. 不打扰用户:工作屏(对应 child session)

Windows 版用第二个交互会话避免抢前台;Android 的对应物是**虚拟显示屏**:

- Portal 创建 VirtualDisplay,在其上启动目标 App(或由 scrcpy 的新建显示能力托管)。
- 无障碍服务通过多显示屏窗口接口读取该屏的树,手势可指定 displayId。
- `SYSTEM DISPLAY work` / `DISPLAY main` 切换,ref 命名空间保持在主机端。
- 已知限制:部分全局动作只作用于主屏;部分 App 拒绝在副屏运行;不同 ROM 行为差异大。**作为 P2 实验特性**,需逐 ROM 验证。
- 替代方案:专用测试机 / 模拟器 / 云手机,最稳定。

## 9. 安全与策略

- **风险分级**:按钮名称或所在页面命中「支付、付款、转账、删除、发送、确认购买、授权、安装、卸载」等词 → `+cap!`(带 `!`),调用返回 `ERR CONFIRMATION_REQUIRED`。
- **包名白名单**:默认只允许操作配置中的 App;银行、支付类 App 默认禁止。
- **系统弹窗**:权限授予、无障碍/设备管理器开启、安装未知来源等系统对话框一律需要用户确认。
- **敏感数据**:`isPassword` 字段只报告 `filled=yes|no`;通知内容默认折叠成数量。
- **合规**:Portal 不上架或单独说明用途;不实现反检测;遵守各 App 的无障碍使用条款。

## 10. 仓库结构(建议直接并入 SomnusUI)

```
somnus/
├── providers/android/
│   ├── provider.py      AndroidProvider:snapshot / perform / events
│   ├── transport.py     adb forward + WebSocket JSON-RPC
│   ├── rawtree.py       Portal JSON → RawTree
│   └── device.py        设备发现、Portal 安装与授权引导
├── compiler/rules/android.py   标签上提、包装折叠、Compose/WebView 规则
├── runtime/                    共用;新增 scroll-find、gesture 路由
├── protocol/tools.py           switch → system(Android 下)
portal/                          Kotlin Android App
├── service/SomnusAccessibilityService.kt
├── service/TreeSerializer.kt
├── service/ActionExecutor.kt
├── ime/SomnusIme.kt            可选
├── display/WorkDisplay.kt      可选
└── server/RpcServer.kt
apps/android-scenario-suite/     验收 App(见第 11 节)
tests/fixtures/android/          录制的 RawTree
```

## 11. 测试与评测

### 11.1 验收 App:一个 App,六个屏幕

| 屏幕 | 技术 | 考察点 |
| --- | --- | --- |
| `store` | RecyclerView 500 行 + 搜索框 | 虚拟化列表、FIND scroll、折叠预览 |
| `form` | View 体系表单 | 可写/只读、开关、下拉、禁用按钮、提交校验 |
| `compose` | Jetpack Compose | 类名无意义时的能力推断 |
| `chat` | 自定义输入框 + 发送 | set_text 通道降级、发送记录作为验证面 |
| `web` | WebView 表单 | WebView 节点编译与折叠 |
| `canvas` | 纯自绘 | 必须报 `ERR SURFACE_OPAQUE`,不能猜 |

每个屏幕同时有真机版本和录制 fixture:`tests/scenarios/` 在任何平台可跑,`tests/e2e/ -m android` 证明 fixture 与真机一致。

### 11.2 基准

- **AndroidWorld**:只给模型 SUI-Text(禁用截图)跑全部任务,对比官方 a11y-tree 基线与视觉基线。
- **自建 50 任务集**:系统设置、微信/支付宝外的常用 App、文件管理、日历等,记录能做/不能做。
- 指标:任务成功率、平均步数、每步 token、每步延迟、`OK_UNVERIFIED` 比例、误操作次数。

## 12. 里程碑

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| M0 | 录制 RawTree fixture + mock provider | `somnus --android-fixture settings observe` 可跑 |
| M1 | Portal:snapshot / click / back / launch | 真机打开设置并进入 WLAN |
| M2 | Android 编译规则 | 验收 App 六屏 L0 均 < 400 token |
| M3 | settle / verify / delta | toggle、set_text 回包 delta 正确 |
| M4 | FIND scroll、set_text 通道降级 | store、chat 屏通过 |
| M5 | 策略与确认 | 高风险按钮 100% 拦截 |
| M6 | MCP 接入 + AndroidWorld 跑分 | 出第一版基准报告 |
| M7 | 工作屏(虚拟显示)实验 | 至少 2 个 ROM 上可用 |

## 13. 风险与开放问题

- **国产 ROM 杀后台**:无障碍服务被杀 → 需要前台通知 + 引导加入电池白名单 + `ERR SERVICE_DISCONNECTED` 自动重连。
- **App 不暴露语义**:自绘或未做无障碍的 App,只能靠手势通道或直接报告不支持;是否引入「最小视觉兜底」需单独决策,默认不做。
- **反无障碍检测**:部分金融类 App 检测到无障碍服务会拒绝运行或限制功能,按策略默认不碰。
- **动画与骨架屏**:settle 阈值需要按 App 自适应。
- **App Pack 自动生成**:是否借鉴 AutoDroid 的离线探索,自动为常用 App 生成领域词汇(`CHAT c1 +send!`),作为 v2 方向。