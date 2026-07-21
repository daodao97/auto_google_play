"""OTP mailbox adapter used by the protocol state machine."""

from __future__ import annotations

from typing import Callable

from ._legacy_clients import MailComClient


class MailComOtpClient(MailComClient):
    """Compatibility adapter around the existing mail.com helper API."""

    def __init__(
        self,
        *,
        api_base: str,
        app_token: str,
        email: str,
        password: str,
        proxy: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not api_base:
            raise ValueError("mail_api_base is required")
        if not app_token:
            raise ValueError("mail_app_token is required")
        super().__init__(
            api_base=api_base,
            app_token=app_token,
            email=email,
            password=password,
            proxy=proxy,
            control_callback=cancelled,
        )

