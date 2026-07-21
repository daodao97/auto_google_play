"""Arkose (FunCaptcha) session token 解析——三层降级。

对标 gopay-pipeline 的 passive_captcha_token + captcha_solver_keys 模式。
claudeaizhuce.har 证实：

    POST https://a-cdn.claude.ai/fc/gt2/public_key/EEA5F558-D6AC-4C03-B678-AABF639EE69A
    → 响应 {"token":"17318bd469825b4d4.7960819401|r=us-east-1|meta=3|...", "pow":false, "challenge_url":""}

这个 `token` 就是 verify_magic_link 请求体里的 `arkose_session_token`（整串，含 `|`）。
低风险时静默放行（challenge_url 为空、pow=false）；但请求体里 11KB 的 `c=` 加密 blob
和 `x-ark-arid` 头由 Arkose 客户端 JS 实时生成，纯 HTTP 伪造不出。

三层降级（resolve_arkose_token 按序尝试）：
  1. 透传 passive_token——外部已拿到的 token，原样返回。
  2. yescaptcha 打码——调 FunCaptchaTaskProxyless，拿已解 token（付费，但真自动）。
  3. 直接重放——curl_cffi 精确复刻 HAR 的 POST，需调用方提供新鲜浏览器抓的 c_blob + x_ark_arid。

依赖: pip install curl-cffi>=0.7.0
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests as curl_requests

from claude_register.core.browser import (
    DEFAULT_ACCEPT_LANGUAGE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_SEC_CH_UA_PLATFORM,
    DEFAULT_UA,
    BrowserProfile,
    build_session,
    resolve_impersonate,
)
from claude_register.core.diagnostics import safe_error_label

log = logging.getLogger("arkose")

# ── 从 claudeaizhuce.har 提取的常量（可被 config 覆盖）──────────────────────
PUBLIC_KEY = "EEA5F558-D6AC-4C03-B678-AABF639EE69A"
ARK_URL_TMPL = "https://a-cdn.claude.ai/fc/gt2/public_key/{pk}"
ARK_BUILD_ID = "f106434e-87cc-45db-ae02-4f86ce43f9d9"
CAPI_VERSION = "4.4.0"
SITE = "https://claude.ai"

YESCAPTCHA_API = "https://api.yescaptcha.com"


def _arkose_rnd() -> str:
    return f"{random.random():.16f}"


def _arkose_headers(
    profile: BrowserProfile | None,
    *,
    ark_build_id: str = ARK_BUILD_ID,
    content_type: str | None = None,
) -> dict[str, str]:
    ua = profile.ua if profile else DEFAULT_UA
    sec_ch_ua = profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA
    platform = profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM
    accept_language = profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE
    headers = {
        "accept": "*/*",
        "accept-language": accept_language,
        "ark-build-id": ark_build_id,
        "origin": SITE,
        "referer": f"{SITE}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
    }
    if content_type:
        headers["content-type"] = content_type
    return headers


def _send_arkose_followups(
    cfg: ArkoseConfig,
    token: str,
    profile: BrowserProfile | None = None,
    *,
    ark_build_id: str = ARK_BUILD_ID,
) -> None:
    """Best-effort replay of read-only/telemetry Arkose followups seen in browser HAR."""
    session = build_session(cfg.impersonate, cfg.proxy, profile=profile)
    token_id = token.split("|", 1)[0]
    try:
        try:
            session.get(
                f"https://a-cdn.claude.ai/v2/{cfg.public_key}/settings",
                headers=_arkose_headers(profile, ark_build_id=ark_build_id),
                timeout=10,
            )
        except Exception:
            pass
        try:
            session.get(
                "https://a-cdn.claude.ai/fc/a/",
                params={
                    "callback": f"__jsonp_{int(time.time() * 1000)}",
                    "category": "loaded",
                    "action": "game loaded",
                    "session_token": token_id,
                    "data[public_key]": cfg.public_key,
                    "data[site]": SITE,
                },
                headers=_arkose_headers(profile, ark_build_id=ark_build_id),
                timeout=10,
            )
        except Exception:
            pass
        ua = profile.ua if profile else DEFAULT_UA
        platform = (profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM).strip('"')
        payload = {
            "id": str(uuid.uuid4()),
            "publicKey": cfg.public_key,
            "isKeyless": False,
            "capiVersion": CAPI_VERSION,
            "mode": "lightbox",
            "suppressed": True,
            "sessionToken": token_id,
            "device": {
                "platform": platform,
                "userAgent": ua,
            },
        }
        headers = _arkose_headers(
            profile,
            ark_build_id=ark_build_id,
            content_type="application/json",
        )
        for _ in range(2):
            try:
                session.post(
                    "https://a-cdn.claude.ai/metrics/ui",
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=10,
                )
            except Exception:
                pass
    finally:
        try:
            session.close()
        except Exception:
            pass


# ── 配置 dataclass ──────────────────────────────────────────────────────────
@dataclass
class SolverConfig:
    provider: str = "yescaptcha"           # 目前只实现 yescaptcha
    api_key: str = ""
    website_url: str = "https://claude.ai/magic-link"
    # Arkose 的 funcaptchaApiJSSubdomain（Claude 的 Arkose 资源在 a-cdn.claude.ai）。
    js_subdomain: str = "https://a-cdn.claude.ai"
    # 可选 data[blob]：某些部署需要页面生成的 blob；空则不带。
    data_blob: str = ""
    timeout: int = 180
    interval: int = 5


@dataclass
class ReplayConfig:
    c_blob: str = ""                       # ← 必填：浏览器抓的 c= 值（URL-decoded 原文）
    x_ark_arid: str = ""                   # ← 必填：浏览器抓的 x-ark-arid 头（JSON 串）
    x_ark_esync_value: int = 1782648000
    ark_build_id: str = ARK_BUILD_ID


@dataclass
class ArkoseConfig:
    public_key: str = PUBLIC_KEY
    impersonate: str | None = None
    proxy: str | None = None
    passive_token: str = ""
    solver: SolverConfig | None = None
    replay: ReplayConfig | None = None


# ── 解析入口：三层降级 ──────────────────────────────────────────────────────
def resolve_arkose_token(cfg: ArkoseConfig | None, profile: BrowserProfile | None = None) -> str:
    """按 透传 → yescaptcha → 直接重放 的顺序拿 arkose_session_token。

    profile: 每账号浏览器指纹，用于 passive 请求头，使不同账号的 Arkose 请求特征各异。
    都没有就抛 RuntimeError。cfg 为 None 也算「都没有」。
    """
    if cfg and cfg.passive_token:
        log.info("[arkose] 透传 passive_token")
        return cfg.passive_token

    if cfg:
        try:
            tok = _replay_passive(cfg, profile=profile)
            if tok:
                log.info("[arkose] passive 静默放行成功")
                return tok
        except RuntimeError as e:
            log.warning("[arkose] passive 未命中: %s；降级", safe_error_label(e))

    if cfg and cfg.solver and cfg.solver.api_key:
        try:
            tok = _solve_yescaptcha(cfg, cfg.solver)
            if tok:
                log.info("[arkose] yescaptcha 打码成功")
                return tok
            log.warning("[arkose] yescaptcha 未返回 token，降级到重放")
        except Exception as e:
            log.warning("[arkose] yescaptcha 失败: %s；降级到重放", safe_error_label(e))

    if cfg and cfg.replay and cfg.replay.c_blob and cfg.replay.x_ark_arid:
        tok = _replay_direct(cfg, cfg.replay, profile=profile)
        if tok:
            log.info("[arkose] 直接重放成功")
            return tok
        raise RuntimeError("arkose 直接重放未返回 token（c_blob/x_ark_arid 可能已过期）")

    raise RuntimeError(
        "无法解析 arkose_session_token：请在 runtime/config.json 提供 passive_token、"
        "solver.api_key、或 replay.c_blob+x_ark_arid 之一。"
    )


# ── 策略 2：yescaptcha 打码 ─────────────────────────────────────────────────
def _solve_yescaptcha(cfg: ArkoseConfig, sc: SolverConfig) -> str:
    """FunCaptchaTaskProxyless：createTask → 轮询 getTaskResult → token。"""
    imp = resolve_impersonate(cfg.impersonate)
    proxies = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None

    task: dict[str, Any] = {
        "type": "FunCaptchaTaskProxyless",
        "websiteURL": sc.website_url,
        "websitePublicKey": cfg.public_key,
        "funcaptchaApiJSSubdomain": sc.js_subdomain,
    }
    if sc.data_blob:
        task["data"] = sc.data_blob

    create = curl_requests.post(
        f"{YESCAPTCHA_API}/createTask",
        json={"clientKey": sc.api_key, "task": task},
        headers={"Content-Type": "application/json"},
        impersonate=imp, timeout=30, proxies=proxies,
    ).json()
    if create.get("errorId"):
        raise RuntimeError("yescaptcha createTask error")
    task_id = create.get("taskId")
    if not task_id:
        raise RuntimeError("yescaptcha createTask missing taskId")
    log.info("[arkose] yescaptcha task created")

    deadline = time.time() + sc.timeout
    while time.time() < deadline:
        time.sleep(sc.interval)
        r = curl_requests.post(
            f"{YESCAPTCHA_API}/getTaskResult",
            json={"clientKey": sc.api_key, "taskId": task_id},
            headers={"Content-Type": "application/json"},
            impersonate=imp, timeout=30, proxies=proxies,
        ).json()
        if r.get("errorId"):
            raise RuntimeError("yescaptcha getTaskResult error")
        status = r.get("status", "")
        if status == "ready":
            sol = r.get("solutions") or {}
            tok = sol.get("token") or r.get("token") or ""
            if tok:
                return tok
            raise RuntimeError("yescaptcha ready 但无 token")
        log.debug("[arkose] yescaptcha 状态: %s", status if status == "processing" else "other")
    raise TimeoutError(f"yescaptcha {sc.timeout}s 超时未完成")


# ── 策略 2.5：passive 静默放行（干净 IP，免 c_blob）─────────────────────────
def _replay_passive(cfg: ArkoseConfig, profile: BrowserProfile | None = None) -> str:
    """最小 body 打 gt2（不带 c=），干净 IP 上 suppressed 静默放行直接拿回 token。

    profile 提供每账号不同的 UA/sec-ch-ua/platform，避免所有 Arkose 请求特征相同。
    """
    imp = profile.impersonate if profile else resolve_impersonate(cfg.impersonate)
    ua = profile.ua if profile else DEFAULT_UA
    sec_ch_ua = profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA
    platform = profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM
    accept_language = profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE
    url = ARK_URL_TMPL.format(pk=cfg.public_key)

    headers = {
        "accept": "*/*",
        "accept-language": accept_language,
        "ark-build-id": ARK_BUILD_ID,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": SITE,
        "referer": f"{SITE}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
    }
    body = urlencode({
        "public_key": cfg.public_key,
        "site": SITE,
        "userbrowser": ua,
        "capi_version": CAPI_VERSION,
        "capi_mode": "lightbox",
        "style_theme": "default",
        "rnd": _arkose_rnd(),
    })
    # 用 Session 而非模块级 curl_requests.post：socks5h + impersonate 下模块级调用
    # 有 TLS "invalid library" 坑（build_session 走 Session 路径稳定）。代理 IP 偶发抖动 → 重试。
    last_err: Exception | None = None
    for attempt in range(3):
        session = build_session(imp, cfg.proxy, profile=profile)
        try:
            resp = session.post(url, headers=headers, data=body, timeout=30)
            break
        except Exception as e:
            last_err = e
            log.warning("[arkose] passive 第 %d 次失败: %s", attempt + 1, safe_error_label(e))
            time.sleep(2)
        finally:
            try:
                session.close()
            except Exception:
                pass
    else:
        raise RuntimeError(f"arkose passive 重试 3 次仍失败: {safe_error_label(last_err or RuntimeError())}")
    if resp.status_code != 200:
        raise RuntimeError(f"arkose passive HTTP {resp.status_code}")
    try:
        data = resp.json()
    except (ValueError, Exception) as exc:
        raise RuntimeError("arkose passive 响应非 JSON") from exc
    tok = data.get("token", "")
    challenge = data.get("challenge_url", "")
    if not tok:
        raise RuntimeError("arkose passive 无 token")
    if challenge:
        raise RuntimeError("arkose passive 需解题（challenge_url 非空）")
    log.debug("[arkose] passive pow_present=%s", data.get("pow") is not None)
    _send_arkose_followups(cfg, tok, profile=profile, ark_build_id=ARK_BUILD_ID)
    return tok


# ── 策略 3：直接重放 HAR 的 POST /fc/gt2/public_key/ ───────────────────────
def _replay_direct(
    cfg: ArkoseConfig,
    rc: ReplayConfig,
    profile: BrowserProfile | None = None,
) -> str:
    """精确复刻 claudeaizhuce.har 里的 Arkose POST，返回响应 token。

    ⚠️ c_blob / x_ark_arid / ark_build_id 必须来自真浏览器（DevTools 抓或 Playwright 产），
    纯 HTTP 造不出。HAR 里的值是一次性的，过期会被拒。
    """
    imp = profile.impersonate if profile else resolve_impersonate(cfg.impersonate)
    ua = profile.ua if profile else DEFAULT_UA
    sec_ch_ua = profile.sec_ch_ua if profile else DEFAULT_SEC_CH_UA
    platform = profile.platform if profile else DEFAULT_SEC_CH_UA_PLATFORM
    accept_language = profile.accept_language if profile else DEFAULT_ACCEPT_LANGUAGE
    proxies = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None
    url = ARK_URL_TMPL.format(pk=cfg.public_key)

    headers = {
        "accept": "*/*",
        "accept-language": accept_language,
        "ark-build-id": rc.ark_build_id,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": SITE,
        "referer": f"{SITE}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": ua,
        "x-ark-arid": rc.x_ark_arid,
        "x-ark-esync-value": str(rc.x_ark_esync_value),
    }
    body = urlencode({
        "c": rc.c_blob,
        "public_key": cfg.public_key,
        "site": SITE,
        "userbrowser": ua,
        "capi_version": CAPI_VERSION,
        "capi_mode": "lightbox",
        "style_theme": "default",
        "rnd": _arkose_rnd(),
    })
    resp = curl_requests.post(
        url, headers=headers, data=body,
        impersonate=imp, timeout=30, proxies=proxies,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"arkose replay HTTP {resp.status_code}")
    data = resp.json()
    tok = data.get("token", "")
    if tok:
        log.debug("[arkose] replay pow_present=%s challenge_present=%s",
                  data.get("pow") is not None, bool(data.get("challenge_url")))
        _send_arkose_followups(cfg, tok, profile=profile, ark_build_id=rc.ark_build_id)
    return tok


# ── 从运行配置 dict 构造 ArkoseConfig ──────────────────────────────────────
def config_from_dict(d: dict[str, Any]) -> ArkoseConfig:
    a = d.get("arkose") or {}
    solver = None
    if a.get("solver"):
        s = a["solver"]
        solver = SolverConfig(
            provider=s.get("provider", "yescaptcha"),
            api_key=s.get("api_key", ""),
            website_url=s.get("website_url", "https://claude.ai/magic-link"),
            js_subdomain=s.get("js_subdomain", "https://a-cdn.claude.ai"),
            data_blob=s.get("data_blob", ""),
            timeout=s.get("timeout", 180),
            interval=s.get("interval", 5),
        )
    replay = None
    if a.get("replay"):
        r = a["replay"]
        replay = ReplayConfig(
            c_blob=r.get("c_blob", ""),
            x_ark_arid=r.get("x_ark_arid", ""),
            x_ark_esync_value=r.get("x_ark_esync_value", 1782648000),
            ark_build_id=r.get("ark_build_id", ARK_BUILD_ID),
        )
    return ArkoseConfig(
        public_key=a.get("public_key", PUBLIC_KEY),
        impersonate=d.get("impersonate"),
        proxy=d.get("proxy") or None,
        passive_token=a.get("passive_token", ""),
        solver=solver,
        replay=replay,
    )


if __name__ == "__main__":
    # 自检：透传 + 缺省报错
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    t = resolve_arkose_token(ArkoseConfig(passive_token="17318bd469825b4d4.7960819401|r=us-east-1|meta=3|..."))
    print("透传自检 token_present=", bool(t))
    try:
        resolve_arkose_token(ArkoseConfig())
    except RuntimeError as e:
        print("空配置报错 OK:", safe_error_label(e))
