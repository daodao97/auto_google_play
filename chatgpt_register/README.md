# ChatGPT Register

从 `gpt_pay/gopay-pipeline` 的 ChatGPT OTP 登录协议中抽出的注册机。项目把协议请求、邮箱取码、
单账号状态机、批量编排和结果持久化分层，避免继续把注册逻辑耦合在支付服务里。

## 注册状态机

```text
authorize
  -> check_email
  -> otp
  -> about_you (仅新账号，create_account)
  -> callback
  -> session
```

新邮箱会经过 `about_you` 并返回 `created=true`；已存在邮箱会完成 OTP 登录并返回
`created=false`。因此同一套协议既能注册也能检测“邮箱已注册”，结果不会混淆。

任务终态：

- `success`：拿到 `/api/auth/session`。
- `partial`：已经执行 `create_account`，但 callback/session 阶段失败；禁止自动整体重试。
- `failed`：账号创建前失败，可按配置重试。

## 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

mkdir -p runtime
cp examples/config.example.json runtime/config.json
cp examples/accounts.example.txt runtime/accounts.txt
python3 -m chatgpt_register.cli --config runtime/config.json --confirm-external-run
```

账号格式：`邮箱----邮箱密码`。当前邮箱适配器复用 `gpt_pay` 的 mail.com helper API；
`mail_api_base` 和 `mail_app_token` 必须由运行环境提供。建议每账号使用独立粘性代理，
`proxy_template` 中的 `{session}` 会替换成随机会话 ID。

结果按 run 写入私有目录：

```text
runtime/runs/<run-id>/
  results.jsonl   成功结果（含敏感 session/token，0600）
  partial.jsonl   已创建但未完整拿到 session
  failed.jsonl    创建前失败（不写邮箱密码）
```

真实运行可能触发 OpenAI 风控、验证码或条款限制。默认并发为 1、重试为 0；请只处理你有权
管理的邮箱和账号，并遵守服务条款。协议响应变化时先更新离线契约测试，再灰度真实请求。

架构与状态迁移见 `docs/architecture.md` 和 `docs/registration-flow.md`。

