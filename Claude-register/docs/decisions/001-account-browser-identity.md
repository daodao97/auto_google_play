# ADR-001：账号级浏览器身份作为唯一事实来源

## 状态

已接受

## 日期

2026-07-14

## 背景

注册链路会创建多个 HTTP Session：动态配置、主注册重试和独立 KYC 检查。过去由编排器分别传递
`anonymous_id`、`device_id`、`BrowserProfile`、`BrowserRuntime` 和 Sentry trace ID。核心 ID 虽然通常
复用，但新 Session 会重新生成 `__ssid`、`_fbp` 和 consent cookie，KYC 还会丢弃刚完成验证的登录
Session。这种分散参数容易漏传，也使同一账号在一个运行周期内表现成多个浏览器会话。

## 决策

- 每个账号在流程入口只创建一个 `BrowserIdentity`。
- `BrowserIdentity` 持有长期身份 ID、`BrowserProfile`、`BrowserRuntime` 和 Sentry trace ID。
- `BrowserRuntime` 持有同一账号运行周期内稳定的 activity session、Datadog trace、`__ssid`、`_fbp`
  与 consent cookie。
- 动态配置、发送、验证、Onboarding、重试 Session 和 KYC 都引用同一身份对象。
- KYC 优先复用验证成功后的 Session；只有独立 KYC CLI 调用才自行创建并关闭 Session。
- 配置指定 `chrome131` 或 `chrome142` 时，只从相同 impersonate 版本的 UA/Client Hints 组合中选择。

## 备选方案

### 继续传递独立参数

接口兼容成本低，但调用点容易漏掉某个字段，辅助 Cookie 也没有稳定的所有者，因此否决。

### 直接覆盖 ZIP 中的扩展指纹池

ZIP 中部分 UA 版本与实际 `curl_cffi impersonate` 版本不一致，还包含未经当前请求资料验证的分析
Cookie。整包覆盖会降低内部一致性，因此只吸收可由离线测试证明的 Session 稳定性改动。

### 所有请求固定同一个 Datadog parent ID

trace ID 和 activity session 需要稳定，但单次请求的 parent/span 可以变化。固定全部 parent ID 会把
不同请求错误地表示成同一个 span，因此保留每请求 parent/span、账号级 trace 稳定的模型。

## 影响

- 新增 HTTP 阶段应接收或从 Session 读取 `BrowserIdentity`，不要再自行生成身份字段。
- 独立 CLI 仍可省略 identity，由模块创建一次性身份并负责关闭自己的 Session。
- 请求体、邮件 provider、KYC 分类和结果文件格式不受此决策影响。
- 离线测试必须覆盖跨 Session Cookie 稳定性和 KYC Session 复用。
