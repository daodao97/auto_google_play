"""
Claude onboarding 自动化脚本

输入「登录态 session」（即 verify_magic_link 成功后拿到 cookie 的 curl_cffi Session），
按 HAR 里 captured 的时序，自动跑完 onboarding 全流程：
接受法律文档 → 年龄/邮件同意 → 设名字 → 建首个会话 → 工作职能。

指纹层：所有 claude.ai 请求走 curl_cffi impersonate（chrome142），TLS 层伪装真 Chrome
（对标 gopay-pipeline），每个请求显式带 impersonate=ctx.impersonate。

本脚本只覆盖阶段 3（onboarding）。阶段 0（发 magic link）、阶段 1（Arkose）、
阶段 2（verify_magic_link）不在本脚本范围内——那些涉及 Arkose 反爬，需另行处理，
拿到登录态后再喂给本脚本。

依赖: pip install curl-cffi>=0.7.0
"""

from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests as curl_requests

from claude_register.config.dynamic import DEFAULT_CLIENT_SHA, DEFAULT_LEGAL_DOCS
from claude_register.core.browser import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_SEC_CH_UA_PLATFORM,
    DEFAULT_SENTRY_ORG_ID,
    DEFAULT_SENTRY_PUBLIC_KEY,
    DEFAULT_UA,
    BrowserIdentity,
    BrowserRuntime,
    IMPERSONATE_DEFAULT,
    build_browser_headers,
    build_session,
)
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.orchestration.errors import FlowCancelled

BASE = "https://claude.ai"

log = logging.getLogger("onboarding")
OPTIONAL_ONBOARDING_STEPS = {"privacy_consents", "grove"}

# 首个对话名随机池——避免所有账号对话名完全相同而被批量关联。
_FIRST_CHAT_NAMES = [
    "Hello", "Hi there", "Getting started", "New conversation", "Let's chat",
    "Introduction", "First message", "Hello Claude", "Exploring", "Questions",
    "General chat", "Hi", "Conversation", "Testing things out", "Just getting started",
    "Learning", "Help me out", "New chat", "Welcome", "Starting out",
]


# ── 会随发版 / 法务更新而变的常量 ──────────────────────────────────────────
# anthropic-client-sha：前端构建哈希。默认值是 HAR 抓取的快照（会过期），
# register/orchestrator 在 verify 前会调 dynamic_config.fetch_client_sha() 拿当前值
# 覆盖默认值；动态获取失败时回退到 DEFAULT_CLIENT_SHA。
CLIENT_SHA = DEFAULT_CLIENT_SHA
CLIENT_VERSION = "1.0.0"

# 法律文档 document_id：版本化。默认值是 HAR 快照，register/orchestrator 在 onboarding
# 前会调 dynamic_config.fetch_legal_docs()（GET /api/legal）拿当前值覆盖；失败回退默认值。
DEFAULT_LEGAL_DOCS = DEFAULT_LEGAL_DOCS


@dataclass
class OnboardingContext:
    """一次 onboarding 所需的上下文。"""
    session: curl_requests.Session               # 已带登录 cookie 的 session
    org_uuid: str                                 # verify_magic_link 响应里的 org uuid
    display_name: str
    full_name: str | None = None
    work_function: str = "Other"
    enable_grove: bool = True                     # 3.6 可选开关
    client_sha: str = CLIENT_SHA
    legal_docs: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_LEGAL_DOCS))
    anonymous_id: str = field(default_factory=lambda: f"claudeai.v1.{uuid.uuid4()}")
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    impersonate: str = IMPERSONATE_DEFAULT
    # Sentry：trace_id per-account 复用，register.py 会传入与 send/verify 同一个值。
    sentry_trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sentry_public_key: str = DEFAULT_SENTRY_PUBLIC_KEY
    sentry_org_id: str = DEFAULT_SENTRY_ORG_ID
    # 浏览器指纹字段——来自 BrowserProfile，使不同账号 headers 各异。
    ua: str = ""               # 空串 = 使用 DEFAULT_UA
    sec_ch_ua: str = ""        # 空串 = 使用 DEFAULT_SEC_CH_UA
    platform: str = ""         # 空串 = 使用 DEFAULT_SEC_CH_UA_PLATFORM
    accept_language: str = ""  # 空串 = 使用 DEFAULT_ACCEPT_LANGUAGE
    client_version: str = "1.0.0"
    browser_runtime: BrowserRuntime | None = None
    identity: BrowserIdentity | None = None
    request_timeout: float = 45.0
    cancel_event: threading.Event | None = None

    def __post_init__(self) -> None:
        if self.identity is None:
            return
        self.anonymous_id = self.identity.anonymous_id
        self.device_id = self.identity.device_id
        self.sentry_trace_id = self.identity.sentry_trace_id
        self.impersonate = self.identity.profile.impersonate
        self.ua = self.identity.profile.ua
        self.sec_ch_ua = self.identity.profile.sec_ch_ua
        self.platform = self.identity.profile.platform
        self.accept_language = self.identity.profile.accept_language
        self.browser_runtime = self.identity.runtime


