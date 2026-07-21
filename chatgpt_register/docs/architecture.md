# 架构

```text
cli -> orchestration -> registration -> protocol
                   \-> mail
                   \-> persistence
```

- `protocol`：只负责 OpenAI/ChatGPT HTTP 状态机；不认识批量任务和结果文件。
- `mail`：实现 `prime_seen` / `wait_for_code` 邮箱端口；协议层只依赖这个最小接口。
- `registration`：单账号应用服务，维护“是否已创建”的不可逆边界和错误分类。
- `orchestration`：账号解析、固定 worker、粘性代理物化、取消和有限重试。
- `persistence`：按终态追加 JSONL，敏感文件固定 `0600`，目录固定 `0700`。

## 与 Claude-register 的对应关系

沿用了它的领域拆分、单账号唯一身份、`success/partial/failed`、账号创建后禁止整体重试、
私有追加落盘和批量 worker 模型。没有引入 Claude 专属的 Arkose、magic link、onboarding、
KYC 和动态前端配置模块。

## 与 gpt_pay 的边界

`gpt_pay` 继续消费注册结果做支付，不再负责创建账号。协议底座暂时来自
`gopay-pipeline/core/session.py`，注册项目在其上提供显式的新号状态机，并修复原流程忽略
`accounts/check.continue_url` 的问题。后续协议变更只需更新 `protocol/_session.py` 和契约测试。

