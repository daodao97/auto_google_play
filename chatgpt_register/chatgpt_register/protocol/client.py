"""Explicit ChatGPT registration state machine built on the gopay protocol primitives."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from ._session import (
    AUTH,
    CHATGPT,
    ChatGPTLoginError,
    ChatGPTSession,
    _do,
    _inject_sentinel,
    _rj,
)


class RegistrationProtocolError(ChatGPTLoginError):
    """A stable registration-flow failure."""


class ChatGPTRegistrationClient(ChatGPTSession):
    """OTP-first client that distinguishes login from actual account creation."""

    def __init__(self, proxy: str = "", country_code: str = "US", impersonate: str = "") -> None:
        super().__init__(proxy=proxy, country_code=country_code, impersonate=impersonate)
        self.created = False
        self.stage = "idle"

    def _set_stage(self, stage: str, log: Callable[[str], None]) -> None:
        self.stage = stage
        log(f"stage={stage}")

    def _follow_auth_landing(self, url: str, referer: str, log: Callable[[str], None]) -> str:
        response = _do(
            self.s,
            "GET",
            urljoin(AUTH, url),
            headers={**self.nav_h, "referer": referer},
            allow_redirects=True,
        )
        landing = str(getattr(response, "url", "") or url)
        log(f"auth_landing={urlparse(landing).path}")
        return landing

    def _complete_about_you(self, log: Callable[[str], None]) -> str:
        self._set_stage("about_you", log)
        continuation = self._handle_about_you(log=log)
        if not continuation:
            raise RegistrationProtocolError("create_account_missing_continue_url")
        # The irreversible boundary is crossed only after a successful response.
        self.created = True
        return continuation

    def _follow_callback_chain(self, continuation: str, referer: str, log: Callable[[str], None]) -> str:
        self._set_stage("callback", log)
        url = urljoin(AUTH, continuation)
        for hop in range(12):
            if "/about-you" in url:
                url = urljoin(AUTH, self._complete_about_you(log))
                continue
            response = _do(
                self.s,
                "GET",
                url,
                headers={**self.nav_h, "referer": referer},
                allow_redirects=False,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return str(getattr(response, "url", "") or url)
            location = response.headers.get("Location") or response.headers.get("location") or ""
            if not location:
                raise RegistrationProtocolError("callback_redirect_missing_location")
            referer, url = url, urljoin(url, location)
            log(f"callback_hop={hop + 1} path={urlparse(url).path}")
        raise RegistrationProtocolError("callback_redirect_limit_exceeded")

    def register(
        self,
        *,
        email: str,
        mail_client: Any,
        log: Callable[[str], None] = print,
    ) -> dict[str, Any]:
        """Register a new email or log in an existing email via OTP."""
        self.created = False
        self._normalize_openai_context_cookies()
        user_agent = self.fp["ua"]

        self._set_stage("authorize", log)
        try:
            _do(
                self.s,
                "GET",
                f"{CHATGPT}/auth/login",
                headers={**self.nav_h, "referer": f"{CHATGPT}/"},
                allow_redirects=True,
            )
        except Exception:
            log("authorize_warmup=failed_ignored")

        csrf_response = _do(
            self.s,
            "GET",
            f"{CHATGPT}/api/auth/csrf",
            headers={"accept": "application/json", "user-agent": user_agent, "referer": f"{CHATGPT}/auth/login"},
        )
        csrf = str((_rj(csrf_response) or {}).get("csrfToken") or "")
        if csrf_response.status_code != 200 or not csrf:
            raise RegistrationProtocolError("csrf_failed")

        query = urlencode({"prompt": "login", "screen_hint": "login_or_signup", "login_hint": email})
        body = urlencode({"callbackUrl": f"{CHATGPT}/", "csrfToken": csrf, "json": "true"})
        signin = _do(
            self.s,
            "POST",
            f"{CHATGPT}/api/auth/signin/openai?{query}",
            headers={
                "accept": "application/json",
                "accept-language": self.fp["lang"],
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": user_agent,
                "referer": f"{CHATGPT}/",
                "origin": CHATGPT,
            },
            data=body,
            allow_redirects=False,
        )
        signin_data = _rj(signin) or {}
        auth_url = str(signin_data.get("url") or signin.headers.get("location") or "")
        if not auth_url or "/auth/error" in auth_url:
            raise RegistrationProtocolError("signin_missing_authorize_url")

        navigation_headers = {**self.nav_h, "referer": f"{CHATGPT}/"}
        _inject_sentinel(
            navigation_headers,
            self.s,
            self.did,
            "authorize_continue",
            fp=self.fp,
            proxy=self.proxy,
        )
        chain = _do(self.s, "GET", auth_url, headers=navigation_headers, allow_redirects=True)
        landing = str(getattr(chain, "url", "") or "")
        path = urlparse(landing).path
        log(f"authorize_landing={path}")

        if urlparse(landing).netloc == "chatgpt.com":
            final_url = landing
        else:
            if "/log-in-or-create-account" in landing:
                self._set_stage("check_email", log)
                continuation = self._ios_check_email(email, self.did, log=log)
                if not continuation:
                    raise RegistrationProtocolError("check_email_missing_continue_url")
                # gpt_pay's combined login path discarded this URL; registration must follow it.
                landing = self._follow_auth_landing(continuation, landing, log)

            self._set_stage("otp", log)
            if any(path in landing for path in ("/log-in/password", "/create-account/password")):
                continuation, _seen = self._login_via_otp_from_login_page(
                    email,
                    "register",
                    mail_client,
                    device_id=self.did,
                    referer_login=landing,
                    log=log,
                )
            elif "/email-verification" in landing:
                continuation = self._handle_otp_verification(mail_client, log=log)
            elif "/about-you" in landing:
                continuation = self._complete_about_you(log)
            else:
                raise RegistrationProtocolError(f"unexpected_authorize_landing:{path}")
            if not continuation:
                raise RegistrationProtocolError("otp_missing_continue_url")
            final_url = self._follow_callback_chain(continuation, landing, log)

        if urlparse(final_url).netloc != "chatgpt.com":
            _do(
                self.s,
                "GET",
                f"{CHATGPT}/",
                headers={**self.nav_h, "referer": final_url},
                allow_redirects=True,
            )

        self._set_stage("session", log)
        session_response = _do(
            self.s,
            "GET",
            f"{CHATGPT}/api/auth/session",
            headers={"accept": "application/json", "user-agent": user_agent, "referer": f"{CHATGPT}/"},
        )
        session_data = _rj(session_response) or {}
        access_token = str(session_data.get("accessToken") or "")
        if session_response.status_code != 200 or not access_token:
            raise RegistrationProtocolError("session_missing_access_token")
        return {
            "accessToken": access_token,
            "expires": session_data.get("expires") or "",
            "email": email,
            "created": self.created,
            "session_data": session_data,
        }
