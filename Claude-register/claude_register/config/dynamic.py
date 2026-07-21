"""动态配置——从 claude.ai 实时获取 CLIENT_SHA 和 legal_docs document_id。

对标 claude_register_v2.py 的 _fetch_dynamic_config：避免硬编码值随前端发版 /
法务文档更新而过期。所有获取均 best-effort，失败回退到默认值，不抛异常
（动态配置是优化项，不应阻断注册主流程）。

两个动态源：
  - CLIENT_SHA：GET /login → 收集 JS chunk URL → 下载 JS → 正则提取
                anthropic-client-sha（前端构建哈希，随发版变）。
  - legal_docs：GET /api/legal → {"aup":"v3:aup:xxx","consumer-terms":"...","privacy":"..."}
                （document_id 版本化，随法务更新变）。

依赖: curl-cffi>=0.7.0
"""

from __future__ import annotations

import logging
import re
from typing import Any

from curl_cffi import requests as curl_requests

from claude_register.core.browser import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_SEC_CH_UA_PLATFORM,
    DEFAULT_SENTRY_ORG_ID,
    DEFAULT_SENTRY_PUBLIC_KEY,
    DEFAULT_UA,
    build_session,
)
from claude_register.core.diagnostics import safe_error_label

log = logging.getLogger("dynamic_config")

BASE = "https://claude.ai"

# ── 回退默认值（HAR 抓取，会过期——动态获取失败时兜底用）───────────────────
DEFAULT_CLIENT_SHA = "cbdcff92c28f90f26b8b9e9dfb4ae8e20b1eb957"
DEFAULT_LEGAL_DOCS: list[dict[str, Any]] = [
    {"document_id": "v3:aup:22742366-2ef0-4c7a-a833-6523f10d3944", "accepted_via_checkbox": True},
    {"document_id": "v3:consumer-terms:79dbc8c6-7f64-43d6-8101-207cede59a4d", "accepted_via_checkbox": True},
    {"document_id": "v3:privacy:cf9b9ac4-d387-48b8-8560-ce1c58b8a34b", "accepted_via_checkbox": False},
]

# anthropic-client-sha 是 40 位 hex，在 app chunk 里以字符串字面量出现。
_SHA_RE = re.compile(r'anthropic-client-sha["\\\',]+\s*["\\\']?([a-f0-9]{40})')
# Sentry DSN：https://<32hex>@o<orgid>.ingest.sentry.io/
_SENTRY_RE = re.compile(r'https://([a-f0-9]{32})@o(\d+)\.ingest\.sentry\.io/')
# anthropic-client-version：如 "1.0.0" 或 "2.3.1"
_VERSION_RE = re.compile(r'anthropic-client-version["\\\',]+\s*["\\\']?([\d]+\.[\d.]*[\d])')

DEFAULT_CLIENT_VERSION = "1.0.0"
_JS_URL_PATTERNS = [
    re.compile(r'(https://assets\.claude\.ai/[^"\'>\s]+\.js)'),
    re.compile(r'["\']?(/_next/static/chunks/[^"\'>\s]+\.js)'),
]
# 超大 JS 是 vendor/polyfill，不含 client_sha/sentry，跳过省流量。
_MAX_JS_BYTES = 500_000
_BOOTSTRAP_PARAMS = {
    "statsig_hashing_algorithm": "djb2",
    "growthbook_format": "sdk",
    "include_system_prompts": "false",
}


