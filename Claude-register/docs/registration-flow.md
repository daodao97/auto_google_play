# Claude 注册全链路 API 文档

基于 `claudeaizhuce.har`（324 条，阶段 1–3）+ `~/Downloads/1.har`（9 条，阶段 0）逆向整理。四阶段已全部覆盖。

## 总览：四个阶段

| 阶段 | 干什么 | 是否在 HAR 内 | 反爬 |
|------|--------|--------------|------|
| 0. 发送 magic link | 输入邮箱 → Claude 发信 | ✅ `1.har` | **Arkose（走 cookie）** |
| 1. 落地 + 挑战 | 点开邮件链接 → 拿 Arkose token | ✅ | **Arkose Labs** |
| 2. 验证 magic link | 用 nonce 换登录态，**账号在此创建** | ✅ | 需 Arkose token（body） |
| 3. Onboarding | 接受条款 / 设名字 / 建首个会话 | ✅ | 仅需登录态 |

> 关键结论：**账号在阶段 2 的 `verify_magic_link` 响应 `"created":true` 时才创建**。magic link 的 nonce + base64 邮箱都在邮件链接的 URL fragment 里。
>
> ⚠️ Arkose 两种传递方式不同：**阶段 0 发信**的 arkose 校验走 **cookie**（Arkose JS 跑完种 `.claude.ai` cookie，token 不在请求里）；**阶段 2 verify** 的 arkose token 放 **body**。所以发信必须在跑过 Arkose 的浏览器会话里调，纯 API 注入 token 行不通。

## 通用请求头

所有 `claude.ai/api/*` 接口都带这些头（阶段 2 之后）：

```
content-type: application/json
origin: https://claude.ai
anthropic-client-platform: web_claude_ai
anthropic-client-version: 1.0.0
anthropic-client-sha: cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957   ← 随前端构建变化
anthropic-anonymous-id: claudeai.v1.<uuid4>                       ← 客户端生成
anthropic-device-id: <uuid4>                                       ← 客户端生成
x-activity-session-id: <uuid4>                                     ← 与 activitySessionId cookie 一致
x-datadog-trace-id: <uint64>
x-datadog-parent-id: <uint64>
x-datadog-sampling-priority: 1
traceparent: 00-0000000000000000<trace-id-hex>-<parent-id-hex>-01
tracestate: dd=s:1;o:rum
referer: https://claude.ai/...
```

登录态由 cookie 承载（`verify_magic_link` 成功后下发）。`anthropic-client-sha` 是前端构建哈希，会随发版变——优先从 `/login` + JS chunk 提取，`/login` 被 403 时用 `/edge-api/bootstrap` best-effort 兜底。

> **指纹层（curl_cffi impersonate）**：所有 `claude.ai` 请求经 `claude_register.core.browser` 走 `curl_cffi`，`impersonate=chrome142` 在 TLS/JA3 层伪装真 Chrome（对标 gopay-pipeline）。UA / sec-ch-ua 与 impersonate 版本对齐。邮箱取信由 `claude_register.mail.fetcher` 独立调用 mail.xcaigc.com。

---

## 阶段 0：发送 magic link

发信流程（`1.har`，9 条）：预检 → Arkose 挑战 → 发信。

### 0.1 预检：login_methods
```
GET https://claude.ai/api/auth/login_methods?email=<urlencoded邮箱>&source=claude-ai
```
响应：
```json
{"methods":["google","magic_link"]}
```
查该邮箱可用哪些登录方式。可选步骤，前端用它决定显示哪些按钮。

### 0.2 Arkose 挑战（拿 cookie，不是 token-in-body）
```
POST https://a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-D6AC-4C03-B678-AABF639EE69A
```
和阶段 1.4 完全相同的 Arkose 入口，返回 `{"token":"84018bd496b6d0ea4.2963690804|r=ap-southeast-1|..."}`。
本次抓包里 `metrics/ui` 上报 `suppressed:true`——**挑战被抑制（静默放行）**，说明风险分低时 Arkose 不弹图形码。

