from __future__ import annotations

import errno
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_register.orchestration import service as orch
from claude_register.presentation import web as webui


def _config(base: Path) -> orch.OrchestratorConfig:
    return orch.OrchestratorConfig(
        output_file=str(base / "results.txt"),
        failed_file=str(base / "failed.txt"),
        partial_file=str(base / "partial.txt"),
        kyc_pass_file=str(base / "kyc_pass.txt"),
        kyc_required_file=str(base / "kyc_required.txt"),
        kyc_unknown_file=str(base / "kyc_unknown.txt"),
        kyc_dead_file=str(base / "kyc_dead.txt"),
    )


class TestResultWriter(unittest.TestCase):
    def test_web_run_archive_directory_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            run_id = "run_123456789_abcdef12"

            with patch.object(webui, "PROJECT_ROOT", project_root):
                webui._configure_run_outputs({}, run_id)

            run_dir = project_root / "runtime" / "runs" / run_id
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_creates_missing_output_directory_and_private_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "nested" / "run"
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                status="success",
                session_key="session",
                kyc_status="not_required",
            )

            orch._write_result(task, _config(run_dir))

            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
            for filename in ("results.txt", "kyc_pass.txt"):
                path = run_dir / filename
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                (run_dir / "results.txt").read_text(encoding="utf-8"),
                "person@example.invalid----password----session\n",
            )

    def test_preserves_microsoft_delivery_field_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            task = orch.AccountTask(
                account=orch.Account(
                    "person@example.invalid",
                    "password",
                    "Person",
                    mail_client_id="client-id",
                    mail_refresh_token="refresh-token",
                ),
                status="partial",
                session_key="session",
                kyc_status="pending",
            )

            orch._write_result(task, _config(base))

            expected = (
                "person@example.invalid----password----client-id----"
                "refresh-token----session\n"
            )
            self.assertEqual((base / "partial.txt").read_text(encoding="utf-8"), expected)
            self.assertEqual((base / "kyc_required.txt").read_text(encoding="utf-8"), expected)

    def test_read_only_result_file_raises_without_exposing_the_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            result_path = base / "results.txt"
            result_path.write_text("existing\n", encoding="utf-8")
            result_path.chmod(0o400)
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                status="success",
                session_key="session",
                kyc_status="not_required",
            )

            try:
                with self.assertRaises(OSError) as captured:
                    orch._write_result(task, _config(base))
            finally:
                result_path.chmod(0o600)

            self.assertNotIn(task.delivery_line(), str(captured.exception))

    def test_disk_write_error_propagates_to_the_worker_boundary(self):
        from claude_register.orchestration.persistence import ResultWriter

        with tempfile.TemporaryDirectory() as temp_dir:
            task = orch.AccountTask(
                account=orch.Account("person@example.invalid", "password", "Person"),
                status="success",
                session_key="session",
                kyc_status="not_required",
            )
            error = OSError(errno.ENOSPC, "disk full")

            with patch.object(ResultWriter, "_append_line", side_effect=error):
                with self.assertRaises(OSError) as captured:
                    orch._write_result(task, _config(Path(temp_dir)))

            self.assertEqual(captured.exception.errno, errno.ENOSPC)
            self.assertNotIn(task.delivery_line(), str(captured.exception))


class TestPersistenceWorkerIsolation(unittest.TestCase):
    def test_first_write_failure_does_not_stop_the_next_task(self):
        accounts = [
            orch.Account("first@example.invalid", "password", "First"),
            orch.Account("second@example.invalid", "password", "Second"),
        ]
        processed: list[str] = []

        def fake_register(task, _config, _on_progress=None, _cancel=None):
            processed.append(task.account.email)
            task.status = "success"
            task.session_key = "session"
            task.kyc_status = "not_required"
            task.finished_at = 1
            return task

        with patch.object(orch, "register_one", side_effect=fake_register), patch.object(
            orch,
            "_write_result",
            side_effect=[OSError(errno.ENOSPC, "disk full"), None],
        ):
            tasks = orch.orchestrate(accounts, orch.OrchestratorConfig(), concurrency=1)

        self.assertEqual(processed, [account.email for account in accounts])
        self.assertEqual([task.status for task in tasks], ["success", "success"])
        self.assertEqual(tasks[0].persistence_status, "failed")
        self.assertEqual(tasks[0].persistence_error_class, "OSError")
        self.assertEqual(tasks[1].persistence_status, "success")
        self.assertEqual(tasks[1].persistence_error_class, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
