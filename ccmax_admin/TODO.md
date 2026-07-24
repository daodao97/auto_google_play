# 开发计划

## 2026-07-24

- [x] Google 账号和 Card 下发后使用独立的 3 分钟租约。
- [x] 未上报的 Google 账号和 Card 在租约到期后自动恢复可下发。
- [x] Card 上报及 Claude 升级成功后立即解除 Card 租约。
- [x] 补充 Card 租约数据库迁移、后台状态、接口响应和过期上报测试。
- [x] Slash 创建卡的默认 Card Group ID 更新为 `card_group_3febhaydgdiq9`。
- [x] 单张 Card 库存不足时自动创建 Slash 卡并完成下发。
- [x] 自动创建失败时保持 `INSUFFICIENT_CARDS` 错误协议。
- [x] 更新管理后台接口文档并新增完整的对外 API 文档。

## 2026-07-23

- [x] `/api/card/report` 支持 `used` 与 `unavailable` 两种结果。
- [x] Card 已使用上报接入 5 小时冷却，并保证同一次下发重复上报不会重复计数。
- [x] `/api/google_account/report` 移除 Claude 邮箱绑定，改为上报结果状态。
- [x] Google 账号支持 `used`、`discarded`、`login_failed` 三种结果。
- [x] 补充数据库迁移、后端测试、管理后台状态展示与接口文档。

## 当前待办

- 暂无。
