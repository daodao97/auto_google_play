# IAP Mode3 Minimal

IAP Mode3 Minimal 是一个最小 Xposed/LSPosed 模块，用于在授权测试范围内把 Google Play Billing 订阅升级或替换模式固定为 raw `3`。

raw `3` 对应：

- `ProrationMode.IMMEDIATE_WITHOUT_PRORATION`
- `ReplacementMode.WITHOUT_PRORATION`

## 功能

- 无界面。
- 无数据库。
- 无通知。
- 无事件回传。
- 仅在 LSPosed 作用域选中的目标 App 进程内生效。
- 只修改订阅升级或替换模式相关的 int 参数。

## 构建

```bash
JAVA_HOME=$(/usr/libexec/java_home -v 17) gradle assembleDebug
```

输出 APK：

```text
app/build/outputs/apk/debug/app-debug.apk
```
