# Claude 注册全量请求参考

> 源数据：`claudeaizhuce.har`（324条，阶段1-3）+ `Downloads/1.har`（9条，阶段0）。
>  两个 HAR 均已从磁盘搬走，本文档从本会话全部提取记录复原——每条请求体/响应体/头都经过 HAR 原始 JSON 抽取验证。

## 通用说明

**所有 `claude.ai` 接口的通用请求头**（每个具体请求不再逐一重复）：

```
content-type: application/json
origin: https://claude.ai
anthropic-client-platform: web_claude_ai
anthropic-client-version: 1.0.0
anthropic-client-sha: cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957   ← 随前端发版变
anthropic-anonymous-id: claudeai.v1.<uuid4>                       ← 端侧生成
anthropic-device-id: <uuid4>                                       ← 端侧生成
user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36
            (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36
```

**Arkose Labs 的 `x-ark-*` 专用头**（`fc/gt2` 请求）：

```
x-ark-arid: <Arkose 客户端 JS 生成的长 blob>
x-ark-esync-value: 1782648000
```

**登录态**：`verify_magic_link` 成功后服务端 Set-Cookie，后续接口凭 cookie 鉴权。

---

## 阶段 0：发送 magic link（`1.har`，9 条全量）

### 0.1 — GET login_methods（预检）
```
GET https://claude.ai/api/auth/login_methods?email=ssunhildazep01%40legislator.com&source=claude-ai
```
请求头：
```
content-type: application/json
anthropic-anonymous-id: claudeai.v1.5c4ba0c7-aa12-489b-9fc9-047a140cc8fc
anthropic-client-sha: cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957
anthropic-client-platform: web_claude_ai
anthropic-client-version: 1.0.0
anthropic-device-id: 5c15d49c-884b-4973-bee5-ab4b6de38574
Referer: https://claude.ai/login
```
响应（200）：
```json
{"methods":["google","magic_link"]}
```

### 0.2 — POST Arkose fc/gt2（拿 token）
```
POST https://a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-D6AC-4C03-B678-AABF639EE69A
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: https://claude.ai
Referer: https://claude.ai/
x-ark-arid: <Arkose blob>
x-ark-esync-value: 1782648000
```
请求体（form-urlencoded）：
```
c=<Arkose 客户端 JS 实时生成的大 blob（>10KB）>
&public_key=EEA5F558-D6AC-4C03-B678-AABF639EE69A
&site=https%3A%2F%2Fclaude.ai
&userbrowser=Mozilla%2F5.0%20(Macintosh%3B%20Intel%20Mac%20OS%20X%2010_15_7)%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F143.0.0.0%20Safari%2F537.36
&capi_version=4.4.0
&capi_mode=lightbox
&style_theme=default
&rnd=<随机浮点数>
```
响应（200）：
```json
{"token":"84018bd496b6d0ea4.2963690804|r=ap-southeast-1|meta=3|metabgclr=transparent|metaiconclr=%23757575|guitextcolor=%23000000|pk=EEA5F558-D6AC-4C03-B678-AABF639EE69A|at=40|sup=1|rid=84|ag=101|cdn_url=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc|surl=https%3A%2F%2Fa-cdn.claude.ai|smurl=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc%2Fassets%2Fstyle-manager","challenge_url":"","challenge_url_sri":null,"challenge_url_cdn":"https://a-cdn.claude.ai/cdn/fc/assets/ec-game-core/bootstrap/1.35.0/standard/game_core_bootstrap.js","challenge_url_cdn_sri":"sha384-z234bMEtrMZyRr8lCnWNYkQGJoFUKCSMh3FPSZuug+L975MXScGDSNunL7nIFqsj","noscript":"Disable","mbio":true,"tbio":true,"kbio":true,"pow":false}
```
→ `token` 是 `arkose_session_token`。`pow:false` = 无需 PoW。`at=40` = 分析类型/风险评分。

