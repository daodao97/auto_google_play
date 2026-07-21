"""共享 HTTP 指纹层——curl_cffi impersonate。

对标 gopay-pipeline 的 build_session() + impersonate：用 curl_cffi 在 TLS/JA3 层
伪装成真 Chrome，规避「裸 requests 一眼 Python」的风控。register / onboarding /
arkose 共用本模块，避免循环 import（register 已 import onboarding）。

依赖: pip install curl-cffi>=0.7.0
"""

from __future__ import annotations

import ipaddress
import json
import random
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

# 会话代理占位符（对标 gopay config 里的 {SESSION}）。每个账号换一个 id 拿独立粘性 IP。
_SESSION_PLACEHOLDER = re.compile(r"\{session}", re.IGNORECASE)

IMPERSONATE_DEFAULT = "chrome142"
IMPERSONATE_FALLBACK = "chrome131"

# 模块级常量保留作默认值和向后兼容。每账号应通过 random_browser_profile() 覆盖。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = '"Chromium";v="142", "Google Chrome";v="142", "Not=A?Brand";v="24"'
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
DEFAULT_SEC_CH_UA_PLATFORM = '"macOS"'
_DATADOG_TRACE_LIMIT = 1 << 64


@dataclass
class BrowserProfile:
    """每账号浏览器指纹。TLS (impersonate)、UA、sec-ch-ua、平台、语言必须内部一致。"""
    impersonate: str
    ua: str
    sec_ch_ua: str
    platform: str        # 含引号：'"macOS"' 或 '"Windows"'
    accept_language: str
    color_scheme: str = "light"   # CH-prefers-color-scheme cookie


def _new_fbp() -> str:
    return f"fb.1.{int(time.time() * 1000)}.{secrets.token_hex(8)}"


def _new_consent_preferences() -> str:
    # Onboarding 会对 analytics/marketing 都提交 OPT_IN，Cookie 与 API 决策保持一致。
    consent = {"analytics": True, "marketing": True}
    return quote(json.dumps(consent))


@dataclass
class BrowserRuntime:
    """每账号浏览器运行时状态：一个真实标签页里会稳定复用的会话值。"""
    activity_session_id: str
    datadog_trace_id: str
    ssid: str = field(default_factory=lambda: str(uuid.uuid4()))
    fbp: str = field(default_factory=_new_fbp)
    consent_preferences: str = field(default_factory=_new_consent_preferences)


@dataclass(frozen=True)
class BrowserIdentity:
    """一个账号完整流程唯一的浏览器身份，不随请求或重试 Session 轮换。"""
    anonymous_id: str
    device_id: str
    sentry_trace_id: str
    profile: BrowserProfile
    runtime: BrowserRuntime


def _random_datadog_id() -> str:
    value = secrets.randbits(64)
    return str(value or 1)


def _normalize_datadog_id(value: str | int | None) -> str:
    if value is not None:
        try:
            n = int(value)
            if 0 < n < _DATADOG_TRACE_LIMIT:
                return str(n)
        except (TypeError, ValueError):
            pass
    return _random_datadog_id()


def _datadog_traceparent(trace_id: str, parent_id: str) -> str:
    trace_int = int(_normalize_datadog_id(trace_id))
    parent_int = int(_normalize_datadog_id(parent_id))
    return f"00-0000000000000000{trace_int:016x}-{parent_int:016x}-01"


def new_browser_runtime(
    *,
    activity_session_id: str | None = None,
    datadog_trace_id: str | int | None = None,
    ssid: str | None = None,
    fbp: str | None = None,
    consent_preferences: str | None = None,
) -> BrowserRuntime:
    runtime = BrowserRuntime(
        activity_session_id=activity_session_id or str(uuid.uuid4()),
        datadog_trace_id=_normalize_datadog_id(datadog_trace_id),
    )
    if ssid is not None:
        runtime.ssid = ssid
    if fbp is not None:
        runtime.fbp = fbp
    if consent_preferences is not None:
        runtime.consent_preferences = consent_preferences
    return runtime


def attach_browser_runtime(
    session: curl_requests.Session,
    runtime: BrowserRuntime | None,
) -> None:
    if runtime is None:
        return
    try:
        setattr(session, "_browser_runtime", runtime)
    except Exception:
        pass


def attach_browser_identity(
    session: curl_requests.Session,
    identity: BrowserIdentity | None,
) -> None:
    if identity is None:
        return
    try:
        setattr(session, "_browser_identity", identity)
    except Exception:
        pass
    attach_browser_profile(session, identity.profile)
    attach_browser_runtime(session, identity.runtime)


