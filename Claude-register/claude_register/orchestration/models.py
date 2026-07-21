"""Canonical orchestration models shared by runners, persistence, and Web APIs."""

from __future__ import annotations

import ipaddress
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from claude_register.challenge.arkose import ArkoseConfig
from claude_register.mail.fetcher import FifoRateLimiter


@dataclass
class Account:
    email: str
    password: str
    display_name: str
    mail_client_id: str = ""
    mail_refresh_token: str = ""
    deliver_prefix: str = ""
    full_name: str = ""


@dataclass(frozen=True)
class AccountParseIssue:
    """One non-sensitive account input issue."""

    line_number: int
    code: str
    message: str


@dataclass
class AccountParseReport:
    """Valid accounts plus every rejected input-line issue."""

    accounts: list[Account]
    issues: list[AccountParseIssue]


@dataclass(frozen=True)
class PublicTaskSnapshot:
    """Browser-safe task state with credentials intentionally omitted."""

    task_id: str
    version: int
    email: str
    display_name: str
    status: str
    stage: str
    kyc_status: str
    worker_id: str
    proxy_session: str
    attempts: int
    error_class: str
    retryable: bool
    elapsed: float
    queue_wait_ms: int
    stage_elapsed_ms: int
    stage_durations_ms: dict[str, int]
    substage_durations_ms: dict[str, int]
    proxy_exit_ip: str
    has_session: bool
    persistence_status: str
    persistence_error_class: str

    def to_dict(self) -> dict[str, Any]:
        """Return the public snapshot as a JSON-compatible dictionary."""
        return asdict(self)


