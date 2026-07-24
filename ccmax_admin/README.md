# CCMax

Claude 账号、Card Pool 与订单交付管理系统。后端使用 Go + Gin + SQLite，管理端使用 Vue 3 + TypeScript + Element Plus。

## 功能

- 多管理员登录及 `super_admin` / `admin` 权限
- Claude 账号录入、批量导入、计划与状态管理
- 带租约锁、失败释放和幂等键的 Free 账号批量下发 API
- Claude 账号升级成功同步 API
- Claude 账号单个/批量 SessionKey 探活
- Card Pool 管理、批量导入、通过 Slash API 创建虚拟卡并自动入库，以及 Qbit / Slash 验证码查询
- 支持幂等、使用次数均衡、单张库存不足自动创建 Slash 卡与结果上报的 Card 下发 API
- 订单可选择 Claude Account 或 ChatGPT CDK，并按计划/SKU 原子分配库存、TXT 下载及记录下载次数
- API Key 创建、禁用和调用鉴权
- 内置 API 文档、cURL 示例及在线调试
- SQLite WAL、外键、自动迁移与审计记录
- ChatGPT CDK 的 UUID 批量生成、CSV 导出、可用性查询及三方升级任务代理

## 本地启动

```bash
pnpm --dir web install
pnpm --dir web build
export BOOTSTRAP_ADMIN_USERNAME=admin
export BOOTSTRAP_ADMIN_PASSWORD='a-strong-password'
export DATABASE_PATH=./data/ccmax.db
go run ./cmd --app-env dev --bind :4001
```

打开后台入口 <http://127.0.0.1:4001/admin>。首次启动必须设置初始化管理员密码，系统不会内置默认密码。独立的 ChatGPT CDK 兑换页仍位于 <http://127.0.0.1:4001/redeem>。

前端热更新开发：

```bash
pnpm --dir web dev
```

Vite 会将 `/api` 代理到 `127.0.0.1:4001`。

Docker 启动：

```bash
cp .env.example .env
# 编辑 .env，设置安全的 BOOTSTRAP_ADMIN_PASSWORD
docker compose up --build
```

Compose 会自动加载同目录的 `.env` 文件；也可以继续通过命令行环境变量覆盖其中的配置。SQLite 数据保存在宿主机的 `./data` 目录。

默认将后台绑定到宿主机 `127.0.0.1:8080`。可在 `.env` 中通过
`CCMAX_BIND_ADDRESS` 修改监听地址，通过 `CCMAX_PORT` 修改对外端口；容器内部端口
始终为 `8080`。需要直接对外提供服务时才将监听地址改为 `0.0.0.0`，线上建议继续
监听 `127.0.0.1` 并通过反向代理访问。

账号探活默认由服务端直连 Claude。需要代理时设置 `CLAUDE_CHECK_PROXY`，支持 `http`、`https`、`socks5` 和 `socks5h`，例如 `socks5h://user:password@host:port`。代理凭据仅保存在服务端环境变量中。

## 对外 API

在后台创建 API Key 后使用 `X-API-Key` 请求。

生产环境 Base URL：`https://aicdk.shop`。

全部对外 API 的鉴权、请求字段、响应格式和错误说明见 [`docs/API.md`](docs/API.md)。

### ChatGPT CDK 兑换

先在后台“ChatGPT CDK”页面按 `plus`、`pro` 或 `prolite` 套餐批量生成 UUID CDK。系统根据当前时间自动生成 CDK 订单号，可填写备注，并按当前筛选结果导出 CSV。

创建销售订单时可选择商品类型：`Claude Account` 按 `free / max_20x` 分配账号，`ChatGPT CDK` 按 `plus / pro / prolite` 分配尚未关联订单的 CDK。CDK 订单下载的 TXT 每行一个兑换码；未下载订单取消后 CDK 会回到可售库存。

服务端通过以下环境变量连接三方升级服务。API Key 不会返回到页面；完整 Session 仅保存在本地任务记录并在后台任务详情中展示，不会出现在 C 端响应或请求日志中：

```bash
CHATGPT_REDEEM_BASE_URL=https://example.com
CHATGPT_REDEEM_API_KEY=YOUR_API_KEY
```

本地联调可以启动仓库内置的三方 Mock，不需要真实 API：

```bash
# 终端 1：Mock 三方服务（默认 127.0.0.1:4100）
MOCK_REDEEM_API_KEY=local-mock-key go run ./cmd/mock-redeem

# 终端 2：让本地服务连接 Mock
CHATGPT_REDEEM_BASE_URL=http://127.0.0.1:4100 \
CHATGPT_REDEEM_API_KEY=local-mock-key \
go run ./cmd

# 服务启动后执行完整 HTTP 端到端测试
./scripts/e2e_mock_redeem.sh
```

Mock 的前三次任务查询依次返回 `pending`、`processing`、`success`。若 Session JSON 带有 `"mockResult":"failed"`，第三次查询返回 `failed`，便于测试失败终态。