# 多个真实 Chrome 指纹组合；每账号随机选一个，让批量账号不再共享相同特征向量。
_PROFILES: list[BrowserProfile] = [
    BrowserProfile(
        impersonate="chrome142",
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="142", "Google Chrome";v="142", "Not=A?Brand";v="24"',
        platform='"macOS"',
        accept_language="en-US,en;q=0.9",
        color_scheme="light",
    ),
    BrowserProfile(
        impersonate="chrome131",
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        platform='"Windows"',
        accept_language="en-US,en;q=0.9",
        color_scheme="light",
    ),
    BrowserProfile(
        impersonate="chrome131",
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        platform='"macOS"',
        accept_language="en-US,en;q=0.8",
        color_scheme="dark",
    ),
    BrowserProfile(
        impersonate="chrome142",
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="142", "Google Chrome";v="142", "Not=A?Brand";v="24"',
        platform='"Windows"',
        accept_language="en-GB,en-US;q=0.9,en;q=0.8",
        color_scheme="light",
    ),
    BrowserProfile(
        impersonate="chrome131",
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        sec_ch_ua='"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        platform='"Windows"',
        accept_language="en-US,en;q=0.7",
        color_scheme="dark",
    ),
]


def random_browser_profile(impersonate: str | None = None) -> BrowserProfile:
    """返回内部一致的随机指纹；指定 TLS 版本时只从对应 UA 组合中选择。"""
    if impersonate:
        matching = [profile for profile in _PROFILES if profile.impersonate == impersonate]
        if not matching:
            raise ValueError(f"没有匹配的浏览器指纹: {impersonate}")
        return random.choice(matching)
    return random.choice(_PROFILES)


def new_browser_identity(
    *,
    profile: BrowserProfile | None = None,
    runtime: BrowserRuntime | None = None,
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    impersonate: str | None = None,
) -> BrowserIdentity:
    """一次创建账号级身份；调用方应在整个流程和所有重试中复用返回值。"""
    return BrowserIdentity(
        anonymous_id=anonymous_id or f"claudeai.v1.{uuid.uuid4()}",
        device_id=device_id or str(uuid.uuid4()),
        sentry_trace_id=sentry_trace_id or uuid.uuid4().hex,
        profile=profile or random_browser_profile(impersonate),
        runtime=runtime or new_browser_runtime(),
    )


def attach_browser_profile(session: curl_requests.Session, profile: BrowserProfile | None) -> None:
    if profile is None:
        return
    try:
        setattr(session, "_browser_profile", profile)
    except Exception:
        pass


# Sentry DSN 默认值（从 claude.ai JS chunk 抓的快照，几乎不变；dynamic_config 会动态刷新）。
# 真浏览器所有 claude.ai 请求都带 baggage + sentry-trace 头（Sentry SDK 自动注入），
# 缺失这俩头是明显的非浏览器特征。
DEFAULT_SENTRY_PUBLIC_KEY = "58e9b9d0fc244061a1b54fe288b0e483"
DEFAULT_SENTRY_ORG_ID = "1158394"


def resolve_impersonate(impersonate: str | None) -> str:
    """给定 impersonate；None 或空 → 默认值。"""
    return impersonate or IMPERSONATE_DEFAULT


def build_session(
    impersonate: str | None = None,
    proxy: str | None = None,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    identity: BrowserIdentity | None = None,
) -> curl_requests.Session:
    """建一个带指纹的 curl_cffi Session。profile.impersonate 优先于 impersonate 参数。"""
    if identity is not None:
        profile = identity.profile
        browser_runtime = identity.runtime
    imp = profile.impersonate if profile else resolve_impersonate(impersonate)
    s = curl_requests.Session(impersonate=imp)
    if identity is not None:
        attach_browser_identity(s, identity)
    else:
        attach_browser_profile(s, profile)
        attach_browser_runtime(s, browser_runtime)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


EXIT_IP_ENDPOINT = "https://api.ipify.org"


def fetch_proxy_exit_ip(session: curl_requests.Session, timeout: float = 5.0) -> str:
    """Fetch and validate one public IP without exposing the response body."""
    try:
        response = session.get(EXIT_IP_ENDPOINT, timeout=timeout)
        response.raise_for_status()
        value = str(response.text).strip()
        return str(ipaddress.ip_address(value))
    except Exception:
        return ""


