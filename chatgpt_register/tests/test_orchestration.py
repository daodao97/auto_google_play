from __future__ import annotations

import json

import pytest

from chatgpt_register.orchestration.models import OrchestratorConfig
from chatgpt_register.orchestration.service import orchestrate, parse_accounts
from chatgpt_register.registration.models import RegistrationResult


def test_parse_accounts_rejects_ambiguous_lines_and_deduplicates():
    accounts = parse_accounts("A@example.com----one\na@example.com----two\n")
    assert len(accounts) == 1
    with pytest.raises(ValueError):
        parse_accounts("not-an-account")


def test_orchestrator_persists_success_without_password(tmp_path):
    accounts = parse_accounts("user@example.com----mail-secret")

    def fake_register(account, _config, **_kwargs):
        return RegistrationResult(
            email=account.email,
            status="success",
            created=True,
            access_token="access-token",
            session_data={"accessToken": "access-token"},
        )

    run_id, tasks = orchestrate(
        accounts,
        OrchestratorConfig(output_root=tmp_path, mail_app_token="app-token"),
        register=fake_register,
        run_id="run_test",
    )
    assert run_id == "run_test"
    assert tasks[0].status == "success"
    payload = json.loads((tmp_path / run_id / "results.jsonl").read_text())
    assert payload["access_token"] == "access-token"
    assert "mail-secret" not in json.dumps(payload)


def test_created_failure_is_not_retried(tmp_path):
    calls = 0

    def fake_register(account, _config, **_kwargs):
        nonlocal calls
        calls += 1
        return RegistrationResult(account.email, "partial", created=True, error_class="CallbackError")

    _, tasks = orchestrate(
        parse_accounts("user@example.com----mail-secret"),
        OrchestratorConfig(output_root=tmp_path, retry_max=2, mail_app_token="app-token"),
        register=fake_register,
        run_id="run_partial",
    )
    assert calls == 1
    assert tasks[0].status == "partial"

