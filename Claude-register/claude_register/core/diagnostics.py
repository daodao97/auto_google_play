"""Stable, non-sensitive labels for logs, UI snapshots, and result metadata."""

from __future__ import annotations

import re

_HTTP_RE = re.compile(r"\bhttp(?:\s+status)?\s*[:=]?\s*(\d{3})\b", re.IGNORECASE)
_ONBOARDING_RE = re.compile(r"onboarding\s+失败:\s*([a-z0-9_, -]+)", re.IGNORECASE)
_ONBOARDING_STEP_NAMES = {
    "start_onboarding",
    "privacy_consents",
    "accept_legal_docs",
    "email_consent",
    "age_verified",
    "grove",
    "set_name",
    "first_chat",
    "account_profile",
    "finish_onboarding",
}


def safe_error_label(error: BaseException) -> str:
    structured_category = getattr(error, "category", None)
    if isinstance(structured_category, str) and re.fullmatch(
        r"[a-z0-9][a-z0-9_:,.-]{0,119}", structured_category
    ):
        return structured_category
    message = str(error)
    lowered = message.lower()

    http_match = _HTTP_RE.search(message)
    if http_match:
        return f"http_{http_match.group(1)}"

    onboarding_match = _ONBOARDING_RE.search(message)
    if onboarding_match:
        steps = [
            step
            for step in re.findall(r"[a-z0-9_]+", onboarding_match.group(1).lower())
            if step in _ONBOARDING_STEP_NAMES
        ]
        if steps:
            return f"onboarding_failed:{','.join(steps[:8])}"
        return "onboarding_failed"

    if "已取消" in message or "cancel" in lowered:
        return "cancelled"
    if "timeout" in lowered or "timed out" in lowered or "curl: (28)" in lowered:
        return "timeout"
    if "certificate" in lowered:
        return "certificate_error"
    if "proxy authentication" in lowered or "proxy auth" in lowered:
        return "proxy_auth_error"
    if any(term in lowered for term in ("connection", "network", "curl: (35)", "curl: (56)")):
        return "network_error"
    if "ssl" in lowered:
        return "tls_error"
    if "邮箱登录失败" in message or type(error).__name__ == "MailFetcherFatalError":
        return "mail_fatal"
    if "arkose" in lowered and any(term in lowered for term in ("无配置", "无法解析", "未提供")):
        return "configuration_error"
    if "sessionkey" in lowered:
        return "missing_session_key"
    return type(error).__name__
