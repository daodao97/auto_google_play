from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_register.orchestration import service as orch
from claude_register.presentation import web as webui


class TestVerifiedRecovery(unittest.TestCase):
    def test_verified_checkpoint_is_recorded_before_post_verify_bootstrap(self):
        calls: list[str] = []
        session = MagicMock()
        session.cookies.get.return_value = "session-fixture"
        identity = MagicMock()
        identity.profile = MagicMock()
        identity.anonymous_id = "anonymous"
        identity.device_id = "device"
        recovery = MagicMock()
        recovery.record_verified.side_effect = lambda _task: calls.append("recovery")

        def bootstrap(*_args, **_kwargs):
            calls.append("bootstrap")

        def verify(*_args, **_kwargs):
            calls.append("verify")
            return {
                "account": {
                    "uuid": "account-fixture",
                    "memberships": [{"organization": {"uuid": "org-fixture"}}],
                }
            }

        with patch.object(orch, "new_browser_identity", return_value=identity), patch.object(
            orch, "build_session", return_value=session
        ), patch.object(orch, "init_browser_cookies"), patch.object(
            orch,
            "_fetch_dynamic_config_with_cache",
            return_value=("client-sha", [], "sentry-key", "sentry-org", "1.0.0"),
        ), patch.object(orch, "warm_claude_login"), patch.object(
            orch, "prime_seen", return_value=set()
        ), patch.object(orch, "login_methods", return_value=["magic_link"]), patch.object(
            orch, "send_magic_link", return_value={"sent": True}
        ), patch.object(
            orch,
            "fetch_magic_link",
            return_value={"nonce": "nonce", "encoded_email_address": "encoded"},
        ), patch.object(orch, "resolve_arkose_token", return_value="token"), patch.object(
            orch, "warm_claude_bootstrap", side_effect=bootstrap
        ), patch.object(orch, "verify_magic_link", side_effect=verify), patch.object(
            orch, "run_onboarding", return_value={}
        ), patch.object(orch, "onboarding_failed_steps", return_value=[]), patch.object(
            orch, "check_kyc_status", return_value=(True, "not_required")
        ), patch.object(
            orch, "_wait_cancelable", return_value=False
        ):
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person")
            )
            orch.register_one(
                task,
                orch.OrchestratorConfig(
                    auto_send=True,
                    retry_max=0,
                    recovery_store=recovery,
                ),
            )

        self.assertEqual(
            calls,
            ["bootstrap", "verify", "recovery", "bootstrap"],
        )
        recovery.record_verified.assert_called_once_with(task)

    def test_recovery_file_is_private_and_not_available_to_web_downloads(self):
        from claude_register.orchestration.recovery import VerifiedRecoveryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run_123456789_abcdef12"
            store = VerifiedRecoveryStore(run_dir)
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                status="partial",
                session_key="SESSION_KEY_SENTINEL",
            )

            store.record_verified(task)

            recovery_file = run_dir / "verified-recovery.jsonl"
            self.assertEqual(stat.S_IMODE(recovery_file.stat().st_mode), 0o600)
            record = json.loads(recovery_file.read_text(encoding="utf-8"))
            self.assertEqual(record["task_id"], task.task_id)
            self.assertEqual(record["delivery_line"], task.delivery_line())
            self.assertFalse(record["exported"])
            with patch.object(webui, "PROJECT_ROOT", Path(temp_dir)):
                self.assertIsNone(
                    webui._run_output_path(
                        "run_123456789_abcdef12",
                        "verified-recovery.jsonl",
                    )
                )

    def test_restart_exports_verified_task_as_partial_and_is_idempotent(self):
        from claude_register.orchestration.recovery import VerifiedRecoveryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run_123456789_abcdef12"
            store = VerifiedRecoveryStore(run_dir)
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                status="running",
                session_key="session",
            )
            store.record_verified(task)

            first_count = store.recover_pending()
            second_count = store.recover_pending()

            expected = task.delivery_line() + "\n"
            self.assertEqual(first_count, 1)
            self.assertEqual(second_count, 0)
            self.assertEqual((run_dir / "partial.txt").read_text(encoding="utf-8"), expected)
            self.assertEqual((run_dir / "kyc_unknown.txt").read_text(encoding="utf-8"), expected)
            for filename in ("partial.txt", "kyc_unknown.txt"):
                self.assertEqual(stat.S_IMODE((run_dir / filename).stat().st_mode), 0o600)

            records = [
                json.loads(line)
                for line in (run_dir / "verified-recovery.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(records[0]["exported"])

    def test_web_startup_recovers_interrupted_runs(self):
        from claude_register.orchestration.recovery import VerifiedRecoveryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            run_dir = project_root / "runtime" / "runs" / "run_123456789_abcdef12"
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                session_key="session",
            )
            VerifiedRecoveryStore(run_dir).record_verified(task)

            with patch.object(webui, "PROJECT_ROOT", project_root):
                recovered = webui._recover_incomplete_runs()

            self.assertEqual(recovered, 1)
            self.assertEqual(
                (run_dir / "partial.txt").read_text(encoding="utf-8"),
                task.delivery_line() + "\n",
            )
            self.assertFalse((run_dir / "verified-recovery.jsonl").exists())

    def test_cleanup_removes_only_exported_recovery_records(self):
        from claude_register.orchestration.recovery import VerifiedRecoveryStore

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            store = VerifiedRecoveryStore(run_dir)
            exported = orch.AccountTask(
                account=orch.Account("exported@example.invalid", "password", "Exported"),
                session_key="session-1",
            )
            pending = orch.AccountTask(
                account=orch.Account("pending@example.invalid", "password", "Pending"),
                session_key="session-2",
            )
            store.record_verified(exported)
            store.record_verified(pending)
            store.mark_exported(exported.task_id)

            store.cleanup_exported()

            text = (run_dir / "verified-recovery.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(exported.task_id, text)
            self.assertIn(pending.task_id, text)


class TestRunStateStore(unittest.TestCase):
    def test_sqlite_state_contains_only_hashed_account_identity(self):
        from claude_register.orchestration.recovery import RunStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            store = RunStateStore(run_dir, "run_123456789_abcdef12")
            task = orch.AccountTask(
                account=orch.Account(
                    "EMAIL_SENTINEL@example.invalid",
                    "PASSWORD_SENTINEL",
                    "Person",
                    mail_refresh_token="REFRESH_TOKEN_SENTINEL",
                ),
                status="success",
                stage="kyc",
                session_key="SESSION_KEY_SENTINEL",
                attempts=2,
                persistence_status="success",
            )

            store.update_task(task)
            store.close()

            database_path = run_dir / "state.sqlite3"
            self.assertEqual(stat.S_IMODE(database_path.stat().st_mode), 0o600)
            raw = database_path.read_bytes()
            for sentinel in (
                b"EMAIL_SENTINEL",
                b"PASSWORD_SENTINEL",
                b"REFRESH_TOKEN_SENTINEL",
                b"SESSION_KEY_SENTINEL",
            ):
                self.assertNotIn(sentinel, raw)

            connection = sqlite3.connect(database_path)
            try:
                row = connection.execute(
                    "SELECT run_id, task_id, email_hash, status, stage, attempts, "
                    "persistence_status FROM task_state"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], "run_123456789_abcdef12")
            self.assertEqual(row[1], task.task_id)
            self.assertEqual(len(row[2]), 64)
            self.assertEqual(row[3:], ("success", "kyc", 2, "success"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