### 0.3 — POST Arkose metrics/ui（遥测 1）
```
POST https://a-cdn.claude.ai/metrics/ui
```
请求体：
```json
{"id":"d1c69afd-e4b8-40dd-9367-5d57444067d5","publicKey":"EEA5F558-D6AC-4C03-B678-AABF639EE69A","isKeyless":false,"capiVersion":"4.4.0","mode":"lightbox","suppressed":true,"device":{"platform":"MacIntel",...}}
```
→ `suppressed:true` = 本次 **Arkose 挑战被静默放行**（低风险，无图形验证码）。

### 0.4 — GET Arkose fc/a/ 游戏加载回调
```
GET https://a-cdn.claude.ai/fc/a/?callback=__jsonp_1782661754257&category=loaded&action=game%20loaded&session_token=84018bd496b6d0ea4.2963690804&data%5Bpublic_key%5D=EEA5F558-D6AC-4C03-B678-AABF639EE69A&data%5Bsite%5D=https%253A%252F%252Fclaude.ai
```
→ game loaded（游戏加载完毕回调，静默放行时也触发但无实际游戏）。

### 0.5 — POST Arkose metrics/ui（遥测 2）
```
POST https://a-cdn.claude.ai/metrics/ui
```
→ 同 0.3，第二次 metrics 上报。

### 0.6 — GET Arkose settings
```
GET https://a-cdn.claude.ai/v2/EEA5F558-D6AC-4C03-B678-AABF639EE69A/settings
```
→ 304（Not Modified，已在 0.2 拿到）。

### 0.7 — ★ POST send_magic_link（发信）
```
POST https://claude.ai/api/auth/send_magic_link
```
请求头：
```
content-type: application/json
anthropic-anonymous-id: claudeai.v1.5c4ba0c7-aa12-489b-9fc9-047a140cc8fc
anthropic-client-sha: cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957
anthropic-client-platform: web_claude_ai
anthropic-client-version: 1.0.0
anthropic-device-id: 5c15d49c-884b-4973-bee5-ab4b6de38574
Referer: https://claude.ai/login
x-activity-session-id: 23cfeb08-9779-42e2-b733-d29c551711cb
x-datadog-trace-id: 13456083532195625354
x-datadog-parent-id: 8693358186769430195
x-datadog-sampling-priority: 1
traceparent: 00-0000000000000000babd9caddc80f98a-78a5033623a036b3-01
tracestate: dd=s:1;o:rum
sec-ch-ua: "Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"
sec-ch-ua-mobile: ?0
sec-ch-ua-platform: "macOS"
```
请求体：
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
> ⚠️ body 无 `arkose_session_token` 字段。arkose 校验走 cookie（Arkose JS 跑完种 `.claude.ai` cookie）。

响应（200）：
```json
{
  "fallback_code_configuration": {
    "charset": "numeric",
    "length": 6,
    "show_input_after_delay": 5
  },
  "sent": true,
  "sso_url": null,
  "magic_link_intent_available": null,
  "sso_browser_requirement": null
}
```
→ `sent:true` = 邮件已发。`fallback_code_configuration` = 6 位数字备用验证码。

### 0.8 — POST a-api.anthropic.com/v1/b（埋点 beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 0.9 — POST Datadog RUM（遥测）
```
POST https://browser-intake-us5-datadoghq.com/api/v2/rum?ddsource=browser&dd-api-key=pub71869dceb5b70dba6123af9ca357d1f9&...
```
→ 202。非业务，忽略。

---

## 阶段 1：落地 + Arkose 挑战（`claudeaizhuce.har`，条目 1-148）

### 1.1 — GET 落地页
```
GET https://claude.ai/magic-link
```
邮件链接格式：`https://claude.ai/magic-link#<nonce>:<base64邮箱>`
- fragment 不发往服务端，前端 JS 解析。
- 例：`#93fa8b09ce1f0d1c4d48cdb636b2e60a:ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==`
  - `nonce` = `93fa8b09ce1f0d1c4d48cdb636b2e60a`（32 hex）
  - `encoded_email_address` = `ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==`（base64，解码 = `durgsfzpqjwbl@mail.com`）
