"""Provider-aware account parsing with legacy input compatibility."""

from __future__ import annotations

import random
import re
from collections.abc import Callable

from claude_register.orchestration.models import (
    Account,
    AccountParseIssue,
    AccountParseReport,
)
from claude_register.shared.names import _LAST as _LAST_NAMES
from claude_register.shared.names import random_american_name


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAIL_PROVIDERS = {"mailcom", "imap", "microsoft"}
REMOVED_MAIL_ACCOUNT_FIELDS = {
    "app_token",
    "client_secret",
    "imap_host",
    "imaphost",
    "imap_port",
    "imap_server",
    "imap_servers",
    "imap_timeout",
    "mail_app_token",
    "mail_base_url",
    "mail_host",
    "mailhost",
    "mail_imap_host",
    "mail_imap_servers",
    "mail_imap_timeout",
    "secret",
}
CLIENT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_client_id(value: str) -> bool:
    return bool(CLIENT_ID_RE.match(value.strip()))


def _looks_like_refresh_token(value: str) -> bool:
    value = value.strip()
    lower = value.lower()
    return (
        len(value) >= 80
        or lower.startswith(("m.", "refresh"))
        or any(character in value for character in ("!", "$", "*"))
    )


def _split_account_fields(parts: list[str]) -> tuple[str, str, str, str, str]:
    """Return password, display name, Microsoft credentials, and delivery prefix."""
    password = parts[1] if len(parts) > 1 else ""
    tail = parts[2:]
    display_name = ""
    client_id = ""
    refresh_token = ""

    if len(parts) >= 3 and _looks_like_client_id(parts[1]) and _looks_like_refresh_token(parts[2]):
        password = ""
        client_id = parts[1]
        refresh_token = parts[2]
        tail = parts[3:]
    elif len(parts) >= 3 and _looks_like_client_id(parts[2]) and _looks_like_refresh_token(parts[1]):
        password = ""
        refresh_token = parts[1]
        client_id = parts[2]
        tail = parts[3:]

    positional: list[str] = []
    for item in tail:
        if "=" not in item:
            positional.append(item)
            continue
        key, value = item.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if key in ("client_id", "clientid", "app_id", "appid"):
            client_id = value
        elif key in ("refresh_token", "refresh", "token"):
            refresh_token = value
        elif key in REMOVED_MAIL_ACCOUNT_FIELDS:
            raise ValueError(f"旧邮箱参数 {key} 已删除，请按新邮箱格式移除该字段")
        elif key in ("display_name", "display", "name", "remark", "备注"):
            display_name = value

    if not client_id and not refresh_token and len(positional) >= 2:
        first, second = positional[0], positional[1]
        if _looks_like_client_id(first):
            client_id, refresh_token = first, second
            positional = positional[2:]
        elif _looks_like_client_id(second):
            refresh_token, client_id = first, second
            positional = positional[2:]
        elif len(parts) == 4:
            client_id, refresh_token = first, second
            positional = positional[2:]

    if not display_name and positional:
        display_name = positional[0]

    deliver_parts = (
        [parts[0], password, client_id, refresh_token]
        if client_id and refresh_token
        else [parts[0], password]
    )
    return password, display_name, client_id, refresh_token, "----".join(deliver_parts)


def _account_with_names(
    *,
    email: str,
    password: str,
    display_name: str,
    client_id: str,
    refresh_token: str,
    deliver_prefix: str,
    name_factory: Callable[[], str] = random_american_name,
) -> Account:
    if display_name:
        full_name = f"{display_name} {random.choice(_LAST_NAMES)}"
    else:
        display_name = name_factory()
        full_name = display_name
    return Account(
        email=email,
        password=password,
        display_name=display_name,
        mail_client_id=client_id,
        mail_refresh_token=refresh_token,
        deliver_prefix=deliver_prefix,
        full_name=full_name,
    )


def parse_accounts(
    text: str,
    *,
    name_factory: Callable[[], str] = random_american_name,
) -> list[Account]:
    """Parse the legacy heuristic account format, deduplicating by email."""
    accounts: list[Account] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) < 2:
            continue
        email = parts[0]
        try:
            password, display_name, client_id, refresh_token, deliver_prefix = (
                _split_account_fields(parts)
            )
        except ValueError:
            continue
        if not email or not EMAIL_RE.match(email):
            continue
        if not password and not (client_id and refresh_token):
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        accounts.append(
            _account_with_names(
                email=email,
                password=password,
                display_name=display_name,
                client_id=client_id,
                refresh_token=refresh_token,
                deliver_prefix=deliver_prefix,
                name_factory=name_factory,
            )
        )
    return accounts


def parse_accounts_with_report(
    text: str,
    provider: str,
    *,
    name_factory: Callable[[], str] = random_american_name,
) -> AccountParseReport:
    """Parse one explicit provider and report every rejected input line."""
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in MAIL_PROVIDERS:
        raise ValueError("unsupported_mail_provider")

    accounts: list[Account] = []
    issues: list[AccountParseIssue] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) < 2:
            issues.append(AccountParseIssue(line_number, "invalid_field_count", "账号字段不足"))
            continue
        email = parts[0]
        if not email or not EMAIL_RE.match(email):
            issues.append(AccountParseIssue(line_number, "invalid_email", "邮箱格式无效"))
            continue

        if normalized_provider == "microsoft":
            try:
                password, display_name, client_id, refresh_token, deliver_prefix = (
                    _split_account_fields(parts)
                )
            except ValueError:
                issues.append(
                    AccountParseIssue(line_number, "legacy_field_removed", "包含已停用的邮箱字段")
                )
                continue
            if not client_id:
                issues.append(
                    AccountParseIssue(line_number, "missing_client_id", "Microsoft 模式缺少 client_id")
                )
                continue
            if not refresh_token:
                issues.append(
                    AccountParseIssue(
                        line_number,
                        "missing_refresh_token",
                        "Microsoft 模式缺少 refresh_token",
                    )
                )
                continue
        else:
            password = parts[1]
            if not password:
                issues.append(AccountParseIssue(line_number, "missing_password", "邮箱密码为空"))
                continue
            display_name = parts[2] if len(parts) > 2 else ""
            client_id = ""
            refresh_token = ""
            deliver_prefix = f"{email}----{password}"

        key = email.lower()
        if key in seen:
            issues.append(AccountParseIssue(line_number, "duplicate_email", "邮箱重复"))
            continue
        seen.add(key)
        accounts.append(
            _account_with_names(
                email=email,
                password=password,
                display_name=display_name,
                client_id=client_id,
                refresh_token=refresh_token,
                deliver_prefix=deliver_prefix,
                name_factory=name_factory,
            )
        )
    return AccountParseReport(accounts=accounts, issues=issues)
