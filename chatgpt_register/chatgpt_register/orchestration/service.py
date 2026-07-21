"""Fixed-worker batch orchestration for ChatGPT registrations."""

from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chatgpt_register.orchestration.models import AccountTask, OrchestratorConfig
from chatgpt_register.orchestration.persistence import ResultWriter
from chatgpt_register.registration.models import Account, RegistrationConfig
from chatgpt_register.registration.service import register_account


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_accounts(text: str) -> list[Account]:
    accounts: list[Account] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----", 1)]
        if len(parts) != 2 or not EMAIL_RE.fullmatch(parts[0]) or not parts[1]:
            raise ValueError("invalid account line; expected email----mail_password")
        key = parts[0].casefold()
        if key in seen:
            continue
        seen.add(key)
        accounts.append(Account(email=parts[0], mail_password=parts[1]))
    return accounts


def _materialize_proxy(config: OrchestratorConfig) -> str:
    if config.proxy_template:
        return config.proxy_template.replace("{session}", uuid.uuid4().hex[:16])
    return config.proxy


def _retryable(error_class: str) -> bool:
    text = error_class.casefold()
    return any(word in text for word in ("timeout", "connection", "network", "temporary"))


def orchestrate(
    accounts: list[Account],
    config: OrchestratorConfig,
    *,
    cancelled: threading.Event | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    register: Callable[..., Any] = register_account,
    run_id: str | None = None,
) -> tuple[str, list[AccountTask]]:
    config.validate()
    cancel = cancelled or threading.Event()
    active_run_id = run_id or f"run_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
    writer = ResultWriter(Path(config.output_root) / active_run_id)
    tasks = [AccountTask(account=account) for account in accounts]
    work: queue.Queue[AccountTask] = queue.Queue()
    for task in tasks:
        work.put(task)

    def emit(task: AccountTask) -> None:
        if on_progress:
            on_progress(task.public_dict())

    def worker() -> None:
        while not cancel.is_set():
            try:
                task = work.get_nowait()
            except queue.Empty:
                return
            task.started_at = time.time()
            task.status = "running"
            emit(task)
            proxy = _materialize_proxy(config)
            for attempt in range(1, config.retry_max + 2):
                task.attempts = attempt

                def progress(message: str) -> None:
                    if message.startswith("stage="):
                        task.stage = message.partition("=")[2]
                        emit(task)

                task.result = register(
                    task.account,
                    RegistrationConfig(
                        proxy=proxy,
                        country_code=config.country_code,
                        impersonate=config.impersonate,
                        mail_api_base=config.mail_api_base,
                        mail_app_token=config.mail_app_token,
                    ),
                    cancelled=cancel.is_set,
                    log=progress,
                )
                if task.result.status != "failed":
                    break
                if attempt > config.retry_max or not _retryable(task.result.error_class):
                    break
            task.status = task.result.status
            task.finished_at = time.time()
            writer.write(task.result)
            emit(task)
            work.task_done()

    workers = [threading.Thread(target=worker, name=f"register-W{i + 1}") for i in range(config.concurrency)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    return active_run_id, tasks