响应（200）：HTML 页面。

### 1.2 — GET edge-api/bootstrap（应用配置）
```
GET https://claude.ai/edge-api/bootstrap?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
响应（200）：应用配置 JSON（含 `client_sha`、feature flags 等）。

### 1.3 — GET Arkose 公钥配置
```
GET https://a-cdn.claude.ai/v2/EEA5F558-D6AC-4C03-B678-AABF639EE69A/settings
```
→ 304（浏览器缓存命中）。
`EEA5F558-D6AC-4C03-B678-AABF639EE69A` = Claude 的 Arkose public_key（固定值）。

### 1.4 — ★ POST Arkose fc/gt2（拿 token，验证侧）
```
POST https://a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-D6AC-4C03-B678-AABF639EE69A
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: https://claude.ai
x-ark-arid: <Arkose blob>
x-ark-esync-value: 1782648000
```
请求体（form-urlencoded，同阶段 0.2）：
```
c=<Arkose blob>
&public_key=EEA5F558-D6AC-4C03-B678-AABF639EE69A
&site=https%3A%2F%2Fclaude.ai
&userbrowser=<UA>
&capi_version=4.4.0
&capi_mode=lightbox
&style_theme=default
&rnd=0.6790340637961746
```
响应（200）：
```json
{"token":"17318bd469825b4d4.7960819401|r=us-east-1|meta=3|metabgclr=transparent|metaiconclr=%23757575|guitextcolor=%23000000|pk=EEA5F558-D6AC-4C03-B678-AABF639EE69A|at=40|sup=1|rid=22|ag=101|cdn_url=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc|surl=https%3A%2F%2Fa-cdn.claude.ai|smurl=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc%2Fassets%2Fstyle-manager","challenge_url":"","challenge_url_sri":null,"challenge_url_cdn":"https://a-cdn.claude.ai/cdn/fc/assets/ec-game-core/bootstrap/1.35.0/standard/game_core_bootstrap.js","challenge_url_cdn_sri":"sha384-z234bMEtrMZyRr8lCnWNYkQGJoFUKCSMh3FPSZuug+L975MXScGDSNunL7nIFqsj","noscript":"Disable","mbio":true,"tbio":true,"kbio":true,"pow":false,"compatibility_mode_enabled":true}
```
→ `token` = `17318bd469825b4d4.7960819401|r=us-east-1|...`，**这个就是 verify 要放进 body 的 `arkose_session_token`**。

### 1.5 — GET Arkose 游戏加载回调
```
GET https://a-cdn.claude.ai/fc/a/?callback=__jsonp_1782658648666&category=loaded&action=game%20loaded&session_token=17318bd469825b4d4.7960819401&data%5Bpublic_key%5D=EEA5F558-D6AC-4C03-B678-AABF639EE69A&data%5Bsite%5D=https%253A%252F%252Fclaude.ai
```
→ 200（game loaded）。

### 1.6 — GET Arkose settings（再次）
```
GET https://a-cdn.claude.ai/v2/EEA5F558-D6AC-4C03-B678-AABF639EE69A/settings
```
→ 304。

---

## 阶段 2：验证 magic link（账号创建点）

### 2.1 — ★ POST verify_magic_link
```
POST https://claude.ai/api/auth/verify_magic_link
```
请求头：
```
content-type: application/json
Referer: https://claude.ai/magic-link
Origin: https://claude.ai
anthropic-anonymous-id: claudeai.v1.48499cd7-1f34-4bc8-b815-730c4427e104
anthropic-client-platform: web_claude_ai
anthropic-client-sha: cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957
anthropic-client-version: 1.0.0
anthropic-device-id: b913f4ab-8c12-4571-94c1-63db7ab89adf
x-activity-session-id: 62edbdd2-2461-46dd-a8bf-ac3340fa393f
x-datadog-trace-id: 7733946121298948418
x-datadog-parent-id: 8321007121171250388
x-datadog-sampling-priority: 1
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
  "arkose_session_token": "17318bd469825b4d4.7960819401|r=us-east-1|meta=3|metabgclr=transparent|metaiconclr=%23757575|guitextcolor=%23000000|pk=EEA5F558-D6AC-4C03-B678-AABF639EE69A|at=40|sup=1|rid=22|ag=101|cdn_url=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc|surl=https%3A%2F%2Fa-cdn.claude.ai|smurl=https%3A%2F%2Fa-cdn.claude.ai%2Fcdn%2Ffc%2Fassets%2Fstyle-manager",
  "source": "claude"
}
```
→ ⚠️ `arkose_session_token` **放在 body**（和阶段 0 发信走 cookie 不同）。

响应（200）：
```json
{
  "success": true,
  "secret": null,
  "account": {
    "tagged_id": "user_01MoUbneUFrgE9kscaPQQUkH",
    "uuid": "a873fede-b950-42a3-9a1f-53a7d4b57422",
    "email_address": "durgsfzpqjwbl@mail.com",
    "full_name": null,
    "display_name": null,
    "memberships": [
      {
        "organization": {
          "id": 305359658,
          "uuid": "6e395e8f-cc30-4cf7-9904-1c0cb240d031",
          "name": "durgsfzpqjwbl@mail.com's Organization",
          "settings": { ... 大量设置项 ... },
          "capabilities": ["chat"],
          "rate_limit_tier": "default_claude_ai",
          "created_at": "2026-06-28T14:57:29.706293Z"
        },
        "role": "admin",
        "seat_tier": null
      }
    ],
    "created_at": "2026-06-28T14:57:29.557449Z",
    "is_verified": true
  },
  "created": true,
  "session_expires_at": null
}
```
→ **`created:true` = 账号在此刻创建**。`account.uuid` 和 `memberships[0].organization.uuid` 是 onboarding 要用的。成功返回后服务端 Set-Cookie 下发登录态。

### 2.2 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

---

## 阶段 2→3 过渡（页面初始加载）

认证后页面重新 bootstrap，拉了大量只读配置。以下全列不省略：

### 2.3 — GET edge-api/bootstrap（再次，认证后）
```
GET https://claude.ai/edge-api/bootstrap?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。这次带 cookie，返回用户相关配置。