查询 CDK 是否可用：

```http
POST /api/chatgpt/cdk/check
X-API-Key: ccm_xxx
Content-Type: application/json

{"code":"550e8400-e29b-41d4-a716-446655440000"}
```

提交兑换任务。`session` 必须是 JSON 序列化后的字符串；套餐直接取自 CDK，调用方不能覆盖：

```http
POST /api/chatgpt/cdk/redeem
X-API-Key: ccm_xxx
Content-Type: application/json

{
  "code":"550e8400-e29b-41d4-a716-446655440000",
  "channel":"official",
  "session":"{\"accessToken\":\"example_token\",\"userId\":\"123456\"}"
}
```

创建成功返回本地任务的不可预测 `hash_id` 作为 `taskId`，不会暴露数据库自增 ID 或三方任务 ID。只有创建成功才会消耗 CDK；三方拒绝或网络失败会自动释放占用。使用同一个 API Key 查询任务：

```http
GET /api/chatgpt/cdk/tasks/ctk_2gR8vL7kYpQ4mN6xT1cD9aB3sE0
X-API-Key: ccm_xxx
```

查询响应透传三方任务数据。根据 `data.status` 判断 `pending`、`processing`、`success` 或 `failed`，到达最终状态后停止轮询；建议间隔 2～5 秒。

每次兑换都会先写入本地 `chatgpt_tasks` 表，再请求三方服务。任务记录包含 CDK、从 `accessToken` JWT payload 解析出的用户邮箱、完整 Session、SKU、渠道、远程 taskId、状态、三方响应、错误信息和时间信息。三方创建失败同样会以 `create_failed` 状态保留，便于排查；CDK 表只通过本地 `task_id` 关联成功创建的任务。后台“ChatGPT 任务”页面可查询任务并查看详情，Session 只在任务详情中展示。

### 下发 Free 账号

```http
POST /api/claude_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-request-id
Content-Type: application/json

{"count": 2, "plan": "free"}
```

下发后账号默认锁定 30 分钟（可通过 `ACCOUNT_DISPATCH_LEASE_MINUTES` 调整）。升级成功时调用升级同步接口；处理失败可主动释放，未处理的账号会在租约到期后重新进入可下发库存。

### 释放处理失败的账号

```http
POST /api/claude_account/release
X-API-Key: ccm_xxx
Content-Type: application/json

{"requestId":"unique-request-id","mails":["user@example.com"]}
```

该接口为可选调用，用于让处理失败的账号立即回到库存；不调用时等待租约超时即可。

### 添加 Free 普号

```http
POST /api/claude_account/add
X-API-Key: ccm_xxx
Content-Type: application/json

{"accounts":[{"mail":"user@example.com","password":"password","sessionKey":"session-key"}]}
```

### 同步升级结果

```http
POST /api/claude_account/upgrade
X-API-Key: ccm_xxx
Content-Type: application/json

{"mail":"user@example.com","plan":"max_20x","cardPoolId":12}
```

`cardPoolId` 为本次升级使用的 Card 下发响应中的卡池 ID，用于关联升级账号与卡片统计。接口不校验卡片的下发归属；若该 ID 对应卡池中的卡，上报成功后该卡使用次数加一并冷却 5 小时。相同账号和卡重复上报不会重复增加次数，但会重新计算冷却时间。

### Google 账号池

后台“Google 账号池”支持按行批量导入，格式为：

```text
google1@example.com|password1
google2@example.com|password2
```

后台列表支持启用、禁用和删除。禁用账号会立即释放下发租约并停止下发；删除 Google 账号只清理该账号及其下发记录，不会删除已关联的 Claude 账号。

下发一个未使用账号：

```http
POST /api/google_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-google-request-id
Content-Type: application/json

{"requestId":"unique-google-request-id"}
```

下发期间账号会被临时锁定，租约时长与账号下发租约配置一致。处理结束后上报结果状态：

```http
POST /api/google_account/report
X-API-Key: ccm_xxx
Content-Type: application/json

{"requestId":"unique-google-request-id","googleAccountId":21,"status":"used"}
```

`status` 支持 `used`（已使用）、`discarded`（已弃用）、`login_failed`（账号无法登录）。任一结果上报成功后，该 Google 账号都不会再次下发；同一结果重复上报为幂等成功。Google 账号上报不再绑定 Claude 邮箱，可用于 Claude、ChatGPT 等不同业务。

### 邮箱账号池

后台“邮箱账号池”批量导入时先选择平台（默认 `mailcom`），再按行粘贴邮箱和密码：

```text
user1@mail.com----password1
user2@gmx.com----password2
```

管理接口仍兼容原有的 `邮箱|密码|平台` 单行格式。

下发时 `platform` 可选；填写后仅下发该平台的未使用邮箱，不填写则从全部平台分配：

