# 项目结构与依赖边界

## 根目录

```text
Claude-register/
├── claude_register/   Python 业务实现
├── docs/              架构、流程和请求资料
├── examples/          可提交的配置与账号示例
├── runtime/           本地配置、账号输入和结果（不提交）
├── tests/             离线测试
├── README.md
└── requirements.txt
```

根目录不再放业务脚本或兼容入口。运行命令统一使用 Python 模块路径。

## 业务包

```text
claude_register/
├── auth/             Claude 登录、magic link 验证和单账号注册
├── challenge/        Arkose 配置与 token 解析
├── cli/              命令行入口实现
├── compliance/       KYC 与 session 有效性分类
├── config/           Claude 前端动态配置发现
├── core/             浏览器会话、脱敏、安全和运行保护
├── diagnostics/      需要显式授权的诊断探针
├── mail/             mail.xcaigc.com tRPC 邮箱适配器
├── onboarding/       验证后的 onboarding 步骤
├── orchestration/    批量队列、重试、任务状态和结果落盘
├── presentation/     Web/API 和静态资源
└── shared/           小型通用辅助函数
```

## 依赖方向

```text
presentation ──> orchestration ──> auth / mail / challenge / onboarding / compliance
                                  │
cli ──────────────────────────────┘

auth / mail / challenge / onboarding / compliance / config ──> core
shared ──> Python 标准库
```

- `core` 和 `shared` 不反向导入业务层。
- `presentation` 不实现注册、取信或结果分类逻辑。
- `orchestration` 负责编排和任务生命周期，不复制底层 HTTP 请求。
- 邮箱 provider 只保留 `mailcom`、`imap`、`microsoft` 三种显式模式，全部通过
  `https://mail.xcaigc.com`；禁止本地邮箱服务、自定义邮箱服务 URL 和本机 IMAP/OAuth。
- 新代码放进所属领域，不新增根目录脚本。

## 浏览器身份生命周期

`claude_register.core.browser.BrowserIdentity` 是单账号运行周期的唯一身份来源。编排器只在账号开始时
创建一次，并让动态配置、认证、Arkose 的浏览器 profile、Onboarding、重试 Session 和 KYC 复用。
新 HTTP 阶段不得自行生成 anonymous/device/activity/Sentry/Datadog 身份字段；独立 CLI 调用除外。

详细决策与取舍见 [ADR-001](decisions/001-account-browser-identity.md)。

## 标准入口

```bash
# Web UI
uvicorn claude_register.presentation.web:app --host 127.0.0.1 --port 8000

# 批量流程
python3 -m claude_register.orchestration.service --confirm-external-run

# 离线测试
python3 -m unittest discover -s tests -v
```
