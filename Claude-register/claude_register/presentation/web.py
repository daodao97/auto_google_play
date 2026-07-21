"""Claude 批量注册 Web 仪表盘。

启动：
    uvicorn claude_register.presentation.web:app --host 127.0.0.1 --port 8000
浏览器开 http://127.0.0.1:8000

内存态配合本地恢复记录。SSE 首次发送安全快照，之后只发送公开增量事件。
⚠️ 自动化批量注册违反 Anthropic ToS，仅供学习研究。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote_plus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.types import ASGIApp, Receive, Scope, Send

from claude_register.orchestration import service as orchestrator
from claude_register.orchestration.recovery import (
    RECOVERY_FILENAME,
    RunStateStore,
    VerifiedRecoveryStore,
)
from claude_register.orchestration.events import RunEventBus, RunSummaryTracker
from claude_register.auth.service import load_config
from claude_register.core.browser import redact_proxy_url

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
STATIC_DIR = HERE / "static"
RUN_OUTPUTS = {
    "output_file": "results.txt",
    "failed_file": "failed.txt",
    "partial_file": "partial.txt",
    "kyc_pass_file": "kyc_pass.txt",
    "kyc_required_file": "kyc_required.txt",
    "kyc_unknown_file": "kyc_unknown.txt",
    "kyc_dead_file": "kyc_dead.txt",
}
RUN_ID_RE = re.compile(r"run_\d+_[0-9a-f]{8}")

# ── 运行态（内存）──
_run_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "starting": False,
    "tasks": [],
    "started_at": 0.0,
    "run_id": "",
    "flow_mode": "register",
}
_cancel: threading.Event | None = None
_worker_thread: threading.Thread | None = None
_event_bus = RunEventBus()
_summary_tracker = RunSummaryTracker()
SSE_HEARTBEAT_SECONDS = 15.0
FLOW_MODES = {"register", "session"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
ACCESS_TOKEN_ENV = "WEBUI_TOKEN"
ACCESS_TOKEN_COOKIE = "webui_token"
ACCESS_TOKEN_SCOPE_KEY = "claude_register.query_access_token"
log = logging.getLogger("webui")
SENSITIVE_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; "
        "script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def _new_run_id() -> str:
    return f"run_{time.time_ns()}_{uuid.uuid4().hex[:8]}"


def _configure_run_outputs(config: dict[str, Any], run_id: str) -> None:
    run_dir = PROJECT_ROOT / "runtime" / "runs" / run_id
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    run_dir.chmod(0o700)
    for config_key, filename in RUN_OUTPUTS.items():
        config[config_key] = str(run_dir.relative_to(PROJECT_ROOT) / filename)


def _run_output_path(run_id: str, filename: str) -> Path | None:
    if not RUN_ID_RE.fullmatch(run_id) or filename not in RUN_OUTPUTS.values():
        return None
    return PROJECT_ROOT / "runtime" / "runs" / run_id / filename


def _is_loopback_host(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _request_allowed(
    _path: str,
    host: str,
    _cfg: dict[str, Any],
    request_host: str | None = None,
) -> bool:
    if not _is_loopback_host(host):
        return False
    return request_host is None or _is_loopback_host(request_host)


def _configured_access_token() -> str:
    """Read the public Web UI token from runtime environment only."""
    return os.environ.get(ACCESS_TOKEN_ENV, "").strip()


def _decode_query_component(value: bytes) -> str:
    return unquote_plus(value.decode("utf-8", errors="replace"))


def _redact_access_token_query(scope: Scope) -> str:
    """Remove query access tokens in place before server access logging."""
    raw_query = scope.get("query_string", b"")
    if not raw_query:
        return ""

    query_token = ""
    retained: list[bytes] = []
    found = False
    for field in raw_query.split(b"&"):
        key, separator, value = field.partition(b"=")
        if _decode_query_component(key) == "token":
            found = True
            query_token = _decode_query_component(value if separator else b"")
            continue
        retained.append(field)

    if found:
        scope["query_string"] = b"&".join(retained)
        scope[ACCESS_TOKEN_SCOPE_KEY] = query_token
    return query_token


class AccessTokenQueryRedactionMiddleware:
    """Keep query-token login compatible without exposing tokens to access logs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        _redact_access_token_query(scope)
        try:
            await self.app(scope, receive, send)
        finally:
            scope.pop(ACCESS_TOKEN_SCOPE_KEY, None)