> ⚠️ 关键差异：阶段 2 verify 把 arkose token 放进请求 **body**；阶段 0 发信则**不放**——arkose 校验靠 Arkose JS 跑完后种在 `.claude.ai` 域的 **cookie**（HAR 里 Cookie/Set-Cookie 被剥，但 send_magic_link 请求体和头里都无 token，只能是 cookie）。所以**发信必须用跑过 Arkose 的浏览器会话调**，纯 API + 注入 token 不行。

### 0.3 ★ send_magic_link
```
POST https://claude.ai/api/auth/send_magic_link
Content-Type: application/json
Referer: https://claude.ai/login
```
请求体（**无 arkose 字段**）：
```json
{
  "utc_offset": -480,
  "email_address": "ssunhildazep01@legislator.com",
  "login_intent": null,
  "locale": "en-US",
  "return_to": null,
  "source": "claude"
}
```
响应：
```json
{
  "fallback_code_configuration": {"charset":"numeric","length":6,"show_input_after_delay":5},
  "sent": true,
  "sso_url": null
}
```
- `sent:true` = 邮件已发。
- `fallback_code_configuration`：6 位数字**备用验证码**，5 秒后可改用输入码验证（另一条验证路径，magic link 收不到时可用）。

---

## 阶段 1：落地 + Arkose 挑战

### 1.1 落地页
```
GET https://claude.ai/magic-link
```
邮件链接形如：`https://claude.ai/magic-link#<nonce>:<base64邮箱>`
- fragment 不发往服务端，由前端 JS 解析。
- 例：`#93fa8b09ce1f0d1c4d48cdb636b2e60a:ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==`
  - nonce = `93fa8b09ce1f0d1c4d48cdb636b2e60a`（32 hex）
  - base64 邮箱 = `ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==` → 解码即邮箱

### 1.2 应用配置
```
GET https://claude.ai/edge-api/bootstrap?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```

### 1.3 Arkose 公钥配置
```
GET https://a-cdn.claude.ai/v2/EEA5F558-D6AC-4C03-B678-AABF639EE69A/settings
```
`EEA5F558-D6AC-4C03-B678-AABF639EE69A` 是 Arkose 的 public_key（Claude 的固定值）。

### 1.4 ★ Arkose 拿 token（反爬核心）
```
POST https://a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-D6AC-4C03-B678-AABF639EE69A
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: https://claude.ai
x-ark-arid: <Arkose 客户端生成的 blob>
x-ark-esync-value: 1782648000
```
请求体（form-urlencoded）：
```
c=<巨大 blob，Arkose 客户端 JS 生成>
&public_key=EEA5F558-D6AC-4C03-B678-AABF639EE69A
&site=https://claude.ai
&userbrowser=<UA>
&capi_version=4.4.0
&capi_mode=lightbox
&style_theme=default
&rnd=<0.x 随机浮点数>
```
响应：
```json
{"token":"17318bd469825b4d4.7960819401|r=us-east-1|meta=3|...","pow":false,
 "challenge_url_cdn":"https://a-cdn.claude.ai/cdn/fc/assets/ec-game-core/bootstrap/.../game_core_bootstrap.js", ...}
```
- `token` 即下一步的 `arkose_session_token`。
- 这是 **Arkose Labs（FunCaptcha）**，`c=` blob 与 `x-ark-*` 头由 `ec-game-core` 客户端 JS 实时生成，纯 HTTP 库无法复刻；风险低时静默放行，风险高时弹图形验证码。

### 1.4a ★ arkose_session_token 三层解析（`arkose.py`）

`register.py` 不再要求外部硬塞 `arkose_session_token`——`arkose.resolve_arkose_token(cfg, profile)` 按序降级（对标 gopay-pipeline 的 `passive_captcha_token` + `captcha_solver_keys`），Arkose 请求复用同账号 UA / sec-ch-ua / platform：

| 优先级 | 策略 | 来源 | 说明 |
|--------|------|------|------|
| 1 | **透传** | `config.arkose.passive_token` | 外部已拿到的 token，原样返回 |
| 2 | **yescaptcha 打码** | `config.arkose.solver.api_key` | `FunCaptchaTaskProxyless`：`createTask` → 轮询 `getTaskResult` → 已解 token（付费，真自动） |
| 3 | **直接重放** | `config.arkose.replay.{c_blob, x_ark_arid}` | curl_cffi 精确复刻上面的 `POST /fc/gt2/public_key/`，需调用方提供**新鲜浏览器抓**的 `c_blob` + `x_ark_arid` |

