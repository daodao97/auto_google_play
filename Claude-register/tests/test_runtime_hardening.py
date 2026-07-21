from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from claude_register.orchestration import service as orch
from claude_register.presentation import web as webui


class TestRuntimeConfigModel(unittest.TestCase):
    def test_exit_ip_probe_errors_are_isolated_from_completed_task(self):
        task = orch.AccountTask(
            account=orch.Account("person@example.invalid", "password", "Person"),
            status="success",
        )
        cfg = orch.OrchestratorConfig(resolve_exit_ip=True)
        thread = MagicMock()

        with patch.object(orch, "resolve_proxy_exit_ip", side_effect=RuntimeError("PROBE_SENTINEL")), \
             patch.object(orch.threading, "Thread", return_value=thread) as thread_factory, \
             self.assertLogs("orchestrator", level="WARNING") as captured:
            orch._start_exit_ip_probe(task, cfg, "http://proxy", MagicMock(), None)
            thread_factory.call_args.kwargs["target"]()

        thread.start.assert_called_once()
        self.assertEqual(task.status, "success")
        self.assertEqual(task.proxy_exit_ip, "")
        self.assertIn("error_class=RuntimeError", "\n".join(captured.output))
        self.assertNotIn("PROBE_SENTINEL", "\n".join(captured.output))

    def test_valid_legacy_config_keeps_runtime_values_and_unknown_fields(self):
        from claude_register.orchestration.config import RuntimeConfig

        model = RuntimeConfig.model_validate(
            {
                "flow_mode": "session",
                "concurrency": 3,
                "retry_max": 1,
                "mail_poll_timeout": 240,
                "legacy_extension": "VALUE_SENTINEL",
            }
        )

        dumped = model.model_dump()
        self.assertEqual(dumped["flow_mode"], "session")
        self.assertEqual(dumped["concurrency"], 3)
        self.assertEqual(dumped["retry_max"], 1)
        self.assertEqual(dumped["mail_poll_timeout"], 240)
        self.assertEqual(dumped["legacy_extension"], "VALUE_SENTINEL")

    def test_mail_fast_path_is_opt_in_and_reaches_orchestrator_config(self):
        from claude_register.orchestration.config import RuntimeConfig

        self.assertFalse(RuntimeConfig.model_validate({}).mail_fast_path)
        self.assertTrue(RuntimeConfig.model_validate({"mail_fast_path": True}).mail_fast_path)

        config, _ = orch.config_from_dict_full({"mail_fast_path": True})
        self.assertTrue(config.mail_fast_path)

    def test_send_settle_delay_is_compatible_by_default_and_configurable(self):
        from claude_register.orchestration.config import RuntimeConfig

        self.assertIsNone(RuntimeConfig.model_validate({}).send_settle_delay)
        config, _ = orch.config_from_dict_full({"send_settle_delay": 1.0})
        self.assertEqual(config.send_settle_delay, 1.0)
        with patch.object(orch.random, "uniform", return_value=4.2):
            self.assertEqual(orch._send_settle_delay(orch.OrchestratorConfig()), 4.2)
        self.assertEqual(orch._send_settle_delay(config), 1.0)

    def test_exit_ip_probe_is_opt_in_and_reaches_orchestrator_config(self):
        from claude_register.orchestration.config import RuntimeConfig

        self.assertFalse(RuntimeConfig.model_validate({}).resolve_exit_ip)
        self.assertTrue(RuntimeConfig.model_validate({"resolve_exit_ip": True}).resolve_exit_ip)

        config, _ = orch.config_from_dict_full({"resolve_exit_ip": True})
        self.assertTrue(config.resolve_exit_ip)

    def test_invalid_flow_mode_and_limits_are_explicit_errors(self):
        from claude_register.orchestration.config import RuntimeConfig

        invalid_configs = (
            {"flow_mode": "unexpected"},
            {"concurrency": 11},
            {"retry_max": -1},
            {"mail_poll_timeout": 0},
            {"send_settle_delay": -0.1},
            {"send_settle_delay": 30.1},
        )
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(ValidationError):
                RuntimeConfig.model_validate(config)

    def test_config_warnings_never_include_unknown_values(self):
        from claude_register.orchestration.config import validate_runtime_config

        with self.assertLogs("orchestrator.config", level="WARNING") as captured:
            validated = validate_runtime_config(
                {
                    "legacy_extension": "VALUE_SENTINEL",
                    "account_timeout": 120,
                    "persistence_journal_file": "JOURNAL_SENTINEL",
                }
            )

        output = "\n".join(captured.output)
        self.assertIn("legacy_extension", output)
        self.assertIn("account_timeout", output)
        self.assertIn("persistence_journal_file", output)
        self.assertNotIn("VALUE_SENTINEL", output)
        self.assertNotIn("JOURNAL_SENTINEL", output)
        self.assertEqual(validated.account_timeout, 120)

    def test_orchestrator_rejects_invalid_flow_mode_instead_of_defaulting(self):
        with self.assertRaises(ValidationError):
            orch.config_from_dict_full({"flow_mode": "unexpected"})

    def test_invalid_worker_overrides_are_rejected_before_scheduling(self):
        invalid_workers = (
            {"flow_mode": "unexpected"},
            {"mail_provider": "unexpected"},
            {"retry_max": -1},
            {"retry_max": 6},
            {"mail_poll_interval": -0.1},
            {"mail_poll_timeout": 0},
            {"interval_seconds": -1},
        )
        for worker in invalid_workers:
            with self.subTest(worker=worker), self.assertRaises(ValidationError):
                orch.config_from_dict_full({"workers": [worker]})

    def test_direct_invalid_worker_config_fails_before_task_queueing(self):
        account = orch.Account("person@example.invalid", "password", "Person")
        task = orch.AccountTask(account=account)
        config = orch.OrchestratorConfig(workers=[{"flow_mode": "unexpected"}])

        with patch.object(orch, "register_one", side_effect=lambda current, *_args: current):
            with self.assertRaises(ValueError):
                orch.orchestrate([account], config, tasks=[task])

        self.assertEqual(task.status, "pending")


