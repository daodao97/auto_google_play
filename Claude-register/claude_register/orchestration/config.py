"""Validated runtime configuration with compatibility-preserving extra fields."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


log = logging.getLogger("orchestrator.config")
INACTIVE_OPTIONS = {"account_timeout", "kyc_timeout", "persistence_journal_file"}


class RuntimeWorkerConfig(BaseModel):
    """Validated per-worker overrides with compatibility fields retained."""

    model_config = ConfigDict(extra="allow")

    flow_mode: Literal["register", "session"] = "register"
    impersonate: str | None = None
    proxy: str | None = None
    proxy_template: str | None = None
    mail_provider: Literal["mailcom", "imap", "microsoft"] = "mailcom"
    mail_poll_interval: float = Field(default=3.0, ge=0)
    mail_poll_timeout: float = Field(default=180.0, gt=0)
    mail_fast_path: bool = False
    send_settle_delay: float | None = Field(default=None, ge=0, le=30)
    resolve_exit_ip: bool = False
    retry_max: int = Field(default=2, ge=0, le=5)
    auto_send: bool = True
    interval_seconds: float = Field(default=0.0, ge=0)

    @field_validator("flow_mode", "mail_provider", mode="before")
    @classmethod
    def normalize_enum_text(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class RuntimeConfig(BaseModel):
    """Strict known settings while retaining legacy extension fields."""

    model_config = ConfigDict(extra="allow")

    flow_mode: Literal["register", "session"] = "register"
    impersonate: str | None = "chrome142"
    proxy: str | None = None
    proxy_template: str | None = None
    concurrency: int = Field(default=2, ge=1, le=10)
    retry_max: int = Field(default=2, ge=0, le=5)
    auto_send: bool = True
    mail_provider: Literal["mailcom", "imap", "microsoft"] = "mailcom"
    mail_request_interval: float = Field(default=7.5, ge=0)
    mail_poll_interval: float = Field(default=3.0, ge=0)
    mail_poll_timeout: float = Field(default=180.0, gt=0)
    mail_fast_path: bool = False
    send_settle_delay: float | None = Field(default=None, ge=0, le=30)
    resolve_exit_ip: bool = False
    account_timeout: float | None = Field(default=None, gt=0)
    kyc_timeout: float = Field(default=15.0, gt=0)
    persistence_journal_file: str | None = None
    output_file: str = "runtime/results.txt"
    failed_file: str = "runtime/failed.txt"
    partial_file: str = "runtime/partial.txt"
    kyc_pass_file: str = "runtime/kyc_pass.txt"
    kyc_required_file: str = "runtime/kyc_required.txt"
    kyc_unknown_file: str = "runtime/kyc_unknown.txt"
    kyc_dead_file: str = "runtime/kyc_dead.txt"
    workers: list[dict[str, Any]] = Field(default_factory=list)
    worker_interval_seconds: float = Field(default=0.0, ge=0)
    interval_seconds: float = Field(default=0.0, ge=0)
    accounts_file: str = "runtime/accounts.txt"
    arkose: dict[str, Any] | None = Field(default_factory=dict)
    email: str = ""
    password: str = ""
    display_name: str = ""

    @field_validator("flow_mode", "mail_provider", mode="before")
    @classmethod
    def normalize_enum_text(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("workers", mode="before")
    @classmethod
    def validate_worker_overrides(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [
            RuntimeWorkerConfig.model_validate(worker).model_dump(exclude_unset=True)
            for worker in value
        ]


def validate_runtime_config(config: dict[str, Any]) -> RuntimeConfig:
    """Validate known fields and warn about names, never values, of compatibility fields."""
    validated = RuntimeConfig.model_validate(config)
    for key in sorted((validated.model_extra or {}).keys()):
        log.warning("unknown runtime option retained key=%s", key)
    known_worker_options = set(RuntimeWorkerConfig.model_fields)
    for index, worker in enumerate(validated.workers):
        for key in sorted(set(worker).difference(known_worker_options)):
            log.warning("unknown worker runtime option retained index=%d key=%s", index, key)
    for key in sorted(INACTIVE_OPTIONS.intersection(config)):
        log.warning("runtime option retained but inactive key=%s", key)
    return validated
