"""Local, non-network recovery state for verified account tasks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_register.orchestration.persistence import ResultWriter, WriteOperation

if TYPE_CHECKING:
    from claude_register.orchestration.models import AccountTask


RECOVERY_FILENAME = "verified-recovery.jsonl"
STATE_FILENAME = "state.sqlite3"


def _ensure_private_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class VerifiedRecoveryStore:
    """Maintain a private checkpoint after verification and export it idempotently."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / RECOVERY_FILENAME
        self._lock = threading.Lock()

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("invalid_recovery_record") from exc
                if not isinstance(record, dict):
                    raise ValueError("invalid_recovery_record")
                if not isinstance(record.get("task_id"), str):
                    raise ValueError("invalid_recovery_record")
                if not isinstance(record.get("delivery_line"), str):
                    raise ValueError("invalid_recovery_record")
                records.append(record)
        return records

    def _rewrite_records(self, records: list[dict[str, Any]]) -> None:
        _ensure_private_directory(self.run_dir)
        temporary = self.run_dir / f".{RECOVERY_FILENAME}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
            _fsync_directory(self.run_dir)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def record_verified(self, task: AccountTask) -> None:
        """Persist the verified delivery line before any later network stage."""
        if not task.session_key:
            raise ValueError("missing_session_key")
        with self._lock:
            records = self._read_records()
            if any(record["task_id"] == task.task_id for record in records):
                return
            record = {
                "task_id": task.task_id,
                "delivery_line": task.delivery_line(),
                "verified_at": int(time.time() * 1000),
                "exported": False,
            }
            _ensure_private_directory(self.run_dir)
            ResultWriter.append_operations(
                [
                    WriteOperation(
                        self.path,
                        json.dumps(record, ensure_ascii=True, separators=(",", ":")),
                    )
                ]
            )

    def mark_exported(self, task_id: str) -> None:
        """Mark one checkpoint exported after normal result persistence succeeds."""
        with self._lock:
            records = self._read_records()
            changed = False
            for record in records:
                if record["task_id"] == task_id and not record.get("exported", False):
                    record["exported"] = True
                    changed = True
            if changed:
                self._rewrite_records(records)

    def cleanup_exported(self) -> None:
        """Remove exported checkpoints while preserving pending recovery data."""
        with self._lock:
            records = self._read_records()
            pending = [record for record in records if not record.get("exported", False)]
            if pending == records:
                return
            if pending:
                self._rewrite_records(pending)
                return
            if self.path.exists():
                self.path.unlink()
                _fsync_directory(self.run_dir)

    def recover_pending(self) -> int:
        """Export pending checkpoints as recovered partial results exactly once."""
        with self._lock:
            records = self._read_records()
            recovered = 0
            for record in records:
                if record.get("exported", False):
                    continue
                delivery_line = record["delivery_line"]
                primary_paths = [self.run_dir / "results.txt", self.run_dir / "partial.txt"]
                kyc_paths = [
                    self.run_dir / "kyc_pass.txt",
                    self.run_dir / "kyc_required.txt",
                    self.run_dir / "kyc_unknown.txt",
                    self.run_dir / "kyc_dead.txt",
                ]
                operations: list[WriteOperation] = []
                if not any(ResultWriter.contains_line(path, delivery_line) for path in primary_paths):
                    operations.append(WriteOperation(self.run_dir / "partial.txt", delivery_line))
                if not any(ResultWriter.contains_line(path, delivery_line) for path in kyc_paths):
                    operations.append(WriteOperation(self.run_dir / "kyc_unknown.txt", delivery_line))
                ResultWriter.append_operations(operations, deduplicate=True)
                record["exported"] = True
                record["recovered_status"] = "recovered_partial"
                recovered += 1
            if recovered:
                self._rewrite_records(records)
            return recovered


class RunStateStore:
    """Thread-safe SQLite state containing only non-sensitive task metadata."""

    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.path = self.run_dir / STATE_FILENAME
        self._lock = threading.Lock()
        _ensure_private_directory(self.run_dir)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_state (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                email_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                kyc_status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                error_class TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                persistence_error_class TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()
        self._tighten_permissions()

    def _tighten_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.chmod(0o600)

    def update_task(self, task: AccountTask) -> None:
        """Upsert one non-sensitive task snapshot."""
        email_hash = hashlib.sha256(
            task.account.email.strip().lower().encode("utf-8")
        ).hexdigest()
        with task._lock:
            values = (
                task.task_id,
                self.run_id,
                email_hash,
                task.status,
                task.stage,
                task.kyc_status,
                task.attempts,
                task.error_class,
                task.persistence_status,
                task.persistence_error_class,
                task.started_at,
                time.time(),
            )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO task_state (
                    task_id, run_id, email_hash, status, stage, kyc_status,
                    attempts, error_class, persistence_status,
                    persistence_error_class, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status=excluded.status,
                    stage=excluded.stage,
                    kyc_status=excluded.kyc_status,
                    attempts=excluded.attempts,
                    error_class=excluded.error_class,
                    persistence_status=excluded.persistence_status,
                    persistence_error_class=excluded.persistence_error_class,
                    started_at=excluded.started_at,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            self._connection.commit()
            self._tighten_permissions()

    def close(self) -> None:
        """Flush and close the run state database."""
        with self._lock:
            self._connection.close()
            self._tighten_permissions()