### 2.4 — GET cowork_settings
```
GET https://claude.ai/api/organizations/6e395e8f-cc30-4cf7-9904-1c0cb240d031/cowork_settings
```
→ 200。

### 2.5 — GET chat_conversations_v2（starred=false）
```
GET https://claude.ai/api/organizations/6e395e8f-cc30-4cf7-9904-1c0cb240d031/chat_conversations_v2?limit=30&starred=false&consistency=eventual
```
→ 200。新账号返回空列表。

### 2.6 — GET privacy-consents（cookies）
```
GET https://claude.ai/v1/privacy-consents?prefix=cookies.
```
→ **401**（未设置，新账号第一次访问）。

### 2.7 — GET bootstrap/current_user_access
```
GET https://claude.ai/api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/current_user_access
```
→ 200。

### 2.8 — GET edge-api/bootstrap/app_start
```
GET https://claude.ai/edge-api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/app_start?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。

### 2.9 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 2.10 — GET referral
```
GET https://claude.ai/api/referral
```
→ 200。

### 2.11 — GET account/raven_eligible
```
GET https://claude.ai/api/account/raven_eligible
```
→ 200。

### 2.12 — GET privacy-consents（cookies，再次）
```
GET https://claude.ai/v1/privacy-consents?prefix=cookies.
```
→ 200（2.6 返回了 401，这次是认证后重试）。

---

## 阶段 3：Onboarding（按 HAR 时序，逐条不省）

### 3.1 — PATCH account/settings（标记 onboarding 开始）
```
PATCH https://claude.ai/api/account/settings
```
请求体：
```json
{"has_started_claudeai_onboarding": true, "has_finished_claudeai_onboarding": false}
```
→ 202

### 3.2a — PUT privacy-consents（cookies.analytics）
```
PUT https://claude.ai/v1/privacy-consents
```
请求体：
```json
{"consent_type":"cookies.analytics","consent_decision":"CONSENT_DECISION_OPT_IN","source":"implicit_regional_default"}
```
→ 200

### 3.2b — PUT privacy-consents（cookies.marketing）
```
PUT https://claude.ai/v1/privacy-consents
```
请求体：
```json
{"consent_type":"cookies.marketing","consent_decision":"CONSENT_DECISION_OPT_IN","source":"implicit_regional_default"}
```
→ 200

### 3.3 — PUT accept_legal_docs
```
PUT https://claude.ai/api/account/accept_legal_docs
```
请求体：
```json
{
  "acceptances": [
    {"document_id": "v3:aup:22742366-2ef0-4c7a-a833-6523f10d3944", "accepted_via_checkbox": true},
    {"document_id": "v3:consumer-terms:79dbc8c6-7f64-43d6-8101-207cede59a4d", "accepted_via_checkbox": true},
    {"document_id": "v3:privacy:cf9b9ac4-d387-48b8-8560-ce1c58b8a34b", "accepted_via_checkbox": false}
  ]
}
```
→ 202。document_id 版本化，随法务更新变。

### 3.4 — PUT email_consent
```
PUT https://claude.ai/api/account/email_consent
```
请求体：
```json
{"consent": true, "accepted_via_checkbox": false, "variant": "notices"}
```
→ 202

### 3.5 — PUT account（年龄验证）
```
PUT https://claude.ai/api/account?statsig_hashing_algorithm=djb2
```
请求体：
```json
{"age_is_verified": true}
```
→ 202

### 3.6 — GET edge-api/bootstrap/app_start（刷新配置）
```
GET https://claude.ai/edge-api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/app_start?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。

