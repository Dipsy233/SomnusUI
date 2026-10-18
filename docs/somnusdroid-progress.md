# SomnusDroid 开发进度

按《Somnusuim 基于无障碍树的安卓 Agent 开发文档.md》§12 里程碑记录。

## 里程碑状态

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| 部署形态:前台 Service | **完成(真机验收通过)** | RpcServer 迁入前台 `RpcService`;`am kill` / `kill -9` 后 RPC 均存活(后者靠 START_STICKY 重启) |
| M0 录制 fixture + mock provider | **完成** | `somnus --android-fixture <json> observe` 可跑;Portal 快照 JSON 走真实转换 + 共享编译器;录制文件在 `tests/fixtures/android/` |
| M1 Portal:snapshot / click / back / launch | **完成(真机验收通过)** | 真机 Android 16(MIUI):snapshot wire 93–281ms(门禁 300);打开设置→点 WLAN 进入详情页;BACK;搜索框 set_text "蓝牙"→列表实时过滤;launch/apps 全通 |
| M2 Android 编译规则 | **完成** | 标签上提、包装折叠、裸 View→UNKNOWN;真机设置/桌面/WLAN/搜索页 L0 全部 ≤ 305 token(门禁 400) |
| M3 settle / verify / delta | **复用即得** | 共享 runtime 直接工作;Android mock `is_deterministic=True`;真机侧靠 Portal `rev`/`events` 计数器(已实现) |
| M4 FIND scroll、set_text 降级 | **host 端完成** | `somnus/runtime/scrollfind.py` + `FIND ... in REF scroll` 语法;store 屏滚动查找通过;set_text 通道 2/3(剪贴板/IME)Portal 侧已留接口,未实装 |
| M5 策略与确认 | **部分复用** | 共享 Policy 生效(包名白名单/critical 确认);中文高风险词表未加 |
| M6 MCP + AndroidWorld | 未开始 | |
| M7 工作屏 | 未开始 | |

## 新增文件

```
somnus/providers/android/          AndroidProvider / transport / rawtree / mock
somnus/compiler/rules/android.py   标签上提、噪声折叠
somnus/runtime/scrollfind.py       FIND scroll(虚拟化列表)
portal/                            Kotlin App(见 portal/README.md)
tests/fixtures/android_scenarios.py 六屏验收 fixture
tests/fixtures/android/*.json      录制快照
tests/android/                     43 个测试(转换/编译/运行时/协议/滚动)
```

## 对 SomnusUI 共用代码的修改(合并接口)

1. `errors.py` — 新增 4 个 Android 错误码 + hint
2. `providers/base.py` — 工厂注册 `android`
3. `compiler/compile.py` — `compile_prep` 预处理钩子(不改变既有管线)
4. `ir/action.py` — ActionKind 增加 `system` / `app`;Action 增加 `scope_scroll`
5. `sui/parse.py` — `SYSTEM` / `APP` / `FIND ... scroll` 语法
6. `runtime/session.py` — `system_action` / `app_action` 分发;find 的 scroll 分支
7. `cli.py` — `--provider android` / `--android-fixture`

Windows 行为零改动:`tests` 全绿(468 passed;clipboard 4 个失败为上游既有 Windows-only 用例)。

## 关键设计决定(与文档的偏差/补充)

- **行名合成时机**:标签上提在预处理阶段完成,同时清空行内文本子节点——否则共享 simplify 的重名折叠规则会先删掉与行同名的子文本,启发式行检测随后把副标题当成行名(实测踩过)。
- **裸 `android.view.View` → `unknown`**:在类名规则表之前特判,否则会被 `view` 片段吞掉伪装成 group。
- **FIND scroll 的 scope-less 形式**(`FIND "x" scroll`)允许:无 scope 时等价于普通 FIND,不会猜一个列表来滚。
- **手势通道默认关**(`gesturesEnabled=false`),与 host 的 `allow_gesture` 双侧开关。
- **RPC 只绑 127.0.0.1**:Portal 的信任边界就是 adb forward,未 attached 的设备不可达。

