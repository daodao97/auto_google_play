from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class TestSafeReleaseArchive(unittest.TestCase):
    def test_cli_uses_the_private_default_release_path(self):
        from scripts import build_release as release

        with patch("sys.argv", ["build_release"]), \
             patch.object(release, "build_release", return_value=42) as build, \
             patch("builtins.print") as output:
            self.assertEqual(release.main(), 0)

        build.assert_called_once_with(Path("dist/Claude-register-source.zip"))
        output.assert_called_once_with("release archive ready members=42")

    def test_sensitive_or_generated_members_are_rejected(self):
        from scripts.build_release import UnsafeArchiveError, assert_safe_archive_members

        forbidden = (
            "runtime/results.txt",
            ".claude/settings.json",
            "claude_register/__pycache__/service.pyc",
            "config.json",
            "accounts.txt",
            "session_keys-1.txt",
            "results.txt",
            ".coverage",
        )
        for member in forbidden:
            with self.subTest(member=member), self.assertRaises(UnsafeArchiveError):
                assert_safe_archive_members([member])

    def test_archive_contains_only_safe_committed_source(self):
        from scripts.build_release import assert_safe_archive_members, build_release

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "Claude-register-source.zip"
            member_count = build_release(output, repo_root=root)

            self.assertGreater(member_count, 0)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()

        assert_safe_archive_members(names)
        self.assertIn("README.md", names)
        self.assertIn("claude_register/presentation/static/index.html", names)
        self.assertIn("examples/config.example.json", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
