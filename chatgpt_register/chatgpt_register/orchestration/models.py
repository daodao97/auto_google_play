"""Batch task and configuration models."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chatgpt_register.registration.models import Account, RegistrationResult


@dataclass
class AccountTask:
    account: Account
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"
    stage: str = ""
    attempts: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    result: RegistrationResult | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public_dict(self) -> dict[str, Any]:
        with self._lock:
            result = self.result
            return {
                "task_id": self.task_id,
                "email": self.account.email,
                "status": self.status,
                "stage": self.stage,
                "attempts": self.attempts,
                "created": bool(result and result.created),
                "error_class": result.error_class if result else "",
                "has_session": bool(result and result.access_token),
                "elapsed": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else 0,
            }


@dataclass(frozen=True)
class OrchestratorConfig:
    output_root: Path = Path("runtime/runs")
    concurrency: int = 1
    retry_max: int = 0
    proxy: str = ""
    proxy_template: str = ""
    country_code: str = "US"
    impersonate: str = "chrome136"
    mail_api_base: str = "http://127.0.0.1:8787"
    mail_app_token: str = ""

    def validate(self) -> None:
        if not 1 <= self.concurrency <= 5:
            raise ValueError("concurrency must be between 1 and 5")
        if not 0 <= self.retry_max <= 2:
            raise ValueError("retry_max must be between 0 and 2")
        if not self.mail_app_token:
            raise ValueError("mail_app_token is required")

