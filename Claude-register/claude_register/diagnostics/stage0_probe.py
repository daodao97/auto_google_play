"""Authorized manual stage-0 connectivity probe with redacted output."""

from __future__ import annotations

import logging
import os
import sys

from curl_cffi import requests as curl_requests

from claude_register.core.browser import (
    build_session,
    init_browser_cookies,
    materialize_session_proxy,
    new_browser_runtime,
    random_browser_profile,
)
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.auth.service import load_config, login_methods, new_identity, send_magic_link

log = logging.getLogger("probe")


def ip_check(session: curl_requests.Session) -> None:
    """Check proxy connectivity without printing the returned address."""
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
        try:
            r = session.get(url, timeout=30)
            print(f"[ip] connectivity HTTP {r.status_code}")
            return
        except Exception as e:
            print(f"[ip] connectivity failed ({type(e).__name__})")


def main() -> int:
    try:
        require_external_confirmation()
    except ExternalRunNotConfirmed as exc:
        print(exc)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    cfg = load_config()
    proxy_template = os.getenv("CLAUDE_REGISTER_PROXY_TEMPLATE") or cfg.get("proxy_template") or cfg.get("proxy")
    email = os.getenv("CLAUDE_REGISTER_EMAIL") or cfg.get("email")
    if not proxy_template or not email:
        print("缺少 proxy/email。请在 runtime/config.json 或环境变量 CLAUDE_REGISTER_* 中配置。")
        return 2

    concrete, sid = materialize_session_proxy(proxy_template)
    print(f"[proxy] configured; sticky_session={bool(sid)}\n")

    profile = random_browser_profile()
    runtime = new_browser_runtime()
    session = build_session(profile=profile, proxy=concrete, browser_runtime=runtime)
    anonymous_id, device_id = new_identity()
    init_browser_cookies(
        session,
        anonymous_id,
        device_id,
        color_scheme=profile.color_scheme,
        browser_runtime=runtime,
    )
    print("=== 1. 代理出口 IP ===")
    ip_check(session)
    print()

    print("=== 2. login_methods 预检（非致命）===")
    try:
        methods = login_methods(
            session,
            email,
            anonymous_id=anonymous_id,
            device_id=device_id,
            profile=profile,
            browser_runtime=runtime,
        )
        print(f"[login_methods] available={bool(methods)}")
    except Exception as e:
        print(f"[login_methods] failed ({type(e).__name__})")
    print()

    print("=== 3. send_magic_link（不带 arkose cookie）===")
    try:
        data = send_magic_link(
            session,
            email,
            anonymous_id=anonymous_id,
            device_id=device_id,
            profile=profile,
            browser_runtime=runtime,
        )
        print(f"[send] completed; sent={bool(data.get('sent'))}")
        print("\nAuthorized probe completed; no response data was printed.")
        return 0
    except Exception as e:
        print(f"[send] failed ({type(e).__name__})")
        print("\nAuthorized probe stopped; no automated fallback was attempted.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
