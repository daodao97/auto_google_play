from pathlib import Path


def test_protocol_explicitly_follows_check_email_continuation():
    source = (
        Path(__file__).parents[1]
        / "chatgpt_register"
        / "protocol"
        / "client.py"
    ).read_text(encoding="utf-8")
    assert "continuation = self._ios_check_email" in source
    assert "self._follow_auth_landing(continuation" in source


def test_created_boundary_requires_create_account_continuation():
    source = (
        Path(__file__).parents[1]
        / "chatgpt_register"
        / "protocol"
        / "client.py"
    ).read_text(encoding="utf-8")
    missing_index = source.index("create_account_missing_continue_url")
    created_index = source.index("self.created = True")
    assert missing_index < created_index


def test_new_create_account_password_landing_uses_otp_path():
    source = (
        Path(__file__).parents[1]
        / "chatgpt_register"
        / "protocol"
        / "client.py"
    ).read_text(encoding="utf-8")
    assert '"/create-account/password"' in source
    assert "self._login_via_otp_from_login_page" in source
