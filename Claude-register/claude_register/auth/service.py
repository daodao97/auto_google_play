"""
Claude 注册全流程编排：发 magic link → 抓邮件 → verify → onboarding。

把四段串成可运行的链路：

    [0 发信]  send_magic_link(session, email)
              ⚠️ arkose 走 cookie——session 必须是跑过 Arkose 的浏览器会话（cookie 里有 arkose 校验）
    [1 抓邮件] mail_fetcher_client.fetch_magic_link(email, pwd) → nonce + base64 邮箱
    [2 verify] POST /api/auth/verify_magic_link {nonce, encoded_email, arkose_session_token}
              arkose_session_token 由 arkose.resolve_arkose_token 三层降级解析：
                透传 passive_token → yescaptcha 打码 → 直接重放
              → 创建账号 + Set-Cookie 登录态（session 自动保留）
    [3 onboarding] 用同一 session 跑完 9 步

指纹层：所有 claude.ai 请求走 curl_cffi impersonate（chrome142），TLS 层伪装真 Chrome
（对标 gopay-pipeline）。发信（阶段 0）的 arkose 仍走 cookie，需跑过 Arkose 的浏览器
session——指纹升级不解决该步，仅提升整体 TLS 真实度。

⚠️ 合规：自动化批量注册违反 Anthropic 服务条款，账号/组织/IP 可能被封。
   公司场景建议优先走 Claude Team 管理员邀请。本脚本仅供学习与研究。

依赖: pip install curl-cffi>=0.7.0 requests>=2.31.0
取信统一通过 mail.xcaigc.com tRPC 服务。
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
import uuid
from typing import Any

from curl_cffi import requests as curl_requests

from claude_register.config.dynamic import DEFAULT_CLIENT_VERSION, fetch_dynamic_config
from claude_register.core.browser import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_SEC_CH_UA_PLATFORM,
    DEFAULT_SENTRY_ORG_ID,
    DEFAULT_SENTRY_PUBLIC_KEY,
    DEFAULT_UA,
    BrowserIdentity,
    BrowserRuntime,
    BrowserProfile,
    attach_browser_identity,
    build_browser_headers,
    build_session,
    init_browser_cookies,
    new_browser_identity,
    warm_claude_bootstrap,
    warm_claude_login,
)
from claude_register.mail.fetcher import fetch_magic_link, prime_seen
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.onboarding.service import CLIENT_SHA, OnboardingContext, onboarding_completed, run_onboarding
from claude_register.challenge.arkose import ArkoseConfig, config_from_dict, resolve_arkose_token
from claude_register.core.diagnostics import safe_error_label

# 美国夏令时各时区偏移（分钟），按人口比例加权：东部/中部各占双份。
_US_UTC_OFFSETS_DST = [-240, -240, -300, -300, -360, -420]

BASE = "https://claude.ai"
CONFIG_PATH = "runtime/config.json"
log = logging.getLogger("register")


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """读本地运行配置；找不到返回空 dict（用代码默认值）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def new_identity() -> tuple[str, str]:
    """生成一对客户端身份 (anonymous_id, device_id)。整个账号流程复用同一对，
    不应每请求换——真浏览器里这俩是 localStorage 长期稳定的，换动既不符规范又更易被风控。"""
    return f"claudeai.v1.{uuid.uuid4()}", str(uuid.uuid4())


def _auth_headers(
    client_sha: str = CLIENT_SHA,
    referer: str = f"{BASE}/login",
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    client_version: str = "1.0.0",
    identity: BrowserIdentity | None = None,
) -> dict[str, str]:
    if identity is not None:
        anonymous_id = identity.anonymous_id
        device_id = identity.device_id
        sentry_trace_id = identity.sentry_trace_id
        profile = identity.profile
        browser_runtime = identity.runtime
    return build_browser_headers(
        client_sha=client_sha,
        anonymous_id=anonymous_id or f"claudeai.v1.{uuid.uuid4()}",
        device_id=device_id or str(uuid.uuid4()),
        sentry_trace_id=sentry_trace_id or uuid.uuid4().hex,
        referer=referer,
        sentry_public_key=sentry_public_key,
        sentry_org_id=sentry_org_id,
        client_version=client_version,
        ua=profile.ua if profile else DEFAULT_UA,
        sec_ch_ua=profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA,
        platform=profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM,
        accept_language=profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE,
        activity_session_id=browser_runtime.activity_session_id if browser_runtime else None,
        datadog_trace_id=browser_runtime.datadog_trace_id if browser_runtime else None,
    )


