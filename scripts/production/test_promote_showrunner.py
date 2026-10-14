#!/usr/bin/env python3
"""promote_showrunner.py in process, with recording stand-ins for tmux, the unit's process, the session
lookups and add_showrunner's start: nothing real stops or starts. Git runs in temporary repositories."""
from __future__ import annotations

import io
import json
import os
import subprocess
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest import mock

import add_showrunner
import promote_showrunner
import showrunners
import unit_effort
import unit_lookup
from add_unit import Production, Refusal, plan_cell_is_retired, read_production, unit_table
from scripts.accounts import accounts

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PID = 4242
PANE = "%7"
# add_showrunner.checked as written: setUp replaces it with a recording stand-in.
ORIGINAL_CHECKED = add_showrunner.checked


def setUpModule() -> None:
    """The retired row's date is in the user's zone; the zone setting must not change these tests."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


def doc_text(checkout: Path, units: str = "") -> str:
    return ("# Production\n\n"
            + "- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {checkout}\n"
            + "- **Log:** log.md\n\n"
            + "## Units\n\n"
            + "| Unit | Plan | Worktree | Branch | Port | Owns |\n"
            + "| --- | --- | --- | --- | --- | --- |\n"
            + units)


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for command in (["init", "-q", "-b", "main"], ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"], ["config", "commit.gpgsign", "false"]):
        _ = subprocess.run(["git", "-C", str(path), *command], check=True, capture_output=True)
    return path


def commit_all(path: Path) -> None:
    _ = subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    _ = subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True, capture_output=True)


@final
class PromoteShowrunnerTests(unittest.TestCase):
    root: Path = Path()
    parent_doc: Path = Path()
    child_doc: Path = Path()
    worktree: Path = Path()
    events: list[str] = []
    alive: list[bool] = []
    started: int = 0
    marked: dict[str, unit_lookup.MarkedUnit] = {}
    unit_account: accounts.ClaudeLaunchAccount = accounts.DefaultAccount()
    # What the stand-ins answer; a refusal case changes one of them.
    runs_production: bool = True
    pane: unit_effort.Ready | unit_effort.Skipped = unit_effort.Ready()
    child_refusal: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        parent = repo(self.root / "parent")
        self.parent_doc = parent / "parent-production.md"
        _ = self.parent_doc.write_text(doc_text(parent, (
            f"| other-unit | `docs/other.md` | `{self.root / 'other'}` | `other` | — | — |\n"
            + f"| demo-unit | `docs/demo.md` (phase 4) | `{self.root / 'demo'}` | `demo` | — | `scripts/demo/` |\n")))
        _ = (parent / "log.md").write_text("log\n")
        commit_all(parent)
        self.worktree = repo(self.root / "demo")
        self.child_doc = self.worktree / "child-production.md"
        _ = self.child_doc.write_text(doc_text(self.worktree))
        commit_all(self.worktree)
        sessions = self.root / "sessions"
        sessions.mkdir()
        _ = (sessions / f"{PID}.json").write_text(json.dumps({"status": "idle"}))
        self.events, self.alive, self.started = [], [True, True, False], 0
        self.marked = {"demo-unit": unit_lookup.MarkedUnit(
            "demo-unit", PANE, "demo", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID))}
        self.unit_account = accounts.DefaultAccount()
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}}))
        self.runs_production, self.pane, self.child_refusal = True, unit_effort.Ready(), ""
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "NOTIFIER_SESSIONS_DIR": str(sessions), "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active")}))
        for module, name, stand_in in (
                (showrunners, "production_of", self.production_of), (unit_lookup, "marked_units", self.marked_units),
                (promote_showrunner, "_tmux", self.tmux), (promote_showrunner, "_alive", self.is_alive),
                (promote_showrunner, "POLL_S", 0), (add_showrunner, "checked", self.child_checked),
                (add_showrunner, "main", self.start), (accounts, "role_account", self.role_account),
                (unit_effort, "verdict", self.verdict)):
            _ = self.enterContext(mock.patch.object(module, name, stand_in))

    def production_of(self, _session: str) -> Path | None:
        return self.parent_doc if self.runs_production else None

    def verdict(self, _capture: str) -> unit_effort.Ready | unit_effort.Skipped:
        return self.pane

    def marked_units(self, _slug: str) -> dict[str, unit_lookup.MarkedUnit]:
        return self.marked

    def role_account(self, _role: str) -> accounts.ClaudeLaunchAccount:
        return self.unit_account

    def tmux(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.events.append(" ".join(("tmux", *arguments)))
        shown = "╭───╮\n│ > │\n╰───╯\n" if arguments[0] == "capture-pane" else ""
        if arguments[0] == "show-options" and arguments[-1] == "@claude_config_dir":
            shown = str(self.root / ".claude" if isinstance(self.unit_account, accounts.DefaultAccount) else self.unit_account.dir) + "\n"
        return subprocess.CompletedProcess(["tmux", *arguments], 0, shown, "")

    def is_alive(self, pid: int) -> bool:
        self.assertEqual(pid, PID)
        return self.alive.pop(0) if len(self.alive) > 1 else self.alive[0]

    def child_checked(self, _child: Production, caller: str, ending: str = "", start_in: Path | None = None, *, config_dir: Path | None = None) -> str:
        self.events.append(f"check child for {caller}, ending {ending}, starting in {start_in}")
        self.assertEqual(config_dir, self.root / ".claude" if isinstance(self.unit_account, accounts.DefaultAccount) else self.unit_account.dir)
        if self.child_refusal:
            raise Refusal(f"{self.child_refusal} (ending {ending})")
        return "tmux"

    def start(self, argv: list[str]) -> int:
        self.events.append("start " + " ".join(argv))
        return self.started

    def run_main(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = promote_showrunner.main(["demo", "--production", str(self.child_doc),
                                            "--model", "opus", "--effort", "high", *extra])
        return code, out.getvalue(), err.getvalue()

    def demo_row(self) -> dict[str, str]:
        return next(cells for cells in unit_table(self.parent_doc.read_text().splitlines())
                    if cells["Unit"] == "demo-unit")

    def assert_nothing_stopped(self) -> None:
        self.assertFalse(any("send-keys" in event or event.startswith("start") for event in self.events))
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def test_each_refusal_names_its_cause_and_stops_nothing(self) -> None:
        def no_production() -> None:
            self.runs_production = False

        def child_taken() -> None:
            self.child_refusal = "child already has a running showrunner"

        def mid_turn() -> None:
            _ = (self.root / "sessions" / f"{PID}.json").write_text(json.dumps({"status": "busy"}))

        def form() -> None:
            self.pane = unit_effort.Skipped("a form, prompt or menu is open")

        def run_open() -> None:
            (self.root / "active").mkdir()
            _ = (self.root / "active" / SESSION).write_text("/tmp/run")

        cases = {
            "this session runs no production": no_production,
            "demo-unit has no live session under parent": lambda: self.marked.clear(),
            "demo-unit is mid-turn": mid_turn,
            "demo-unit's pane: a form, prompt or menu is open": form,
            "demo-unit has a /unit:delegate run open": run_open,
            "has uncommitted work; it checkpoints first": lambda: (self.worktree / "draft.py").write_text("x"),
            "child already has a running showrunner (ending demo)": child_taken,
        }
        for cause, arrange in cases.items():
            with self.subTest(cause=cause):
                self.setUp()
                _ = arrange()
                code, _out, err = self.run_main()
                self.assertEqual(code, 2, err)
                self.assertIn(cause, err)
                self.assert_nothing_stopped()

    def test_an_uncommitted_child_doc_is_refused(self) -> None:
        outside = repo(self.root / "elsewhere")
        _ = (outside / "seed").write_text("seed")
        commit_all(outside)
        self.child_doc = outside / "child-production.md"
        _ = self.child_doc.write_text(doc_text(outside))
        code, _out, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not committed; the unit commits it first", err)
        self.assert_nothing_stopped()

    def test_a_retired_or_unlisted_unit_is_refused(self) -> None:
        for unit, cause in (("ghost", "ghost-unit has no row in the Units table"), ("other", "other-unit is retired")):
            with self.subTest(unit=unit):
                text = self.parent_doc.read_text().replace("`docs/other.md`", "(retired 2026-10-01) `docs/other.md`")
                _ = self.parent_doc.write_text(text)
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = promote_showrunner.main([unit, "--production", str(self.child_doc),
                                                    "--model", "opus", "--effort", "high"])
                self.assertEqual(code, 2)
                self.assertIn(cause, err.getvalue())

    def test_check_stops_nothing(self) -> None:
        code, out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("check: ok — would stop demo-unit (tmux demo), resume it as the showrunner of child", out)
        self.assert_nothing_stopped()

    def test_the_unit_is_gone_before_it_restarts_and_its_row_is_retired(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        stop_at = self.events.index(f"tmux send-keys -t {PANE} -l /exit")
        self.assertEqual(self.events[stop_at + 1], f"tmux send-keys -t {PANE} Enter")
        ended_at = self.events.index(f"tmux kill-session -t {PANE}")
        started_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(stop_at, ended_at)
        self.assertLess(ended_at, started_at)
        self.assertEqual(self.alive, [False])
        self.assertIn(f"--resume {SESSION} --cwd {self.worktree}", self.events[started_at])
        self.assertIn("--model opus --effort high", self.events[started_at])
        plan = self.demo_row()["Plan"]
        self.assertTrue(plan_cell_is_retired(plan))
        self.assertRegex(plan, r"^\(retired \d{4}-\d{2}-\d{2}: promoted to showrunner of child\) `docs/demo.md`")
        self.assertEqual(self.demo_row()["Owns"], "`scripts/demo/`")
        subject = subprocess.run(["git", "-C", str(self.root / "parent"), "log", "-1", "--format=%s"],
                                 check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(subject, "production(parent): retire demo-unit, promoted to showrunner of child")
        self.assertIn("demo-unit is now the showrunner of child", out)

    def test_promotion_reads_the_units_account_before_destroying_tmux_and_passes_it_to_resume(self) -> None:
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude 3.md").write_text("---\nlogin: unit@example.invalid\n---\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)})))
        self.assertEqual(accounts.setup("claude 3"), [])
        directory = accounts.account_dir("claude 3")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "unit@example.invalid"}}))
        self.unit_account = accounts.NamedAccount("claude 3", directory)
        code, _, err = self.run_main()
        self.assertEqual(code, 0, err)
        read_at = next(index for index, event in enumerate(self.events)
                       if "show-options" in event and "@claude_config_dir" in event)
        kill_at = next(index for index, event in enumerate(self.events) if "kill-session" in event)
        start_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(read_at, kill_at)
        self.assertLess(kill_at, start_at)
        self.assertIn("--config-dir", self.events[start_at])
        self.assertIn(str(directory), self.events[start_at])
        self.assertNotIn("env unit", self.events[start_at])

    def test_promotion_retains_external_account_directory_and_restore_uses_same_directory(self) -> None:
        directory = self.root / "external account"
        directory.mkdir()
        self.unit_account = accounts.NamedAccount("external", directory)
        self.started = 2
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn("--config-dir " + str(directory), start)
        self.assertNotIn("--account", start)
        self.assertIn("--config-dir " + shlex.quote(str(directory)), out)
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def test_promotion_refuses_missing_retained_account_before_stopping_the_unit(self) -> None:
        original = self.tmux
        def missing(*arguments: str) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "show-options":
                return subprocess.CompletedProcess(list(arguments), 1, "", "")
            return original(*arguments)
        with mock.patch.object(promote_showrunner, "_tmux", side_effect=missing):
            code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("--account", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assert_nothing_stopped()

    def test_a_unit_still_running_after_exit_is_not_restarted(self) -> None:
        self.alive = [True]
        with mock.patch.object(promote_showrunner, "STOP_WAIT_S", 0):
            code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"Claude (pid {PID}) still runs", out)
        self.assertNotIn(f"tmux kill-session -t {PANE}", self.events)
        self.assertFalse(any(event.startswith("start") for event in self.events))
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def test_a_failed_restart_prints_the_way_back_and_leaves_the_row(self) -> None:
        self.started = 2
        code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("demo-unit is stopped and did not start as a showrunner; its Units row is unchanged.", out)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py --production "
                      + f"{self.parent_doc} demo --plan docs/demo.md --resume {SESSION} --cwd {self.worktree}", out)
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def test_the_childs_check_lets_the_units_own_session_carry_its_name(self) -> None:
        child = read_production(self.child_doc)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(child.checkout): {"hasTrustDialogAccepted": True}}}))
        with (mock.patch.object(add_showrunner, "running", mock.Mock(return_value=False)),
              mock.patch.object(add_showrunner, "tmux_live", mock.Mock(return_value=True)),
              mock.patch.object(add_showrunner, "claude_json", return_value=config),
              mock.patch.object(showrunners, "descendants_of", mock.Mock(return_value=[])),
              mock.patch.object(unit_lookup, "tmux_binary", mock.Mock(return_value="tmux"))):
            self.assertEqual(ORIGINAL_CHECKED(child, "parent", ending="child"), "tmux")
            with self.assertRaisesRegex(Refusal, "a tmux session named child is already live"):
                _ = ORIGINAL_CHECKED(child, "parent")

    def test_the_childs_check_is_for_the_units_worktree_which_must_be_trusted(self) -> None:
        code, _out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn(f"check child for parent, ending demo, starting in {self.worktree}", self.events)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(self.root.resolve()): {"hasTrustDialogAccepted": True}}}))
        child = read_production(self.child_doc)
        with (mock.patch.dict(os.environ, {"ADD_SHOWRUNNER_CLAUDE_JSON": str(config)}),
              mock.patch.object(add_showrunner, "running", mock.Mock(return_value=False)),
              mock.patch.object(add_showrunner, "tmux_live", mock.Mock(return_value=False)),
              mock.patch.object(showrunners, "descendants_of", mock.Mock(return_value=[])),
              mock.patch.object(unit_lookup, "tmux_binary", mock.Mock(return_value="tmux"))):
            with self.assertRaisesRegex(Refusal, f"child: Claude is not trusted in {self.worktree.resolve()} on "
                                        + "default account. Run:"):
                _ = ORIGINAL_CHECKED(child, "parent", ending="demo", start_in=self.worktree)


if __name__ == "__main__":
    _ = unittest.main()
