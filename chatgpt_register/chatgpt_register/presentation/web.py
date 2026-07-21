"""Loopback-only Web console for one active registration run."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from chatgpt_register.orchestration.models import OrchestratorConfig
from chatgpt_register.orchestration.service import orchestrate, parse_accounts


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
STATIC_DIR = HERE / "static"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: str = Field(min_length=3, max_length=100_000)
    proxy: str = Field(default="", max_length=2_048)
    proxy_template: str = Field(default="", max_length=2_048)
    country_code: str = Field(default="US", min_length=2, max_length=2)
    impersonate: str = Field(default="chrome136", max_length=32)
    concurrency: int = Field(default=1, ge=1, le=3)
    retry_max: int = Field(default=0, ge=0, le=1)


app = FastAPI(title="ChatGPT Register", docs_url=None, redoc_url=None)
_lock = threading.Lock()
_cancel: threading.Event | None = None
_worker: threading.Thread | None = None
_state: dict[str, Any] = {
    "running": False,
    "run_id": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "tasks": [],
    "summary": {"success": 0, "partial": 0, "failed": 0},
    "error": "",
}


def _snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "running": bool(_state["running"]),
            "run_id": str(_state["run_id"]),
            "started_at": float(_state["started_at"]),
            "finished_at": float(_state["finished_at"]),
            "tasks": [dict(item) for item in _state["tasks"]],
            "summary": dict(_state["summary"]),
            "error": str(_state["error"]),
        }


def _mail_app_token() -> str:
    return os.environ.get("MAILCOM_APP_TOKEN", "").strip()


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client not in LOOPBACK:
        return JSONResponse({"detail": "loopback access only"}, status_code=403, headers=NO_STORE_HEADERS)
    response = await call_next(request)
    response.headers.update(NO_STORE_HEADERS)
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE_HEADERS)


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript", headers=NO_STORE_HEADERS)


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css", headers=NO_STORE_HEADERS)


@app.get("/api/state")
def state() -> dict[str, Any]:
    return _snapshot()


@app.post("/api/run", status_code=202)
def start_run(payload: RunRequest) -> dict[str, Any]:
    global _cancel, _worker
    token = _mail_app_token()
    if not token:
        return JSONResponse({"detail": "MAILCOM_APP_TOKEN 未配置"}, status_code=503)
    try:
        accounts = parse_accounts(payload.accounts)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    if not accounts:
        return JSONResponse({"detail": "没有有效账号"}, status_code=422)

    with _lock:
        if _state["running"]:
            return JSONResponse({"detail": "已有任务正在运行"}, status_code=409)
        _cancel = threading.Event()
        _state.update(
            running=True,
            run_id="",
            started_at=time.time(),
            finished_at=0.0,
            tasks=[
                {
                    "task_id": "",
                    "email": account.email,
                    "status": "pending",
                    "stage": "",
                    "attempts": 0,
                    "created": False,
                    "error_class": "",
                    "has_session": False,
                    "elapsed": 0,
                }
                for account in accounts
            ],
            summary={"success": 0, "partial": 0, "failed": 0},
            error="",
        )
        cancel = _cancel

    config = OrchestratorConfig(
        output_root=PROJECT_ROOT / "runtime" / "runs",
        concurrency=payload.concurrency,
        retry_max=payload.retry_max,
        proxy=payload.proxy.strip(),
        proxy_template=payload.proxy_template.strip(),
        country_code=payload.country_code.upper(),
        impersonate=payload.impersonate.strip(),
        mail_api_base=os.environ.get("MAILCOM_API_BASE", "http://127.0.0.1:8787"),
        mail_app_token=token,
    )

    def progress(snapshot: dict[str, Any]) -> None:
        with _lock:
            tasks = _state["tasks"]
            for index, existing in enumerate(tasks):
                if existing.get("email") == snapshot.get("email"):
                    tasks[index] = dict(snapshot)
                    break

    def execute() -> None:
        try:
            run_id, tasks = orchestrate(accounts, config, cancelled=cancel, on_progress=progress)
            summary = {
                "success": sum(task.status == "success" for task in tasks),
                "partial": sum(task.status == "partial" for task in tasks),
                "failed": sum(task.status == "failed" for task in tasks),
            }
            with _lock:
                _state["run_id"] = run_id
                _state["tasks"] = [task.public_dict() for task in tasks]
                _state["summary"] = summary
        except Exception as error:
            with _lock:
                _state["error"] = type(error).__name__
        finally:
            with _lock:
                _state["running"] = False
                _state["finished_at"] = time.time()

    _worker = threading.Thread(target=execute, name="web-register-run", daemon=True)
    _worker.start()
    return {"accepted": True, "count": len(accounts)}


@app.post("/api/stop")
def stop_run() -> dict[str, bool]:
    with _lock:
        cancel = _cancel
        running = bool(_state["running"])
    if cancel and running:
        cancel.set()
    return {"stopping": bool(cancel and running)}


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True, "mail_token_configured": bool(_mail_app_token())}