def resolve_proxy_exit_ip(
    proxy: str | None,
    *,
    identity: BrowserIdentity | None = None,
    timeout: float = 5.0,
) -> str:
    """Resolve the public IP through one already-materialized proxy session."""
    session = build_session(proxy=proxy, identity=identity)
    try:
        return fetch_proxy_exit_ip(session, timeout=timeout)
    finally:
        try:
            session.close()
        except Exception:
            pass


def init_browser_cookies(
    session: curl_requests.Session,
    anonymous_id: str,
    device_id: str,
    color_scheme: str = "light",
    *,
    browser_runtime: BrowserRuntime | None = None,
    activity_session_id: str | None = None,
    identity: BrowserIdentity | None = None,
) -> BrowserRuntime:
    """往 session 里种真浏览器首次打开 claude.ai 时 JS 会设的基础 cookie。

    color_scheme 来自 BrowserProfile，使不同账号 cookie 特征各异。
    """
    if identity is not None:
        anonymous_id = identity.anonymous_id
        device_id = identity.device_id
        color_scheme = identity.profile.color_scheme
        browser_runtime = identity.runtime
        attach_browser_identity(session, identity)
    runtime = (
        browser_runtime
        or getattr(session, "_browser_runtime", None)
        or new_browser_runtime(activity_session_id=activity_session_id)
    )
    if activity_session_id and runtime.activity_session_id != activity_session_id:
        runtime = new_browser_runtime(
            activity_session_id=activity_session_id,
            datadog_trace_id=runtime.datadog_trace_id,
        )
    attach_browser_runtime(session, runtime)
    domain = ".claude.ai"
    session.cookies.set("activitySessionId", runtime.activity_session_id, domain=domain)
    session.cookies.set("anthropic-device-id", device_id, domain=domain)
    session.cookies.set("CH-prefers-color-scheme", color_scheme, domain=domain)
    session.cookies.set("ajs_anonymous_id", anonymous_id, domain=domain)
    session.cookies.set("__ssid", runtime.ssid, domain=domain)
    session.cookies.set("_fbp", runtime.fbp, domain=domain)
    session.cookies.set("anthropic-consent-preferences", runtime.consent_preferences, domain=domain)
    return runtime


def warm_claude_login(
    session: curl_requests.Session,
    timeout: float = 30,
    *,
    ua: str | None = None,
    sec_ch_ua: str | None = None,
    platform: str | None = None,
    accept_language: str | None = None,
) -> None:
    """Best-effort visit to /login so Cloudflare/browser cookies can settle."""
    profile = getattr(session, "_browser_profile", None)
    ua = ua or (profile.ua if profile else DEFAULT_UA)
    sec_ch_ua = sec_ch_ua or (profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA)
    platform = platform or (profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM)
    accept_language = accept_language or (profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": accept_language,
        "Priority": "u=0, i",
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": platform,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": ua,
    }
    try:
        session.get("https://claude.ai/login", headers=headers, timeout=timeout)
    except Exception:
        pass


def warm_claude_bootstrap(
    session: curl_requests.Session,
    *,
    client_sha: str,
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    org_uuid: str | None = None,
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    client_version: str = "1.0.0",
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    identity: BrowserIdentity | None = None,
    timeout: float = 15,
) -> None:
    """Best-effort 拉取页面 bootstrap，只做只读预热，失败不影响注册主流程。"""
    if identity is not None:
        anonymous_id = identity.anonymous_id
        device_id = identity.device_id
        sentry_trace_id = identity.sentry_trace_id
        profile = identity.profile
        browser_runtime = identity.runtime
    anonymous_id = anonymous_id or f"claudeai.v1.{uuid.uuid4()}"
    device_id = device_id or str(uuid.uuid4())
    sentry_trace_id = sentry_trace_id or uuid.uuid4().hex
    profile = profile or getattr(session, "_browser_profile", None)
    runtime = browser_runtime or getattr(session, "_browser_runtime", None)
    referer = "https://claude.ai/chats" if org_uuid else "https://claude.ai/magic-link"
    headers = build_browser_headers(
        client_sha=client_sha,
        anonymous_id=anonymous_id,
        device_id=device_id,
        sentry_trace_id=sentry_trace_id,
        referer=referer,
        sentry_public_key=sentry_public_key,
        sentry_org_id=sentry_org_id,
        client_version=client_version,
        ua=profile.ua if profile else DEFAULT_UA,
        sec_ch_ua=profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA,
        platform=profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM,
        accept_language=profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE,
        activity_session_id=runtime.activity_session_id if runtime else None,
        datadog_trace_id=runtime.datadog_trace_id if runtime else None,
    )
    params = {
        "statsig_hashing_algorithm": "djb2",
        "growthbook_format": "sdk",
        "include_system_prompts": "false",
    }
    requests_to_make: list[tuple[str, dict[str, str] | None]] = [
        ("https://claude.ai/edge-api/bootstrap", params),
    ]
    if org_uuid:
        requests_to_make.extend([
            (f"https://claude.ai/api/bootstrap/{org_uuid}/current_user_access", None),
            (f"https://claude.ai/edge-api/bootstrap/{org_uuid}/app_start", params),
        ])
    for url, query in requests_to_make:
        try:
            session.get(url, params=query, headers=headers, timeout=timeout)
        except Exception:
            pass