三者皆空 → 抛 `RuntimeError`。

> ⚠️ 直接重放里的 `c_blob` / `x_ark_arid` / `ark_build_id` 必须来自真浏览器（DevTools 抓或 Playwright 产），纯 HTTP 造不出；HAR 里的值是一次性的，过期会被拒。打码平台（yescaptcha）是在其后端跑 Arkose JS 拿已解 token，是当前唯一能纯自动的路子，但付费且仍属绕反爬。

### 1.5 Arkose 游戏加载（如需）
```
GET https://a-cdn.claude.ai/fc/a/?callback=__jsonp_...&session_token=<上一步 token>&category=loaded&action=game%20loaded&...
```

---

## 阶段 2：验证 magic link（账号创建点）

### 2.1 ★ verify_magic_link
```
POST https://claude.ai/api/auth/verify_magic_link
Content-Type: application/json
Referer: https://claude.ai/magic-link
```
请求体：
```json
{
  "credentials": {
    "method": "nonce",
    "nonce": "93fa8b09ce1f0d1c4d48cdb636b2e60a",
    "encoded_email_address": "ZHVyZ3NmenBxandibEBtYWlsLmNvbQ=="
  },
  "locale": "en-US",
  "arkose_session_token": "17318bd469825b4d4.7960819401|r=us-east-1|meta=3|...",
  "source": "claude"
}
```
响应（关键字段）：
```json
{
  "success": true,
  "created": true,                          ← true = 新账号在此创建
  "account": {
    "uuid": "a873fede-b950-42a3-9a1f-53a7d4b57422",        ← account_uuid
    "email_address": "durgsfzpqjwbl@mail.com",
    "memberships": [{
      "role": "admin",
      "organization": {
        "uuid": "6e395e8f-cc30-4cf7-9904-1c0cb240d031",    ← org_uuid
        "name": "<email>'s Organization"
      }
    }]
  }
}
```
成功后服务端下发 session cookie，后续接口凭 cookie 鉴权。

---

## 阶段 3：Onboarding（登录态后，按序）

以下接口都需携带阶段 2 拿到的 cookie + 通用头。顺序按 HAR 时序：

### 3.1 标记 onboarding 开始
```
PATCH https://claude.ai/api/account/settings
{"has_started_claudeai_onboarding": true, "has_finished_claudeai_onboarding": false}
```
→ 202

### 3.2 隐私 / Cookie 同意（两次）
```
PUT https://claude.ai/v1/privacy-consents
{"consent_type":"cookies.analytics","consent_decision":"CONSENT_DECISION_OPT_IN","source":"implicit_regional_default"}

PUT https://claude.ai/v1/privacy-consents
{"consent_type":"cookies.marketing","consent_decision":"CONSENT_DECISION_OPT_IN","source":"implicit_regional_default"}
```
→ 200

### 3.3 接受法律文档
```
PUT https://claude.ai/api/account/accept_legal_docs
{
  "acceptances": [
    {"document_id":"v3:aup:22742366-2ef0-4c7a-a833-6523f10d3944","accepted_via_checkbox":true},
    {"document_id":"v3:consumer-terms:79dbc8c6-7f64-43d6-8101-207cede59a4d","accepted_via_checkbox":true},
    {"document_id":"v3:privacy:cf9b9ac4-d387-48b8-8560-ce1c58b8a34b","accepted_via_checkbox":false}
  ]
}
```
→ 202。**document_id 是版本化的**，会随法务文档更新而变；当前由 `dynamic_config.fetch_legal_docs()` 通过 `GET /api/legal` 动态获取，失败时回退到默认快照。

### 3.4 邮件营销同意
```
PUT https://claude.ai/api/account/email_consent
{"consent": true, "accepted_via_checkbox": false, "variant": "notices"}
```
→ 202