class TestAccountParseReport(unittest.TestCase):
    def test_mailcom_reports_bad_lines_without_dropping_valid_accounts(self):
        report = orch.parse_accounts_with_report(
            "\n".join(
                (
                    "valid@example.invalid----password----Valid",
                    "not-an-email----password----Invalid",
                    "missing@example.invalid----",
                    "valid@example.invalid----other-password----Duplicate",
                )
            ),
            "mailcom",
        )

        self.assertEqual([account.email for account in report.accounts], ["valid@example.invalid"])
        self.assertEqual(
            [(issue.line_number, issue.code) for issue in report.issues],
            [(2, "invalid_email"), (3, "missing_password"), (4, "duplicate_email")],
        )

    def test_microsoft_requires_refresh_token_and_keeps_valid_legacy_shape(self):
        report = orch.parse_accounts_with_report(
            "\n".join(
                (
                    "valid@outlook.invalid----password----client_id=client-id----"
                    "refresh_token=refresh-token----display_name=Valid",
                    "missing@outlook.invalid----password----client-id=client-only",
                )
            ),
            "microsoft",
        )

        self.assertEqual(len(report.accounts), 1)
        self.assertEqual(report.accounts[0].mail_client_id, "client-id")
        self.assertEqual(report.accounts[0].mail_refresh_token, "refresh-token")
        self.assertEqual(report.issues[0].line_number, 2)
        self.assertEqual(report.issues[0].code, "missing_refresh_token")

    def test_legacy_parse_function_keeps_its_existing_heuristic_path(self):
        text = (
            "valid@outlook.invalid----password----client_id=client-id----"
            "refresh_token=refresh-token----display_name=Valid"
        )

        legacy = orch.parse_accounts(text)
        reported = orch.parse_accounts_with_report(text, "microsoft").accounts

        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].deliver_prefix, reported[0].deliver_prefix)


class TestStructuredErrors(unittest.TestCase):
    def test_structured_retry_decision_matches_legacy_samples(self):
        from claude_register.orchestration.errors import FlowError

        samples = (
            (RuntimeError("HTTP 503"), True),
            (RuntimeError("HTTP 429"), True),
            (RuntimeError("HTTP 403"), False),
            (RuntimeError("certificate verify failed"), False),
        )
        for legacy, expected in samples:
            structured = FlowError(
                stage="verify",
                category="fixture",
                retryable=expected,
            )
            with self.subTest(error=str(legacy)):
                self.assertEqual(orch._is_retryable(legacy), expected)
                self.assertEqual(orch._is_retryable(structured), expected)

    def test_start_request_rejects_empty_override_and_invalid_mode(self):
        with self.assertRaises(ValidationError):
            webui.StartReq(
                proxy_mode="override",
                proxy_template="",
                accounts_text="fixture",
            )
        with self.assertRaises(ValidationError):
            webui.StartReq(flow_mode="unexpected", accounts_text="fixture")

    def test_start_request_rejects_values_outside_existing_runtime_limits(self):
        invalid_inputs = (
            {"concurrency": 0},
            {"concurrency": 11},
            {"retry_max": -1},
            {"retry_max": 6},
            {"mail_poll_interval": -0.1},
        )
        for values in invalid_inputs:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                webui.StartReq(accounts_text="fixture", **values)


if __name__ == "__main__":
    unittest.main(verbosity=2)
