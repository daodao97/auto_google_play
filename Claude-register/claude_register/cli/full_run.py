"""端到端跑一次注册：send → 抓信 → verify（passive arkose）→ onboarding。

全程用同一 sticky proxy session（gt2 + verify + onboarding 同 IP）。
arkose token 走 arkose.py 的 passive 静默放行路径（免 c_blob/免打码）。
"""

from __future__ import annotations

import logging
import os
import sys

from claude_register.challenge.arkose import PUBLIC_KEY, ArkoseConfig
from claude_register.core.browser import build_session, materialize_session_proxy, random_browser_profile
from claude_register.core.run_guard import ExternalRunNotConfirmed, require_external_confirmation
from claude_register.shared.names import random_name_parts
from claude_register.onboarding.service import onboarding_completed
from claude_register.auth.service import load_config, register

OUTPUT_FILE = "runtime/results.txt"


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
    password = os.getenv("CLAUDE_REGISTER_PASSWORD") or cfg.get("password")
    if not proxy_template or not email or not password:
        print("缺少 proxy/email/password。请在 runtime/config.json 或环境变量 CLAUDE_REGISTER_* 中配置。")
        return 2

    # 单账号固定 sticky session → 全程同一 IP（arkose token IP 一致性）
    concrete, sid = materialize_session_proxy(proxy_template)
    print(f"[proxy] configured; sticky_session={bool(sid)}\n")

    # 每次运行随机选浏览器指纹
    profile = random_browser_profile()
    print("[profile] configured\n")

    arkose_config = ArkoseConfig(public_key=PUBLIC_KEY, impersonate=profile.impersonate, proxy=concrete)
    send_session = build_session(profile=profile, proxy=concrete)

    display_name, full_name = random_name_parts()
    print("Authorized manual run confirmed.\n")
    try:
        summary = register(
            email, password, None,
            display_name=display_name,
            full_name=full_name,
            send_session=send_session,
            arkose_config=arkose_config,
            proxy=concrete,
            mail_provider=cfg.get("mail_provider", "mailcom"),
            mail_client_id=cfg.get("mail_client_id", ""),
            mail_refresh_token=cfg.get("mail_refresh_token", ""),
        )
    except Exception as e:
        print(f"\nFull run failed ({type(e).__name__}); details were not printed.")
        return 2

    session_key = summary.get("session_key") or ""
    print("\n=== 全流程完成 ===")
    print(f"send      sent={summary['send'].get('sent') if summary.get('send') else '(跳过)'}")
    print(f"verify    created={summary['verify'].get('created')}")
    print(f"onboarding completed={onboarding_completed(summary.get('onboarding'))}")

    # 输出账号行：账号----密码----sk-ant-sid02-xxx
    line = f"{email}----{password}----{session_key}"
    if session_key:
        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print("\nResult appended; sensitive fields were not printed.")
    else:
        print("\n⚠️ 未拿到 sessionKey，不写结果文件")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