def _headers(ctx: OnboardingContext, referer: str = "https://claude.ai/") -> dict[str, str]:
    runtime = ctx.browser_runtime or getattr(ctx.session, "_browser_runtime", None)
    return build_browser_headers(
        client_sha=ctx.client_sha,
        anonymous_id=ctx.anonymous_id,
        device_id=ctx.device_id,
        sentry_trace_id=ctx.sentry_trace_id,
        referer=referer,
        sentry_public_key=ctx.sentry_public_key,
        sentry_org_id=ctx.sentry_org_id,
        client_version=ctx.client_version,
        ua=ctx.ua or DEFAULT_UA,
        sec_ch_ua=ctx.sec_ch_ua or DEFAULT_SEC_CH_UA,
        platform=ctx.platform or DEFAULT_SEC_CH_UA_PLATFORM,
        accept_language=ctx.accept_language or DEFAULT_ACCEPT_LANGUAGE,
        activity_session_id=runtime.activity_session_id if runtime else None,
        datadog_trace_id=runtime.datadog_trace_id if runtime else None,
    )


def _raise_if_cancelled(ctx: OnboardingContext) -> None:
    if ctx.cancel_event and ctx.cancel_event.is_set():
        raise FlowCancelled()


def _step_delay(ctx: OnboardingContext) -> None:
    """步骤间随机等待，模拟用户在 UI 上的操作时间，避免机械速率特征。"""
    delay = random.uniform(0.8, 3.0)
    if ctx.cancel_event:
        if ctx.cancel_event.wait(delay):
            raise FlowCancelled()
    else:
        time.sleep(delay)


def _ok(resp: curl_requests.Response, step: str) -> bool:
    # onboarding 接口成功状态码是 200/201/202
    if resp.status_code in (200, 201, 202):
        log.info("  ✓ %s -> %s", step, resp.status_code)
        return True
    log.error("  ✗ %s -> %s", step, resp.status_code)
    return False


