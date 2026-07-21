"""Build a private source archive from committed files only."""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


_FORBIDDEN_PARTS = {
    ".claude",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "runtime",
    "sessions",
    "test-results",
}
_FORBIDDEN_NAMES = {
    ".coverage",
    ".ds_store",
    "account.txt",
    "accounts.txt",
    "bandit-report.json",
    "config.json",
    "cookies.json",
    "coverage.xml",
    "failed.txt",
    "mailbox.txt",
    "mailboxes.txt",
    "partial.txt",
    "proxies.txt",
    "proxy.txt",
    "results.txt",
}
_FORBIDDEN_GLOBS = (
    "*.local.json",
    "*.log",
    "*.session",
    "kyc_*.txt",
    "routing_hints*.txt",
    "session_keys*.txt",
)


class UnsafeArchiveError(RuntimeError):
    """Raised when a release archive contains a forbidden member."""


def _member_is_forbidden(member: str) -> bool:
    path = PurePosixPath(member)
    lowered_parts = tuple(part.lower() for part in path.parts)
    if path.is_absolute() or ".." in path.parts:
        return True
    if any(part in _FORBIDDEN_PARTS for part in lowered_parts):
        return True
    name = path.name.lower()
    if name in _FORBIDDEN_NAMES:
        return True
    if name.startswith(".env") and name != ".env.example":
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in _FORBIDDEN_GLOBS)


def assert_safe_archive_members(members: Iterable[str]) -> None:
    """Reject archive member names associated with runtime data or secrets."""
    for member in members:
        if _member_is_forbidden(member):
            raise UnsafeArchiveError("release archive contains a forbidden path")


def build_release(
    output: Path,
    *,
    repo_root: Path | None = None,
    revision: str = "HEAD",
) -> int:
    """Archive one committed Git revision and return its validated member count."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o600)
    try:
        subprocess.run(
            [
                "git",
                "archive",
                "--format=zip",
                f"--output={temporary}",
                revision,
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        with zipfile.ZipFile(temporary) as archive:
            members = archive.namelist()
        assert_safe_archive_members(members)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        return len(members)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("dist/Claude-register-source.zip"),
    )
    args = parser.parse_args()
    count = build_release(args.output)
    print(f"release archive ready members={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
