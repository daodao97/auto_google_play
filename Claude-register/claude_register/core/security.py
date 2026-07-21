"""Shared redaction helpers for untrusted external errors and logs."""

from __future__ import annotations

import re
from typing import Any, Iterable


_PROXY_CREDENTIAL_RE = re.compile(
    r"(?i)((?:https?|socks(?:4a?|5h?)?)://)[^\s/@:]+:[^\s/@]+@"
)
_FOUR_PART_PROXY_RE = re.compile(
    r"(?i)(?<![\w.-])"
    r"(?P<host>(?:[a-z0-9-]+\.)*[a-z0-9-]+|(?:\d{1,3}\.){3}\d{1,3})"
    r":(?P<port>\d{2,5}):[^\s:,;/]+:[^\s,;/]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_BASIC_AUTH_RE = re.compile(r"(?i)(\bAuthorization\s*:\s*Basic\s+)[A-Za-z0-9+/=]+")
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(Set-Cookie|Cookie)\s*:\s*[^\r\n]+")
_MAGIC_LINK_RE = re.compile(r"https?://claude\.ai/magic-link#[^\s\"'<>]+", re.IGNORECASE)
_SESSION_TOKEN_RE = re.compile(r"\bsk-ant-[A-Za-z0-9._-]+")
_KEYED_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:password|username|proxy_username|refresh_token|access_token|client_secret|api_key|"
    r"token|passive_token|arkose_session_token|session_key|sessionkey|routing_hint|"
    r"routinghint|c_blob|x_ark_arid|nonce)[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\"'\s,;&}\]]+)(?P=quote)"
)


def sanitize_external_text(
    value: Any,
    *,
    limit: int = 500,
    secrets: Iterable[Any] = (),
) -> str:
    """Return a single-line, bounded string with common credentials removed."""
    text = str(value)
    for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _PROXY_CREDENTIAL_RE.sub(r"\1***:***@", text)
    text = _FOUR_PART_PROXY_RE.sub(
        lambda match: f"{match.group('host')}:{match.group('port')}:***:***",
        text,
    )
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _BASIC_AUTH_RE.sub(r"\1***", text)
    text = _COOKIE_HEADER_RE.sub(lambda match: f"{match.group(1)}: ***", text)
    text = _MAGIC_LINK_RE.sub("https://claude.ai/magic-link#***", text)
    text = _SESSION_TOKEN_RE.sub("***", text)
    text = _KEYED_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}***{match.group('quote')}",
        text,
    )
    return text.replace("\r", " ").replace("\n", " ").strip()[: max(0, int(limit))]
