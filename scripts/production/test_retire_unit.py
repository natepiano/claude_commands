#!/usr/bin/env python3
"""retire_unit.py in process. Git runs in temporary repositories; shutdown, the Codex mesh, the review
pool and build_hold are recording stand-ins, and the unit's Claude sessions are records the test
writes: nothing real stops."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.guilds import roles  # noqa: E402

import retire_unit
import unit_lookup
import unit_move
from herdr import HerdrPlace
from add_unit import Production, read_production

if TYPE_CHECKING:
    from scripts.message import sessions
    from scripts.message.sessions import SessionRecord
    from scripts.shutdown import record as shutdown_record
    from scripts.shutdown.record import SessionShutdown
else:
    # retire_unit puts the message and shutdown folders on the path.
    import record as shutdown_record
    import sessions
    from sessions import SessionRecord

DIRECTOR = "11111111-1111-1111-1111-111111111111"
# The fixture replaces the lookup; a test of the lookup itself puts it back.
CLAUDE_SESSIONS = retire_unit.claude_sessions
SEAT = "22222222-2222-2222-2222-222222222222"


def setUpModule() -> None:
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True).stdout.strip()


def session(session_id: str, name: str, pid: int) -> SessionRecord:
    return SessionRecord(pid=pid, sessionId=session_id, name=name, messagingSocketPath="/nonexistent.sock",
                         updatedAt=0, tmux="", formerNames=[])


@final
class RetireUnitTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    run_folder: Path = Path()
    calls: list[list[str]] = []
    claude: list[SessionRecord] = []
    pool_lines: str = ""
    marked: unit_lookup.MarkedUnit | None = None
    config: Path = Path()

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
        _ = self.doc.write_text(
            "# Production\n\n- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n\n## Units\n\n"
            + "| Unit | Plan | Worktree | Branch | Port | Owns |\n| --- | --- | --- | --- | --- | --- |\n"
            + f"| alpha-unit | docs/plans/alpha.md | {self.worktree} | alpha-branch | 1 | scripts |\n",
            encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")
        _ = git(self.checkout, "worktree", "add", "-q", "-b", "alpha-branch", str(self.worktree))
        _ = (self.worktree / "work.txt").write_text("done\n", encoding="utf-8")
        _ = git(self.worktree, "add", "work.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the work")
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge alpha", "alpha-branch")

        runs = self.root / "runs"
        self.run_folder = runs / "run-1"
        self.run_folder.mkdir(parents=True)
        _ = (self.run_folder / "progress_history_state.json").write_text(
            json.dumps({"working_dir": str(self.worktree)}), encoding="utf-8")
        _ = (self.run_folder / "director").write_text(DIRECTOR + "\n", encoding="utf-8")
        # The test's own pid stands in for the app-server: the mesh stand-in never signals it.
        _ = (self.run_folder / "mesh_server.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        _ = (self.run_folder / "mesh_roster.json").write_text(
            json.dumps({"alpha-impl": {"status": "running"}, "alpha-old": {"status": "ended"}}), encoding="utf-8")
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / DIRECTOR).write_text("{}", encoding="utf-8")

        self.calls = []
        self.claude = [session(DIRECTOR, "alpha", 4101), session(SEAT, "alpha-seat", 4102)]
        self.pool_lines = "code-1\tcode\talpha-unit:1\tidle\t3\ncode-2\tcode\tbeta-unit:1\tidle\t1\n"
        production = read_production(self.doc)

        def production_of(_slug: str) -> Production:
            return production

        def claude_sessions(_worktree: Path, _ids: frozenset[str]) -> tuple[SessionRecord, ...]:
            return tuple(self.claude)

        def nothing_inside(_worktree: Path) -> list[int]:
            return []

        def no_reservations(_worktree: Path) -> tuple[str, ...]:
            return ()

        def no_records() -> list[SessionShutdown]:
            return []

        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(runs),
                                                       "BUILD_HOLD_DIR": str(holds),
                                                       "GUILD_ROLES_STATE": str(self.root / "guild-roles"),
                                                       "GUILD_OWNERSHIP_STATE": str(self.root / "guild-ownership")}))
        _ = self.enterContext(mock.patch.object(retire_unit, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(retire_unit, "production_of", production_of))
        _ = self.enterContext(mock.patch.object(retire_unit, "run", self.fake_run))
        _ = self.enterContext(mock.patch.object(retire_unit, "claude_sessions", claude_sessions))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "reservations", no_reservations))
        _ = self.enterContext(mock.patch.object(shutdown_record, "all_session_records", no_records))
        self.marked = unit_lookup.MarkedUnit(
            "alpha-unit", unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 4101), HerdrPlace("alpha", "w1:p1"))

        def marked_unit(_slug: str, _name: str) -> unit_lookup.MarkedUnit | None:
            return self.marked

        self.config = self.root / "config"
        _ = self.enterContext(mock.patch.object(unit_lookup, "marked_unit", marked_unit))
        _ = self.enterContext(mock.patch.object(unit_move, "config_dirs", lambda: [self.config]))

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; every other command is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        call = [Path(argv[1]).name, *argv[2:]] if argv[0] == sys.executable else argv
        self.calls.append(call)
        stdout = ""
        if call[:2] == ["review_pool.py", "status"]:
            stdout = self.pool_lines
        elif call[:2] == ["review_pool.py", "retire"]:
            stdout = "".join(line.split("\t")[0] + "\n" for line in self.pool_lines.splitlines()
                             if "\talpha-unit:" in line and "\tidle\t" in line)
        elif call[:2] == ["shutdown.py", "down"]:
            self.claude = []
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def retire(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = retire_unit.main(["alpha", "alpha", *arguments])
        return code, output.getvalue()

    def stops(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] != ["review_pool.py", "status"]]

    def test_refuses_uncommitted_work_and_an_unmerged_checkpoint_and_changes_nothing(self) -> None:
        _ = (self.worktree / "late.txt").write_text("late\n", encoding="utf-8")
        _ = git(self.worktree, "add", "late.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the late work")
        _ = (self.worktree / "draft.txt").write_text("draft\n", encoding="utf-8")
        before = self.doc.read_text(encoding="utf-8")

        code, output = self.retire()

        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: draft.txt", output)
        self.assertIn("refused: alpha-branch has a checkpoint not merged: checkpoint(alpha): the late work", output)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_the_recorders_project_started_line_is_not_uncommitted_work(self) -> None:
        plan = self.worktree / "work.txt"
        recorded = plan.read_text(encoding="utf-8") + "- **Project started:** 2026-10-18T22:00:00-04:00\n"
        _ = plan.write_text(recorded, encoding="utf-8")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        self.assertIn(f"would remove: worktree {self.worktree}", output.splitlines())
        # A line a person wrote beside it is still uncommitted work.
        _ = plan.write_text(recorded + "a note\n", encoding="utf-8")
        code, output = self.retire("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: work.txt", output)

    def test_dry_run_lists_the_seat_the_worker_and_the_reviewer_and_changes_nothing(self) -> None:
        before = self.doc.read_text(encoding="utf-8")

        code, output = self.retire("--dry-run")

        self.assertEqual(code, 0, output)
        for line in (f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through shutdown down",
                     f"would stop: Claude session alpha-seat ({SEAT}, pid 4102), through shutdown down",
                     f"would stop: app-server pid {os.getpid()} of run run-1; workers alpha-impl",
                     "would retire: reviewer code-1",
                     f"would release: build hold of {DIRECTOR}",
                     f"would remove: worktree {self.worktree}",
                     "would delete: branch alpha-branch here and on origin"):
            self.assertIn(line, output.splitlines())
        self.assertNotIn("code-2", output)
        self.assertEqual(self.stops(), [])
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.doc.read_text(encoding="utf-8"), before)

    def test_a_run_director_working_outside_the_worktree_is_still_stopped(self) -> None:
        director = session(DIRECTOR, "alpha", 4101)
        elsewhere = session("33333333-3333-3333-3333-333333333333", "elsewhere", 4103)
        with (mock.patch.object(retire_unit, "claude_sessions", CLAUDE_SESSIONS),
              mock.patch.object(sessions, "live_sessions", return_value=[director, elsewhere])):
            code, output = self.retire("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn(f"would stop: Claude session alpha ({DIRECTOR}, pid 4101), through shutdown down", output.splitlines())
        self.assertNotIn("elsewhere", output)

    def test_retires_a_unit_with_a_seat_a_worker_and_a_reviewer(self) -> None:
        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.stops(), [
            ["shutdown.py", "down", "--here", "--only", f"{DIRECTOR},{SEAT}"],
            ["shutdown.py", "forget", DIRECTOR],
            ["shutdown.py", "forget", SEAT],
            ["codex_mesh.py", "stop", "--session-dir", str(self.run_folder)],
            ["review_pool.py", "retire", "--production", "alpha", "--unit", "alpha-unit"],
            ["build_hold.py", "release", "--holder", DIRECTOR],
        ])
        self.assertIn("stopped: reviewer code-1", output)
        self.assertIn("alpha-unit retired", output)
        row = next(line for line in self.doc.read_text(encoding="utf-8").splitlines() if line.startswith("| alpha-unit"))
        self.assertRegex(row, r"^\| alpha-unit \| \(retired \d{4}-\d{2}-\d{2}, worktree removed, session "
                         + DIRECTOR + r"\) docs/plans/alpha\.md \|")
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production(alpha): retire alpha-unit")
        self.assertEqual(git(self.checkout, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(git(self.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")

    def test_role_cleanup_refusal_keeps_the_unit_row_worktree_and_branch(self) -> None:
        before = self.doc.read_bytes()
        with mock.patch.object(roles, "end_holder", side_effect=roles.RoleRefused("designer turn is still running")):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("held role cleanup", output)
        self.assertIn("designer turn is still running", output)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.checkout, "branch", "--list", "--format=%(refname:short)", "alpha-branch"), "alpha-branch")

    def test_a_stopped_director_is_found_by_its_newest_interactive_transcript(self) -> None:
        self.marked = None
        folder = self.config / "projects" / unit_move.project_folder(str(self.worktree))
        folder.mkdir(parents=True)
        for name, entrypoint, mtime in (("older", "cli", 100), (DIRECTOR, "cli", 200), (SEAT, "sdk-cli", 300)):
            transcript = folder / f"{name}.jsonl"
            _ = transcript.write_text('{"type": "summary"}\n' + json.dumps({"entrypoint": entrypoint}) + "\n",
                                      encoding="utf-8")
            os.utime(transcript, (mtime, mtime))

        code, output = self.retire()

        self.assertEqual(code, 0, output)
        self.assertIn(f"worktree removed, session {DIRECTOR}) docs/plans/alpha.md", self.doc.read_text(encoding="utf-8"))

    def test_a_reviewer_still_reviewing_keeps_the_unit_from_retiring(self) -> None:
        self.pool_lines = "code-1\tcode\talpha-unit:1\tbusy\t3\n"

        code, output = self.retire()

        self.assertEqual(code, 1)
        self.assertIn("left running: reviewer code-1: it is reviewing", output)
        self.assertIn("alpha-unit is not retired: rerun once these stop", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertNotIn("(retired", self.doc.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
