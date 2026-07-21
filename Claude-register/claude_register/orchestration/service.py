"""批量注册编排——gopay 风格，仅注册（无支付）。

对标 gopay-pipeline core/orchestrator.py 的队列分发 + 重试 + 结果落盘，去掉所有支付环节。
每个账号独立 session 代理（{session} 占位符 → 唯一 id → 独立粘性 IP）+ 独立指纹 id，
线程池并发跑：arkose → send → mail → verify → onboarding。

⚠️ 自动化批量注册违反 Anthropic ToS，账号/组织/IP 可能被封。仅供学习研究。
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import random
import re
import threading
import time
from dataclasses import replace
from typing import Any

from claude_register.challenge.arkose import (
    ArkoseConfig as ArkoseConfig,
    config_from_dict,
    resolve_arkose_token,
)
from claude_register.config.dynamic import DEFAULT_CLIENT_VERSION, fetch_dynamic_config
from claude_register.core.browser import (
    BrowserIdentity,
    build_session,
    resolve_proxy_exit_ip,
    init_browser_cookies,
    materialize_session_proxy,
    new_browser_identity,
    warm_claude_bootstrap,
    warm_claude_login,
)
from claude_register.compliance.kyc import check_kyc_status
from claude_register.mail.fetcher import (
    FifoRateLimiter,
    MailFetcherFatalError,
    MailFetcherTimeoutError,
    MailFetcherTransientError,
    _detect_provider,
    _validate_mail_config,
    fetch_magic_link,
    prime_seen,
)
from claude_register.shared.names import random_american_name
from claude_register.onboarding.service import OnboardingContext, onboarding_failed_steps, run_onboarding
from claude_register.auth.service import _extract_org_uuid, login_methods, send_magic_link, verify_magic_link
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.core.diagnostics import safe_error_label
from claude_register.orchestration.config import validate_runtime_config
from claude_register.orchestration.errors import FlowCancelled, FlowError
from claude_register.orchestration.persistence import ResultWriter
from claude_register.orchestration.parsing import (
    MAIL_PROVIDERS as MAIL_PROVIDERS,
    REMOVED_MAIL_ACCOUNT_FIELDS as REMOVED_MAIL_ACCOUNT_FIELDS,
    _account_with_names as _account_with_names,
    _looks_like_client_id as _looks_like_client_id,
    _looks_like_refresh_token as _looks_like_refresh_token,
    _split_account_fields as _split_account_fields,
    parse_accounts as _parse_accounts,
    parse_accounts_with_report as _parse_accounts_with_report,
)
from claude_register.orchestration.models import (
    Account,
    AccountParseIssue as AccountParseIssue,
    AccountParseReport,
    AccountTask,
    OrchestratorConfig,
    ProgressCb,
    PublicTaskSnapshot as PublicTaskSnapshot,
)

# HAR 和当前线上错误只验证了 Other；其它展示文案可能不是 API 允许枚举。
_WORK_FUNCTIONS = ["Other"]

log = logging.getLogger("orchestrator")

FLOW_MODES = {"register", "session"}


_mail_limiter = FifoRateLimiter(7.5)
_dynamic_config_lock = threading.Lock()
_dynamic_config_cache: tuple[float, tuple[str, list[dict[str, Any]], str, str, str]] | None = None
_DYNAMIC_CONFIG_CACHE_TTL_SECONDS = 60.0


def _should_rate_limit_mail(task: AccountTask, cfg: OrchestratorConfig) -> bool:
    provider = _detect_provider(
        task.account.email,
        cfg.mail_provider,
        refresh_token=task.account.mail_refresh_token,
        client_id=task.account.mail_client_id,
    )
    return provider == "mailcom"


def _is_retryable(e: Exception) -> bool:
    if isinstance(e, FlowError):
        return e.retryable
    if isinstance(e, (MailFetcherFatalError, MailFetcherTransientError, MailFetcherTimeoutError)):
        return False
    msg = str(e).lower()
    fatal_patterns = (
        "certificate_verify_failed",
        "certificate verify failed",
        "unable to get local issuer certificate",
        "ssl certificate",
        "unsupported protocol",
        "protocol unsupported",
        "proxy authentication required",
        "http 407",
        "407 proxy",
        "connect tunnel failed, response 400",
        "connect tunnel failed: 400",
    )
    if any(k in msg for k in fatal_patterns):
        return False
    if re.search(r"\b(429|5\d\d)\b", msg):
        return True
    retry_patterns = (
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection closed",
        "temporarily unavailable",
        "network",
        "reset by peer",
        "curl: (28)",
        "curl: (35)",
        "curl: (56)",
        "ssl",
    )
    if any(k in msg for k in retry_patterns):
        return True
    return False


def _wait_cancelable(seconds: float, cancel: threading.Event | None = None) -> bool:
    """等待 seconds；如果 cancel 期间被置位，返回 True。"""
    if seconds <= 0:
        return bool(cancel and cancel.is_set())
    if cancel:
        return cancel.wait(seconds)
    time.sleep(seconds)
    return False


def _timed_substage(task: AccountTask, name: str, operation: Any) -> Any:
    """Run one operation and record only its elapsed time under a safe name."""
    started = time.monotonic()
    try:
        return operation()
    finally:
        task.record_substage_duration(
            name,
            max(0, int(round((time.monotonic() - started) * 1000))),
        )


def _raise_if_cancelled(cancel: threading.Event | None, stage: str) -> None:
    if cancel and cancel.is_set():
        raise FlowCancelled(stage)


def _close_session(session: Any) -> None:
    try:
        session.close()
    except Exception:
        pass


def _normalize_dynamic_config(values: tuple[Any, ...]) -> tuple[str, list[dict[str, Any]], str, str, str]:
    if len(values) == 5:
        return values  # type: ignore[return-value]
    if len(values) == 4:
        client_sha, legal_docs, sentry_key, sentry_org = values
        return client_sha, legal_docs, sentry_key, sentry_org, DEFAULT_CLIENT_VERSION
    raise ValueError(f"动态配置返回值数量异常: {len(values)}")


def _fetch_dynamic_config_with_cache(
    session: Any,
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    global _dynamic_config_cache
    now = time.monotonic()
    with _dynamic_config_lock:
        if _dynamic_config_cache and _dynamic_config_cache[0] > now:
            return _dynamic_config_cache[1]

    values = _normalize_dynamic_config(fetch_dynamic_config(session))
    with _dynamic_config_lock:
        _dynamic_config_cache = (time.monotonic() + _DYNAMIC_CONFIG_CACHE_TTL_SECONDS, values)
    return values


def _worker_cfg(base: OrchestratorConfig, worker: dict[str, Any] | None) -> OrchestratorConfig:
    """把 worker 局部配置覆盖到基础配置上，借鉴 gopay 的 worker 隔离模型。"""
    if not worker:
        return base
    _validate_mail_config(worker)
    return replace(
        base,
        flow_mode=_normalize_flow_mode(worker.get("flow_mode", base.flow_mode)),
        impersonate=worker.get("impersonate", base.impersonate),
        proxy_template=worker.get("proxy_template", base.proxy_template) or None,
        proxy=worker.get("proxy", base.proxy) or None,
        mail_provider=_normalize_mail_provider(worker.get("mail_provider", base.mail_provider)),
        mail_poll_interval=worker.get("mail_poll_interval", base.mail_poll_interval),
        mail_poll_timeout=worker.get("mail_poll_timeout", base.mail_poll_timeout),
        mail_fast_path=worker.get("mail_fast_path", base.mail_fast_path),
        send_settle_delay=worker.get("send_settle_delay", base.send_settle_delay),
        resolve_exit_ip=worker.get("resolve_exit_ip", base.resolve_exit_ip),
        retry_max=worker.get("retry_max", base.retry_max),
        auto_send=worker.get("auto_send", base.auto_send),
    )


def _normalize_flow_mode(value: Any) -> str:
    mode = str(value or "register").strip().lower()
    if mode not in FLOW_MODES:
        raise ValueError("flow_mode 必须是 register 或 session")
    return mode


def _normalize_mail_provider(value: Any) -> str:
    provider = str(value or "mailcom").strip().lower()
    if provider not in MAIL_PROVIDERS:
        raise ValueError("邮件取信方式必须是 mailcom、imap 或 microsoft")
    return provider


def _set_kyc_status(
    task: AccountTask,
    proxy: str | None,
    identity: BrowserIdentity,
    session: Any | None = None,
    client_sha: str | None = None,
) -> None:
    if not task.session_key:
        return
    alive, kyc = check_kyc_status(
        task.session_key,
        proxy,
        impersonate=identity.profile.impersonate,
        client_sha=client_sha,
        session=session,
        identity=identity,
    )
    task.kyc_status = kyc if alive else ("dead" if kyc == "dead" else "error")


def _safe_work_function(value: Any) -> str:
    return value if value in _WORK_FUNCTIONS else "Other"


def _runtime_workers(cfg: OrchestratorConfig, concurrency: int) -> list[dict[str, Any]]:
    """生成运行时 worker 列表；未配置 workers 时按 concurrency 补空 worker。"""
    workers = [dict(w) for w in cfg.workers if isinstance(w, dict)]
    if workers:
        return workers
    return [{} for _ in range(max(1, concurrency))]


def _task_summary_event(task: AccountTask, cfg: OrchestratorConfig) -> dict[str, Any]:
    now = time.time()
    with task._lock:
        status = task.status
        stage = task.stage
        started_at = task.started_at
        finished_at = task.finished_at
        attempts = task.attempts
        retryable = task.retryable
        error_class = task.error_class
        queue_wait_ms = task.queue_wait_ms
        stage_durations_ms = dict(task._stage_durations_ms)
        substage_durations_ms = dict(task._substage_durations_ms)
    outcome = status if status in ("success", "partial", "failed") else "unknown"
    elapsed_ms = AccountTask._elapsed_ms(started_at, finished_at or now) if started_at else 0
    mode = cfg.flow_mode if cfg.flow_mode in FLOW_MODES else "unknown"
    return {
        "event": "account_task_summary",
        "mode": mode,
        "stage": stage,
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts,
        "retryable": retryable,
        "error_class": error_class,
        "queue_wait_ms": queue_wait_ms,
        "stage_durations_ms": stage_durations_ms,
        "substage_durations_ms": substage_durations_ms,
    }


def _log_task_summary(task: AccountTask, cfg: OrchestratorConfig) -> None:
    log.info("%s", json.dumps(_task_summary_event(task, cfg), ensure_ascii=False, sort_keys=True))


def _update_local_state(task: AccountTask, cfg: OrchestratorConfig) -> None:
    if cfg.state_store is None:
        return
    try:
        cfg.state_store.update_task(task)
    except Exception as exc:
        log.error("[state] update_failed error_class=%s", safe_error_label(exc))


def _record_verified_recovery(task: AccountTask, cfg: OrchestratorConfig) -> None:
    if cfg.recovery_store is None:
        return
    try:
        cfg.recovery_store.record_verified(task)
    except Exception as exc:
        log.error("[recovery] checkpoint_failed error_class=%s", safe_error_label(exc))


def _send_settle_delay(cfg: OrchestratorConfig) -> float:
    """Return the post-send settle delay, preserving the legacy range by default."""
    if cfg.send_settle_delay is not None:
        return cfg.send_settle_delay
    return random.uniform(2.5, 5.5)


def _start_exit_ip_probe(
    task: AccountTask,
    cfg: OrchestratorConfig,
    proxy: str | None,
    identity: BrowserIdentity,
    on_progress: ProgressCb | None,
) -> None:
    """Resolve the exit IP after the flow so the probe never delays registration."""
    if not cfg.resolve_exit_ip:
        return

    def probe() -> None:
        try:
            exit_ip = resolve_proxy_exit_ip(proxy, identity=identity)
            if not exit_ip:
                return
            with task._lock:
                task.proxy_exit_ip = exit_ip
            _update_local_state(task, cfg)
            if on_progress:
                task.publish_update()
                on_progress(task)
        except Exception as exc:
            # The optional probe must remain isolated from the completed flow.
            log.warning("[exit-ip] probe_failed error_class=%s", safe_error_label(exc))

    threading.Thread(
        target=probe,
        name=f"exit-ip-{task.task_id[:8]}",
        daemon=True,
    ).start()


# ── 单账号全流程 ────────────────────────────────────────────────────────────
def register_one(
    task: AccountTask,
    cfg: OrchestratorConfig,
    on_progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> AccountTask:
    """跑单个账号全流程，带重试。每次阶段变化调 on_progress。"""

    def emit():
        task.publish_update()
        _update_local_state(task, cfg)
        if on_progress:
            on_progress(task)

    task.mark_running()
    emit()

    # 物化代理（每账号独立 session id → 独立粘性 IP）
    proxy = None
    mp = materialize_session_proxy(cfg.proxy_template)
    if mp:
        proxy, task.proxy_session = mp
    elif cfg.proxy:
        proxy = cfg.proxy
        task.proxy_session = "static"
    emit()

    # 一个账号只创建一次身份对象；重试只换 HTTP Session，不换浏览器身份。
    identity = new_browser_identity(impersonate=cfg.impersonate)
    profile = identity.profile
    verified = False  # verify 成功=账号已创建，之后不再整体重试

    # 动态配置（client_sha + legal_docs + sentry + client_version）：best-effort，失败回退默认值。
    log.info("[flow] 动态配置: client_sha + legal_docs + sentry + client_version")
    _dc_session = build_session(proxy=proxy, identity=identity)
    try:
        init_browser_cookies(
            _dc_session,
            identity.anonymous_id,
            identity.device_id,
            identity=identity,
        )
        client_sha, legal_docs, sentry_key, sentry_org, client_version = _fetch_dynamic_config_with_cache(
            _dc_session
        )
    finally:
        _close_session(_dc_session)

    mail_seen: set[str] | None = None
    mail_seen_initialized = False
    rate_limit_mail = _should_rate_limit_mail(task, cfg)
    active_mail_limiter = cfg.mail_limiter or _mail_limiter
    mail_rate_limiter = active_mail_limiter.acquire if rate_limit_mail else None
    prime_rate_limiter = None if cfg.mail_fast_path else mail_rate_limiter

    for attempt in range(1, cfg.retry_max + 2):  # retry_max + 1 次总尝试
        if cancel and cancel.is_set():
            with task._lock:
                task.status = "partial" if verified else "failed"
                task.error = "已取消"
            break
        with task._lock:
            task.attempts = attempt
        session = None
        try:
            session = build_session(proxy=proxy, identity=identity)
            init_browser_cookies(
                session,
                identity.anonymous_id,
                identity.device_id,
                identity=identity,
            )
            _timed_substage(task, "send.warm_login", lambda: warm_claude_login(session))
            _raise_if_cancelled(cancel, "login")

            # ── prime_seen：发信前标记现有邮件，后续只取新到的 ──
            mail_not_before_ms: float | None = None
            if cfg.auto_send and not mail_seen_initialized:
                try:
                    mail_seen = prime_seen(
                        task.account.email, task.account.password,
                        provider=cfg.mail_provider,
                        client_id=task.account.mail_client_id,
                        refresh_token=task.account.mail_refresh_token,
                        cancel_event=cancel,
                        rate_limiter=prime_rate_limiter,
                    )
                except Exception:
                    mail_seen = None
                mail_seen_initialized = True

            # ── send（cookie-arkose，best-effort）──
            if cfg.auto_send:
                _raise_if_cancelled(cancel, "send")
                task.set_stage("send")
                emit()
                mail_not_before_ms = time.time() * 1000
                def best_effort_login_methods() -> None:
                    try:
                        login_methods(session, task.account.email, client_sha=client_sha,
                                      sentry_public_key=sentry_key, sentry_org_id=sentry_org,
                                      client_version=client_version, identity=identity)
                    except Exception:
                        pass

                _timed_substage(task, "send.login_methods", best_effort_login_methods)
                _timed_substage(
                    task,
                    "send.magic_link",
                    lambda: send_magic_link(
                        session, task.account.email, client_sha=client_sha,
                        sentry_public_key=sentry_key, sentry_org_id=sentry_org,
                        client_version=client_version, identity=identity,
                    ),
                )
                if _wait_cancelable(_send_settle_delay(cfg), cancel):
                    raise FlowCancelled("send")

            # ── mail（全局节流）──
            _raise_if_cancelled(cancel, "mail")
            task.set_stage("mail")
            emit()
            ml = fetch_magic_link(
                task.account.email, task.account.password,
                provider=cfg.mail_provider,
                poll_interval=cfg.mail_poll_interval,
                poll_timeout=cfg.mail_poll_timeout,
                cancel_event=cancel,
                seen=mail_seen,
                not_before_ms=mail_not_before_ms,
                client_id=task.account.mail_client_id,
                refresh_token=task.account.mail_refresh_token,
                rate_limiter=mail_rate_limiter,
                mail_fast_path=cfg.mail_fast_path,
            )
            if mail_seen is not None and ml.get("mail_id"):
                mail_seen.add(str(ml["mail_id"]))

            # ── arkose（紧贴 verify 前解析，避免 token 在 send+mail 期间空耗过期）──
            _raise_if_cancelled(cancel, "arkose")
            task.set_stage("arkose")
            emit()
            arkose_cfg = copy.deepcopy(cfg.arkose_config) if cfg.arkose_config else None
            if arkose_cfg:
                arkose_cfg.proxy = proxy
            arkose_token = resolve_arkose_token(arkose_cfg, profile=profile)
            warm_claude_bootstrap(
                session,
                client_sha=client_sha,
                sentry_public_key=sentry_key,
                sentry_org_id=sentry_org,
                client_version=client_version,
                identity=identity,
            )

            # ── verify（账号在此创建）──
            _raise_if_cancelled(cancel, "verify")
            task.set_stage("verify")
            emit()
            verify_resp = verify_magic_link(
                session, ml["nonce"], ml["encoded_email_address"], arkose_token,
                client_sha=client_sha,
                sentry_public_key=sentry_key, sentry_org_id=sentry_org,
                client_version=client_version, identity=identity,
            )
            task.account_uuid = verify_resp.get("account", {}).get("uuid", "")
            task.session_key = session.cookies.get("sessionKey") or ""      # sk-ant-sid02-...

            if cfg.flow_mode == "session":
                if not task.session_key:
                    raise RuntimeError("verify 成功但未拿到 sessionKey")
                try:
                    task.org_uuid = _extract_org_uuid(verify_resp)
                except RuntimeError:
                    task.org_uuid = ""
                verified = True
                _record_verified_recovery(task, cfg)
                _raise_if_cancelled(cancel, "verify")
                task.set_stage("kyc")
                emit()
                _set_kyc_status(
                    task, proxy, identity,
                    session=session, client_sha=client_sha,
                )
                task.complete_current_stage()
                with task._lock:
                    task.status = "success"
                    task.error = ""
                    task.error_class = ""
                    task.retryable = False
                break

            task.org_uuid = _extract_org_uuid(verify_resp)
            verified = True  # ← 账号已创建，后续失败不再整体重试（避免对已存在账号再 send/verify）
            _record_verified_recovery(task, cfg)
            _raise_if_cancelled(cancel, "verify")
            warm_claude_bootstrap(
                session,
                client_sha=client_sha,
                org_uuid=task.org_uuid,
                sentry_public_key=sentry_key,
                sentry_org_id=sentry_org,
                client_version=client_version,
                identity=identity,
            )

            # ── onboarding ──
            _raise_if_cancelled(cancel, "onboarding")
            task.set_stage("onboarding")
            emit()
            ctx = OnboardingContext(
                session=session, org_uuid=task.org_uuid,
                display_name=task.account.display_name,
                full_name=task.account.full_name or None,
                work_function=_safe_work_function(random.choice(_WORK_FUNCTIONS)),
                client_sha=client_sha, legal_docs=legal_docs,
                sentry_public_key=sentry_key, sentry_org_id=sentry_org,
                client_version=client_version,
                identity=identity,
                cancel_event=cancel,
            )
            onboarding_summary = run_onboarding(ctx)
            failed_steps = onboarding_failed_steps(onboarding_summary)
            if failed_steps:
                raise RuntimeError(f"onboarding 失败: {', '.join(failed_steps)}")

            # ── KYC 检测（注册成功后查 KYC 状态，用于分流落盘）──
            _raise_if_cancelled(cancel, "onboarding")
            task.set_stage("kyc")
            emit()
            _set_kyc_status(
                task, proxy, identity,
                session=session, client_sha=client_sha,
            )

            task.complete_current_stage()
            with task._lock:
                task.status = "success"
                task.error = ""
                task.error_class = ""
                task.retryable = False
            break
        except Exception as e:
            task.complete_current_stage()
            retryable = _is_retryable(e)
            error_class = safe_error_label(e)
            with task._lock:
                task.error = f"[attempt {attempt}/{cfg.retry_max + 1}] {error_class}"
                task.error_class = error_class
                task.retryable = retryable
            emit()
            if cancel and cancel.is_set():
                task.status = "partial" if verified else "failed"
                break
            if verified:
                # 账号已创建但后续步骤失败——不重跑 send/verify（会撞已存在账号），标 partial
                task.status = "partial"
                break
            if not retryable:
                break  # 不可重试（verify 失败 / arkose 无配置 / send 4xx）直接退出
            if attempt > cfg.retry_max:
                break
            log.warning("[retry] stage=%s attempt=%d wait=%ds error_class=%s",
                        task.stage, attempt, 2, error_class)
            if _wait_cancelable(2, cancel):
                task.status = "partial" if verified else "failed"
                break
        finally:
            if session is not None:
                _close_session(session)

    with task._lock:
        task._complete_current_stage_locked(time.time())
        task.finished_at = time.time()
        if task.status not in ("success", "partial"):
            task.status = "failed"
    _log_task_summary(task, cfg)
    emit()
    _start_exit_ip_probe(task, cfg, proxy, identity, on_progress)
    return task


def _write_result(task: AccountTask, cfg: OrchestratorConfig) -> None:
    """落盘：注册失败 → failed.txt；成功 → results.txt 交付行 + 按 KYC 分流
    (not_required/approved → kyc_pass.txt，pending/denied → kyc_required.txt，
    dead → kyc_dead.txt，其他/检测异常 → kyc_unknown.txt)。

    交付行格式：
      普通邮箱：email----password----sessionKey
      微软令牌：email----password----client_id----refresh_token----sessionKey
    """
    ResultWriter(cfg).write(task)


# ── 账号解析 ────────────────────────────────────────────────────────────────
def parse_accounts(text: str) -> list[Account]:
    """Compatibility wrapper for legacy heuristic account parsing."""
    return _parse_accounts(text, name_factory=random_american_name)


def parse_accounts_with_report(text: str, provider: str) -> AccountParseReport:
    """Compatibility wrapper for explicit-provider parsing reports."""
    return _parse_accounts_with_report(
        text,
        provider,
        name_factory=random_american_name,
    )


# ── 批量调度 ────────────────────────────────────────────────────────────────
def orchestrate(
    accounts: list[Account],
    cfg: OrchestratorConfig,
    concurrency: int = 2,
    on_progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
    tasks: list[AccountTask] | None = None,
) -> list[AccountTask]:
    """批量调度。

    借鉴 gopay 的 worker 队列模型：固定 worker 从队列取任务，支持 worker 级代理/限速。
    stop 后不再启动未开始的账号，避免把剩余账号逐个跑成失败。
    tasks 可传入预建列表（webui 用，便于实时读同一对象状态）；None 则内部新建。
    """
    if tasks is None:
        tasks = [AccountTask(account=a) for a in accounts]
    elif len(tasks) != len(accounts):
        raise ValueError("tasks 长度与 accounts 不一致")

    runtime_workers = _runtime_workers(cfg, concurrency)
    prepared_workers = [(worker, _worker_cfg(cfg, worker)) for worker in runtime_workers]

    task_queue: queue.Queue[int] = queue.Queue()
    for i in range(len(tasks)):
        tasks[i].mark_queued()
        _update_local_state(tasks[i], cfg)
        task_queue.put(i)

    done_lock = threading.Lock()
    completed_task_ids: set[int] = set()

    def worker_loop(
        wid: int,
        worker: dict[str, Any],
        local_cfg: OrchestratorConfig,
    ) -> None:
        tag = f"W{wid}"
        interval = float(worker.get("interval_seconds", cfg.worker_interval_seconds) or 0.0)
        last_start = 0.0

        while not (cancel and cancel.is_set()):
            try:
                idx = task_queue.get(block=False)
            except queue.Empty:
                break
            task = tasks[idx]
            try:
                if cancel and cancel.is_set():
                    return
                if interval > 0 and last_start > 0:
                    wait = interval - (time.time() - last_start)
                    if wait > 0 and _wait_cancelable(wait, cancel):
                        return
                task.worker_id = tag
                last_start = time.time()
                register_one(task, local_cfg, on_progress, cancel)
            except Exception as e:  # 兜底，不应到这
                with task._lock:
                    task.status = "failed"
                    task.error_class = safe_error_label(e)
                    task.error = f"worker_error:{task.error_class}"
                    task.retryable = False
                    task.finished_at = time.time()
                if on_progress:
                    task.publish_update()
                    on_progress(task)
            finally:
                if task.status != "pending":
                    try:
                        _write_result(task, cfg)
                    except Exception as exc:
                        error_class = safe_error_label(exc)
                        with task._lock:
                            task.persistence_status = "failed"
                            task.persistence_error_class = error_class
                        log.error("[persistence] write_failed error_class=%s", error_class)
                    else:
                        with task._lock:
                            task.persistence_status = "success"
                            task.persistence_error_class = ""
                        if cfg.recovery_store is not None and task.session_key:
                            try:
                                cfg.recovery_store.mark_exported(task.task_id)
                                cfg.recovery_store.cleanup_exported()
                            except Exception as exc:
                                log.error(
                                    "[recovery] export_mark_failed error_class=%s",
                                    safe_error_label(exc),
                                )
                    _update_local_state(task, cfg)
                    if on_progress:
                        task.publish_update()
                        on_progress(task)
                    with done_lock:
                        completed_task_ids.add(idx)
                task_queue.task_done()

    threads = [
        threading.Thread(target=worker_loop, args=(wid, worker, local_cfg), daemon=True)
        for wid, (worker, local_cfg) in enumerate(prepared_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # stop 后没有启动的账号只更新内存态，不写 failed.txt，避免用户主动停止时污染失败文件。
    if cancel and cancel.is_set():
        now = time.time()
        for i, task in enumerate(tasks):
            if i in completed_task_ids or task.status != "pending":
                continue
            with task._lock:
                task.status = "failed"
                task.error = "已取消（未开始）"
                task.finished_at = now
            if on_progress:
                task.publish_update()
                on_progress(task)

    return tasks


def config_from_dict_full(cfg_dict: dict[str, Any]) -> tuple[OrchestratorConfig, int]:
    """从 config dict 构造 OrchestratorConfig + concurrency。"""
    runtime_config = validate_runtime_config(cfg_dict)
    validated = runtime_config.model_dump()
    _validate_mail_config(validated)
    arkose_cfg = config_from_dict(validated)
    oc = OrchestratorConfig(
        flow_mode=runtime_config.flow_mode,
        impersonate=runtime_config.impersonate,
        proxy_template=runtime_config.proxy_template or None,
        proxy=runtime_config.proxy or None,
        arkose_config=arkose_cfg,
        mail_provider=runtime_config.mail_provider,
        mail_request_interval=runtime_config.mail_request_interval,
        mail_poll_interval=runtime_config.mail_poll_interval,
        mail_poll_timeout=runtime_config.mail_poll_timeout,
        mail_fast_path=runtime_config.mail_fast_path,
        send_settle_delay=runtime_config.send_settle_delay,
        resolve_exit_ip=runtime_config.resolve_exit_ip,
        retry_max=runtime_config.retry_max,
        auto_send=runtime_config.auto_send,
        output_file=runtime_config.output_file,
        failed_file=runtime_config.failed_file,
        partial_file=runtime_config.partial_file,
        kyc_pass_file=runtime_config.kyc_pass_file,
        kyc_required_file=runtime_config.kyc_required_file,
        kyc_unknown_file=runtime_config.kyc_unknown_file,
        kyc_dead_file=runtime_config.kyc_dead_file,
        workers=runtime_config.workers,
        worker_interval_seconds=(
            runtime_config.worker_interval_seconds
            if "worker_interval_seconds" in cfg_dict
            else runtime_config.interval_seconds
        ),
    )
    oc.mail_limiter = FifoRateLimiter(max(0.0, oc.mail_request_interval))
    concurrency = len(oc.workers) if oc.workers else runtime_config.concurrency
    return oc, concurrency


# ── headless CLI ────────────────────────────────────────────────────────────
def main() -> int:
    try:
        require_external_confirmation()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    print("Authorized manual batch run confirmed.")

    cfg_dict = json.load(open("runtime/config.json", encoding="utf-8"))
    accounts_file = cfg_dict.get("accounts_file", "runtime/accounts.txt")
    try:
        accounts = parse_accounts(open(accounts_file, encoding="utf-8").read())
    except FileNotFoundError:
        print(f"账号文件 {accounts_file} 不存在（格式：email----password----display_name）")
        return 2
    if not accounts:
        print(f"{accounts_file} 里没有有效账号")
        return 2
    print(f"共 {len(accounts)} 个账号，开始批量注册…")

    oc, concurrency = config_from_dict_full(cfg_dict)

    def on_progress(t: AccountTask) -> None:
        log.info("[task] stage=%s status=%s error_class=%s", t.stage, t.status, t.error_class)

    tasks = orchestrate(accounts, oc, concurrency=concurrency, on_progress=on_progress)
    ok = sum(1 for t in tasks if t.status == "success")
    print(f"\n=== 完成：{ok}/{len(tasks)} 成功；结果已按配置写入 ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
