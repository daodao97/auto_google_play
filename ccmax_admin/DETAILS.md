# 模块说明

## API

- `api/server.go`：注册公开接口与管理接口；处理 Card 三分钟租约下发、单张库存不足时自动创建和批量结果上报。
- `api/slash_card.go`：封装 Slash 卡创建、默认 Card Group、Vault 卡密导入及自动创建能力。
- `api/google_account.go`：处理 Google 账号导入、下发与结果上报，不绑定具体业务账号。
- `api/server_test.go`：覆盖 Card 接口鉴权、已使用冷却、不可用状态与幂等行为。
- `api/integration_test.go`：覆盖完整 HTTP 接口流程、参数校验和数据库状态变化。

## 数据访问

- `dao/cards.go`：Card 池查询、三分钟租约、均衡下发、使用次数、冷却时间和上报幂等逻辑。
- `dao/google_accounts.go`：Google 账号池三分钟租约、结果状态、统计与上报幂等逻辑。
- `dao/accounts.go`：Claude 账号升级流程；与 Card 上报共用统一的 5 小时冷却配置。
- `dao/store.go`：SQLite 初始化和增量迁移；保存 Card 租约、下发上报记录及 Google 账号结果状态。
- `dao/store_test.go`：覆盖数据访问层状态机、迁移后字段及统计结果。

## 管理后台

- `web/src/views/GoogleAccountsView.vue`：Google 账号列表、结果筛选和分类统计。
- `web/src/views/CardsView.vue`：Card Pool 管理、租约状态、Slash 创建表单及默认 Card Group 配置。
- `web/src/views/ApiDocsView.vue`：公开 API 的请求字段、状态值和响应示例。

## 文档

- `README.md`：服务部署、主要业务流程和公开 API 使用说明。
- `TODO.md`：按真实日期记录功能计划与完成状态。
- `DETAILS.md`：说明目录与主要文件职责。
- `docs/API.md`：以生产 Base URL 为准的全部对外 API 文档。