## 真机验收记录(2026-10-18,设备 M332BF / Android 16 / MIUI)

- 构建:`gradle assembleDebug` 通过(JDK 21 @ E:\java,SDK android-34,阿里云镜像;系统残留代理 10.79.122.23:8502 不可达,已移出 gradle.properties)
- 部署:`adb install` + 手动开启无障碍(Android 16 无受限设置拦截)
- snapshot:3 窗口(应用/桌面/系统 UI),52–53 节点,wire 93ms
- perform CLICK:"设置"列表 WLAN 行 → 成功进入 WLAN 详情页(delta 可见返回按钮/扫描二维码/13 行列表)
- perform SET_TEXT:搜索框写入 "蓝牙" → 列表实时过滤
- global BACK:正常
- launch/apps:正常

### 真机暴露并修复的三个缺陷

1. **序列化器索引漂移(严重)**:原实现跳过不可见+不可点的子节点,但 key 路径用的是 live 树的 `getChild(i)` 真实索引——被跳过的孩子占了索引位,后续所有兄弟的 key 全部错位,`perform` 永远 NODE_GONE。修复:占位 null 保持槽位(host 转换时跳过)。
2. **findByKey 多走一层**:误以为 key 首段是根自身又 drop(1),14 层 key 只走 11 层。修复:windowId 之后的所有段都是从根开始的完整路径。
3. **key 分隔符与 window id 类型**:Portal 发 `6674/0/0`(斜杠),host 重建也用斜杠但 `_parse_key` 只认点号;perform 的 `window` 参数传了 `"0/6674"` 字符串。修复:两侧统一斜杠 + int()。

### 待办(部署形态)

- ~~RpcServer 生命周期挂在 MainActivity~~ ✅ 已迁入前台 `RpcService`(见下)

## 前台 Service 改造(2026-10-18,真机 M332BF 验收通过)

**动机**:真机实测 MIUI 回收 Activity 后端口消失(host 无法连接)。

**改动**:
- 新增 `portal/.../RpcService.kt`:`startForegroundService` + `START_STICKY` + 常驻通知(通知从 Activity 迁入 Service,Activity 仅引导启动);manifest 声明 `foregroundServiceType="specialUse"` + `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` + `FOREGROUND_SERVICE_SPECIAL_USE` 权限(Android 14 要求);MainActivity 的旧通知代码删除,状态页改为显示 `RpcService.running`。
- `local.properties`(不入库):`sdk.dir=C:/Users/Dipsy233/AppData/Local/Android/Sdk`。

**真机验收**:

| 场景 | 结果 |
|---|---|
| RpcService 前台状态(`isForeground=true`, type=specialUse) | ✅ |
| `am kill`(后台杀)后 snapshot 正常 | ✅ |
| `kill -9` 整进程 → START_STICKY 自动重启(pid 2732→12227),RPC 恢复 | ✅ |
| kill -9 后无障碍服务标记 crashed,MIUI 不自动重绑;`settings put secure enabled_accessibility_services` 重写一次即重绑成功(rev=1, 3 windows) | 已知限制,见下 |

**已知限制**:进程被杀后无障碍服务需要用户/adb 重写一次 enabled_accessibility_services 才重绑(MIUI 行为);RPC 端口本身已不受 Activity 存活影响。host 侧应对 `SERVICE_DISCONNECTED` 提示用户检查无障碍开关。

## 真机验收清单(M1/M2 上机)

1. ~~`./gradlew :app:assembleDebug` 构建通过~~ ✅
2. ~~Android 13+ 允许受限设置 → 开启服务 → 前台通知出现~~ ✅
3. ~~`adb forward tcp:18677 tcp:8677 && somnus --provider android observe` 输出当前屏 SUI-Text~~ ✅
4. ~~L0 snapshot 端到端 < 300ms~~ ✅(93ms 含全部窗口)
5. settings 屏 CALL sw.activate() 后 delta 正确
