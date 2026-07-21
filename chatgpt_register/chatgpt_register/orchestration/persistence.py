"""Append-only private JSONL persistence."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from chatgpt_register.registration.models import RegistrationResult


_LOCK = threading.Lock()


class ResultWriter:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(self.run_dir, 0o700)

    def write(self, result: RegistrationResult) -> Path:
        name = {
            "success": "results.jsonl",
            "partial": "partial.jsonl",
            "failed": "failed.jsonl",
        }.get(result.status, "failed.jsonl")
        record = result.private_record()
        if result.status == "failed":
            record.pop("access_token", None)
            record.pop("session_data", None)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = self.run_dir / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with _LOCK:
            descriptor = os.open(path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    descriptor = -1
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        return path

