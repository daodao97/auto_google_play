"""Authorized manual Arkose connectivity probe with redacted output."""

from __future__ import annotations

import os
import random
from urllib.parse import urlencode

from claude_register.core.browser import build_session, materialize_session_proxy, random_browser_profile
from claude_register.challenge.arkose import ARK_BUILD_ID, ARK_URL_TMPL, CAPI_VERSION, PUBLIC_KEY, SITE
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.auth.service import load_config


def _rnd() -> str:
    return f"{random.random():.16f}"


def main() -> int:
    try:
        require_external_confirmation()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2

    cfg = load_config()
    proxy_template = os.getenv("CLAUDE_REGISTER_PROXY_TEMPLATE") or cfg.get("proxy_template") or cfg.get("proxy")
    if not proxy_template:
        print("缺少 proxy。请在 runtime/config.json 或环境变量 CLAUDE_REGISTER_PROXY_TEMPLATE 中配置。")
        return 2
    concrete, sid = materialize_session_proxy(proxy_template)
    print(f"[proxy] configured; sticky_session={bool(sid)}\n")
    profile = random_browser_profile()
    session = build_session(profile=profile, proxy=concrete)
    url = ARK_URL_TMPL.format(pk=PUBLIC_KEY)

    headers = {
        "accept": "*/*",
        "accept-language": profile.accept_language,
        "ark-build-id": ARK_BUILD_ID,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": SITE,
        "referer": f"{SITE}/",
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": profile.platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": profile.ua,
    }

    variants = [
        ("no-c", {
            "public_key": PUBLIC_KEY, "site": SITE, "userbrowser": profile.ua,
            "capi_version": CAPI_VERSION, "capi_mode": "lightbox",
            "style_theme": "default", "rnd": _rnd(),
        }),
        ("empty-c", {
            "c": "", "public_key": PUBLIC_KEY, "site": SITE, "userbrowser": profile.ua,
            "capi_version": CAPI_VERSION, "capi_mode": "lightbox",
            "style_theme": "default", "rnd": _rnd(),
        }),
    ]

    for label, body_dict in variants:
        try:
            r = session.post(url, headers=headers, data=urlencode(body_dict), timeout=30)
            print(f"[{label}] HTTP {r.status_code}")
            try:
                tok = r.json().get("token", "")
                print(f"  token_present={bool(tok)}")
            except Exception:
                print("  response_json=false")
        except Exception as e:
            print(f"[{label}] failed ({type(e).__name__})")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