# ── 各步骤 ──────────────────────────────────────────────────────────────────
def step_start_onboarding(ctx: OnboardingContext) -> bool:
    resp = ctx.session.patch(
        f"{BASE}/api/account/settings",
        headers=_headers(ctx),
        data=json.dumps({"has_started_claudeai_onboarding": True, "has_finished_claudeai_onboarding": False}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.1 start onboarding")


def step_privacy_consents(ctx: OnboardingContext) -> bool:
    headers = _headers(ctx)
    body = {"consent_decision": "CONSENT_DECISION_OPT_IN", "source": "implicit_regional_default"}
    ok = True
    for ct in ("cookies.analytics", "cookies.marketing"):
        resp = ctx.session.put(
            f"{BASE}/v1/privacy-consents",
            headers=headers,
            data=json.dumps({**body, "consent_type": ct}),
            impersonate=ctx.impersonate,
            timeout=ctx.request_timeout,
        )
        ok = _ok(resp, f"3.2 privacy-consents/{ct}") and ok
    return ok


def step_accept_legal_docs(ctx: OnboardingContext) -> bool:
    resp = ctx.session.put(
        f"{BASE}/api/account/accept_legal_docs",
        headers=_headers(ctx),
        data=json.dumps({"acceptances": ctx.legal_docs}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.3 accept_legal_docs")


def step_email_consent(ctx: OnboardingContext) -> bool:
    resp = ctx.session.put(
        f"{BASE}/api/account/email_consent",
        headers=_headers(ctx),
        data=json.dumps({"consent": True, "accepted_via_checkbox": False, "variant": "notices"}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.4 email_consent")


def step_age_verified(ctx: OnboardingContext) -> bool:
    resp = ctx.session.put(
        f"{BASE}/api/account",
        headers=_headers(ctx),
        params={"statsig_hashing_algorithm": "djb2"},
        data=json.dumps({"age_is_verified": True}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.5 age_is_verified")


def step_grove(ctx: OnboardingContext) -> bool:
    if not ctx.enable_grove:
        return True
    resp = ctx.session.patch(
        f"{BASE}/api/account/settings",
        headers=_headers(ctx),
        data=json.dumps({"grove_enabled": True}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.6 grove_enabled (optional)")


def step_set_name(ctx: OnboardingContext) -> bool:
    full = ctx.full_name or ctx.display_name
    resp = ctx.session.put(
        f"{BASE}/api/account",
        headers=_headers(ctx),
        params={"statsig_hashing_algorithm": "djb2"},
        data=json.dumps({"display_name": ctx.display_name, "full_name": full}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.7 set name")


def step_first_chat(ctx: OnboardingContext) -> str | None:
    conv_uuid = str(uuid.uuid4())
    chat_name = random.choice(_FIRST_CHAT_NAMES)
    resp = ctx.session.post(
        f"{BASE}/api/organizations/{ctx.org_uuid}/chat_conversations",
        headers=_headers(ctx),
        data=json.dumps({"uuid": conv_uuid, "name": chat_name}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    if _ok(resp, "3.8 first chat"):
        return conv_uuid
    return None


def step_account_profile(ctx: OnboardingContext) -> bool:
    resp = ctx.session.put(
        f"{BASE}/api/account_profile",
        headers=_headers(ctx),
        data=json.dumps({"work_function": ctx.work_function}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.9 account_profile")


def step_finish_onboarding(ctx: OnboardingContext) -> bool:
    resp = ctx.session.patch(
        f"{BASE}/api/account/settings",
        headers=_headers(ctx),
        data=json.dumps({"has_finished_claudeai_onboarding": True}),
        impersonate=ctx.impersonate,
        timeout=ctx.request_timeout,
    )
    return _ok(resp, "3.10 finish onboarding")


def run_onboarding(ctx: OnboardingContext) -> dict[str, Any]:
    """按序跑完全部 onboarding 步骤。返回每步结果摘要。"""
    result: dict[str, Any] = {}
    _raise_if_cancelled(ctx)
    log.info("=== onboarding start ===")

    # 顺序严格按 HAR 时序；步骤间随机延迟模拟人工填表速度，避免机械速率被识别。
    result["start_onboarding"] = step_start_onboarding(ctx)
    if result["start_onboarding"] is False:
        log.warning("=== onboarding stopped: start_onboarding failed ===")
        return result
    _step_delay(ctx)
    result["privacy_consents"] = step_privacy_consents(ctx)
    _step_delay(ctx)
    result["accept_legal_docs"] = step_accept_legal_docs(ctx)
    _step_delay(ctx)
    result["email_consent"] = step_email_consent(ctx)
    _step_delay(ctx)
    result["age_verified"] = step_age_verified(ctx)
    _step_delay(ctx)
    result["grove"] = step_grove(ctx)
    _step_delay(ctx)
    result["set_name"] = step_set_name(ctx)
    _step_delay(ctx)
    result["first_chat"] = step_first_chat(ctx)        # 返回 conv uuid 或 None
    _step_delay(ctx)
    result["account_profile"] = step_account_profile(ctx)

    # 只有所有必需步骤都通过才标记 onboarding 完成，防止法律同意失败后服务端仍被告知已完成。
    required_results = {k: v for k, v in result.items() if k not in OPTIONAL_ONBOARDING_STEPS}
    all_required_ok = all(v is not False and v is not None for v in required_results.values())
    if all_required_ok:
        _step_delay(ctx)
        result["finish_onboarding"] = step_finish_onboarding(ctx)
    else:
        failed_now = [k for k, v in required_results.items() if v is False or v is None]
        log.warning("=== onboarding: 必需步骤失败，跳过 finish_onboarding: %s ===", failed_now)
        result["finish_onboarding"] = False

    failed = onboarding_failed_steps(result)
    if failed:
        log.warning("=== onboarding done with FAILURES: %s ===", failed)
    else:
        log.info("=== onboarding done (all green) ===")
    return result


def onboarding_failed_steps(result: dict[str, Any]) -> list[str]:
    """返回失败的 onboarding 步骤。

    普通关键步骤用 False 表示失败；first_chat 用 None 表示未创建成功，也应视为失败。
    privacy_consents/grove 属于非关键侧效，失败不阻断账号交付。
    """
    return [k for k, v in result.items() if k not in OPTIONAL_ONBOARDING_STEPS and (v is False or v is None)]


def onboarding_completed(result: dict[str, Any] | None) -> bool:
    if not result or result.get("finish_onboarding") is not True:
        return False
    return not onboarding_failed_steps(result)


# ── 演示入口：从 cookie 字符串构造 session 跑一遍 ───────────────────────────
def session_from_cookie_string(
    cookie_str: str,
    impersonate: str = IMPERSONATE_DEFAULT,
    proxy: str | None = None,
) -> curl_requests.Session:
    """把浏览器里复制的 cookie 字符串喂进 session。

    cookie_str 形如: "sessionKey=sk-ant-...; __cf_bm=...; ..."
    从 verify_magic_link 成功后的浏览器 DevTools → Application → Cookies 复制。
    proxy: 住宅代理（http://... 或 socks5h://...），None=直连。
    """
    s = build_session(impersonate, proxy)
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        s.cookies.set(k.strip(), v.strip(), domain=".claude.ai")
    return s


def main() -> int:
    try:
        require_external_confirmation()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── 这里填本次注册拿到的值 ──
    org_uuid = "replace-with-org-uuid"
    display_name = "replace-with-display-name"
    cookie_str = "sessionKey=sk-ant-xxx; __cf_bm=xxx"           # 改成本次登录态 cookie

    ctx = OnboardingContext(
        session=session_from_cookie_string(cookie_str),
        org_uuid=org_uuid,
        display_name=display_name,
    )
    summary = run_onboarding(ctx)
    completed = onboarding_completed(summary)
    print(f"onboarding_completed={completed}")
    return 0 if completed else 1


if __name__ == "__main__":
    sys.exit(main())