@dataclass
class AccountTask:
    account: Account
    status: str = "pending"
    stage: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    proxy_session: str = ""
    proxy_exit_ip: str = ""
    account_uuid: str = ""
    org_uuid: str = ""
    session_key: str = ""
    routing_hint: str = ""
    kyc_status: str = ""
    attempts: int = 0
    worker_id: str = ""
    error_class: str = ""
    retryable: bool = False
    queue_wait_ms: int = 0
    persistence_status: str = "pending"
    persistence_error_class: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    version: int = 0
    _queued_at: float | None = field(default=None, init=False, repr=False, compare=False)
    _stage_started_at: float | None = field(default=None, init=False, repr=False, compare=False)
    _stage_durations_ms: dict[str, int] = field(default_factory=dict, init=False, repr=False, compare=False)
    _substage_durations_ms: dict[str, int] = field(default_factory=dict, init=False, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    @staticmethod
    def _elapsed_ms(started_at: float, finished_at: float) -> int:
        return max(0, int(round((finished_at - started_at) * 1000)))

    def mark_queued(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        with self._lock:
            self._queued_at = now

    def mark_running(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        with self._lock:
            self.status = "running"
            self.started_at = now
            self.queue_wait_ms = self._elapsed_ms(self._queued_at, now) if self._queued_at is not None else 0

    def _complete_current_stage_locked(self, now: float) -> None:
        if self.stage and self._stage_started_at is not None:
            elapsed_ms = self._elapsed_ms(self._stage_started_at, now)
            self._stage_durations_ms[self.stage] = self._stage_durations_ms.get(self.stage, 0) + elapsed_ms
        self._stage_started_at = None

    def set_stage(self, stage: str, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        with self._lock:
            self._complete_current_stage_locked(now)
            self.stage = stage
            self._stage_started_at = now

    def complete_current_stage(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        with self._lock:
            self._complete_current_stage_locked(now)

    def record_substage_duration(self, name: str, elapsed_ms: int) -> None:
        """Accumulate a safe, non-sensitive duration for a named substage."""
        if not name or elapsed_ms < 0:
            return
        with self._lock:
            self._substage_durations_ms[name] = self._substage_durations_ms.get(name, 0) + elapsed_ms

    def publish_update(self) -> int:
        """Advance the public version at an existing progress boundary."""
        with self._lock:
            self.version += 1
            return self.version

    def _timing_snapshot(self, now: float) -> tuple[float, int, int, dict[str, int]]:
        elapsed = 0
        if self.started_at:
            elapsed = (
                round(self.finished_at - self.started_at, 1)
                if self.finished_at
                else round(now - self.started_at, 1)
            )
        stage_elapsed_ms = (
            self._elapsed_ms(self._stage_started_at, now)
            if self._stage_started_at is not None
            else 0
        )
        return elapsed, self.queue_wait_ms, stage_elapsed_ms, dict(self._stage_durations_ms)

    def _substage_snapshot(self) -> dict[str, int]:
        return dict(self._substage_durations_ms)

    @staticmethod
    def _public_exit_ip(value: str) -> str:
        try:
            return str(ipaddress.ip_address(value)) if value else ""
        except ValueError:
            return ""

    def to_public_dict(self) -> dict[str, Any]:
        """Return the allowlisted task state permitted in API and SSE responses."""
        now = time.time()
        with self._lock:
            elapsed, queue_wait_ms, stage_elapsed_ms, stage_durations_ms = self._timing_snapshot(now)
            substage_durations_ms = self._substage_snapshot()
            proxy_exit_ip = self._public_exit_ip(self.proxy_exit_ip)
            proxy_session = "" if not self.proxy_session else (
                "static" if self.proxy_session == "static" else "sticky"
            )
            snapshot = PublicTaskSnapshot(
                task_id=self.task_id,
                version=self.version,
                email=self.account.email,
                display_name=self.account.display_name,
                status=self.status,
                stage=self.stage,
                kyc_status=self.kyc_status,
                worker_id=self.worker_id,
                proxy_session=proxy_session,
                attempts=self.attempts,
                error_class=self.error_class,
                retryable=self.retryable,
                elapsed=elapsed,
                queue_wait_ms=queue_wait_ms,
                stage_elapsed_ms=stage_elapsed_ms,
                stage_durations_ms=stage_durations_ms,
                substage_durations_ms=substage_durations_ms,
                proxy_exit_ip=proxy_exit_ip,
                has_session=bool(self.session_key),
                persistence_status=self.persistence_status,
                persistence_error_class=self.persistence_error_class,
            )
        return snapshot.to_dict()

    def to_internal_dict(self) -> dict[str, Any]:
        """Return the complete in-process task state for trusted Python callers."""
        now = time.time()
        with self._lock:
            status, stage, error = self.status, self.stage, self.error
            proxy_session = self.proxy_session
            proxy_exit_ip = self.proxy_exit_ip
            account_uuid, org_uuid, session_key = self.account_uuid, self.org_uuid, self.session_key
            kyc_status, attempts, worker_id = self.kyc_status, self.attempts, self.worker_id
            error_class, retryable = self.error_class, self.retryable
            persistence_status = self.persistence_status
            persistence_error_class = self.persistence_error_class
            version = self.version
            elapsed, queue_wait_ms, stage_elapsed_ms, stage_durations_ms = self._timing_snapshot(now)
            substage_durations_ms = self._substage_snapshot()
        return {
            "task_id": self.task_id,
            "version": version,
            "email": self.account.email,
            "display_name": self.account.display_name,
            "status": status,
            "stage": stage,
            "error": error,
            "elapsed": elapsed,
            "queue_wait_ms": queue_wait_ms,
            "stage_elapsed_ms": stage_elapsed_ms,
            "stage_durations_ms": stage_durations_ms,
            "substage_durations_ms": substage_durations_ms,
            "proxy_session": proxy_session,
            "proxy_exit_ip": proxy_exit_ip,
            "account_uuid": account_uuid,
            "org_uuid": org_uuid,
            "session_key": session_key,
            "kyc_status": kyc_status,
            "password": self.account.password,
            "mail_client_id": self.account.mail_client_id,
            "mail_refresh_token": self.account.mail_refresh_token,
            "deliver_prefix": self.account.deliver_prefix,
            "attempts": attempts,
            "worker_id": worker_id,
            "error_class": error_class,
            "retryable": retryable,
            "persistence_status": persistence_status,
            "persistence_error_class": persistence_error_class,
        }

    def to_dict(self) -> dict[str, Any]:
        """Compatibility alias for trusted callers of the legacy task snapshot."""
        return self.to_internal_dict()

    def delivery_line(self) -> str:
        """Build the existing credential delivery format without changing field order."""
        deliver_prefix = self.account.deliver_prefix
        if not deliver_prefix:
            if self.account.mail_client_id and self.account.mail_refresh_token:
                deliver_prefix = "----".join(
                    (
                        self.account.email,
                        self.account.password,
                        self.account.mail_client_id,
                        self.account.mail_refresh_token,
                    )
                )
            else:
                deliver_prefix = f"{self.account.email}----{self.account.password}"
        return f"{deliver_prefix}----{self.session_key}"


@dataclass
class OrchestratorConfig:
    flow_mode: str = "register"
    impersonate: str | None = None
    proxy_template: str | None = None
    proxy: str | None = None
    arkose_config: ArkoseConfig | None = None
    mail_provider: str = "mailcom"
    mail_request_interval: float = 7.5
    mail_poll_interval: float = 3.0
    mail_poll_timeout: float = 180.0
    mail_fast_path: bool = False
    send_settle_delay: float | None = None
    resolve_exit_ip: bool = False
    mail_limiter: FifoRateLimiter | None = field(default=None, repr=False, compare=False)
    retry_max: int = 2
    auto_send: bool = True
    output_file: str = "runtime/results.txt"
    failed_file: str = "runtime/failed.txt"
    partial_file: str = "runtime/partial.txt"
    kyc_pass_file: str = "runtime/kyc_pass.txt"
    kyc_required_file: str = "runtime/kyc_required.txt"
    kyc_unknown_file: str = "runtime/kyc_unknown.txt"
    kyc_dead_file: str = "runtime/kyc_dead.txt"
    workers: list[dict[str, Any]] = field(default_factory=list)
    worker_interval_seconds: float = 0.0
    state_store: Any | None = field(default=None, repr=False, compare=False)
    recovery_store: Any | None = field(default=None, repr=False, compare=False)


ProgressCb = Callable[[AccountTask], None]