### 3.5 年龄验证
```
PUT https://claude.ai/api/account?statsig_hashing_algorithm=djb2
{"age_is_verified": true}
```
→ 202

### 3.6（可选）开 grove
```
PATCH https://claude.ai/api/account/settings
{"grove_enabled": true}
```
→ 202。UI 触发的开关，可省略。

### 3.7 ★ 设名字
```
PUT https://claude.ai/api/account?statsig_hashing_algorithm=djb2
{"display_name": "xiaoshua", "full_name": "xiaoshua"}
```
→ 202

### 3.8 建首个会话（onboarding 欢迎聊天）
```
POST https://claude.ai/api/organizations/<org_uuid>/chat_conversations
{"uuid": "<新生成 uuid4>", "name": "Your first chat with Claude"}
```
→ 201

### 3.9 工作职能
```
PUT https://claude.ai/api/account_profile
{"work_function": "Other"}
```
→ 200。当前只使用 HAR 已验证的 `Other`；不要把前端展示文案直接当作 API 枚举。

---

## 邮箱系统对接（阶段 0→1 衔接）

magic link 会发到邮箱，项目通过 `claude_register.mail.fetcher` 调用
`https://mail.xcaigc.com` 的 tRPC 服务统一取信：

| 模式 | 适用 | 实现 |
|------|------|------|
| `mailcom` | mail.com 系列邮箱 | xcaigc `mail.fetch` |
| `imap` | Gmail/Outlook/Zoho/自定义域 | xcaigc `mail.fetch` |
| `microsoft` | Outlook/Hotmail/Live token 号 | xcaigc `mail.fetchMsGraphByCredential` |

### 对接模块：`claude_register.mail.fetcher`

封装了上面的 tRPC 调用，自动完成：登录邮箱 → 按 `claude.ai`/`anthropic.com` 发件人或主题挑邮件 → 拉详情 → 从 `links`/`bodyHtml` 正则抽出 `magic-link#<nonce>:<base64邮箱>` → 解析出 `nonce` + `encoded_email_address`。

```python
from claude_register.mail.fetcher import fetch_magic_link
res = fetch_magic_link("emp1@mail.com", "pwd", provider="mailcom")
# res = {nonce, encoded_email_address, magic_link_url, email, mail_id, subject}
```

`res["nonce"]` + `res["encoded_email_address"]` 直接喂给阶段 2 的
`verify_magic_link`。模块带轮询、FIFO 限流、截止时间和错误脱敏；邮箱服务 URL 固定为
`https://mail.xcaigc.com`。

### 整条链路串起来

`register.py` 是可运行的编排脚本，四段串成一条链路：

```
[0/3 发信]   POST /api/auth/send_magic_link {utc_offset,email_address,locale,source}
             → sent:true（Claude 发邮件）
             ⚠️ arkose 走 cookie，必须在跑过 Arkose 的浏览器会话里调，纯 API 不行
        │  Claude 发信到邮箱
        ▼
[1/3 抓邮件]  mail_fetcher_client.fetch_magic_link(email, pwd)
              → nonce + encoded_email_address            ✅ 已实测（mail.com 登录+取件正常）
        │
[2/3 verify]  POST /api/auth/verify_magic_link {nonce, encoded_email, arkose_session_token}
              → 创建账号 + Set-Cookie 登录态（session 自动保留）
              arkose_session_token 可由 arkose.resolve_arkose_token 三层解析（透传→yescaptcha→重放）
        ▼
[3/3 onboarding]  onboarding.run_onboarding(session, org_uuid, display_name)
                  → 跑完 9 步 onboarding                            ✅ 已封装
```

用法：
```bash
python3 -m claude_register.auth.service --confirm-external-run
```

> Arkose 硬卡点现状：**发信（阶段 0）走 cookie、验证（阶段 2）走 body token**，两步都要真浏览器跑 Arkose JS。`arkose.py` 让 verify 的 `arkose_session_token` 可由 config 三层解析（透传/yescaptcha/重放，见 1.4a），不再必填；但发信那步仍需用跑过 Arkose 的浏览器 session 调（见附录 A 架构建议）。

---