def _normalize_dynamic_config(values: tuple[Any, ...]) -> tuple[str, list[dict[str, Any]], str, str, str]:
    if len(values) == 5:
        return values  # type: ignore[return-value]
    if len(values) == 4:
        client_sha, legal_docs, sentry_key, sentry_org = values
        return client_sha, legal_docs, sentry_key, sentry_org, DEFAULT_CLIENT_VERSION
    raise ValueError(f"动态配置返回值数量异常: {len(values)}")


def login_methods(
    session: curl_requests.Session,
    email: str,
    *,
    client_sha: str = CLIENT_SHA,
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    client_version: str = "1.0.0",
    identity: BrowserIdentity | None = None,
) -> list[str]:
    """阶段 0.1 预检：GET /api/auth/login_methods → 返回该邮箱可用的登录方式。"""
    resp = session.get(
        f"{BASE}/api/auth/login_methods",
        params={"email": email, "source": "claude-ai"},
        headers=_auth_headers(client_sha, anonymous_id=anonymous_id, device_id=device_id,
                              sentry_trace_id=sentry_trace_id,
                              sentry_public_key=sentry_public_key, sentry_org_id=sentry_org_id,
                              profile=profile, browser_runtime=browser_runtime,
                              client_version=client_version, identity=identity),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("methods", [])


def send_magic_link(
    session: curl_requests.Session,
    email: str,
    *,
    login_intent: str | None = None,
    utc_offset: int | None = None,
    locale: str = "en-US",
    client_sha: str = CLIENT_SHA,
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    client_version: str = "1.0.0",
    identity: BrowserIdentity | None = None,
) -> dict[str, Any]:
    """阶段 0.3 发信：POST /api/auth/send_magic_link → sent:true。

    utc_offset=None 时随机选一个美国夏令时偏移，使不同账号申报不同时区。
    """
    if utc_offset is None:
        utc_offset = random.choice(_US_UTC_OFFSETS_DST)
    body = {
        "utc_offset": utc_offset,
        "email_address": email,
        "login_intent": login_intent,
        "locale": locale,
        "return_to": None,
        "source": "claude",
    }
    resp = session.post(
        f"{BASE}/api/auth/send_magic_link",
        headers=_auth_headers(client_sha, anonymous_id=anonymous_id, device_id=device_id,
                              sentry_trace_id=sentry_trace_id,
                              sentry_public_key=sentry_public_key, sentry_org_id=sentry_org_id,
                              profile=profile, browser_runtime=browser_runtime,
                              client_version=client_version, identity=identity),
        data=json.dumps(body),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"send_magic_link HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("sent"):
        raise RuntimeError("send_magic_link 未发送")
    return data


def verify_magic_link(
    session: curl_requests.Session,
    nonce: str,
    encoded_email_address: str,
    arkose_session_token: str,
    *,
    client_sha: str = CLIENT_SHA,
    locale: str = "en-US",
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY,
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    client_version: str = "1.0.0",
    identity: BrowserIdentity | None = None,
) -> dict[str, Any]:
    """阶段 2：POST /api/auth/verify_magic_link —— 用 magic link 换登录态，账号在此创建。"""
    body = {
        "credentials": {
            "method": "nonce",
            "nonce": nonce,
            "encoded_email_address": encoded_email_address,
        },
        "locale": locale,
        "arkose_session_token": arkose_session_token,
        "source": "claude",
    }
    resp = session.post(
        f"{BASE}/api/auth/verify_magic_link",
        headers=_auth_headers(client_sha, referer=f"{BASE}/magic-link",
                              anonymous_id=anonymous_id, device_id=device_id,
                              sentry_trace_id=sentry_trace_id,
                              sentry_public_key=sentry_public_key, sentry_org_id=sentry_org_id,
                              profile=profile, browser_runtime=browser_runtime,
                              client_version=client_version, identity=identity),
        data=json.dumps(body),
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"verify_magic_link HTTP {resp.status_code}")
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError("verify_magic_link 失败")
    return data


def _extract_org_uuid(verify_resp: dict[str, Any]) -> str:
    """从 verify 响应里取 org_uuid（onboarding 建会话要用）。"""
    memberships = verify_resp.get("account", {}).get("memberships") or []
    if not memberships:
        raise RuntimeError("verify 响应里没有 memberships，拿不到 org_uuid")
    org_uuid = memberships[0].get("organization", {}).get("uuid")
    if not org_uuid:
        raise RuntimeError("verify 响应里没有 organization.uuid")
    return org_uuid


def register(
    email: str,
    password: str,
    arkose_session_token: str | None = None,
    *,
    display_name: str,
    full_name: str | None = None,
    work_function: str = "Other",
    login_intent: str | None = None,
    mail_provider: str = "mailcom",
    mail_client_id: str = "",
    mail_refresh_token: str = "",
    send_session: curl_requests.Session | None = None,
    arkose_config: ArkoseConfig | None = None,
    impersonate: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """跑完整条注册链路。

    0. （可选）发 magic link——仅当传入 send_session（跑过 Arkose 的浏览器 session，带 cookie）
    1. 抓 magic link（轮询直到邮件到达）
    2. verify_magic_link 换登录态。arkose_session_token 为空时用 arkose_config 三层降级解析
       （透传 → yescaptcha → 直接重放）。
    3. onboarding 跑完 9 步

    返回 {send, magic_link, verify, onboarding} 汇总。
    """
    send_result: dict[str, Any] | None = None

    # 指纹/代理：参数优先，回退到 arkose_config。
    px = proxy or (arkose_config.proxy if arkose_config else None)
    configured_impersonate = impersonate or (arkose_config.impersonate if arkose_config else None)
    identity = new_browser_identity(impersonate=configured_impersonate)
    profile = identity.profile
    session = build_session(proxy=px, identity=identity)
    init_browser_cookies(
        session,
        identity.anonymous_id,
        identity.device_id,
        identity=identity,
    )
    warm_claude_login(session)
    if send_session is not None:
        attach_browser_identity(send_session, identity)
        init_browser_cookies(
            send_session,
            identity.anonymous_id,
            identity.device_id,
            identity=identity,
        )
        warm_claude_login(send_session)

    # 动态配置（client_sha + legal_docs + sentry + client_version）——best-effort，失败回退默认值。
    log.info("[*] 动态配置: client_sha + legal_docs + sentry + client_version")
    client_sha, legal_docs, sentry_key, sentry_org, client_version = _normalize_dynamic_config(
        fetch_dynamic_config(session)
    )

    # ── prime_seen：发信前标记现有邮件，后续只取新到的 ──
    mail_seen: set[str] | None = None
    mail_not_before_ms: float | None = None
    if send_session is not None:
        try:
            mail_seen = prime_seen(
                email, password,
                provider=mail_provider,
                client_id=mail_client_id,
                refresh_token=mail_refresh_token,
            )
        except Exception:
            mail_seen = None

    # ── 0. 发信（可选）──
    if send_session is not None:
        log.info("[0/3] send_magic_link")
        mail_not_before_ms = time.time() * 1000
        try:
            methods = login_methods(send_session, email, client_sha=client_sha,
                                    sentry_public_key=sentry_key, sentry_org_id=sentry_org,
                                    client_version=client_version, identity=identity)
            log.info("  login_methods available=%s", bool(methods))
        except Exception as e:
            log.warning("  login_methods 预检失败（非致命）: %s", safe_error_label(e))
        send_result = send_magic_link(
            send_session, email, login_intent=login_intent, client_sha=client_sha,
            sentry_public_key=sentry_key, sentry_org_id=sentry_org,
            client_version=client_version, identity=identity,
        )
        log.info("  sent=%s", send_result.get("sent"))
        time.sleep(random.uniform(2.5, 5.5))

    # ── 1. 抓 magic link ──
    log.info("[1/3] 抓 magic link")
    ml = fetch_magic_link(
        email, password,
        provider=mail_provider,
        seen=mail_seen,
        not_before_ms=mail_not_before_ms,
        client_id=mail_client_id,
        refresh_token=mail_refresh_token,
    )
    log.info("  magic_link received=%s", bool(ml.get("nonce")))

    # ── 解析 arkose token（紧贴 verify 前，避免 token 在发信+抓邮件期间空耗过期）──
    if not arkose_session_token:
        log.info("[*] arkose_session_token 未提供，走 config 三层解析")
        arkose_session_token = resolve_arkose_token(arkose_config, profile=profile)

    warm_claude_bootstrap(
        session,
        client_sha=client_sha,
        sentry_public_key=sentry_key,
        sentry_org_id=sentry_org,
        client_version=client_version,
        identity=identity,
    )

    # ── 2. verify ──
    log.info("[2/3] verify_magic_link（创建账号）")
    verify_resp = verify_magic_link(
        session, ml["nonce"], ml["encoded_email_address"], arkose_session_token,
        client_sha=client_sha,
        sentry_public_key=sentry_key, sentry_org_id=sentry_org,
        client_version=client_version, identity=identity,
    )
    org_uuid = _extract_org_uuid(verify_resp)
    log.info("  created=%s  organization_present=%s",
             verify_resp.get("created"), bool(org_uuid))

    # sessionKey（sk-ant-sid02-...）由 verify 的 Set-Cookie 下发，onboarding 同一 session 复用。
    # routingHint（sk-ant-rh-... JWT）是路由/刷新 token，和 sessionKey 一起交付。
    session_key = session.cookies.get("sessionKey") or ""
    routing_hint = session.cookies.get("routingHint") or ""
    if session_key:
        log.info("  sessionKey present=true")
    else:
        log.warning("  ⚠️ 未抓到 sessionKey cookie")
    if routing_hint:
        log.info("  routingHint present=true")
    warm_claude_bootstrap(
        session,
        client_sha=client_sha,
        org_uuid=org_uuid,
        sentry_public_key=sentry_key,
        sentry_org_id=sentry_org,
        client_version=client_version,
        identity=identity,
    )

    # ── 3. onboarding（同一身份 + 同一 session + 动态 legal_docs）──
    log.info("[3/3] onboarding（用同一 session）")
    ctx = OnboardingContext(
        session=session,
        org_uuid=org_uuid,
        display_name=display_name,
        full_name=full_name,
        work_function=work_function,
        client_sha=client_sha,
        legal_docs=legal_docs,
        sentry_public_key=sentry_key,
        sentry_org_id=sentry_org,
        client_version=client_version,
        identity=identity,
    )
    onboarding_result = run_onboarding(ctx)

    return {"send": send_result, "magic_link": ml, "verify": verify_resp,
            "session_key": session_key, "routing_hint": routing_hint,
            "onboarding": onboarding_result}


def main() -> int:
    try:
        require_external_confirmation()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    print("Authorized manual run confirmed.\n")

    cfg = load_config()
    arkose_config = config_from_dict(cfg)
    impersonate = cfg.get("impersonate")
    proxy = cfg.get("proxy") or None

    email = cfg.get("email") or ""
    password = cfg.get("password") or ""
    from claude_register.shared.names import random_american_name
    display_name = cfg.get("display_name") or random_american_name()
    if not email or not password:
        print("请先在 runtime/config.json 填 email/password，或改用 Web 批量入口。")
        return 2
    arkose_session_token = ""          # 留空 → 走 config 的 passive_token/solver/replay
    send_session = None                # 如需自动发信，把跑过 Arkose 的浏览器 cookie 灌进 curl_cffi session 传这

    try:
        summary = register(
            email, password, arkose_session_token or None,
            display_name=display_name,
            send_session=send_session,
            arkose_config=arkose_config,
            impersonate=impersonate, proxy=proxy,
            mail_provider=cfg.get("mail_provider", "mailcom"),
            mail_client_id=cfg.get("mail_client_id", ""),
            mail_refresh_token=cfg.get("mail_refresh_token", ""),
        )
    except Exception as e:
        print(f"\nRun failed: {safe_error_label(e)}")
        return 2

    print("\n=== 完成 ===")
    safe_summary = {
        "sent": bool((summary.get("send") or {}).get("sent")),
        "magic_link_received": bool((summary.get("magic_link") or {}).get("nonce")),
        "created": bool((summary.get("verify") or {}).get("created")),
        "has_session_key": bool(summary.get("session_key")),
        "has_routing_hint": bool(summary.get("routing_hint")),
        "onboarding_completed": onboarding_completed(summary.get("onboarding")),
    }
    print(json.dumps(safe_summary, ensure_ascii=False, indent=2, default=str)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