```http
POST /api/mail_account
X-API-Key: ccm_xxx
Idempotency-Key: unique-mail-request-id
Content-Type: application/json

{"requestId":"unique-mail-request-id","platform":"mailcom"}
```

使用成功后上报并关联已存在的 Claude 账号：

```http
POST /api/mail_account/report
X-API-Key: ccm_xxx
Content-Type: application/json

{"requestId":"unique-mail-request-id","mailAccountId":21,"claudeAccountMail":"claude@example.com"}
```

上报成功后邮箱账号永久变为 `used`，不会再次下发。后台列表支持按平台查询、启用、禁用和删除。

### 注册机联动

后台“注册机”菜单从当前 `mail_account` 邮箱池中选择指定平台和数量发起 Claude-register 任务。首次使用时填写与 Claude-register `WEBUI_TOKEN` 相同的 Token；Token 只保存在服务端，不会回显到浏览器。

统一 Compose 部署默认通过 `http://claude-register:8000` 访问注册机，也可以使用 `CLAUDE_REGISTER_BASE_URL` 覆盖。任务状态和账号关联保存在 SQLite，页面关闭或 CCMax 重启后仍可继续同步。

页面可保存并开启每分钟执行一次的定时任务。服务端每分钟检查所选平台的邮箱库存：上一个注册任务仍在运行时跳过；库存为空时跳过；有库存时按“配置数量”和“当前可用数量”的较小值启动。平台、数量、并发、重试、代理模式和邮箱快速路径均使用页面最后保存的配置，服务重启后配置和开关仍然有效。

注册完成后，处理规则如下：

- KYC 状态为 `not_required` 或 `approved`：自动创建可用的 Free Claude 账号，并把邮箱账号标记为已使用和关联该 Claude 账号。
- 已获得 Session 但需要 KYC、状态未知或 Session 失效：不进入 Claude 账号池，邮箱账号标记为已使用，避免重复注册。
- 注册失败且未获得 Session：释放邮箱锁定，可以重新发起任务。

### 查询卡验证码

支持 Qbit 和 Slash 渠道。先下发 Card：

```http
POST /api/card
X-API-Key: ccm_xxx
Idempotency-Key: unique-card-request-id
Content-Type: application/json

{"count":1,"source":"slash"}
```

响应中的 `cardPoolId` 用于升级关联、验证码查询和状态上报。启用且不在升级冷却期的卡会按使用次数均衡下发。

请求数量为 1 且没有可用卡时，系统会通过 Slash 创建一张新卡、读取 Vault 中的 PAN/CVV、写入 Card Pool 并直接返回。自动创建使用 `card_group_3febhaydgdiq9` 作为默认 Card Group ID。请求中的 `source` 为 `slash` 或 `slash_*` 时使用对应渠道；省略 `source` 时选择已启用的 Slash 渠道。明确指定非 Slash 来源时不会改用其他渠道。

Slash 创建、详情读取或本地导入失败时返回：

```json
{
  "code": "INSUFFICIENT_CARDS",
  "message": "insufficient cards: available=0 requested=1"
}
```

卡片已使用或不可用时上报：

```http
POST /api/card/report
X-API-Key: ccm_xxx
Content-Type: application/json

{"requestId":"unique-card-request-id","cards":[{"cardPoolId":12,"status":"used"}]}
```

`status=used` 会增加一次使用次数并让 Card 进入 5 小时冷却期；`status=unavailable` 会将 Card 标记为不可用，管理员可在 Card Pool 中重新启用。`reason` 为可选审计说明。同一次下发的相同结果可安全重复提交。

```http
POST /api/card/verify-code
X-API-Key: ccm_xxx
Fingerprint: optional
Content-Type: application/json

{"cardPoolId":12,"googleRef":"BMR"}
```

验证码接口只允许查询由当前 API Key 下发过的卡。

### 上传验证码渠道访问凭证

该接口与其他对外接口一样使用 `X-API-Key`：

```http
POST /api/card/verify-code/token
X-API-Key: ccm_xxx
Content-Type: application/json

{"source":"qbit","token":"qbit-access-token"}
```

Slash 渠道将 `source` 设为 `slash` 或以 `slash` 开头的唯一名称（例如 `slash_ccmax`），`token` 填对应的 Slash API Key。创建 Slash 卡时可选择具体渠道，新卡会以所选 source 入库，后续验证码和渠道历史也会使用该 source 对应的 Token。卡片池还支持按已有 Slash Card ID（`c_...`）快速入库：系统读取卡片详情及 Vault 中的 PAN/CVV 后写入本地，不会再创建一张 Slash 卡。Token 会覆盖对应来源的现有凭证，响应和后台列表均不会回显 Token。

## 验证

```bash
pnpm --dir web run typecheck
pnpm --dir web run build
go test ./... -timeout 60s
go vet ./...
go build ./cmd
```

SQLite 文件及 `-wal`、`-shm` 文件位于 `data/`，部署时必须持久化该目录。SQLite 适合单实例运行，不应让多个服务实例共享同一个数据库文件。
