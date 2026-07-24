# 对外 API 文档

本文档覆盖 CCMax 使用 `X-API-Key` 鉴权的全部对外 API。

## 基本信息

- Base URL：`https://aicdk.shop`
- 请求与响应格式：`application/json`
- 鉴权请求头：`X-API-Key: ccm_xxx`
- API Key 在管理后台“API Key”页面创建。

除特别说明外，成功响应使用以下结构：

```json
{
  "data": {}
}
```

失败响应使用以下结构：

```json
{
  "code": "ERROR_CODE",
  "message": "error detail"
}
```

## 接口清单

| 业务 | 方法 | 路径 |
| --- | --- | --- |
| 添加 Claude Free 账号 | `POST` | `/api/claude_account/add` |
| 下发 Claude Free 账号 | `POST` | `/api/claude_account` |
| 释放 Claude Free 账号 | `POST` | `/api/claude_account/release` |
| 同步 Claude 升级结果 | `POST` | `/api/claude_account/upgrade` |
| 下发 Google 账号 | `POST` | `/api/google_account` |
| 上报 Google 账号结果 | `POST` | `/api/google_account/report` |
| 下发邮箱账号 | `POST` | `/api/mail_account` |
| 上报邮箱账号已使用 | `POST` | `/api/mail_account/report` |
| 下发 Card | `POST` | `/api/card` |
| 上报 Card 使用结果 | `POST` | `/api/card/report` |
| 上传验证码渠道凭证 | `POST` | `/api/card/verify-code/token` |
| 查询 Card 验证码 | `POST` | `/api/card/verify-code` |
| 查询 ChatGPT CDK | `POST` | `/api/chatgpt/cdk/check` |
| 提交 ChatGPT CDK 兑换 | `POST` | `/api/chatgpt/cdk/redeem` |
| 查询 ChatGPT CDK 兑换任务 | `GET` | `/api/chatgpt/cdk/tasks/{taskId}` |

## 通用请求头

| 请求头 | 必填 | 说明 |
| --- | --- | --- |
| `X-API-Key` | 是 | 管理后台创建的 API Key。 |
| `Content-Type: application/json` | POST 请求是 | 请求体为 JSON。 |
| `Idempotency-Key` | 下发接口建议 | 同一业务请求重试时保持不变。 |
| `Fingerprint` | Card 验证码接口否 | 存在时传给 Qbit。 |

## 通用状态码

| HTTP 状态 | 说明 |
| --- | --- |
| `200` | 请求成功。 |
| `400` | 参数或业务操作不符合要求。 |
| `401` | API Key 缺失、禁用或无效。 |
| `404` | 资源不存在，或当前 API Key 无权访问该资源。 |
| `409` | 库存不足、幂等冲突、租约冲突或资源状态冲突。 |
| `502` | 上游服务请求失败或响应无效。 |
| `503` | 依赖服务未配置。 |

# Claude 账号

## 添加 Claude Free 账号

```http
POST https://aicdk.shop/api/claude_account/add
X-API-Key: ccm_xxx
Content-Type: application/json
```

支持单个账号或最多 100 个账号的批量请求。账号固定保存为 `free` 计划。

批量请求：

```json
{
  "accounts": [
    {
      "mail": "user@example.com",
      "password": "password",
      "sessionKey": "session-key"
    }
  ]
}
```

单个请求：