### 3.7 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 3.8 — PUT account（设名字）
```
PUT https://claude.ai/api/account?statsig_hashing_algorithm=djb2
```
请求体：
```json
{"display_name": "xiaoshua", "full_name": "xiaoshua"}
```
→ 202

### 3.9 — POST billing/consumer_pricing
```
POST https://claude.ai/api/billing/6e395e8f-cc30-4cf7-9904-1c0cb240d031/consumer_pricing
```
→ 200。

### 3.10 — POST billing/consumer_pricing（第二次）
```
POST https://claude.ai/api/billing/6e395e8f-cc30-4cf7-9904-1c0cb240d031/consumer_pricing
```
→ 200。前端两次调用（可能是并发）。

### 3.11 — POST billing/individual_plan_pricing/v2
```
POST https://claude.ai/api/billing/6e395e8f-cc30-4cf7-9904-1c0cb240d031/individual_plan_pricing/v2
```
→ 200。

### 3.12 — GET edge-api/bootstrap/app_start
```
GET https://claude.ai/edge-api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/app_start?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。

### 3.13 — GET paused_subscription_details
```
GET https://claude.ai/api/organizations/6e395e8f-cc30-4cf7-9904-1c0cb240d031/paused_subscription_details
```
→ 200。

### 3.14 — GET team-signup/voucher-eligible
```
GET https://claude.ai/api/team-signup/voucher-eligible
```
→ 200。

### 3.15 — GET team-trial/exposure-eligible
```
GET https://claude.ai/api/team-trial/exposure-eligible
```
→ 200。

### 3.16 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 3.17 — GET edge-api/bootstrap/app_start
```
GET https://claude.ai/edge-api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/app_start?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。

### 3.18 — POST a-api.anthropic.com/v1/m（埋点）
```
POST https://a-api.anthropic.com/v1/m
```
→ 200。

### 3.19 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 3.20 — PATCH account/settings（grove）
```
PATCH https://claude.ai/api/account/settings
```
请求体：
```json
{"grove_enabled": true}
```
→ 202。可选步骤，UI 触发。

