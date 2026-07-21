from __future__ import annotations

from chatgpt_register.registration.models import Account, RegistrationConfig
from chatgpt_register.registration.service import register_account


class FakeMail:
    def __init__(self, **_kwargs):
        self.warmed = False

    def prime_seen(self, _seen):
        self.warmed = True
        return 0


class FakeClient:
    def __init__(self, **_kwargs):
        self.created = False
        self.closed = False

    def register(self, **_kwargs):
        self.created = True
        return {
            "accessToken": "token",
            "expires": "tomorrow",
            "created": True,
            "session_data": {"accessToken": "token"},
        }

    def close(self):
        self.closed = True


def test_success_preserves_created_flag_and_private_session():
    result = register_account(
        Account("user@example.com", "mail-password"),
        RegistrationConfig(mail_app_token="app-token"),
        client_factory=FakeClient,
        mail_factory=FakeMail,
    )
    assert result.status == "success"
    assert result.created is True
    assert result.access_token == "token"


def test_failure_after_creation_is_partial():
    class PartialClient(FakeClient):
        def register(self, **_kwargs):
            self.created = True
            raise RuntimeError("callback failed")

    result = register_account(
        Account("user@example.com", "mail-password"),
        RegistrationConfig(mail_app_token="app-token"),
        client_factory=PartialClient,
        mail_factory=FakeMail,
    )
    assert result.status == "partial"
    assert result.created is True
    assert result.access_token == ""


def test_sensitive_protocol_log_is_redacted():
    messages: list[str] = []

    class LoggingClient(FakeClient):
        def register(self, **kwargs):
            kwargs["log"]("OTP=123456 at=secret.token.value")
            return super().register(**kwargs)

    register_account(
        Account("user@example.com", "mail-password"),
        RegistrationConfig(mail_app_token="app-token"),
        log=messages.append,
        client_factory=LoggingClient,
        mail_factory=FakeMail,
    )
    assert "123456" not in " ".join(messages)
    assert "secret.token.value" not in " ".join(messages)


def test_failure_message_redacts_account_secrets():
    class LeakyClient(FakeClient):
        def register(self, **_kwargs):
            raise RuntimeError("user@example.com mail-password app-token")

    result = register_account(
        Account("user@example.com", "mail-password"),
        RegistrationConfig(mail_app_token="app-token"),
        client_factory=LeakyClient,
        mail_factory=FakeMail,
    )
    assert "user@example.com" not in result.error
    assert "mail-password" not in result.error
    assert "app-token" not in result.error
