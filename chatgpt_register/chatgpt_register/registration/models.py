"""Canonical single-account registration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Account:
    email: str
    mail_password: str


@dataclass(frozen=True)
class RegistrationConfig:
    proxy: str = ""
    country_code: str = "US"
    impersonate: str = "chrome136"
    mail_api_base: str = "http://127.0.0.1:8787"
    mail_app_token: str = ""


@dataclass
class RegistrationResult:
    email: str
    status: str
    created: bool = False
    access_token: str = ""
    expires: str = ""
    session_data: dict[str, Any] = field(default_factory=dict)
    error_class: str = ""
    error: str = ""

    def private_record(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "status": self.status,
            "created": self.created,
            "access_token": self.access_token,
            "expires": self.expires,
            "session_data": self.session_data,
            "error_class": self.error_class,
            "error": self.error,
        }