def _request_has_valid_access_token(request: Request, expected: str) -> bool:
    if not expected:
        return False

    candidates = [
        str(request.scope.get(ACCESS_TOKEN_SCOPE_KEY, "")),
        request.query_params.get("token", ""),
        request.cookies.get(ACCESS_TOKEN_COOKIE, ""),
    ]
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() == "bearer":
        candidates.append(credential.strip())

    return any(candidate and hmac.compare_digest(candidate, expected) for candidate in candidates)


def _login_response() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>访问验证</title></head><body><main>
<h1>访问验证</h1><form method="get" action="/">
<label for="token">访问令牌</label><input id="token" name="token" type="password"
autocomplete="current-password" required autofocus><button type="submit">进入控制台</button>
</form></main></body></html>""",
        status_code=401,
        headers=SENSITIVE_RESPONSE_HEADERS,
    )


def _cancel_current_run(wait_seconds: float = 0) -> bool:
    """请求当前批量任务停止；wait_seconds>0 时等待后台线程短暂收尾。"""
    global _cancel, _worker_thread
    with _run_lock:
        cancel = _cancel
        worker = _worker_thread
    if cancel:
        cancel.set()
    if worker and worker.is_alive() and wait_seconds > 0:
        worker.join(wait_seconds)
    stopped = not (worker and worker.is_alive())
    if stopped:
        with _run_lock:
            if _worker_thread is worker:
                _worker_thread = None
            if _cancel is cancel:
                _cancel = None
            _state["running"] = False
    return stopped


def _recover_incomplete_runs() -> int:
    """Export private verified checkpoints left by an interrupted process."""
    runs_root = PROJECT_ROOT / "runtime" / "runs"
    if not runs_root.is_dir():
        return 0
    recovered = 0
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir() or not RUN_ID_RE.fullmatch(run_dir.name):
            continue
        if not (run_dir / RECOVERY_FILENAME).is_file():
            continue
        try:
            store = VerifiedRecoveryStore(run_dir)
            recovered += store.recover_pending()
            store.cleanup_exported()
        except Exception as exc:
            log.error("[recovery] startup_failed error_class=%s", type(exc).__name__)
    return recovered


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        _recover_incomplete_runs()
        yield
    finally:
        _cancel_current_run(wait_seconds=5)


app = FastAPI(title="Claude 批量注册控制台", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.exception_handler(RequestValidationError)
async def redacted_validation_error(
    _request: Request | None,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return validation metadata without echoing submitted request values."""
    errors = [
        {
            "type": item.get("type", "validation_error"),
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg", "Invalid input"),
        }
        for item in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.middleware("http")
async def restrict_access(request: Request, call_next):
    host = request.client.host if request.client else ""
    local_request = _request_allowed(
        request.url.path,
        host,
        {},
        request_host=request.url.hostname or "",
    )
    access_token = _configured_access_token()
    if not local_request and not _request_has_valid_access_token(request, access_token):
        return _login_response()

    query_access_token = str(request.scope.get(ACCESS_TOKEN_SCOPE_KEY, ""))
    if not local_request and (query_access_token or request.query_params.get("token")):
        response = RedirectResponse(
            str(request.url.remove_query_params("token")),
            status_code=303,
            headers=SENSITIVE_RESPONSE_HEADERS,
        )
        response.set_cookie(
            ACCESS_TOKEN_COOKIE,
            access_token,
            max_age=86400,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    response = await call_next(request)
    for name, value in SENSITIVE_RESPONSE_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


app.add_middleware(AccessTokenQueryRedactionMiddleware)


class StartReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_mode: Literal["register", "session"] = "register"
    proxy_mode: Literal["configured", "override", "direct"] = "configured"
    proxy_template: str | None = None
    impersonate: str = ""
    concurrency: int = Field(default=2, ge=1, le=10)
    retry_max: int = Field(default=2, ge=0, le=5)
    auto_send: bool = True
    mail_provider: Literal["mailcom", "imap", "microsoft"] = "mailcom"
    mail_poll_interval: float = Field(default=3, ge=0)
    mail_fast_path: bool = False
    send_settle_delay: float | None = Field(default=None, ge=0, le=30)
    resolve_exit_ip: bool = False
    accounts_text: str = Field(default="")

    @model_validator(mode="after")
    def validate_proxy_override(self):
        if self.proxy_mode == "override" and not (self.proxy_template or "").strip():
            raise ValueError("proxy_template is required for override mode")
        return self


def _apply_proxy_mode(config: dict[str, Any], req: StartReq) -> None:
    """Apply an explicit per-run proxy choice without exposing server credentials."""
    if req.proxy_mode == "configured":
        return
    if req.proxy_mode == "override":
        config["proxy_template"] = req.proxy_template
        config["proxy"] = None
        workers = config.get("workers")
        if isinstance(workers, list):
            for worker in workers:
                if isinstance(worker, dict):
                    worker["proxy_template"] = req.proxy_template
                    worker["proxy"] = None
        return

    config["proxy_template"] = None
    config["proxy"] = None
    workers = config.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if isinstance(worker, dict):
                worker["proxy"] = None
                worker["proxy_template"] = None


def _configured_proxy_value(config: dict[str, Any]) -> str | None:
    """Return one configured proxy value for a redacted UI preview."""
    configured = config.get("proxy_template") or config.get("proxy")
    if configured:
        return str(configured)
    workers = config.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            configured = worker.get("proxy_template") or worker.get("proxy")
            if configured:
                return str(configured)
    return None


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict:
    cfg = load_config()
    configured_proxy = _configured_proxy_value(cfg)
    return {
        "proxy_configured": bool(configured_proxy),
        "proxy_preview": redact_proxy_url(configured_proxy),
        "impersonate": cfg.get("impersonate", "chrome142"),
        "concurrency": cfg.get("concurrency", 2),
        "retry_max": cfg.get("retry_max", 2),
        "auto_send": cfg.get("auto_send", True),
        "flow_mode": cfg.get("flow_mode", "register"),
        "mail_provider": cfg.get("mail_provider", "mailcom"),
        "mail_poll_interval": cfg.get("mail_poll_interval", 3),
        "mail_fast_path": cfg.get("mail_fast_path", False),
        "send_settle_delay": cfg.get("send_settle_delay"),
        "resolve_exit_ip": cfg.get("resolve_exit_ip", False),
    }


def _snapshot() -> dict:
    with _run_lock:
        running = bool(_state["running"])
        run_id = str(_state["run_id"])
        flow_mode = str(_state.get("flow_mode", "register"))
        task_refs = list(_state["tasks"])
    tasks = [task.to_public_dict() for task in task_refs]
    summary = {
        "total": len(tasks),
        "success": sum(1 for t in tasks if t["status"] == "success"),
        "partial": sum(1 for t in tasks if t["status"] == "partial"),
        "failed": sum(1 for t in tasks if t["status"] == "failed"),
        "running": sum(1 for t in tasks if t["status"] == "running"),
        "pending": sum(1 for t in tasks if t["status"] == "pending"),
        "kyc_pass": sum(1 for t in tasks if t["kyc_status"] in ("not_required", "approved")),
        "kyc_required": sum(1 for t in tasks if t["kyc_status"] in ("pending", "denied")),
        "kyc_dead": sum(
            1
            for t in tasks
            if t["has_session"]
            and t["status"] in ("success", "partial")
            and t["kyc_status"] == "dead"
        ),
        "kyc_unknown": sum(
            1
            for t in tasks
            if t["has_session"]
            and t["status"] in ("success", "partial")
            and t["kyc_status"] not in ("not_required", "approved", "pending", "denied", "dead")
        ),
    }
    return {
        "running": running,
        "run_id": run_id,
        "flow_mode": flow_mode,
        "tasks": tasks,
        "summary": summary,
    }


def _summary_event_payload(summary: dict[str, int] | None = None) -> dict[str, Any]:
    if summary is None:
        snapshot = _snapshot()
        return {
            "running": snapshot["running"],
            "run_id": snapshot["run_id"],
            "flow_mode": snapshot["flow_mode"],
            "summary": snapshot["summary"],
        }
    with _run_lock:
        return {
            "running": bool(_state["running"]),
            "run_id": str(_state["run_id"]),
            "flow_mode": str(_state.get("flow_mode", "register")),
            "summary": summary,
        }


def _publish_task_update(task: orchestrator.AccountTask) -> None:
    """Publish one public task delta followed by its public summary."""
    public_task = task.to_public_dict()
    with _run_lock:
        run_id = str(_state["run_id"])
    _event_bus.publish(
        "task_updated",
        {"run_id": run_id, "task": public_task},
    )
    summary = _summary_tracker.update(public_task)
    _event_bus.publish("summary_updated", _summary_event_payload(summary))


def _format_sse_event(
    event_type: str,
    data: dict[str, Any],
    *,
    event_id: int | None = None,
) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def _parse_last_event_id(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


async def _progress_events(last_event_id: int | None):
    cursor = last_event_id
    if cursor is None:
        cursor = _event_bus.cursor
        yield "retry: 2000\n" + _format_sse_event(
            "run_started",
            _snapshot(),
            event_id=cursor,
        )
    else:
        replay = _event_bus.events_after(cursor)
        if replay is None:
            cursor = _event_bus.cursor
            yield _format_sse_event("run_started", _snapshot(), event_id=cursor)
        else:
            for event in replay:
                cursor = event.event_id
                yield _format_sse_event(
                    event.event_type,
                    event.data,
                    event_id=event.event_id,
                )

    while True:
        replay = await asyncio.to_thread(
            _event_bus.wait_after,
            cursor,
            SSE_HEARTBEAT_SECONDS,
        )
        if replay is None:
            cursor = _event_bus.cursor
            yield _format_sse_event("run_started", _snapshot(), event_id=cursor)
            continue
        if replay:
            for event in replay:
                cursor = event.event_id
                yield _format_sse_event(
                    event.event_type,
                    event.data,
                    event_id=event.event_id,
                )
            continue
        with _run_lock:
            run_id = str(_state["run_id"])
        yield _format_sse_event("heartbeat", {"run_id": run_id})


@app.get("/api/current-run")
def current_run() -> dict[str, Any]:
    """Return only the metadata needed to reattach a refreshed page."""
    with _run_lock:
        return {
            "running": bool(_state["running"]),
            "run_id": str(_state["run_id"]),
            "flow_mode": str(_state.get("flow_mode", "register")),
            "started_at": float(_state.get("started_at", 0.0)),
        }


@app.get("/api/status")
def run_status() -> dict[str, Any]:
    """Return the current public task snapshot for trusted API integrations."""
    return _snapshot()


@app.post("/api/start")
def start(req: StartReq) -> dict:
    parse_report = orchestrator.parse_accounts_with_report(
        req.accounts_text,
        req.mail_provider,
    )
    accounts = parse_report.accounts
    parse_issues = [
        {
            "line_number": issue.line_number,
            "code": issue.code,
            "message": issue.message,
        }
        for issue in parse_report.issues
    ]
    if not accounts:
        return {
            "ok": False,
            "error": "账号列表中没有可运行账号",
            "ignored_count": len(parse_issues),
            "issues": parse_issues,
        }

    with _run_lock:
        if _state["running"] or _state["starting"]:
            return {"ok": False, "error": "已有运行中的任务，请先停止"}
        _state["starting"] = True

    try:
        return _start_reserved(req, accounts, parse_issues)
    finally:
        with _run_lock:
            _state["starting"] = False


def _start_reserved(
    req: StartReq,
    accounts: list[orchestrator.Account],
    parse_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Initialize one run after the caller atomically reserves the start slot."""
    global _cancel, _worker_thread

    # 构造 config dict（UI 入参覆盖本地运行配置）
    cfg_dict = load_config()
    _apply_proxy_mode(cfg_dict, req)
    if req.impersonate:
        cfg_dict["impersonate"] = req.impersonate
    cfg_dict["concurrency"] = req.concurrency
    cfg_dict["retry_max"] = req.retry_max
    cfg_dict["auto_send"] = req.auto_send
    cfg_dict["flow_mode"] = req.flow_mode
    cfg_dict["mail_provider"] = req.mail_provider or "mailcom"
    cfg_dict["mail_poll_interval"] = req.mail_poll_interval
    cfg_dict["mail_fast_path"] = req.mail_fast_path
    cfg_dict["send_settle_delay"] = req.send_settle_delay
    cfg_dict["resolve_exit_ip"] = req.resolve_exit_ip
    run_id = _new_run_id()
    try:
        _configure_run_outputs(cfg_dict, run_id)
    except OSError:
        return {"ok": False, "error": "创建本次运行归档失败"}

    oc, concurrency = orchestrator.config_from_dict_full(cfg_dict)
    run_dir = PROJECT_ROOT / "runtime" / "runs" / run_id
    try:
        oc.state_store = RunStateStore(run_dir, run_id)
        oc.recovery_store = VerifiedRecoveryStore(run_dir)
    except Exception as exc:
        log.error("[state] initialize_failed error_class=%s", type(exc).__name__)
        return {"ok": False, "error": "初始化本次运行状态失败"}

    # 预建 tasks（webui 持有引用，register_one 原地更新，SSE 实时读到阶段变化）
    tasks = [orchestrator.AccountTask(account=a) for a in accounts]
    cancel_event = threading.Event()
    with _run_lock:
        _cancel = cancel_event
        _state["tasks"] = tasks
        _state["running"] = True
        _state["started_at"] = time.time()
        _state["run_id"] = run_id
        _state["flow_mode"] = cfg_dict["flow_mode"]

    _event_bus.reset()
    _summary_tracker.reset([task.to_public_dict() for task in tasks])
    _event_bus.publish("run_started", _snapshot())

    def run():
        global _cancel, _worker_thread
        try:
            orchestrator.orchestrate(
                accounts,
                oc,
                concurrency=concurrency,
                on_progress=_publish_task_update,
                cancel=cancel_event,
                tasks=tasks,
            )
        finally:
            if oc.state_store is not None:
                try:
                    oc.state_store.close()
                except Exception as exc:
                    log.error("[state] close_failed error_class=%s", type(exc).__name__)
            with _run_lock:
                _state["running"] = False
                if _cancel is cancel_event:
                    _cancel = None
                if _worker_thread is threading.current_thread():
                    _worker_thread = None
            _event_bus.publish(
                "run_finished",
                _summary_event_payload(_summary_tracker.snapshot()),
            )

    thread = threading.Thread(target=run, daemon=True)
    with _run_lock:
        _worker_thread = thread
    thread.start()
    return {
        "ok": True,
        "run_id": run_id,
        "count": len(accounts),
        "ignored_count": len(parse_issues),
        "issues": parse_issues,
    }


@app.post("/api/stop")
def stop() -> dict:
    return {"ok": True, "stopped": _cancel_current_run(wait_seconds=0)}


@app.get("/api/progress")
async def progress(request: Request):
    """Stream a safe initial snapshot, replayable task deltas, and heartbeats."""
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    headers = {**SENSITIVE_RESPONSE_HEADERS, "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _progress_events(last_event_id),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/api/results.txt")
def results_file():
    return _download_output_file("output_file", "results.txt")


@app.get("/api/failed.txt")
def failed_file():
    return _download_output_file("failed_file", "failed.txt")


@app.get("/api/partial.txt")
def partial_file():
    return _download_output_file("partial_file", "partial.txt")


@app.get("/api/kyc_pass.txt")
def kyc_pass_file():
    return _download_output_file("kyc_pass_file", "kyc_pass.txt")


@app.get("/api/kyc_required.txt")
def kyc_required_file():
    return _download_output_file("kyc_required_file", "kyc_required.txt")


@app.get("/api/kyc_unknown.txt")
def kyc_unknown_file():
    return _download_output_file("kyc_unknown_file", "kyc_unknown.txt")


@app.get("/api/kyc_dead.txt")
def kyc_dead_file():
    return _download_output_file("kyc_dead_file", "kyc_dead.txt")


@app.get("/api/runs/{run_id}/{filename}")
def run_output_file(run_id: str, filename: str):
    return _download_run_output_file(run_id, filename)


def _download_run_output_file(run_id: str, filename: str):
    path = _run_output_path(run_id, filename)
    if path is None or not path.exists():
        return HTMLResponse("尚无本次运行结果", status_code=404)
    return FileResponse(
        path,
        filename=filename,
        media_type="text/plain",
        headers=SENSITIVE_RESPONSE_HEADERS,
    )


def _download_output_file(config_key: str, default_name: str):
    raw = load_config().get(config_key, default_name) or default_name
    p = Path(raw)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        return HTMLResponse(f"尚无 {default_name}", status_code=404)
    return FileResponse(
        p,
        filename=Path(raw).name or default_name,
        media_type="text/plain",
        headers=SENSITIVE_RESPONSE_HEADERS,
    )
