#!/usr/bin/env python3
"""resume_unit.py in process. Git runs in temporary repositories; add_unit.py is a recording stand-in
and the transcripts are files the test writes: no Claude starts."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import MutableMapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import resume_unit
import retire_unit
import unit_lookup
import unit_move
from production_units import Production, read_production
from shutdown_record import SessionShutdown

DIRECTOR = "11111111-1111-1111-1111-111111111111"


def setUpModule() -> None:
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


@final
class ResumeUnitTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    configs: list[Path] = []
    calls: list[list[str]] = []
    launch_code: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.checkout = self.root / "checkout"
        _ = subprocess.run(["git", "clone", "-q", str(origin), str(self.checkout)], check=True,
                           capture_output=True)
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        self.write_row(f"(retired 2026-10-20, worktree removed, session {DIRECTOR}) docs/plans/alpha.md")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production(alpha): retire alpha-unit")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        self.configs = [self.root / "claude1", self.root / "claude2"]
        self.transcript(self.configs[0])

        self.calls = []
        self.launch_code = 0
        production = read_production(self.doc)

        def production_of(_slug: str) -> Production:
            return production

        def not_marked(_slug: str, _name: str) -> unit_lookup.MarkedUnit | None:
            return None

        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(unit_lookup, "marked_unit", not_marked))
        _ = self.enterContext(mock.patch.object(unit_move, "config_dirs", lambda: self.configs))
        _ = cast(MutableMapping[str, str], self.enterContext(
            mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")})))

    def write_row(self, plan: str) -> None:
        _ = self.doc.write_text(
            "# Production\n\n- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n\n## Units\n\n"
            + "| Unit | Plan | Worktree | Branch | Port | Owns |\n| --- | --- | --- | --- | --- | --- |\n"
            + f"| alpha-unit | {plan} | {self.worktree} | alpha-branch | 1 | scripts |\n",
            encoding="utf-8")

    def transcript(self, config: Path, cwd: Path | None = None) -> None:
        cwd = self.worktree if cwd is None else cwd
        folder = config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n", encoding="utf-8")

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; add_unit.py is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append([Path(argv[1]).name, *argv[2:]])
        return subprocess.CompletedProcess(argv, self.launch_code, "", "launch failed" if self.launch_code else "")

    def resume(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = resume_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def row(self) -> str:
        return next(line for line in self.doc.read_text(encoding="utf-8").splitlines() if line.startswith("| alpha-unit"))

    def assert_resumed(self, code: int, output: str) -> None:
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[-1], ["add_unit.py", "--production", str(self.doc), "alpha-unit", "--resume", DIRECTOR,
                                          "--cwd", str(self.worktree), "--plan", "docs/plans/alpha.md"])
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha-branch")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "main~1"))
        self.assertTrue(self.row().startswith("| alpha-unit | docs/plans/alpha.md |"), self.row())
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production(alpha): resume alpha-unit")
        self.assertIn("alpha-unit resumed", output)

    def test_resumes_the_recorded_session_in_a_worktree_made_from_the_merge_branch(self) -> None:
        self.assert_resumed(*self.resume())

    def test_retired_as_built_without_a_header_is_passed_to_the_resume_launch(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        self.write_row(f"(retired 2026-10-28, worktree removed, session {DIRECTOR}) `{plan}` (run done)")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "condensed plan")

        code, output = self.resume()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls, [["add_unit.py", "--production", str(self.doc), "alpha-unit",
                                      "--resume", DIRECTOR, "--cwd", str(self.worktree), "--plan", plan]])
        self.assertIn(f"| `{plan}` (run done) |", self.row())
        self.assertNotIn("(retired", self.row())

    def test_a_transcript_under_another_account_resumes(self) -> None:
        # The units account moved since the retire: the transcript is under the other config dir.
        self.configs.reverse()
        self.assert_resumed(*self.resume())

    def test_a_failed_launch_keeps_the_note_and_a_rerun_resumes(self) -> None:
        document = self.doc.read_bytes()
        head = git(self.checkout, "rev-parse", "HEAD")
        self.launch_code = 1
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("refused: add_unit.py --resume: launch failed", output)
        self.assertIn(f"session {DIRECTOR})", self.row())
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha-branch")
        self.assertEqual(len(self.calls), 1)
        worktree_head = git(self.worktree, "rev-parse", "HEAD")
        self.launch_code = 0
        code, output = self.resume()
        self.assert_resumed(code, output)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), worktree_head)
        self.assertNotIn("made: worktree", output)

    def test_refusals_change_nothing(self) -> None:
        for plan, expected in (("docs/plans/alpha.md", "alpha-unit is not retired"),):
            with self.subTest(plan=plan):
                self.write_row(plan)
                code, output = self.resume()
                self.assertEqual(code, 1, output)
                self.assertIn(f"refused: {expected}", output)
        _ = git(self.checkout, "checkout", "-q", "--", self.doc.name)
        self.configs = [self.root / "claude3"]
        _ = git(self.checkout, "branch", "alpha-branch", "main")
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"refused: alpha-unit: no account has the transcript of session {DIRECTOR}", output)
        self.assertIn("refused: branch alpha-branch still exists; retire did not finish", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production(alpha): retire alpha-unit")

    def test_dry_run_changes_nothing(self) -> None:
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would resume: session {DIRECTOR} through add_unit.py --resume", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())

    def test_missing_session_refuses_both_dry_run_and_launch_when_no_evidence_exists(self) -> None:
        self.write_row("(retired 2026-10-28, worktree removed, session ) `docs/plans/alpha.md`")
        self.configs = [self.root / "empty-config"]
        before = self.doc.read_bytes()
        for flags in (("--dry-run",), ()):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertIn("refused: alpha-unit: its Units row names no session to resume", output)
                self.assertNotIn("would resume:", output)
                self.assertNotIn("resumed:", output)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.worktree.exists())
                self.assertEqual(self.doc.read_bytes(), before)

    def test_missing_session_uses_the_units_newest_interactive_transcript(self) -> None:
        self.write_row("(retired 2026-10-28, worktree removed) docs/plans/alpha.md")
        older = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / "older.jsonl"
        _ = older.write_text(json.dumps({"entrypoint": "cli", "cwd": str(self.worktree)}) + "\n")
        os.utime(older, (1, 1))
        worker = older.with_name("worker.jsonl")
        _ = worker.write_text(json.dumps({"entrypoint": "sdk-py", "cwd": str(self.worktree)}) + "\n")
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would resume: session {DIRECTOR} through", output)
        self.assertEqual(self.calls, [])
        self.assert_resumed(*self.resume())

    def test_missing_session_prefers_the_newest_retained_shutdown_record(self) -> None:
        self.write_row("(retired 2026-10-28, worktree removed) docs/plans/alpha.md")
        # Only the fields consumed by last_session at this shutdown-record boundary.
        records = cast(list[SessionShutdown], cast(object, [
            {"requested_at": "2026-10-27T23:00:00+00:00", "session": {"session_id": "older"}},
            {"requested_at": "2026-10-28T23:00:00+00:00", "session": {"session_id": DIRECTOR}},
        ]))
        with mock.patch.object(retire_unit, "unit_shutdowns", return_value=records):
            self.assert_resumed(*self.resume())

    def external_transcript(self) -> Path:
        for transcript in (self.configs[0] / "projects").glob(f"*/{DIRECTOR}.jsonl"):
            transcript.unlink()
        folder = self.root / "original-checkout"
        folder.mkdir()
        self.transcript(self.configs[0], folder)
        return folder

    def test_folder_note_resumes_in_the_start_folder_and_skips_worktree_and_branch_steps(self) -> None:
        folder = self.external_transcript()
        self.write_row(f"(retired 2026-10-28, worktree removed, session {DIRECTOR}, folder {folder}) `docs/plans/alpha.md` (run done)")
        # Even a remaining branch is irrelevant when the director runs in a separate checkout.
        _ = git(self.checkout, "branch", "alpha-branch", "main")
        with mock.patch.object(resume_unit, "make_worktree", side_effect=AssertionError("worktree step must be skipped")):
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls, [["add_unit.py", "--production", str(self.doc), "alpha-unit", "--resume", DIRECTOR,
                                      "--cwd", str(folder), "--plan", "docs/plans/alpha.md"]])
        self.assertFalse(self.worktree.exists())
        self.assertIn("| `docs/plans/alpha.md` (run done) |", self.row())
        self.assertNotIn("(retired", self.row())

    def test_older_note_finds_the_original_folder_from_the_transcript(self) -> None:
        folder = self.external_transcript()
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would skip: worktree and branch steps; session starts in {folder}", output)
        self.assertIn(f"--cwd {folder}", output)
        self.assertEqual(self.calls, [])
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn(["--cwd", str(folder)], [call[index:index + 2] for call in self.calls for index in range(len(call) - 1)])
        self.assertFalse(self.worktree.exists())

    def test_renamed_transcript_resumes_in_its_current_project_folder(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old = self.root / "alpha-old-wt"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, self.worktree, self.worktree)) + "\n")
        code, output = self.resume("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"--cwd {self.worktree}", output)
        self.assertNotIn(str(old), output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assert_resumed(*self.resume())
        self.assertEqual(len(self.calls), 1)

    def test_no_cwd_matches_the_holding_project_folder(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        _ = transcript.write_text(json.dumps({"cwd": str(self.root / "alpha-old-wt")}) + "\n")
        before = self.doc.read_bytes()
        for arguments in (("--dry-run",), ()):
            with self.subTest(arguments=arguments):
                code, output = self.resume(*arguments)
                self.assertEqual(code, 1, output)
                self.assertIn(f"no cwd matching its project folder {transcript.parent.name}", output)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.worktree.exists())
                self.assertEqual(self.doc.read_bytes(), before)

    def test_older_note_refuses_no_transcript_or_two_project_folders(self) -> None:
        first = self.external_transcript()
        second = self.root / "another-checkout"
        self.transcript(self.configs[1], second)
        before = self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        for folder, config in ((first, self.configs[0]), (second, self.configs[1])):
            self.assertIn(str(config / "projects" / unit_move.project_folder(str(folder))), output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.doc.read_bytes(), before)
        for config in self.configs:
            for transcript in (config / "projects").glob(f"*/{DIRECTOR}.jsonl"):
                transcript.unlink()
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("found: none", output)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.doc.read_bytes(), before)

    def test_backtick_plan_keeps_its_retired_note_until_resume_and_preserves_the_raw_plan(self) -> None:
        plan = "`docs/as-built/feature-log.md` (run done)"
        self.write_row(f"(retired 2026-10-28, worktree removed, session {DIRECTOR}) {plan}")
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[-1][-2:], ["--plan", "docs/as-built/feature-log.md"])
        self.assertIn(f"| {plan} |", self.row())

    def test_shared_projects_symlink_is_one_transcript_location(self) -> None:
        self.configs[1].mkdir()
        (self.configs[1] / "projects").symlink_to(self.configs[0] / "projects", target_is_directory=True)
        self.assert_resumed(*self.resume())


if __name__ == "__main__":
    _ = unittest.main(argv=sys.argv)
