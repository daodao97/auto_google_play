# Claude-register

本地 Claude magic link 注册/session 流程工具，包含批量任务编排、远程邮箱取信、
onboarding、KYC 分类和 Web 控制台。

## 目录

```text
claude_register/   Python 业务实现
docs/              架构、流程和请求资料
examples/          配置与账号示例
runtime/           本地配置、账号输入和结果（Git 忽略）
tests/             离线测试
```

详细模块边界见 `docs/architecture.md`。根目录不放业务脚本。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock

cp examples/config.example.json runtime/config.json
cp examples/accounts.example.txt runtime/accounts.txt
```

真实密码、代理、token 和结果只放在 `runtime/`，不要提交。

## 启动 Web UI

```bash
uvicorn claude_register.presentation.web:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000`。

## Docker Compose 部署

项目根目录已提供 `Dockerfile` 和 `compose.yaml`。线上部署前准备运行目录和访问令牌：

```bash
cd Claude-register
mkdir -p runtime
cp .env.example .env
openssl rand -hex 32
```

将生成的随机值写入 `.env` 的 `WEBUI_TOKEN`。Linux 上如果当前部署用户不是
UID/GID `1000:1000`，同时将 `PUID`、`PGID` 改为 `id -u`、`id -g` 的结果。
Compose 启动时会先运行一次无网络的权限初始化服务，把宿主机 `runtime/` 调整为
`PUID:PGID` 且目录权限设为 `0700`；初始化成功后才会启动注册机，避免全新部署时
Docker 将挂载目录创建为 `root:root` 导致运行归档无法写入。

配置文件是可选的；需要预设代理、并发或 Arkose 参数时再复制：

```bash
cp examples/config.example.json runtime/config.json
```

构建并启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f claude-register
```

默认只映射到宿主机 `127.0.0.1:8000`，建议通过 Nginx/Caddy 反向代理并启用
HTTPS。直接开放端口时将 `.env` 中的 `BIND_ADDRESS` 改为 `0.0.0.0`，访问页面
后输入 `WEBUI_TOKEN`。`runtime/` 已挂载到宿主机，配置、任务恢复记录和结果在
重建容器后仍会保留。

如果反向代理也运行在 Docker 网络中，将 `FORWARDED_ALLOW_IPS` 设置为该代理
的固定容器 IP 或可信网段；不要设为 `*`，否则客户端可伪造代理来源信息。

更新与停止：

```bash
docker compose up -d --build
docker compose down
```

Web 服务必须保持单 worker；任务状态保存在进程内存中，Compose 已固定
`--workers 1`。容器停止时会最多等待 30 秒让当前任务收尾。

## 邮箱取信

邮箱统一通过 `https://mail.xcaigc.com` 的 tRPC 服务，不再包含本地 `mail.com`
Node 服务、本机 IMAP 连接或本机 Microsoft OAuth token 兑换逻辑。

`mail_provider` 必须显式设置为：

| 值 | 凭据格式 |
| --- | --- |
| `mailcom` | `email----password----display_name` |
| `imap` | `email----password----display_name` |
| `microsoft` | `email----password----client_id----refresh_token----display_name` |

旧字段 `auto`、`mail_base_url`、`mail_app_token`、`imap_*`、`mail_imap_*` 和
`client_secret` 已删除，检测到时会直接报配置迁移错误。

## 运行模式

- `register`：验证 magic link 后执行 onboarding。
- `session`：验证后跳过 onboarding，提取 session 并执行 KYC 分类。

### 运行时加速与出口信息

- `mail_fast_path`：默认 `false`。启用后只合并首次邮箱列表和首个候选详情的限流等待，后续轮询仍使用原有间隔和超时。
- `send_settle_delay`：默认留空，继续使用原来的随机 `2.5～5.5` 秒；填写非负秒数后才使用固定等待，建议先用 `1.0` 做小批量灰度。
- `resolve_exit_ip`：默认 `false`。启用后，任务主流程完成后通过同一代理后台探测出口 IP；探测失败不会改变任务结果，也不会阻塞注册链路。
- 两个开关都不改变请求参数、注册阶段顺序、重试边界或并发限制。Web UI 可在开始运行前分别启用。

批量 CLI 需要显式确认外部运行：

```bash
python3 -m claude_register.orchestration.service --confirm-external-run
```

其他诊断入口：

```bash
python3 -m claude_register.auth.service --confirm-external-run
python3 -m claude_register.cli.full_run --confirm-external-run
python3 -m claude_register.mail.fetcher --confirm-external-run email password mailcom
```

## 本地数据

默认路径：

| 文件 | 内容 |
| --- | --- |
| `runtime/config.json` | 本地配置 |
| `runtime/accounts.txt` | 批量账号输入 |
| `runtime/results.txt` | 成功结果 |
| `runtime/partial.txt` | verify 成功但后续部分失败 |
| `runtime/failed.txt` | verify 前失败 |
| `runtime/kyc_pass.txt` | KYC 不需要或已通过 |
| `runtime/kyc_required.txt` | KYC pending/denied |
| `runtime/kyc_unknown.txt` | 网络或未知状态 |
| `runtime/kyc_dead.txt` | session 失效 |

## 验证

```bash
python3 -m compileall -q claude_register tests
python3 -m unittest discover -s tests -v
coverage run -m unittest discover -s tests
coverage report --fail-under=82
ruff check claude_register tests scripts
bandit -q -ll -r claude_register scripts
git diff --check
```

开发和 CI 工具使用已锁定的依赖：

```bash
pip install -r requirements-dev.lock
```

## 源码发布包

发布包只从当前 Git `HEAD` 生成，并校验不含运行数据、凭据、结果和缓存：

```bash
python3 -m scripts.build_release
```

默认输出 `dist/Claude-register-source.zip`，文件权限为 `0600`。
