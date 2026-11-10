"""Deadlines isolate maintenance failures; completed parts survive an interrupted pass."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast, override
from unittest.mock import patch

from scripts.maintenance import agent_notes_job as job
from scripts.maintenance import vault_commit
from scripts.maintenance.settings import Part, Schedule, settings
from scripts.production.staff_test_support import isolated_staff_file, unit_membership


class JobTests(unittest.TestCase):
    root: Path = Path()
    config: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "job.toml"
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = self.config.write_text((job.ROOT / "config/agent_notes.toml").read_text())
        self.enterContext(patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root), "AGENT_NOTES_JOB_CONFIG": str(self.config)}))

    def test_refusal_part_hands_moves_to_the_detached_batch_before_stopping(self) -> None:
        from scripts.accounts import refusal_watch, moves
        from scripts.agents import stop_resume

        def refusal_pass(**_options: object) -> list[str]:
            results = stop_resume.move_sessions(["member"], "claude5", why="auto: head", cause="auto",
                                               source_accounts={"member": "claude4"})
            self.assertEqual(results, [stop_resume.MoveHandedOff("member")])
            return []

        with (patch.dict(os.environ, {"HOME": str(self.root)}),
              patch.object(refusal_watch, "run", side_effect=refusal_pass),
              patch.object(stop_resume, "_detach") as detach,
              patch.object(stop_resume, "_stop") as stop,
              patch.object(moves, "append_move") as append):
            self.assertEqual(job._perform(Part.REFUSALS), 0)  # pyright: ignore[reportPrivateUsage]
        stop.assert_not_called()
        append.assert_not_called()
        detach.assert_called_once_with(["batch", "--target", "claude5", "--why", "auto: head", "--cause", "auto",
            "--warn-seconds", "0", "--source-account", "member=claude4", "--", "member"], "move")

    def test_successful_parts_are_saved_and_a_rerun_retries_only_the_failure(self) -> None:
        def part(name: Part, _cadence: Schedule, _deadline: float) -> job.PartResult:
            return job.PartResult(name, job.Outcome.FAILED if name is Part.PACER else job.Outcome.SUCCESS,
                                  0, 1 if name is Part.PACER else 0)
        with patch.object(job, "run_part", side_effect=part):
            first = job.run_job()
        self.assertEqual([item.part for item in first if item.exit_code], [Part.PACER])
        saved = cast(dict[str, object], json.loads((self.root / "agent-notes/job/completed.json").read_text()))
        self.assertEqual(set(saved), {part.value for part in Part if part is not Part.PACER})
        with patch.object(job, "run_part", return_value=job.PartResult(Part.PACER, job.Outcome.SUCCESS, 0, 0)) as run:
            _ = job.run_job()
            self.assertEqual(run.call_args.args[0], Part.PACER)
            run.assert_called_once()
        path = self.root / "agent-notes/job/completed.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(job, "run_part") as run:
            self.assertTrue(all(item.outcome is job.Outcome.NOT_DUE for item in job.run_job()))
        run.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_quota_follows_notes_and_vault_follows_all_writers(self) -> None:
        notes_done = Event()
        finished: set[Part] = set()
        def part(name: Part, _cadence: Schedule, _deadline: float) -> job.PartResult:
            if name in (Part.QUOTA, Part.PACER):
                self.assertTrue(notes_done.is_set())
            if name is Part.VAULT:
                self.assertEqual(finished, set(Part) - {Part.VAULT})
            finished.add(name)
            if name is Part.NOTES:
                notes_done.set()
            return job.PartResult(name, job.Outcome.SUCCESS, 0, 0)
        with patch.object(job, "run_part", side_effect=part):
            self.assertTrue(all(item.exit_code == 0 for item in job.run_job()))

    def vault_git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root / "rust/hanadocs"), *arguments], check=True,
            capture_output=True, text=True,
        ).stdout.rstrip("\n")

    def init_vault(self) -> Path:
        vault = self.root / "rust/hanadocs"
        vault.mkdir(parents=True)
        _ = self.vault_git("init", "-q", "-b", "main")
        _ = self.vault_git("config", "user.name", "Fixture")
        _ = self.vault_git("config", "user.email", "fixture@example.test")
        _ = self.vault_git("config", "commit.gpgsign", "false")
        return vault

    def test_phase_notes_join_an_existing_commit_without_causing_note_only_commits(self) -> None:
        vault = self.init_vault()
        git = self.vault_git
        agent_note = vault / "agents/notes.md"
        agent_note.parent.mkdir()
        _ = agent_note.write_text("date_modified: 1\noriginal agent note\n")
        hand = vault / "hand.md"
        _ = hand.write_text("original hand note\n")
        old_note = vault / "showrunners/show/old-unit.md"
        old_note.parent.mkdir(parents=True)
        _ = old_note.write_text(f"---\nphase_table: true\nunit: {unit_membership('show', 'old-unit').id}\n---\nold unit\n")
        _ = git("add", ".")
        _ = git("commit", "-qm", "Initial notes")
        original_head = git("rev-parse", "HEAD")
        old_note.unlink()
        note = old_note.with_name("new-unit.md")
        note_text = f"---\nphase_table: true\nunit: {unit_membership('show', 'new-unit').id}\n---\nnew unit\n"
        _ = note.write_text(note_text)
        _ = hand.write_text("unrelated hand change\n")
        _ = git("add", "hand.md")

        with patch.object(Path, "home", return_value=self.root), patch("builtins.print"):
            self.assertEqual(job._perform(Part.VAULT), 0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(git("rev-parse", "HEAD"), original_head)
            _ = agent_note.write_text("date_modified: 2\noriginal agent note\n")
            self.assertEqual(job._perform(Part.VAULT), 0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(git("rev-parse", "HEAD"), original_head)
            _ = agent_note.write_text("date_modified: 3\nchanged agent note\n")
            self.assertEqual(job._perform(Part.VAULT), 0)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(git("rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git("show", "HEAD:showrunners/show/new-unit.md"), note_text.strip())
        self.assertNotIn("showrunners/show/old-unit.md", git("ls-tree", "-r", "--name-only", "HEAD").splitlines())
        self.assertEqual(git("show", "HEAD:hand.md"), "original hand note")
        self.assertEqual(git("status", "--porcelain"), "M  hand.md")

    def test_content_commit_leaves_unrelated_showrunner_markdown_unchanged(self) -> None:
        vault = self.init_vault()
        folder = vault / "showrunners/tools"
        folder.mkdir(parents=True)
        agent_note = vault / "agents/notes.md"
        agent_note.parent.mkdir()
        _ = agent_note.write_text("original agent note\n")
        design = folder / "design.md"
        deleted = folder / "deleted-design.md"
        _ = design.write_text("original design\n")
        _ = deleted.write_text("kept design\n")
        _ = self.vault_git("add", ".")
        _ = self.vault_git("commit", "-qm", "Initial notes")

        _ = design.write_text("unrelated design edit\n")
        _ = self.vault_git("add", "showrunners/tools/design.md")
        deleted.unlink()
        personal = folder / "personal.md"
        _ = personal.write_text("# Personal note\nphase_table: true\n")
        disabled = folder / "disabled.md"
        _ = disabled.write_text("---\nphase_table: false\nproduction: tools\nunit: disabled\n---\n")
        phase = folder / "unit.md"
        phase_text = f"---\nphase_table: true\nunit: {unit_membership('tools', 'tools-unit').id}\n---\n"
        _ = phase.write_text(phase_text)
        _ = agent_note.write_text("changed agent note\n")
        with patch.object(Path, "home", return_value=self.root), patch("builtins.print"):
            self.assertEqual(job._perform(Part.VAULT), 0)  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(self.vault_git("rev-list", "--count", "HEAD"), "2")
        self.assertEqual(self.vault_git("show", "HEAD:showrunners/tools/unit.md"), phase_text.strip())
        self.assertEqual(self.vault_git("show", "HEAD:showrunners/tools/design.md"), "original design")
        self.assertEqual(self.vault_git("show", "HEAD:showrunners/tools/deleted-design.md"), "kept design")
        self.assertEqual(set(self.vault_git("status", "--porcelain", "--untracked-files=all").splitlines()), {
            "M  showrunners/tools/design.md", " D showrunners/tools/deleted-design.md",
            "?? showrunners/tools/personal.md", "?? showrunners/tools/disabled.md",
        })

    def test_a_hung_part_times_out_without_costing_a_parallel_part(self) -> None:
        script = self.root / "part.py"
        _ = script.write_text("import signal,sys\nif sys.argv[-1] == 'notes': signal.pause()\nprint('completed')\n")
        cadence = Schedule(1, 0.2)
        with patch.object(job, "__file__", str(script)), ThreadPoolExecutor() as pool:
            blocked = pool.submit(job.run_part, Part.NOTES, cadence, time.monotonic() + 2)
            healthy = pool.submit(job.run_part, Part.PACER, cadence, time.monotonic() + 2)
            good, bad = healthy.result(), blocked.result()
        self.assertEqual((good.outcome, good.exit_code, good.output.strip()), (job.Outcome.SUCCESS, 0, "completed"))
        self.assertEqual((bad.outcome, bad.exit_code), (job.Outcome.TIMED_OUT, 124))
        self.assertLess(bad.seconds, 2)

    def test_unexpected_part_exits_still_fail_the_job(self) -> None:
        script = self.root / "part.py"
        _ = script.write_text("raise SystemExit(2)\n")
        with patch.object(job, "__file__", str(script)), patch("sys.argv", ["agent-notes"]), patch("builtins.print"):
            self.assertEqual(job.main(), 1)

    def test_a_vault_without_note_paths_succeeds(self) -> None:
        with patch.object(Path, "home", return_value=self.root), patch.object(
                vault_commit, "git", return_value=subprocess.CompletedProcess[str]([], 0, stdout="")):
            self.assertEqual(job.perform(Part.VAULT), 0)

    def test_runtime_configuration_controls_each_parts_interval_and_deadline(self) -> None:
        original = self.config.read_text()
        _ = self.config.write_text(original.replace("timeout_seconds = 60", "timeout_seconds = 0.5"))
        self.assertEqual(settings().parts[Part.NOTES].timeout_seconds, 0.5)
        def result(part: Part, _cadence: Schedule, _deadline: float) -> job.PartResult:
            return job.PartResult(part, job.Outcome.SUCCESS, 0, 0)
        with patch.object(job, "run_part", side_effect=result):
            _ = job.run_job()
        _ = self.config.write_text(original.replace("interval_seconds = 120", "interval_seconds = 0.000000001"))
        with patch.object(job, "run_part", side_effect=result) as run:
            _ = job.run_job()
        self.assertEqual(run.call_count, len(Part))


if __name__ == "__main__":
    _ = unittest.main()
