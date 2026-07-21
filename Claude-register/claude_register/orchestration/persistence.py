"""Private, durable result-file persistence for completed account tasks."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_register.orchestration.models import AccountTask, OrchestratorConfig


_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class WriteOperation:
    """One append-only result-file operation."""

    path: Path
    line: str


def _create_private_parent(path: Path) -> None:
    if path.exists():
        return

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in reversed(missing):
        os.chmod(directory, 0o700)


class ResultWriter:
    """Build and execute the existing result-classification write plan."""

    def __init__(self, config: OrchestratorConfig):
        self._config = config

    def build_plan(self, task: AccountTask) -> list[WriteOperation]:
        """Return ordered writes while preserving legacy files and line formats."""
        cfg = self._config
        if not task.session_key:
            failed_line = (
                f"{task.account.email}----{task.account.password}----"
                f"{task.error or task.status}"
            )
            return [WriteOperation(Path(cfg.failed_file), failed_line)]

        delivery_line = task.delivery_line()
        primary_path = Path(cfg.partial_file if task.status == "partial" else cfg.output_file)
        operations = [WriteOperation(primary_path, delivery_line)]

        if task.kyc_status in ("not_required", "approved"):
            kyc_path = cfg.kyc_pass_file
        elif task.kyc_status in ("pending", "denied"):
            kyc_path = cfg.kyc_required_file
        elif task.kyc_status == "dead":
            kyc_path = cfg.kyc_dead_file
        else:
            kyc_path = cfg.kyc_unknown_file
        operations.append(WriteOperation(Path(kyc_path), delivery_line))
        return operations

    @staticmethod
    def _append_line(operation: WriteOperation) -> None:
        _create_private_parent(operation.path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(operation.path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(operation.line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def contains_line(path: Path, line: str) -> bool:
        """Return whether an exact result line is already present."""
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            return any(existing.rstrip("\n") == line for existing in handle)

    @classmethod
    def append_operations(
        cls,
        operations: list[WriteOperation],
        *,
        deduplicate: bool = False,
    ) -> None:
        """Append prebuilt operations, optionally skipping exact existing lines."""
        with _WRITE_LOCK:
            for operation in operations:
                if deduplicate and cls.contains_line(operation.path, operation.line):
                    continue
                cls._append_line(operation)

    def write(self, task: AccountTask) -> None:
        """Append all classified result lines under a process-wide lock."""
        self.append_operations(self.build_plan(task))
