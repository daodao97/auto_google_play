"""Single-account application service with an explicit creation boundary."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from chatgpt_register.mail.client import MailComOtpClient
from chatgpt_register.protocol.client import ChatGPTRegistrationClient
from chatgpt_register.registration.models import Account, RegistrationConfig, RegistrationResult


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_error_class(error: Exception) -> str:
    name = type(error).__name__
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", name)[:80] or "RegistrationError"


def _safe_error_message(error: Exception, *secrets: str) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    message = re.sub(r"(?i)(access_token|refresh_token|password)(?:=|\s+)\S+", r"\1=***", message)
    return message[:300]


def _redacting_logger(log: Callable[[str], None]) -> Callable[[str], None]:
    def emit(message: str) -> None:
        text = re.sub(r"(?i)\bOTP\s*=\s*\d{6}\b", "OTP=******", str(message))
        text = re.sub(r"(?i)\bat=[A-Za-z0-9._-]+", "at=***", text)
        log(text)

    return emit


def register_account(
    account: Account,
    config: RegistrationConfig,
    *,
    cancelled: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
    client_factory: Callable[..., Any] = ChatGPTRegistrationClient,
    mail_factory: Callable[..., Any] = MailComOtpClient,
) -> RegistrationResult:
    """Register or log in one email and return a classified private result."""
    if not EMAIL_RE.fullmatch(account.email.strip()):
        raise ValueError("invalid email")
    if not account.mail_password:
        raise ValueError("mail password is required")
    if cancelled and cancelled():
        return RegistrationResult(account.email, "failed", error_class="Cancelled", error="cancelled")

    raw_log = log or (lambda _message: None)
    safe_log = _redacting_logger(raw_log)
    mail = mail_factory(
        api_base=config.mail_api_base,
        app_token=config.mail_app_token,
        email=account.email,
        password=account.mail_password,
        proxy=config.proxy,
        cancelled=cancelled,
    )
    client = client_factory(
        proxy=config.proxy,
        country_code=config.country_code,
        impersonate=config.impersonate,
    )
    try:
        safe_log("stage=mail_warmup")
        mail.prime_seen(set())
        if cancelled and cancelled():
            return RegistrationResult(account.email, "failed", error_class="Cancelled", error="cancelled")
        response = client.register(email=account.email, mail_client=mail, log=safe_log)
        token = str(response.get("accessToken") or "")
        if not token:
            raise RuntimeError("session_missing_access_token")
        return RegistrationResult(
            email=account.email,
            status="success",
            created=bool(response.get("created")),
            access_token=token,
            expires=str(response.get("expires") or ""),
            session_data=dict(response.get("session_data") or {}),
        )
    except Exception as error:
        created = bool(getattr(client, "created", False))
        return RegistrationResult(
            email=account.email,
            status="partial" if created else "failed",
            created=created,
            error_class=_safe_error_class(error),
            error=_safe_error_message(error, account.email, account.mail_password, config.mail_app_token),
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