def _collect_js_urls(html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for rx in _JS_URL_PATTERNS:
        for m in rx.finditer(html):
            u = m.group(1)
            if not u.startswith("http"):
                u = f"{BASE}{u}"
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def _profile_header_values(session: curl_requests.Session) -> tuple[str, str, str, str]:
    profile = getattr(session, "_browser_profile", None)
    return (
        profile.ua if profile else DEFAULT_UA,
        profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA,
        profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM,
        profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE,
    )


def _find_string_by_key(obj: Any, keys: set[str]) -> str:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower().replace("-", "_") in keys and isinstance(value, str):
                return value
        for value in obj.values():
            found = _find_string_by_key(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_string_by_key(item, keys)
            if found:
                return found
    return ""


def _fetch_bootstrap_config(
    session: curl_requests.Session,
    timeout: int = 15,
) -> tuple[str, str]:
    """GET /edge-api/bootstrap 兜底提取 (client_sha, client_version)。失败返回空串。"""
    ua, sec_ch_ua, platform, accept_language = _profile_header_values(session)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": accept_language,
        "Referer": f"{BASE}/magic-link",
        "Sec-CH-UA": sec_ch_ua,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": platform,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": ua,
    }
    try:
        resp = session.get(
            f"{BASE}/edge-api/bootstrap",
            params=_BOOTSTRAP_PARAMS,
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.warning("[dyn] GET /edge-api/bootstrap -> %s，bootstrap 配置跳过", resp.status_code)
            return "", ""
        try:
            data = resp.json()
        except Exception:
            data = {}
        sha = _find_string_by_key(
            data,
            {"client_sha", "clientsha", "anthropic_client_sha", "anthropicclientsha"},
        )
        version = _find_string_by_key(
            data,
            {"client_version", "clientversion", "anthropic_client_version", "anthropicclientversion"},
        )
        text = getattr(resp, "text", "") or ""
        if not isinstance(text, str):
            text = ""
        if not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
            m = re.search(r'"(?:client_sha|clientSha|anthropic_client_sha)"\s*:\s*"([a-f0-9]{40})"', text)
            sha = m.group(1) if m else ""
        if not re.fullmatch(r"[\d]+\.[\d.]*[\d]", version or ""):
            m = re.search(r'"(?:client_version|clientVersion|anthropic_client_version)"\s*:\s*"([\d]+\.[\d.]*[\d])"', text)
            version = m.group(1) if m else ""
        if sha:
            log.info("[dyn] bootstrap client_sha found")
        if version:
            log.info("[dyn] bootstrap client_version found")
        return sha, version
    except Exception as e:
        log.warning("[dyn] GET /edge-api/bootstrap 异常: %s，bootstrap 配置跳过",
                    safe_error_label(e))
        return "", ""


def fetch_client_sha(
    session: curl_requests.Session | None = None,
    *,
    impersonate: str | None = None,
    proxy: str | None = None,
    timeout: int = 15,
) -> str:
    """GET /login → 收集 JS chunk → 下载 → 正则提取 anthropic-client-sha。

    失败（网络异常 / 找不到）返回 DEFAULT_CLIENT_SHA，不抛异常。
    session 可传入复用（带代理/指纹）；None 时新建一个。
    """
    own_session = session is None
    if session is None:
        session = build_session(impersonate, proxy)
    try:
        sha, _key, _org, _ver = _fetch_js_config(session, timeout=timeout)
        return sha
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def fetch_sentry_config(
    session: curl_requests.Session | None = None,
    *,
    impersonate: str | None = None,
    proxy: str | None = None,
    timeout: int = 15,
) -> tuple[str, str]:
    """从 JS chunk 提取 Sentry (public_key, org_id)。失败回退默认值。

    和 client_sha 同源（都在 app chunk 里），_fetch_js_config 一次遍历同时提取。
    """
    own_session = session is None
    if session is None:
        session = build_session(impersonate, proxy)
    try:
        _sha, key, org, _ver = _fetch_js_config(session, timeout=timeout)
        return key, org
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _fetch_js_config(
    session: curl_requests.Session,
    timeout: int = 15,
) -> tuple[str, str, str, str]:
    """一次遍历 JS chunk 同时提取 (client_sha, sentry_public_key, sentry_org_id, client_version)。

    任一未找到用默认值。全部找到后立即停止遍历，避免多下无用 JS。
    """
    ua, sec_ch_ua, platform, accept_language = _profile_header_values(session)
    page_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": accept_language,
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
        resp = session.get(
            f"{BASE}/login",
            headers=page_headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.warning("[dyn] GET /login -> %s，JS 配置用默认值", resp.status_code)
            return DEFAULT_CLIENT_SHA, DEFAULT_SENTRY_PUBLIC_KEY, DEFAULT_SENTRY_ORG_ID, DEFAULT_CLIENT_VERSION
        html = resp.text
    except Exception as e:
        log.warning("[dyn] GET /login 异常: %s，JS 配置用默认值", safe_error_label(e))
        return DEFAULT_CLIENT_SHA, DEFAULT_SENTRY_PUBLIC_KEY, DEFAULT_SENTRY_ORG_ID, DEFAULT_CLIENT_VERSION

    sha = DEFAULT_CLIENT_SHA
    sentry_key = DEFAULT_SENTRY_PUBLIC_KEY
    sentry_org = DEFAULT_SENTRY_ORG_ID
    client_version = DEFAULT_CLIENT_VERSION
    sha_found = False
    sentry_found = False
    version_found = False
    for url in _collect_js_urls(html):
        if sha_found and sentry_found and version_found:
            break
        try:
            r = session.get(
                url,
                headers={
                    "Accept": "*/*",
                    "Accept-Language": accept_language,
                    "Referer": f"{BASE}/login",
                    "Sec-CH-UA": sec_ch_ua,
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": platform,
                    "User-Agent": ua,
                },
                timeout=timeout,
            )
            if r.status_code != 200 or len(r.text) > _MAX_JS_BYTES:
                continue
            txt = r.text
            if not sha_found:
                m = _SHA_RE.search(txt)
                if m:
                    sha = m.group(1)
                    sha_found = True
                    log.info("[dyn] client_sha found")
            if not sentry_found:
                m = _SENTRY_RE.search(txt)
                if m:
                    sentry_key = m.group(1)
                    sentry_org = m.group(2)
                    sentry_found = True
                    log.info("[dyn] sentry config found")
            if not version_found:
                m = _VERSION_RE.search(txt)
                if m:
                    client_version = m.group(1)
                    version_found = True
                    log.info("[dyn] client_version found")
        except Exception:
            continue
    if not sha_found:
        log.warning("[dyn] JS chunk 里没找到 client_sha，用默认值")
    if not sentry_found:
        log.warning("[dyn] JS chunk 里没找到 sentry DSN，用默认值")
    if not version_found:
        log.warning("[dyn] JS chunk 里没找到 client_version，用默认值")
    return sha, sentry_key, sentry_org, client_version


def fetch_legal_docs(
    session: curl_requests.Session | None = None,
    *,
    impersonate: str | None = None,
    proxy: str | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """GET /api/legal → 映射成 onboarding 要的 acceptances 列表。

    失败返回 DEFAULT_LEGAL_DOCS，不抛异常。
    """
    own_session = session is None
    if session is None:
        session = build_session(impersonate, proxy)
    try:
        ua, sec_ch_ua, platform, accept_language = _profile_header_values(session)
        api_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": accept_language,
            "Referer": f"{BASE}/login",
            "Sec-CH-UA": sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": platform,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": ua,
        }
        try:
            resp = session.get(
                f"{BASE}/api/legal",
                headers=api_headers,
                timeout=timeout,
            )
            if resp.status_code != 200:
                log.warning("[dyn] GET /api/legal -> %s，legal_docs 用默认值", resp.status_code)
                return list(DEFAULT_LEGAL_DOCS)
            docs = resp.json()
            if not isinstance(docs, dict) or not docs.get("aup"):
                log.warning("[dyn] /api/legal 响应非预期格式，legal_docs 用默认值")
                return list(DEFAULT_LEGAL_DOCS)
        except Exception as e:
            log.warning("[dyn] GET /api/legal 异常: %s，legal_docs 用默认值", safe_error_label(e))
            return list(DEFAULT_LEGAL_DOCS)

        built = [
            {"document_id": docs["aup"], "accepted_via_checkbox": True},
            {"document_id": docs["consumer-terms"], "accepted_via_checkbox": True},
            {"document_id": docs["privacy"], "accepted_via_checkbox": False},
        ]
        log.info("[dyn] legal_docs: OK")
        return built
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def fetch_dynamic_config(
    session: curl_requests.Session | None = None,
    *,
    impersonate: str | None = None,
    proxy: str | None = None,
) -> tuple[str, list[dict[str, Any]], str, str, str]:
    """一次拿 (client_sha, legal_docs, sentry_public_key, sentry_org_id, client_version)。

    session 复用时只发一次 /login + 遍历 JS chunk + 一次 /api/legal。
    五个值各自 best-effort：任一失败用默认值，不抛异常。
    """
    own_session = session is None
    if session is None:
        session = build_session(impersonate, proxy)
    try:
        sha, sentry_key, sentry_org, client_version = _fetch_js_config(session)
        bootstrap_sha, bootstrap_version = _fetch_bootstrap_config(session)
        if sha == DEFAULT_CLIENT_SHA and bootstrap_sha:
            sha = bootstrap_sha
        if client_version == DEFAULT_CLIENT_VERSION and bootstrap_version:
            client_version = bootstrap_version
        docs = fetch_legal_docs(session)
        return sha, docs, sentry_key, sentry_org, client_version
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass
