"""Claude 账号 KYC 状态检测——注册成功后用 sessionKey 查 KYC，用于分流。

移植自 kyc_app.py 的 check_kyc_status：
  GET /api/organizations                 → 拿 org_uuid（兼作 sessionKey 有效性校验）
  GET /api/organizations/{org_uuid}/kyc_status → status
返回 (is_alive, status)，status ∈ not_required/approved/pending/denied/dead/error。
is_alive=True 表示 sessionKey 有效（能拿到组织列表）。

依赖: curl-cffi>=0.7.0
"""

from __future__ import annotations

import logging
import uuid

from curl_cffi import requests as curl_requests

from claude_register.core.browser import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_SEC_CH_UA_PLATFORM,
    DEFAULT_SENTRY_ORG_ID,
    DEFAULT_SENTRY_PUBLIC_KEY,
    DEFAULT_UA,
    BrowserIdentity,
    BrowserProfile,
    BrowserRuntime,
    build_browser_headers,
    build_session,
    init_browser_cookies,
    new_browser_runtime,
)
from claude_register.core.run_guard import ExternalRunNotConfirmed, confirmed_external_args
from claude_register.core.diagnostics import safe_error_label

log = logging.getLogger("kyc")

BASE = "https://claude.ai"


def check_kyc_status(
    session_key: str,
    proxy: str | None = None,
    impersonate: str | None = None,
    timeout: int = 15,
    profile: BrowserProfile | None = None,
    browser_runtime: BrowserRuntime | None = None,
    anonymous_id: str | None = None,
    device_id: str | None = None,
    sentry_trace_id: str | None = None,
    client_sha: str | None = None,
    session: curl_requests.Session | None = None,
    identity: BrowserIdentity | None = None,
) -> tuple[bool, str]:
    """返回 (is_alive, status)。dead 只表示明确的登录态失效，error 表示检测异常。"""
    if identity is not None:
        profile = identity.profile
        browser_runtime = identity.runtime
        anonymous_id = identity.anonymous_id
        device_id = identity.device_id
        sentry_trace_id = identity.sentry_trace_id
    device_id = device_id or str(uuid.uuid4())
    anonymous_id = anonymous_id or f"claudeai.v1.{uuid.uuid4()}"
    sentry_trace_id = sentry_trace_id or uuid.uuid4().hex
    runtime = browser_runtime or new_browser_runtime()
    own_session = session is None
    if session is None:
        session = build_session(
            impersonate,
            proxy,
            profile=profile,
            browser_runtime=runtime,
            identity=identity,
        )
    if own_session:
        init_browser_cookies(
            session,
            anonymous_id,
            device_id,
            color_scheme=profile.color_scheme if profile else "light",
            browser_runtime=runtime,
            identity=identity,
        )
        session.cookies.set("sessionKey", session_key, domain=".claude.ai")
        session.cookies.set("anthropic-device-id", device_id, domain=".claude.ai")

    # KYC 检测是注册后独立调用，client_sha 用默认值即可（仅查状态，不创建资源）。
    from claude_register.onboarding.service import CLIENT_SHA
    headers = build_browser_headers(
        client_sha=client_sha or CLIENT_SHA,
        anonymous_id=anonymous_id,
        device_id=device_id,
        sentry_trace_id=sentry_trace_id,
        referer=f"{BASE}/chats",
        sentry_public_key=DEFAULT_SENTRY_PUBLIC_KEY,
        sentry_org_id=DEFAULT_SENTRY_ORG_ID,
        ua=profile.ua if profile else DEFAULT_UA,
        sec_ch_ua=profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA,
        platform=profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM,
        accept_language=profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE,
        activity_session_id=runtime.activity_session_id,
        datadog_trace_id=runtime.datadog_trace_id,
    )

    try:
        resp = session.get(f"{BASE}/api/organizations", headers=headers, timeout=timeout)
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                return False, "dead"
            return False, "error"
        data = resp.json()
        if not (isinstance(data, list) and data):
            return True, "error"
        org_uuid = data[0].get("uuid")
        if not org_uuid:
            return True, "error"
        resp_kyc = session.get(
            f"{BASE}/api/organizations/{org_uuid}/kyc_status", headers=headers, timeout=timeout
        )
        if resp_kyc.status_code != 200:
            return True, "error"
        return True, resp_kyc.json().get("status", "error")
    except Exception as e:
        log.debug("[kyc] 检测异常: %s", safe_error_label(e))
        return False, "error"
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def main() -> int:
    try:
        args = confirmed_external_args()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args:
        print("用法: python kyc.py <session_key> [proxy]")
        return 2
    sk = args[0]
    proxy = args[1] if len(args) > 1 else None
    alive, status = check_kyc_status(sk, proxy)
    print({"alive": alive, "status": status})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