def build_browser_headers(
    *,
    client_sha: str,
    anonymous_id: str,
    device_id: str,
    sentry_trace_id: str,
    referer: str = "https://claude.ai/",
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    content_type: str = "application/json",
    sec_fetch_site: str = "same-origin",
    client_version: str = "1.0.0",
    ua: str = DEFAULT_UA,
    sec_ch_ua: str = DEFAULT_SEC_CH_UA,
    platform: str = DEFAULT_SEC_CH_UA_PLATFORM,
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE,
    activity_session_id: str | None = None,
    datadog_trace_id: str | int | None = None,
    datadog_parent_id: str | int | None = None,
) -> dict[str, str]:
    """构造 claude.ai API 请求的完整浏览器头。

    ua/sec_ch_ua/platform/accept_language/client_version 来自 BrowserProfile，
    使不同账号的请求头各不相同，避免批量关联。
    """
    span = uuid.uuid4().hex[:16]
    activity_session_id = activity_session_id or str(uuid.uuid4())
    datadog_trace_id = _normalize_datadog_id(datadog_trace_id)
    datadog_parent_id = _normalize_datadog_id(datadog_parent_id)
    return {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": accept_language,
        "Content-Type": content_type,
        "Origin": "https://claude.ai",
        "Priority": "u=1, i",
        "Referer": referer,
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": platform,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": sec_fetch_site,
        "User-Agent": ua,
        "anthropic-client-platform": "web_claude_ai",
        "anthropic-client-version": client_version,
        "anthropic-client-sha": client_sha,
        "anthropic-anonymous-id": anonymous_id,
        "anthropic-device-id": device_id,
        "x-activity-session-id": activity_session_id,
        "x-datadog-trace-id": datadog_trace_id,
        "x-datadog-parent-id": datadog_parent_id,
        "x-datadog-sampling-priority": "1",
        "traceparent": _datadog_traceparent(datadog_trace_id, datadog_parent_id),
        "tracestate": "dd=s:1;o:rum",
        "baggage": (
            f"sentry-environment=production,sentry-release={client_sha},"
            f"sentry-public_key={sentry_public_key},sentry-trace_id={sentry_trace_id},"
            f"sentry-org_id={sentry_org_id}"
        ),
        "sentry-trace": f"{sentry_trace_id}-{span}",
    }


def apply_proxy(session: curl_requests.Session, proxy: str | None) -> None:
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}


def redact_proxy_url(proxy: str | None) -> str:
    """日志里展示代理时隐藏用户名/密码。"""
    if not proxy:
        return ""
    if "://" in proxy:
        parsed = urlsplit(proxy)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"***:***@{host}{port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return proxy
    parts = proxy.split(":", 3)
    if len(parts) == 4:
        host, port, _user, _pwd = parts
        return f"{host}:{port}:***:***"
    return proxy


def materialize_session_proxy(template: str | None) -> tuple[str, str] | None:
    """把代理模板里的 {session}/{SESSION} 占位符替换成唯一 id，返回 (具体代理URL, session_id)。

    对标 gopay config 的 {SESSION} 模式：每个账号调一次，拿一个独立粘性 IP。
    模板形如：http://storm-xxx_session-{session}_life-1:PWD@us.stormip.cn:1100
    template 为空/None 或不含占位符 → 返回 None（直连或按字面用）。
    """
    if not template:
        return None
    session_id = secrets.token_hex(8)
    concrete = _SESSION_PLACEHOLDER.sub(session_id, template)
    if concrete == template:
        # 不含占位符：当作具体代理，session_id 留空
        return (concrete, "")
    return (concrete, session_id)
