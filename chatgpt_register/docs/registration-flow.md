# 注册流程

| 阶段 | 请求/动作 | 成功输出 | 可重试边界 |
| --- | --- | --- | --- |
| `authorize` | ChatGPT csrf + signin + Auth0 跳转 | 授权落地 URL | 可重试 |
| `check_email` | `POST /api/accounts/check` | `continue_url` | 可重试 |
| `otp` | 发送 OTP、轮询邮箱、validate | `continue_url` | 可重试 |
| `about_you` | `POST /api/accounts/create_account` | `continue_url` | 成功后不可整体重试 |
| `callback` | 跟随 OAuth redirect | ChatGPT cookie | 新号失败记 partial |
| `session` | `GET /api/auth/session` | access token/session JSON | 新号失败记 partial |

`created` 只能在 `create_account` 返回成功且包含 `continue_url` 后置为真，不能仅凭进入
`/about-you` 推断。已存在账号不会经过该阶段，最终结果仍可能是 `success`，但
`created=false`。

