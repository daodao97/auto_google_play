"""通过 mail.xcaigc.com 抓取 Claude magic link。

Mail.com、IMAP 和 Microsoft token 三类邮箱统一走远程 tRPC 服务。
本模块保留 ``prime_seen`` / ``fetch_magic_link`` 的高层接口，供注册编排器复用。
邮箱密码和 Microsoft refresh token 会通过 HTTPS 发送到 mail.xcaigc.com，
不会由本模块写入文件或日志。
"""

from __future__ import annotations

import base64
import email.utils
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from claude_register.core.run_guard import ExternalRunNotConfirmed, confirmed_external_args
from claude_register.core.security import sanitize_external_text

log = logging.getLogger("mail_fetcher")

MAIL_API_BASE = "https://mail.xcaigc.com"
MAIL_FETCH_LIMIT = 20
MAX_DETAIL_CANDIDATES_PER_POLL = 3
MAIL_REQUEST_TIMEOUT = 30.0
MAIL_TIMESTAMP_TOLERANCE_MS = 90_000
MAIL_QUEUE_GRACE_SECONDS = 60.0
MAIL_PROVIDERS = {"mailcom", "imap", "microsoft"}
REMOVED_MAIL_CONFIG_FIELDS = {
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
    "mail_client_secret",
    "mail_host",
    "mailhost",
    "mail_imap_host",
    "mail_imap_servers",
    "mail_imap_timeout",
}

MAGIC_LINK_RE = re.compile(
    r"https?://(?:platform\.claude\.com|claude\.ai)/magic-link#([0-9a-fA-F]+):([^\s\"'<>]+)"
)
CLAUDE_SENDERS = (
    "claude",
    "anthropic",
    "noreply@anthropic.com",
    "no-reply@anthropic.com",
    "noreply@claude.ai",
)
_MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MAILCOM_DISPLAY_DATE_RE = re.compile(
    r"^[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s+at\s+"
    r"(\d{1,2}):(\d{2})\s+([AP]M)$",
    re.IGNORECASE,
)


class MailFetcherFatalError(RuntimeError):
    """邮箱侧确定不可继续等待的错误。"""


class MailFetcherTransientError(RuntimeError):
    """邮箱服务在本次轮询预算内持续不可用，可由账号级重试恢复。"""


class MailFetcherTimeoutError(TimeoutError):
    """No matching mail arrived within the bounded wall-clock budget."""


RateLimiter = Callable[[threading.Event | None, float | None], None]