```json
{
  "mail": "user@example.com",
  "password": "password",
  "sessionKey": "session-key"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `accounts` | array | 否 | 批量账号；与顶层单账号字段二选一。 |
| `mail` | string | 是 | Claude 邮箱。 |
| `password` | string | 是 | Claude 密码。 |
| `sessionKey` | string | 是 | Claude Session Key。 |

成功响应：

```json
{
  "data": {
    "created": 1,
    "duplicates": 0,
    "errors": [],
    "ids": [1]
  }
}
```

重复邮箱或 Session Key 计入 `duplicates`。

## 下发 Claude Free 账号

```http
POST https://aicdk.shop/api/claude_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-claude-request-id
Content-Type: application/json
```

```json
{
  "count": 1,
  "plan": "free"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `count` | integer | 否 | 默认 `1`，范围 `1～100`。 |
| `plan` | string | 否 | 固定为 `free`，可省略。 |

成功响应：

```json
{
  "data": {
    "requestId": "unique-claude-request-id",
    "leaseExpiresAt": "2026-07-24T12:30:00+08:00",
    "count": 1,
    "accounts": [
      {
        "mail": "user@example.com",
        "password": "password",
        "sessionKey": "session-key",
        "plan": "free"
      }
    ]
  }
}
```

下发后账号进入临时租约。相同 `Idempotency-Key` 在有效租约内返回相同账号；租约释放、过期或账号被重新分配后，旧幂等键返回 `409 LEASE_CONFLICT`。

库存不足时返回 `409 INSUFFICIENT_ACCOUNTS`。

## 释放处理失败的 Claude 账号

```http
POST https://aicdk.shop/api/claude_account/release
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "requestId": "unique-claude-request-id",
  "mails": ["user@example.com"]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requestId` | string | 是 | 下发响应中的 `requestId`。 |
| `mails` | string[] | 是 | 本次请求中需要释放的邮箱，可批量提交。 |

成功响应：

```json
{
  "data": {
    "released": 1,
    "errors": []
  }
}
```

该接口用于处理失败时立即归还库存。不调用时，账号会在租约到期后的下一次库存操作中自动释放。

## 同步 Claude 账号升级结果

```http
POST https://aicdk.shop/api/claude_account/upgrade
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "mail": "user@example.com",
  "plan": "max_20x",
  "cardPoolId": 12
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mail` | string | 是 | 已存在的 Claude 邮箱。 |
| `plan` | string | 是 | 必须为 `max_20x`。 |
| `cardPoolId` | integer | 是 | 本次升级使用的 Card Pool ID。 |
| `upgradedAt` | RFC3339 string | 否 | 升级时间；省略时使用服务器当前时间。 |

成功响应：

```json
{
  "data": {
    "id": 1,
    "mail": "user@example.com",
    "plan": "max_20x",
    "cardPoolId": 12,
    "deliveryStatus": "upgraded",
    "upgradedAt": "2026-07-24T12:00:00+08:00"
  }
}
```

上报成功后 Card 使用次数增加一次并冷却 5 小时。相同账号与 Card 重复上报不会重复计数，但会重新计算冷却时间。

# Google 账号

## 下发 Google 账号

```http
POST https://aicdk.shop/api/google_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-google-request-id
Content-Type: application/json
```

```json
{
  "requestId": "unique-google-request-id"
}
```

`requestId` 可省略。省略时使用 `Idempotency-Key`；两者均未提供时由服务端生成。

成功响应：

```json
{
  "data": {
    "requestId": "unique-google-request-id",
    "leaseExpiresAt": "2026-07-24T12:03:00+08:00",
    "account": {
      "googleAccountId": 21,
      "mail": "google@example.com",
      "password": "password"
    }
  }
}
```

Google 账号下发后默认锁定 3 分钟，期间不会分配给其他请求。未上报时租约到期自动释放；上报成功时立即结束租约。租约时长可通过 `GOOGLE_ACCOUNT_DISPATCH_LEASE_MINUTES` 配置。

库存不足时返回 `409 INSUFFICIENT_GOOGLE_ACCOUNTS`。

## 上报 Google 账号结果

```http
POST https://aicdk.shop/api/google_account/report
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "requestId": "unique-google-request-id",
  "googleAccountId": 21,
  "status": "used"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requestId` | string | 是 | Google 账号下发响应中的请求 ID。 |
| `googleAccountId` | integer | 是 | 下发响应中的 Google 账号 ID。 |
| `status` | string | 是 | `used`、`discarded` 或 `login_failed`。 |

成功响应：

```json
{
  "data": {
    "googleAccountId": 21,
    "status": "used",
    "reportedAt": "2026-07-24T12:05:00+08:00"
  }
}
```

任一结果上报成功后，该账号不再下发。相同结果重复上报为幂等成功；提交不同结果会返回冲突错误。

首次上报必须在 3 分钟租约内完成。租约过期或账号已重新分配时返回租约冲突错误。

# 邮箱账号

## 下发邮箱账号

```http
POST https://aicdk.shop/api/mail_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-mail-request-id
Content-Type: application/json
```

```json
{
  "requestId": "unique-mail-request-id",
  "platform": "mailcom"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requestId` | string | 否 | 省略时使用 `Idempotency-Key` 或由服务端生成。 |
| `platform` | string | 否 | 邮箱平台，例如 `mailcom`；省略时从全部平台选择。 |

成功响应：

```json
{
  "data": {
    "requestId": "unique-mail-request-id",
    "leaseExpiresAt": "2026-07-24T12:30:00+08:00",
    "account": {
      "mailAccountId": 21,
      "mail": "user@mail.com",
      "password": "password",
      "platform": "mailcom"
    }
  }
}
```

库存不足时返回 `409 INSUFFICIENT_MAIL_ACCOUNTS`。

## 上报邮箱账号已使用

```http
POST https://aicdk.shop/api/mail_account/report
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "requestId": "unique-mail-request-id",
  "mailAccountId": 21,
  "claudeAccountMail": "claude@example.com"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requestId` | string | 是 | 邮箱账号下发响应中的请求 ID。 |
| `mailAccountId` | integer | 是 | 下发响应中的邮箱账号 ID。 |
| `claudeAccountMail` | string | 是 | 需要关联的已入库 Claude 邮箱。 |

成功响应：

```json
{
  "data": {
    "mailAccountId": 21,
    "status": "used",
    "claudeAccountId": 10,
    "claudeAccountMail": "claude@example.com",
    "usedAt": "2026-07-24T12:05:00+08:00"
  }
}
```

上报成功后邮箱账号永久变为 `used`。相同关联重复上报为幂等成功。

# Card

## 下发 Card

```http
POST https://aicdk.shop/api/card
X-API-Key: ccm_xxx
Idempotency-Key: unique-card-request-id
Content-Type: application/json
```

```json
{
  "count": 1,
  "source": "slash"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `count` | integer | 否 | 默认 `1`，范围 `1～100`。 |
| `source` | string | 否 | 指定卡片来源；省略时从全部来源选择。 |

成功响应：

```json
{
  "data": {
    "requestId": "unique-card-request-id",
    "leaseExpiresAt": "2026-07-24T12:03:00+08:00",
    "count": 1,
    "cards": [
      {
        "cardPoolId": 12,
        "source": "slash",
        "cardId": "c_example",
        "cardNo": "4111111111111111",
        "expireMmyy": "1228",
        "ccv": "123"
      }
    ]
  }
}
```

系统按使用次数、最近下发时间和本地 ID 选择已启用、未锁定且不在冷却期的卡。

Card 下发后默认锁定 3 分钟，期间不会分配给其他请求。未上报时租约到期自动释放；上报 `used` 或 `unavailable` 时立即结束租约。租约时长可通过 `CARD_DISPATCH_LEASE_MINUTES` 配置。

### 单张库存不足时自动创建

满足以下条件时，系统尝试通过 Slash 创建一张虚拟卡：

1. `count` 为 `1`。
2. 没有符合来源要求、已启用且不在冷却期的卡。
3. `source` 未指定，或者为 `slash`/`slash_*`。
4. 对应 Slash 渠道已经配置并启用 API Key。

自动创建默认使用 Card Group ID `card_group_3febhaydgdiq9`。创建后，系统读取 Slash Card API 与 Vault 中的有效期、PAN、CVV，写入 Card Pool 并按原请求返回。

未指定 `source` 时，系统优先选择名为 `slash` 的渠道；不存在时选择按名称排序的第一个 `slash_*` 渠道。明确指定非 Slash 来源时不会改用 Slash。

创建、详情读取、Vault 读取或本地导入失败时返回 HTTP `409`：

```json
{
  "code": "INSUFFICIENT_CARDS",
  "message": "insufficient cards: available=0 requested=1"
}
```

批量请求不会自动创建 Card。

## 上报 Card 使用结果

```http
POST https://aicdk.shop/api/card/report
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "requestId": "unique-card-request-id",
  "cards": [
    {
      "cardPoolId": 12,
      "status": "used",
      "reason": ""
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `requestId` | string | 是 | Card 下发响应中的请求 ID。 |
| `cards` | array | 是 | 需要上报的 Card 列表。 |
| `cards[].cardPoolId` | integer | 是 | Card Pool ID。 |
| `cards[].status` | string | 是 | `used` 或 `unavailable`。 |
| `cards[].reason` | string | 否 | 审计说明。 |

状态处理：

| 状态 | 结果 |
| --- | --- |
| `used` | 立即解除租约，使用次数增加一次，并进入 5 小时冷却期。 |
| `unavailable` | 立即解除租约，Card 标记为不可用，不再参与下发。 |

成功响应：

```json
{
  "data": {
    "reported": 1,
    "errors": []
  }
}
```

相同结果重复上报不会重复计数；同一次下发提交不同结果会返回冲突错误。

首次上报必须在 3 分钟租约内完成。租约过期或 Card 已重新分配时返回租约冲突错误。

## 上传验证码渠道凭证

```http
POST https://aicdk.shop/api/card/verify-code/token
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "source": "slash",
  "token": "slash-api-key"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | string | 是 | `qbit`、`qbit_*`、`slash` 或 `slash_*`。 |
| `token` | string | 是 | Qbit Bearer Token 或 Slash API Key。 |

Token 会覆盖对应来源的现有凭证，且不会在响应中回显。

成功响应：

```json
{
  "data": {
    "source": "slash",
    "updated": true
  }
}
```

## 查询 Card 验证码

```http
POST https://aicdk.shop/api/card/verify-code
X-API-Key: ccm_xxx
Fingerprint: optional
Content-Type: application/json
```

```json
{
  "cardPoolId": 12,
  "googleRef": "BMR"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `cardPoolId` | integer | 是 | Card 下发响应中的 Card Pool ID。 |
| `googleRef` | string | 是 | Google 交易引用，例如 `BMR`。 |

当前 API Key 必须曾经下发过该 Card。系统根据来源查询 Qbit 交易记录或 Slash 验证事件，并提取匹配的六位验证码。

已找到验证码：

```json
{
  "data": {
    "status": "ok",
    "code": "123456"
  }
}
```

尚未找到验证码：

```json
{
  "data": {
    "status": "pending"
  }
}
```

# ChatGPT CDK

## 查询 CDK 是否可用

```http
POST https://aicdk.shop/api/chatgpt/cdk/check
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "code": "550e8400-e29b-41d4-a716-446655440000"
}
```

成功响应：

```json
{
  "data": {
    "code": "550e8400-e29b-41d4-a716-446655440000",
    "sku": "pro",
    "available": true,
    "used": false,
    "status": "available"
  }
}
```

CDK 不存在时返回 `404 CDK_NOT_FOUND`。

## 提交 CDK 兑换任务

```http
POST https://aicdk.shop/api/chatgpt/cdk/redeem
X-API-Key: ccm_xxx
Content-Type: application/json
```

```json
{
  "code": "550e8400-e29b-41d4-a716-446655440000",
  "channel": "official",
  "session": "{\"accessToken\":\"example_token\",\"userId\":\"123456\"}"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | 是 | 可用 CDK。 |
| `channel` | string | 是 | 升级渠道，例如 `official`。 |
| `session` | string | 是 | JSON 序列化后的 Session 字符串，不能直接传对象。 |

成功响应：

```json
{
  "data": {
    "taskId": "ctk_2gR8vL7kYpQ4mN6xT1cD9aB3sE0",
    "status": "pending",
    "createdAt": "2026-07-24T11:30:00.000Z"
  }
}
```

`taskId` 是本地生成的不可预测标识，不是数据库自增 ID 或上游任务 ID。三方任务创建失败时会释放 CDK。

常见错误：

| HTTP 状态 | 错误码 | 说明 |
| --- | --- | --- |
| `400` | `INVALID_SESSION` | `session` 不是有效 JSON 字符串，或无法解析所需用户信息。 |
| `404` | `CDK_NOT_FOUND` | CDK 不存在。 |
| `409` | `CDK_NOT_AVAILABLE` | CDK 已被使用或占用。 |
| `502` | `REDEEM_UPSTREAM_ERROR` | 三方升级服务失败。 |
| `503` | `REDEEM_NOT_CONFIGURED` | 服务端未配置三方升级服务。 |

## 查询 CDK 兑换任务

```http
GET https://aicdk.shop/api/chatgpt/cdk/tasks/ctk_2gR8vL7kYpQ4mN6xT1cD9aB3sE0
X-API-Key: ccm_xxx
```

路径参数：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `taskId` | string | 是 | 提交兑换任务接口返回的本地任务 ID。 |

只能使用创建任务时的同一个 API Key 查询。响应数据来自上游任务结果，并将 `taskId` 替换为本地任务 ID：

```json
{
  "data": {
    "taskId": "ctk_2gR8vL7kYpQ4mN6xT1cD9aB3sE0",
    "status": "success"
  }
}
```

任务状态通常为 `pending`、`processing`、`success` 或 `failed`。建议每 2～5 秒查询一次，达到 `success` 或 `failed` 后停止。

常见错误：

| HTTP 状态 | 错误码 | 说明 |
| --- | --- | --- |
| `404` | `TASK_NOT_FOUND` | 任务不存在，或不属于当前 API Key。 |
| `409` | `TASK_NOT_READY` | 本地任务尚未获得上游任务 ID。 |
| `502` | `REDEEM_UPSTREAM_ERROR` | 查询三方任务失败。 |