### 3.21 — PUT account（再次确认账户）
```
PUT https://claude.ai/api/account?statsig_hashing_algorithm=djb2
```
请求体：
```json
{"display_name": "xiaoshua", "full_name": "xiaoshua"}
```
→ 202。前端发了两次 PUT account。

### 3.22 — GET edge-api/bootstrap/app_start
```
GET https://claude.ai/edge-api/bootstrap/6e395e8f-cc30-4cf7-9904-1c0cb240d031/app_start?statsig_hashing_algorithm=djb2&growthbook_format=sdk&include_system_prompts=false
```
→ 200。

### 3.23 — POST a-api.anthropic.com/v1/b（beacon）
```
POST https://a-api.anthropic.com/v1/b
```
→ 200。

### 3.24 — POST chat_conversations（建首个会话）
```
POST https://claude.ai/api/organizations/6e395e8f-cc30-4cf7-9904-1c0cb240d031/chat_conversations
```
请求体：
```json
{"uuid": "acd0c183-e0b7-44ad-a465-d2f4be8118d2", "name": "Your first chat with Claude"}
```
→ 201（Created）。

### 3.25 — PUT account_profile
```
PUT https://claude.ai/api/account_profile
```
请求体：
```json
{"work_function": "Other"}
```
→ 200。

### 3.26 — PATCH account/settings（最后标记）
```
PATCH https://claude.ai/api/account/settings
```
→ 202。最后一次 settings 更新（onboarding 收尾）。

---

## 阶段 3 后续（应用里拉配置的只读请求）

以下全是登录态后的页面初始化拉取，HAR 共有但**对注册流程非必需**：

### GET 只读列表（全部 200）
```
GET /api/bootstrap/.../current_user_access
GET /api/organizations/.../cowork_settings
GET /api/organizations/.../chat_conversations_v2?limit=30&starred=false&consistency=eventual
GET /edge-api/bootstrap/.../app_start?...
GET /api/referral
GET /api/account/raven_eligible
GET /api/organizations/.../sync/settings
GET /api/organizations/.../projects?include_harmony_projects=true&limit=30&starred=true
GET /api/organizations/.../chat_conversations_v2?limit=30&starred=true&consistency=eventual
GET /api/account_profile
GET /api/accounts/.../invites
GET /api/organizations/discoverable
GET /api/billing/.../gift/purchase_eligibility
GET /api/organizations/.../overage_spend_limit
GET /api/organizations/.../chat_conversations/acd0c183-...?tree=True&rendering_mode=messages&render_all_tools=true&consistency=strong
GET /api/organizations/...  (org info)
GET /api/organizations/.../notification/preferences
GET /api/organizations/.../projects?include_harmony_projects=true&limit=200&creator_filter=is_creator
GET /api/organizations/.../projects?include_harmony_projects=true&limit=200&creator_filter=is_not_creator
GET /api/organizations/.../experiences/claude_web?locale=en-US
GET /api/organizations/.../memory/settings
GET /api/organizations/.../memory
GET /api/claude_code/organizations/.../user_settings  → 404（新账号无）
GET /api/organizations/.../marketplaces/list-default-marketplaces
GET /api/organizations/.../pending_domain_claim
GET /api/team-trial/exposure-eligible
GET /api/organizations/.../trial_status
GET /api/organizations/.../paused_subscription_details
GET /api/organizations/.../plugins/list-plugins?enabled_only=true
GET /api/organizations/.../projects_v2?limit=1&offset=0
GET /api/organizations/.../mcp/remote_servers
GET /api/organizations/.../skills/list-skills
GET /api/organizations/.../prosumer_activation/tasks
GET /api/organizations/.../mcp/v2/bootstrap
```
→ 以上全 200（除 `claude_code/user_settings` = 404）。页面渲染需要，但注册完成核心到 3.25 就结束。

