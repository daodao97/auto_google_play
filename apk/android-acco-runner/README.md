# AutoAcco Android Runner

AutoAcco Android Runner 是运行在 Android 设备上的自动化执行端。它接收管理端任务，执行 YAML 流程，完成界面操作、设备命令和账号管理动作，并回传状态、截图、UI dump 和执行结果。

## 技术栈

- Android：Kotlin + Java
- 构建：Gradle + Android Gradle Plugin
- UI 自动化：AccessibilityService
- 本地 HTTP：NanoHTTPD
- 服务端通信：OkHttp WebSocket + HTTP
- YAML 解析：SnakeYAML
- 账号管理：LSPosed/Xposed 模块能力

## 功能

- 通过 WebSocket 接收管理端命令，HTTP 作为回退通道。
- 执行管理端下发的远程 YAML 流程。
- 支持 YAML `imports/use` 引入可复用流程模块。
- 上报任务、步骤、截图和 UI dump 结果。
- 支持启动、暂停、恢复、停止、单步执行、替换运行期资源、打开应用和清理应用数据。
- 支持 Google 账号列表查询和移除。
- 支持无线调试固定端口。

## 运行配置

APK 首页可配置：

- 手机 ID
- 任务超时时间
- API 地址，例如 `https://<server-host>:4399`
- WS 地址，例如 `wss://<server-host>:4400`
- API Token

## 构建

```bash
JAVA_HOME=$(/usr/libexec/java_home -v 17) ./gradlew :app:assembleDebug
```

输出 APK：

```text
app/build/outputs/apk/debug/app-debug.apk
```