## 每次注册会变的变量

| 变量 | 来源 | 示例 |
|------|------|------|
| `nonce` | 邮件链接 fragment 第一段 | `93fa8b09ce1f0d1c4d48cdb636b2e60a` |
| `encoded_email_address` | 邮件链接 fragment 第二段（base64 邮箱） | `ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==` |
| `arkose_session_token` | 阶段 1.4 响应 | `17318bd469825b4d4.7960819401\|...` |
| `account_uuid` | 阶段 2.1 响应 | `a873fede-b950-42a3-9a1f-53a7d4b57422` |
| `org_uuid` | 阶段 2.1 响应 | `6e395e8f-cc30-4cf7-9904-1c0cb240d031` |
| `anthropic-client-sha` | 页面/bootstrap（随发版变） | `cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957` |
| legal doc `document_id` | `GET /api/legal`（版本化） | `v3:aup:22742366-...` |

## 反爬与合规提醒

- **Arkose Labs** 保护发信 + 验证两步（已确认，非推测）：发信走 cookie、验证走 body token。`c=` blob / `x-ark-*` 头只能由真浏览器跑 Arkose 的 JS 产生。`1.har` 里 `suppressed:true` 说明低风险时静默放行，但批量同 IP 注册风险分飙升会强制弹图形码。
- 自动化批量注册违反 Anthropic 服务条款，账号/组织/IP 可能被封。公司场景建议走 **Claude Team/Enterprise** 管理员邀请。

---

## 附录 A：自动化架构建议

发信（阶段 0）的 arkose 走 cookie，决定了整条链路**浏览器绑定**——纯 HTTP 客户端走不通发信那步。现实可行的两种架构：

**架构一：Playwright 驱动真浏览器（自包含）**
- 一个浏览器会话：打开 `claude.ai/login` → 输入邮箱 → Arkose 在浏览器里跑（低风险静默放行）→ `send_magic_link` 自动带 cookie 发出。
- 邮件到后用 `mail_fetcher_client` 抓 magic link。
- 浏览器导航到 magic link → 再次跑 Arkose → verify 自动完成（或从浏览器抓 arkose token 调 verify API）→ 拿到登录态。
- 用该 session 跑 `onboarding.py`。
- 风险：Playwright 指纹可能被 Arkose 识破 → 弹码 → 卡住。需指纹伪装（stealth）。

**架构二：API + 外部 arkose token（当前 `register.py` 的路子）**
- 发信：用一个「跑过 Arkose 的浏览器 session」导出 cookie，喂给 `requests.Session` 调 `send_magic_link`。
- verify：外部（打码平台）拿 `arkose_session_token` 喂给 `register.py`。
- 适合发信由人工/半自动触发、仅 verify+onboarding 自动化的场景。

**架构三：`arkose.py` 三层解析（已实现，推荐与架构二并用）**
- `runtime/config.json` 的 `arkose` 段配 `passive_token` / `solver.api_key`(yescaptcha) / `replay.{c_blob,x_ark_arid}` 任一。
- `register()` 在 verify 前自动调 `resolve_arkose_token(cfg, profile)` 拿 token（透传 → yescaptcha 打码 → 直接重放）。
- 发信那步仍走架构二的浏览器 session（cookie-arkose 未解）。
- 直接重放分支可配合架构一：Playwright 跑 Arkose JS 抓 `c_blob` + `x_ark_arid`，再由 `arkose.py` 用 curl_cffi 发请求拿 token——把「跑 JS」和「发请求」拆开。

三条路都不在 `register.py` 里凭空实现 Arkose 的 `c=` blob——那是反爬机制，绕它既不稳又违规；`arkose.py` 只做 token 的解析编排（透传/打码/重放），不伪造 blob。

---

## 附录 B：批量编排 + Web 仪表盘

对标 gopay-pipeline 的 orchestrator（队列分发 + 重试 + 结果落盘），**仅注册，无支付**。