class FifoRateLimiter:
    """按到达顺序分配请求槽的线程安全限流器。

    ``deadline`` 使用 ``time.monotonic()`` 时间基准。取消或截止的等待者会
    放弃自己的票号，后续等待者仍可继续获得请求槽。
    """

    def __init__(self, min_interval: float):
        self._min_interval = max(0.0, float(min_interval))
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._abandoned_tickets: set[int] = set()
        self._next_allowed_at = 0.0

    def _advance_abandoned_locked(self) -> None:
        while self._serving_ticket in self._abandoned_tickets:
            self._abandoned_tickets.remove(self._serving_ticket)
            self._serving_ticket += 1

    def _abandon_locked(self, ticket: int) -> None:
        if ticket >= self._serving_ticket:
            self._abandoned_tickets.add(ticket)
            self._advance_abandoned_locked()
        self._condition.notify_all()

    def acquire(
        self,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1

            while True:
                if cancel_event and cancel_event.is_set():
                    self._abandon_locked(ticket)
                    raise TimeoutError("request slot wait cancelled")

                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._abandon_locked(ticket)
                    raise TimeoutError("no request slot before deadline")

                self._advance_abandoned_locked()
                if ticket != self._serving_ticket:
                    wait_timeout = 0.05
                    if deadline is not None:
                        wait_timeout = min(wait_timeout, max(0.0, deadline - now))
                    self._condition.wait(wait_timeout)
                    continue

                slot_delay = max(0.0, self._next_allowed_at - now)
                if deadline is not None and now + slot_delay >= deadline:
                    self._abandon_locked(ticket)
                    raise TimeoutError("no request slot before deadline")
                if slot_delay > 0:
                    wait_timeout = min(slot_delay, 0.05)
                    if deadline is not None:
                        wait_timeout = min(wait_timeout, max(0.0, deadline - now))
                    self._condition.wait(wait_timeout)
                    continue

                self._serving_ticket += 1
                self._next_allowed_at = now + self._min_interval
                self._advance_abandoned_locked()
                self._condition.notify_all()
                return

    def __call__(
        self,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> None:
        self.acquire(cancel_event, deadline)


_INVALID_CREDENTIAL_HINTS = (
    "credential rejected by mailbox",
    "invalid credential",
    "invalid credentials",
    "invalid_grant",
    "invalid password",
    "incorrect password",
    "wrong password",
    "login failed",
    "refresh token expired",
    "refresh token revoked",
    "expired refresh token",
    "revoked refresh token",
    "登录失败",
    "账号或密码错误",
    "密码错误",
    "拒绝登录",
)


def _short(error: Exception, limit: int = 220) -> str:
    return sanitize_external_text(error, limit=limit)


def _wait(seconds: float, cancel: threading.Event | None) -> None:
    if seconds <= 0:
        if cancel and cancel.is_set():
            raise MailFetcherFatalError("已取消邮箱抓取")
        return
    if cancel:
        if cancel.wait(seconds):
            raise MailFetcherFatalError("已取消邮箱抓取")
        return
    time.sleep(seconds)


def _acquire_rate_limiter(
    rate_limiter: RateLimiter | None,
    cancel_event: threading.Event | None,
    deadline: float | None = None,
) -> float:
    if cancel_event and cancel_event.is_set():
        raise MailFetcherFatalError("已取消邮箱抓取")
    started_at = time.monotonic()
    if rate_limiter is not None:
        rate_limiter(cancel_event, deadline)
    return max(0.0, time.monotonic() - started_at)


def _reject_removed_options(options: dict[str, Any]) -> None:
    if not options:
        return
    names = ", ".join(sorted(str(name) for name in options))
    raise ValueError(
        f"旧邮箱参数已删除: {names}；请只使用 provider、client_id 和 refresh_token"
    )


def _validate_mail_config(config: dict[str, Any]) -> None:
    removed = {key for key in config if key in REMOVED_MAIL_CONFIG_FIELDS}
    workers = config.get("workers")
    if isinstance(workers, list):
        for worker in workers:
            if isinstance(worker, dict):
                removed.update(key for key in worker if key in REMOVED_MAIL_CONFIG_FIELDS)
    if removed:
        names = ", ".join(sorted(removed))
        raise ValueError(f"旧邮箱配置已删除: {names}；请迁移到三种 mail_provider 配置")


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _is_claude_sender(message: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(message.get(key, ""))
        for key in ("sender", "fromAddress", "from", "from_address", "fromName")
    ).lower()
    return any(sender in haystack for sender in CLAUDE_SENDERS)


def _extract_magic_link_from_text(text: str) -> dict[str, Any] | None:
    match = MAGIC_LINK_RE.search(text)
    if not match:
        return None
    return {
        "nonce": match.group(1),
        "encoded_email_address": match.group(2),
        "magic_link_url": match.group(0),
    }


def _decode_email(encoded: str) -> str | None:
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
    except Exception:
        return None


def _msg_datetime_ms(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 10_000_000_000 else number * 1000

    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = 0
    if number:
        return number if number > 10_000_000_000 else number * 1000

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        display_match = _MAILCOM_DISPLAY_DATE_RE.fullmatch(text)
        if display_match:
            month_name, day, year, hour, minute, meridiem = display_match.groups()
            month = _MONTH_NUMBERS.get(month_name.casefold())
            if month is None:
                return None
            hour_number = int(hour) % 12
            if meridiem.casefold() == "pm":
                hour_number += 12
            try:
                parsed = datetime(
                    int(year),
                    month,
                    int(day),
                    hour_number,
                    int(minute),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                return None
        else:
            try:
                parsed = email.utils.parsedate_to_datetime(text)
            except Exception:
                return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000


def _is_before_not_before(value: float | None, not_before_ms: float | None) -> bool:
    if not not_before_ms:
        return False
    if value is None:
        return True
    return value + MAIL_TIMESTAMP_TOLERANCE_MS < not_before_ms


def _message_timestamp(message: dict[str, Any]) -> float:
    return _msg_datetime_ms(
        message.get("dateOrTime")
        or message.get("receivedDateTime")
        or message.get("received_at")
        or message.get("timestamp")
    ) or 0.0


def _detect_provider(
    email: str,
    provider: str = "mailcom",
    *,
    refresh_token: str = "",
    client_id: str = "",
) -> str:
    del email, refresh_token, client_id
    normalized = str(provider or "mailcom").strip().lower()
    if normalized not in MAIL_PROVIDERS:
        raise ValueError(
            f"不支持的邮件取信方式: {normalized}；请选择 mailcom、imap 或 microsoft"
        )
    return normalized


def _validate_provider_credentials(
    email: str,
    provider: str,
    *,
    refresh_token: str = "",
    client_id: str = "",
) -> str:
    selected = _detect_provider(
        email,
        provider,
        refresh_token=refresh_token,
        client_id=client_id,
    )
    if selected == "microsoft":
        missing = [
            name
            for name, value in (("client_id", client_id), ("refresh_token", refresh_token))
            if not value
        ]
        if missing:
            raise MailFetcherFatalError(
                f"Microsoft token 取信缺少 {' 和 '.join(missing)}"
            )
    return selected


def _validated_base_url(base_url: str) -> str:
    parsed = urlsplit(str(base_url or MAIL_API_BASE).strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "mail.xcaigc.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in ("", "/api/trpc")
    ):
        raise ValueError("邮箱服务地址只允许 https://mail.xcaigc.com")
    return f"https://{parsed.hostname}" + (":443" if parsed.port == 443 else "")


def _trpc_url(base_url: str, procedure: str) -> str:
    return f"{_validated_base_url(base_url)}/api/trpc/{procedure}?batch=1"


def _redact_message(message: str, secrets: tuple[str, ...]) -> str:
    safe = message.replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "***")
    safe = re.sub(r"(?i)(refresh_token|access_token|client_secret)(?:=|\s+)\S+", r"\1=***", safe)
    return safe[:300]


def _trpc_error_message(payload: Any) -> tuple[str, int | None]:
    entry = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(entry, dict):
        return "", None
    error = entry.get("error")
    if not isinstance(error, dict):
        return "", None
    body = error.get("json") if isinstance(error.get("json"), dict) else error
    message = str(body.get("message") or "")
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    status = data.get("httpStatus")
    if isinstance(status, str):
        try:
            status = int(status.strip())
        except ValueError:
            status = None
    return message, status if isinstance(status, int) and not isinstance(status, bool) else None


def _unwrap_trpc(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError("邮箱服务返回了无效的 tRPC 响应")
    result = payload[0].get("result")
    if not isinstance(result, dict):
        raise RuntimeError("邮箱服务响应缺少 result")
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("json"), dict):
        raise RuntimeError("邮箱服务响应缺少 data.json")
    return data["json"]


def _trpc_call(
    base_url: str,
    procedure: str,
    input_data: dict[str, Any],
    *,
    timeout: float = MAIL_REQUEST_TIMEOUT,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    response = requests.post(
        _trpc_url(base_url, procedure),
        json={"0": {"json": input_data}},
        headers={"accept": "application/json", "content-type": "application/json"},
        timeout=timeout,
        allow_redirects=False,
    )
    response_status = int(response.status_code)
    if 400 <= response_status < 500 and response_status not in (408, 429):
        raise MailFetcherFatalError(f"邮箱服务 {procedure} HTTP {response_status}")
    try:
        payload = response.json()
    except Exception as error:
        error_type = (
            MailFetcherTransientError
            if response_status in (408, 429) or response_status >= 500
            else RuntimeError
        )
        raise error_type(f"邮箱服务返回非 JSON 响应 (HTTP {response_status})") from error

    raw_message, error_status = _trpc_error_message(payload)
    if raw_message or error_status is not None:
        message = _redact_message(raw_message or "未提供错误信息", secrets)
        effective_status = error_status or (response_status if response_status != 200 else None)
        if (
            effective_status is not None
            and 400 <= effective_status < 500
            and effective_status not in (408, 429)
        ):
            error_type = MailFetcherFatalError
        elif effective_status in (408, 429) or (effective_status or 0) >= 500:
            error_type = MailFetcherTransientError
        elif any(hint in raw_message.lower() for hint in _INVALID_CREDENTIAL_HINTS):
            error_type = MailFetcherFatalError
        else:
            error_type = RuntimeError
        raise error_type(f"邮箱服务 {procedure} 失败: {message}")
    if response_status != 200:
        error_type = (
            MailFetcherTransientError
            if response_status in (408, 429) or response_status >= 500
            else MailFetcherFatalError if 400 <= response_status < 500 else RuntimeError
        )
        raise error_type(f"邮箱服务 {procedure} HTTP {response_status}")
    return _unwrap_trpc(payload)


def _message_id(message: dict[str, Any]) -> str:
    return str(message.get("mailId") or message.get("messageId") or message.get("id") or "")


def _validate_inbox(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    session_id = str(data.get("sessionId") or "")
    messages = data.get("messages")
    if not session_id:
        raise RuntimeError("邮箱服务响应缺少 sessionId")
    if not isinstance(messages, list):
        raise RuntimeError("邮箱服务响应缺少 messages")
    return session_id, [message for message in messages if isinstance(message, dict) and _message_id(message)]


def _microsoft_credential(
    email: str,
    password: str,
    client_id: str,
    refresh_token: str,
) -> str:
    if not client_id:
        raise MailFetcherFatalError("Microsoft token 取信缺少 client_id")
    if not refresh_token:
        raise MailFetcherFatalError("Microsoft token 取信缺少 refresh_token")
    return "----".join((email, password, client_id, refresh_token))


def _fetch_inbox(
    base_url: str,
    email: str,
    password: str,
    *,
    provider: str,
    client_id: str = "",
    refresh_token: str = "",
    limit: int = MAIL_FETCH_LIMIT,
    timeout: float = MAIL_REQUEST_TIMEOUT,
) -> tuple[str, list[dict[str, Any]]]:
    fetch_limit = max(1, min(500, int(limit)))
    if provider == "microsoft":
        credential = _microsoft_credential(email, password, client_id, refresh_token)
        data = _trpc_call(
            base_url,
            "mail.fetchMsGraphByCredential",
            {"credential": credential, "limit": fetch_limit},
            timeout=timeout,
            secrets=(email, password, client_id, refresh_token, credential),
        )
    else:
        credential = f"{email}:{password}"
        data = _trpc_call(
            base_url,
            "mail.fetch",
            {"credential": credential, "limit": fetch_limit, "provider": provider},
            timeout=timeout,
            secrets=(email, password, credential),
        )
    return _validate_inbox(data)


def _fetch_detail(
    base_url: str,
    session_id: str,
    mail_id: str,
    *,
    timeout: float = MAIL_REQUEST_TIMEOUT,
) -> dict[str, Any]:
    data = _trpc_call(
        base_url,
        "mail.message",
        {"sessionId": session_id, "mailId": mail_id},
        timeout=timeout,
        secrets=(session_id, mail_id),
    )
    return data


def _detail_text(detail: dict[str, Any]) -> str:
    chunks = [str(detail.get("bodyText") or ""), str(detail.get("bodyHtml") or "")]
    links = detail.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict):
                chunks.append(str(link.get("url") or ""))
            elif isinstance(link, str):
                chunks.append(link)
    return "\n".join(chunks)


def prime_seen(
    email: str,
    password: str,
    *,
    base_url: str = MAIL_API_BASE,
    provider: str = "mailcom",
    refresh_token: str = "",
    client_id: str = "",
    cancel_event: threading.Event | None = None,
    rate_limiter: RateLimiter | None = None,
    timeout: float = MAIL_REQUEST_TIMEOUT,
    **_legacy: Any,
) -> set[str]:
    """发信前记录现有邮件 ID；Microsoft 模式避免预先兑换 refresh token。"""
    _reject_removed_options(_legacy)
    selected = _validate_provider_credentials(
        email,
        provider,
        refresh_token=refresh_token,
        client_id=client_id,
    )
    if selected == "microsoft":
        return set()
    deadline = time.monotonic() + max(0.1, float(timeout))
    _acquire_rate_limiter(rate_limiter, cancel_event, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("邮箱基线读取截止前没有可用请求槽")
    _, messages = _fetch_inbox(
        base_url,
        email,
        password,
        provider=selected,
        client_id=client_id,
        refresh_token=refresh_token,
        timeout=min(MAIL_REQUEST_TIMEOUT, remaining),
    )
    return {_message_id(message) for message in messages if _message_id(message)}


def fetch_magic_link(
    email: str,
    password: str,
    *,
    base_url: str = MAIL_API_BASE,
    provider: str = "mailcom",
    poll_interval: float = 3.0,
    poll_timeout: float = 180.0,
    max_fetch_errors: int = 5,
    cancel_event: threading.Event | None = None,
    seen: set[str] | None = None,
    refresh_token: str = "",
    client_id: str = "",
    not_before_ms: float | None = None,
    rate_limiter: RateLimiter | None = None,
    mail_fast_path: bool = False,
    deadline: float | None = None,
    **_legacy: Any,
) -> dict[str, Any]:
    """轮询远程邮箱服务并返回 Claude magic link 的结构化字段。

    ``max_fetch_errors`` 仅限制瞬时错误的退避级别；认证等确定性错误立即终止，
    网络、限流和远端临时错误会在 ``poll_timeout`` 总预算内继续恢复。
    """
    _reject_removed_options(_legacy)
    if cancel_event and cancel_event.is_set():
        raise MailFetcherFatalError("已取消邮箱抓取")

    selected = _validate_provider_credentials(
        email,
        provider,
        refresh_token=refresh_token,
        client_id=client_id,
    )
    started_at = time.monotonic()
    active_deadline = started_at + max(0.0, float(poll_timeout))
    wall_deadline = active_deadline + MAIL_QUEUE_GRACE_SECONDS
    if deadline is not None:
        wall_deadline = min(wall_deadline, float(deadline))
    has_seen_baseline = seen is not None
    seen_ids = set(seen or ())
    inspected_ids = set(seen_ids)
    detail_failures: dict[str, int] = {}
    consecutive_errors = 0
    last_transient_error: Exception | None = None
    polls = 0
    # 快速路径只合并首次列表和首个候选详情的等待，后续轮询继续限流。
    initial_slots_remaining = 2 if mail_fast_path and rate_limiter is not None else 0

    def acquire_request_slot() -> None:
        nonlocal active_deadline, initial_slots_remaining
        if initial_slots_remaining:
            initial_slots_remaining -= 1
            return
        active_deadline += _acquire_rate_limiter(
            rate_limiter,
            cancel_event,
            wall_deadline,
        )

    while time.monotonic() < min(active_deadline, wall_deadline):
        if cancel_event and cancel_event.is_set():
            raise MailFetcherFatalError("已取消邮箱抓取")
        polls += 1
        try:
            acquire_request_slot()
            deadline = min(active_deadline, wall_deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session_id, messages = _fetch_inbox(
                base_url,
                email,
                password,
                provider=selected,
                client_id=client_id,
                refresh_token=refresh_token,
                timeout=min(MAIL_REQUEST_TIMEOUT, remaining),
            )

            details_checked = 0
            candidates = sorted(
                messages,
                key=lambda message: (
                    detail_failures.get(_message_id(message), 0),
                    -_message_timestamp(message),
                ),
            )
            for message in candidates:
                mail_id = _message_id(message)
                if not mail_id or mail_id in inspected_ids:
                    continue
                timestamp = _message_timestamp(message) or None
                if _is_before_not_before(timestamp, not_before_ms) and not (
                    timestamp is None and has_seen_baseline
                ):
                    continue
                subject = str(message.get("subjectPreview") or message.get("subject") or "")
                if not _is_claude_sender(message) and "claude" not in subject.lower():
                    continue

                if details_checked >= MAX_DETAIL_CANDIDATES_PER_POLL:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                acquire_request_slot()
                deadline = min(active_deadline, wall_deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                details_checked += 1
                try:
                    detail = _fetch_detail(
                        base_url,
                        session_id,
                        mail_id,
                        timeout=min(MAIL_REQUEST_TIMEOUT, remaining),
                    )
                except MailFetcherFatalError:
                    raise
                except Exception:
                    detail_failures[mail_id] = detail_failures.get(mail_id, 0) + 1
                    log.warning("邮箱候选邮件详情读取失败，继续检查后续邮件")
                    continue
                inspected_ids.add(mail_id)
                match = _extract_magic_link_from_text(_detail_text(detail))
                if not match:
                    continue
                decoded_email = _decode_email(match["encoded_email_address"])
                if not decoded_email or decoded_email.casefold() != email.casefold():
                    continue
                match.update(
                    {
                        "email_address": decoded_email,
                        "email": email,
                        "mail_id": mail_id,
                        "subject": subject,
                    }
                )
                return match
            consecutive_errors = 0
            last_transient_error = None
        except TimeoutError as error:
            if cancel_event and cancel_event.is_set():
                raise MailFetcherFatalError("已取消邮箱抓取") from error
            consecutive_errors = max(1, consecutive_errors)
            last_transient_error = error
            break
        except MailFetcherFatalError:
            raise
        except Exception as error:
            if cancel_event and cancel_event.is_set():
                raise MailFetcherFatalError("已取消邮箱抓取") from error
            consecutive_errors += 1
            last_transient_error = error
            log.warning("邮箱服务请求失败，稍后重试: %s", _short(error))

        remaining = min(active_deadline, wall_deadline) - time.monotonic()
        if remaining <= 0:
            break
        poll_wait = max(0.0, float(poll_interval))
        if rate_limiter is not None:
            poll_wait = min(poll_wait, 3.0)
        if consecutive_errors:
            backoff_level = min(consecutive_errors, max(1, int(max_fetch_errors)))
            poll_wait = max(poll_wait, min(30.0, 2.0 ** backoff_level))
        _wait(min(poll_wait, remaining), cancel_event)

    if consecutive_errors and last_transient_error is not None:
        raise MailFetcherTransientError(
            f"邮箱服务在轮询截止前持续失败: {_short(last_transient_error)}"
        ) from last_transient_error
    raise MailFetcherTimeoutError(
        f"{poll_timeout}s 内未在 {email} 找到 Claude magic-link (mail.xcaigc.com, polled {polls})"
    )


def main() -> int:
    import json

    try:
        args = confirmed_external_args()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2
    email = args[0] if len(args) > 0 else "your@mail.com"
    password = args[1] if len(args) > 1 else "your-password"
    provider = args[2] if len(args) > 2 else "mailcom"
    try:
        result = fetch_magic_link(email, password, provider=provider)
    except Exception:
        print("邮箱抓取失败，详情已隐藏")
        return 2
    safe_result = {
        "email_address": result.get("email_address"),
        "mail_id": result.get("mail_id"),
        "subject": result.get("subject"),
    }
    print(json.dumps(safe_result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
