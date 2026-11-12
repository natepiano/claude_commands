"""Test rebuild needs with scratch folders and scratch Git repositories."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.rebuild import need


class NeedTests(unittest.TestCase):
    """Keep every need record and Git operation inside a scratch folder."""

    root: Path = Path()
    folder: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "needs"
        self.enterContext(patch.dict(os.environ, {"REBUILD_NEEDS_DIR": str(self.folder)}))

    def command(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(need.main(list(arguments)), 0)
        return output.getvalue()

    def record(self, machine: need.Machine, commit: str) -> need.NeedRecord:
        return cast(need.NeedRecord, json.loads(
            (self.folder / f"{machine}-{commit}.json").read_text(encoding="utf-8")))

    def test_add_and_same_key_rerun(self) -> None:
        commit = "a" * 40
        _ = self.command("add", "--machine", "natedev", "--commit", commit,
                         "--reason", "Apply the settings.", "--concerns", "staff-a",
                         "--requested-by", "staff-b")
        filename = self.folder / f"natedev-{commit}.json"
        before = filename.read_bytes(), filename.stat().st_mtime_ns
        record = self.record("natedev", commit)
        self.assertEqual(set(record), {"id", "machine", "nixos_commit", "reason", "concerns",
                                       "requested_by", "created_at"})
        self.assertEqual(record["requested_by"], "staff-b")
        self.assertEqual(record["concerns"], "staff-a")
        self.assertEqual(record["reason"], "Apply the settings.")
        self.assertEqual(record["nixos_commit"], commit)
        self.assertEqual(record["machine"], "natedev")
        _ = datetime.fromisoformat(record["created_at"])
        need.add_need("natedev", commit, "A different reason.", "staff-c")
        self.assertEqual((filename.read_bytes(), filename.stat().st_mtime_ns), before)
        need.add_need("mac", commit, "Apply the Mac settings.", "staff-a")
        self.assertEqual(self.record("mac", commit)["requested_by"], "")
        self.assertEqual(len(list(self.folder.glob("*.json"))), 2)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_list_newest_per_machine_and_joined_reasons_without_subprocess(self) -> None:
        need.add_need("natedev", "a" * 40, "Apply the first settings.", "staff-a")
        need.add_need("natedev", "b" * 40, "Apply the second settings.", "staff-b")
        need.add_need("mac", "c" * 40, "Apply the Mac settings.", "staff-c")
        for commit, created_at in (("a" * 40, "2026-01-02T00:00:00+00:00"),
                                   ("b" * 40, "2026-01-01T23:00:00-02:00")):
            record = self.record("natedev", commit)
            record["created_at"] = created_at
            _ = (self.folder / f"natedev-{commit}.json").write_text(json.dumps(record), encoding="utf-8")
        done = self.folder / "done"
        done.mkdir()
        _ = (done / "ignored.json").write_text("invalid JSON", encoding="utf-8")
        with patch("subprocess.run", side_effect=AssertionError("A reader called a subprocess.")), \
                patch("subprocess.Popen", side_effect=AssertionError("A reader started a subprocess.")):
            summaries = need.read_needs()
            self.assertEqual(len(summaries), 2)
            local = next(summary for summary in summaries if summary.newest["machine"] == "natedev")
            self.assertEqual(local.newest["nixos_commit"], "b" * 40)
            self.assertEqual(local.reasons, ("Apply the first settings.", "Apply the second settings."))
            self.assertEqual(local.records, (self.record("natedev", "a" * 40), self.record("natedev", "b" * 40)))
            self.assertEqual([record["concerns"] for record in local.records], ["staff-a", "staff-b"])
            rows = cast(list[need.NeedRecord], json.loads(self.command("list", "--json")))
            row = next(record for record in rows if record["machine"] == "natedev")
            self.assertEqual(row["reason"], "Apply the first settings. Apply the second settings.")
            self.assertIn(f"natedev {'b' * 40} {row['reason']}", self.command("list"))

    def test_list_missing_folder_is_empty(self) -> None:
        self.assertEqual(need.read_needs(), [])
        self.assertEqual(self.command("list", "--json"), "[]\n")
        self.assertFalse(self.folder.exists())

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                                capture_output=True, text=True,
                                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                     "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                     "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        return result.stdout.strip()

    def history(self) -> tuple[str, str]:
        _ = self.git("init", "--quiet")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "First settings")
        ancestor = self.git("rev-parse", "HEAD")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Second settings")
        return ancestor, self.git("rev-parse", "HEAD")

    def clear(self, machine: need.Machine, *lines: str) -> str:
        generations = self.root / "generations.log"
        _ = generations.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.command("clear", "--machine", machine, "--generations", str(generations),
                            "--nixos-repo", str(self.root))

    def log_line(self, commit: str, action: str = "switch") -> str:
        return f"2026-11-12T12:34:56-0500 {action} system-42-link {commit} /nix/store/test-system"

    def test_clear_ancestor_moves_record_with_log_line(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the first settings.", "staff-a")
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-b")
        need.add_need("mac", ancestor, "Apply the Mac settings.", "staff-c")
        line = self.log_line(descendant)
        self.assertEqual(self.clear("natedev", line), "")
        for commit in (ancestor, descendant):
            filename = f"natedev-{commit}.json"
            self.assertFalse((self.folder / filename).exists())
            archived = cast(need.CompletedNeedRecord, json.loads((self.folder / "done" / filename).read_text()))
            self.assertEqual(archived["cleared_by"], line)
            before = (self.folder / "done" / filename).read_bytes()
            need.add_need("natedev", commit, "A repeated request.", "staff-c")
            self.assertFalse((self.folder / filename).exists())
            self.assertEqual((self.folder / "done" / filename).read_bytes(), before)
        self.assertEqual(self.record("mac", ancestor)["reason"], "Apply the Mac settings.")

    def test_clear_dirty_suffix(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("mac", ancestor, "Apply the settings.", "staff-a")
        line = self.log_line(descendant + "-dirty")
        self.assertEqual(self.clear("mac", line), "")
        archived = cast(need.CompletedNeedRecord, json.loads(
            (self.folder / "done" / f"mac-{ancestor}.json").read_text()))
        self.assertEqual(archived["cleared_by"], line)

    def test_clear_unknown_commit_keeps_needs_and_reports_one_anomaly(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the first settings.", "staff-a")
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-b")
        unknown = "f" * 40
        line = self.log_line(unknown)
        output = self.clear("natedev", line, line)
        self.assertEqual(output, f"anomaly: unknown logged commit {unknown}\n")
        self.assertEqual(len(list(self.folder.glob("*.json"))), 2)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_unknown_need_commit_keeps_need_and_reports_one_anomaly(self) -> None:
        _ancestor, descendant = self.history()
        unknown = "f" * 40
        need.add_need("natedev", unknown, "Apply the settings.", "staff-a")
        line = self.log_line(descendant)
        output = self.clear("natedev", line, line)
        self.assertEqual(output, f"anomaly: unknown need commit {unknown} (need natedev-{unknown})\n")
        self.assertEqual(self.record("natedev", unknown)["nixos_commit"], unknown)
        self.assertFalse((self.folder / "done").exists())

    def test_clear_non_ancestor_keeps_need(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", descendant, "Apply the second settings.", "staff-a")
        self.assertEqual(self.clear("natedev", self.log_line(ancestor)), "")
        self.assertTrue((self.folder / f"natedev-{descendant}.json").exists())

    def test_clear_ignores_boot_and_test(self) -> None:
        ancestor, descendant = self.history()
        need.add_need("natedev", ancestor, "Apply the settings.", "staff-a")
        self.assertEqual(self.clear("natedev", self.log_line(descendant, "boot"),
                                    self.log_line(descendant, "test")), "")
        self.assertTrue((self.folder / f"natedev-{ancestor}.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