### 会话代理轮换
`config.proxy_template` 形如 `http://storm-xxx_session-{session}_life-1:PWD@us.stormip.cn:1100`，`{session}` 占位符（gopay `{SESSION}` 同款）。`http_client.materialize_session_proxy()` 每个账号调一次，`secrets.token_hex(8)` 生成唯一 id 替换 → 每账号独立粘性 IP。`proxy`（具体代理）作为无模板时的回退。

### `claude_register.orchestration.service`（批量编排）
- `Account` / `AccountTask`（status: pending|running|success|failed|partial，stage: send|mail|arkose|verify|onboarding|kyc）。
- `register_one(task, cfg, on_progress, cancel)`：每账号独立代理 + 独立指纹 id，阶段级推进，`retry_max` 重试（网络/429/5xx/超时可重试；verify 失败/arkose 无配置/send 4xx/邮箱认证失败直接退出）。verify 成功后如果 onboarding/KYC 异常，标 `partial`，不再重跑 send/verify。
- `orchestrate(accounts, cfg, concurrency, on_progress, cancel, tasks)`：固定 worker 从队列取账号；webui 传入预建 `tasks` 列表，`register_one` 原地更新对象，SSE 实时读到阶段变化。停止后不再启动未开始账号，未开始账号只更新内存态，不写入 `failed.txt`。
- `workers`：可在 `runtime/config.json` 配置 worker 级 `proxy` / `proxy_template` / `impersonate` / `mail_provider` / `mail_poll_interval` / `mail_poll_timeout` / `mail_fast_path` / `send_settle_delay` / `resolve_exit_ip` / `interval_seconds`。
- **邮箱限流**：`FifoRateLimiter` 对 xcaigc mailcom 请求按到达顺序分配请求槽。
- **邮箱快速路径**：`mail_fast_path=true` 仅复用首次邮箱列表和首个候选详情的等待槽，后续轮询保持原有限流、轮询间隔和总超时；默认关闭。
- **发信后等待**：`send_settle_delay` 留空时保持原来的随机 `2.5～5.5` 秒；显式填写后才使用固定值，建议先灰度验证。
- **出口 IP**：`resolve_exit_ip=true` 在账号流程完成后使用同一代理和浏览器身份做 best-effort 探测，结果只进入公开的 `proxy_exit_ip` 字段；探测失败不改变业务结果。
- 结果统一写入 `runtime/`；Microsoft token 号保留 `client_id`/`refresh_token` 交付前缀。
- headless CLI：`python3 -m claude_register.orchestration.service --confirm-external-run`。

### `claude_register.presentation.web`（Web 仪表盘）
启动：
```bash
uvicorn claude_register.presentation.web:app --host 127.0.0.1 --port 8000
```
- `GET /` 仪表盘页；`GET /api/config` 预填表单；`POST /api/start` 后台线程跑 `orchestrate`；`GET /api/progress` SSE 先发一次安全全量快照，之后发送 `run_started` / `task_updated` / `summary_updated` / `run_finished` / `heartbeat` 增量事件；`GET /api/current-run` 用于页面刷新后接管当前运行；`POST /api/stop` 取消；`GET /api/results.txt` / `/api/failed.txt` 下载。
- 前端：表单（注册/提 session 模式、代理模板/并发/重试/邮件取信方式/自动发信/邮箱快速路径/出口 IP/账号列表）+ 汇总条 + 实时表格（邮箱/Worker/阶段/状态/KYC/耗时/出口 IP/错误）+ 按「免KYC / 需KYC / 异常未知 / 失效」四列实时输出，`EventSource` 订阅，自动重连。公开任务 DTO 不包含密码、refresh token、session 或代理凭据。

### 已知约束
1. **send_magic_link 的 cookie-arkose**：纯 HTTP 仍无法保证通过。chrome 指纹+住宅代理有真实通过可能（旧裸 requests 必失败），失败时该账号标 failed@send；需稳则 `auto_send:false`（假设邮件已发，从抓邮件起跑）。
2. **xcaigc provider 频率限制**：FIFO 邮件限流器兜底，并发越高单账号越慢。
3. **代理 `life-1`**：单账号超 1 分钟 IP 可能轮换；建议模板调大 life 或控单账号耗时。
4. **yescaptcha 付费 + ToS**：批量注册违反 Anthropic ToS。