### 埋点 beacon（全部 200，非业务）
```
POST a-api.anthropic.com/v1/b  ×5
POST a-api.anthropic.com/v1/m  ×1
```

### 外部服务（与注册无关）
```
GET api.anthropic.com/directory/servers?limit=500&visibility=commercial,gsuite,gsuite-google&verified_tier=anthropic,partner,community
OPTIONS api.anthropic.com/directory/servers?...
GET api.anthropic.com/directory/servers?...&cursor=...
OPTIONS api.anthropic.com/directory/servers?...&cursor=...
```
→ MCP 服务目录拉取。

---

## 注册必需请求清单（最小集）


| # | Method | URL | Body | 阶段 |
|---|--------|-----|------|------|
| 1 | GET | `/api/auth/login_methods?email=...&source=claude-ai` | — | 0（预检） |
| 2 | POST | `a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-...` | form: `c=` blob + `public_key` + `site` + … | 0（Arkose） |
| 3 | POST | `/api/auth/send_magic_link` | `{utc_offset,email_address,login_intent:null,locale,return_to:null,source:"claude"}` | 0（发信） |
| 4 | POST | `a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-...` | 同上 form | 1（Arkose） |
| 5 | POST | `/api/auth/verify_magic_link` | `{credentials:{method:"nonce",nonce,encoded_email_address},locale,arkose_session_token,source:"claude"}` | 2（创建账号） |
| 6 | PATCH | `/api/account/settings` | `{has_started_claudeai_onboarding:true,has_finished_claudeai_onboarding:false}` | 3.1 |
| 7 | PUT | `/v1/privacy-consents` | `{consent_type:"cookies.analytics",consent_decision:"CONSENT_DECISION_OPT_IN",source:"implicit_regional_default"}` | 3.2a |
| 8 | PUT | `/v1/privacy-consents` | `{consent_type:"cookies.marketing",...}` | 3.2b |
| 9 | PUT | `/api/account/accept_legal_docs` | `{acceptances:[{document_id:"v3:aup:..."},...]}` | 3.3 |
| 10 | PUT | `/api/account/email_consent` | `{consent:true,accepted_via_checkbox:false,variant:"notices"}` | 3.4 |
| 11 | PUT | `/api/account?statsig_...=djb2` | `{age_is_verified:true}` | 3.5 |
| 12 | PUT | `/api/account?statsig_...=djb2` | `{display_name:"...",full_name:"..."}` | 3.7 |
| 13 | POST | `/api/organizations/<org>/chat_conversations` | `{uuid:"<uuid4>",name:"Your first chat with Claude"}` | 3.8 |
| 14 | PUT | `/api/account_profile` | `{work_function:"Other"}` | 3.9 |

**共 14 个业务请求**（Arkose ×2 不算在内），从发信到账号 ready。

---

## 关键变量速查

| 变量 | 来源 | 示例 | 用在哪 |
|------|------|------|--------|
| `nonce` | 邮件链接 `#` 后第一段 | `93fa8b09ce1f0d1c4d48cdb636b2e60a` | verify_magic_link body |
| `encoded_email_address` | 邮件链接 `#` 后第二段 | `ZHVyZ3NmenBxandibEBtYWlsLmNvbQ==` | verify_magic_link body |
| `arkose_session_token` | `fc/gt2` 响应 `token` | `17318bd469825b4d4.7960819401\|r=us-east-1\|...` | 发信(cookie) / verify(body) |
| `org_uuid` | verify 响应 `memberships[0].organization.uuid` | `6e395e8f-cc30-4cf7-9904-1c0cb240d031` | 3.8 建会话 |
| `account_uuid` | verify 响应 `account.uuid` | `a873fede-b950-42a3-9a1f-53a7d4b57422` | 后续 API 路由 |
| `anthropic-client-sha` | bootstrap / 页面（随发版变） | `cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957` | 所有 claude.ai 请求头 |
| legal `document_id` | 前端 JS bundle（版本化） | `v3:aup:22742366-...` | 3.3 accept_legal_docs |
