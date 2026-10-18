# Somnus Portal

手机端 Portal App(SomnusDroid doc §4)。一个最小 Android 应用,只做三件事:

1. **导出树**:`SomnusAccessibilityService` 把所有可交互窗口序列化成一个 JSON 快照(RawTree);
2. **执行原语**:`perform` / `global` / `gesture` / `launch` / `clipboard.set`;
3. **RPC 服务**:`RpcServer` 在本机端口上接受 JSON-RPC over WebSocket(经 `adb forward` 暴露给主机)。

## 红线(与文档 §1.3 / §9 一致)

- resource-id、bounds、坐标只在 Portal 内部与指纹中使用,**不进入任何面向模型的文本**——Portal 根本不知道模型的存在;
- 不绕过反无障碍检测、安全键盘、支付验证;
- 密码字段序列化时文本替换为 `•••`(isPassword 仍照报);
- 前台通知常驻,提示无障碍服务运行中(也是被杀后用户可感知的信号)。

## 模块

| 文件 | 职责 |
| --- | --- |
| `service/SomnusAccessibilityService.kt` | 无障碍服务:窗口枚举、事件脏标记 |
| `service/TreeSerializer.kt` | AccessibilityNodeInfo 树 → RawTree JSON(单次遍历,节点及时回收) |
| `service/ActionExecutor.kt` | perform / global / gesture 的执行与错误归类 |
| `server/RpcServer.kt` | 嵌入式 WebSocket + JSON-RPC 分发 |
| `RpcService.kt` | 前台 Service:持有 RpcServer 生命周期(脱离 Activity 存活) |
| `MainActivity.kt` | 状态页:服务是否开启、adb forward 命令提示 |

## 构建

```
cd portal
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
# Android 13+:先在应用信息里「允许受限设置」,再到无障碍设置里开启服务
adb forward tcp:18677 tcp:8677
```

主机端验证:`somnus --provider android observe`。
