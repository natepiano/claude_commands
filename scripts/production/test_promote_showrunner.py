#!/usr/bin/env python3
"""promote_showrunner.py in process, with recording stand-ins for herdr, the unit's process, the session
lookups and add_showrunner's start: nothing real stops or starts. Git runs in temporary repositories."""
from __future__ import annotations

import io
import json
import os
import subprocess
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
import herdr
from add_unit import Production, read_production
from production_units import Refusal, plan_cell_is_retired, unit_table
from scripts.accounts import accounts
from scripts.herdr import herdr as trust_reader
from scripts.whoami import account

CHILD_PREFLIGHT = add_showrunner.checked

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PID = 4242
PANE = "w7:p1"
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
    unit_account: accounts.ClaudeLaunchAccount = accounts.NamedAccount("claude2", Path())
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
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "conversation-pause"),
        })))
        self.marked = {"demo-unit": unit_lookup.MarkedUnit(
            "demo-unit", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID), herdr.HerdrPlace("demo", PANE))}
        self.unit_account = accounts.NamedAccount("claude2", self.root / ".claude2")
        (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}}))
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: default@example.invalid\n---\n")
        directory = self.root / ".claude2"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}}))
        self.runs_production, self.pane, self.child_refusal = True, unit_effort.Ready(), ""
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(notes), "NOTIFIER_SESSIONS_DIR": str(sessions), "UNIT_ACTIVE_DIR": str(self.root / "active")}))
        self.assertEqual(accounts.setup("claude2"), [])
        for module, name, stand_in in (
                (showrunners, "production_of", self.production_of), (unit_lookup, "marked_unit", self.marked_unit),
                (herdr, "stop_session", self.stop_host), (promote_showrunner, "type_place", self.type_pane), (promote_showrunner, "_alive", self.is_alive),
                (promote_showrunner, "POLL_S", 0), (add_showrunner, "checked", self.child_checked),
                (add_showrunner, "main", self.start), (accounts, "role_account", self.role_account),
                (unit_effort, "verdict", self.verdict)):
            _ = self.enterContext(mock.patch.object(module, name, stand_in))

    def ready_showrunner_account(self) -> accounts.NamedAccount:
        notes = self.root / "notes"
        notes.mkdir(exist_ok=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)})))
        _ = (notes / "claude3.md").write_text("---\nlogin: showrunner@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude3"), [])
        directory = accounts.account_dir("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "showrunner@example.invalid"}}))
        return accounts.NamedAccount("claude3", directory)

    def production_of(self, _session: str) -> Path | None:
        return self.parent_doc if self.runs_production else None

    def verdict(self, _capture: str) -> unit_effort.Ready | unit_effort.Skipped:
        return self.pane

    def marked_unit(self, _slug: str, name: str) -> unit_lookup.MarkedUnit | None:
        return self.marked.get(name)

    def role_account(self, _role: str) -> accounts.ClaudeLaunchAccount:
        return self.unit_account

    def stop_host(self, session: str) -> None:
        self.events.append(f"stop herdr {session}")

    def type_pane(self, place: herdr.HerdrPlace, text: str) -> None:
        herdr.Herdr(place.session).prompt(place.pane, text)
        self.events.append(f"prompt herdr {place.session} {text}")

    def is_alive(self, pid: int) -> bool:
        self.assertEqual(pid, PID)
        return self.alive.pop(0) if len(self.alive) > 1 else self.alive[0]

    def child_checked(self, _child: Production, caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None, *, config_dir: Path | None = None) -> None:
        self.events.append(f"check child for {caller}, ending {ending}, starting in {start_in}")
        self.assertEqual(config_dir, self.unit_account.dir)
        if self.child_refusal:
            raise Refusal(f"{self.child_refusal} (ending {ending})")

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
        self.assertFalse(any(event.startswith(("prompt herdr ", "stop herdr ", "start")) for event in self.events))
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
            "demo-unit has a /unit:direct run open": run_open,
            "has uncommitted work; it checkpoints first": lambda: (self.worktree / "draft.py").write_text("x"),
            f"child already has a running showrunner (ending {add_showrunner.EndingUnitTab(PANE)})": child_taken,
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
        self.assertIn("check: ok — would stop demo-unit (herdr demo), resume it as the showrunner of child", out)
        self.assert_nothing_stopped()

    def test_the_unit_is_gone_before_it_restarts_and_its_row_is_retired(self) -> None:
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        stop_at = self.events.index("prompt herdr demo /exit")
        ended_at = self.events.index("stop herdr demo")
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

    def test_unready_showrunner_still_retires_the_units_row_and_names_the_owed_prompt(self) -> None:
        """cbc14765, 2026-10-20: the session opened, its prompt was refused, and the row stayed a live unit."""
        self.started = add_showrunner.SESSION_NOT_READY
        with mock.patch.object(promote_showrunner, "restore_line") as restore:
            code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        restore.assert_not_called()
        self.assertTrue(plan_cell_is_retired(self.demo_row()["Plan"]))
        self.assertIn("demo-unit is open as the session child, but its showrunner prompt is not proven to have run; "
                      + "its Units row is retired.", out)
        self.assertIn("herdr session attach agents, workspace child, tab child", out)
        self.assertIn(f"/showrunner:produce {self.child_doc} resume", out)
        self.assertNotIn("add_unit.py", out)

    def test_unready_showrunner_whose_retire_fails_reports_the_row_as_owed(self) -> None:
        self.started = add_showrunner.SESSION_NOT_READY
        before = self.parent_doc.read_text()
        with mock.patch.object(promote_showrunner, "retire", side_effect=OSError("disk full")):
            code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(self.parent_doc.read_text(), before)
        self.assertIn("its Units row was not retired and is owed: disk full.", out)
        self.assertIn(f"/showrunner:produce {self.child_doc} resume", out)

    def test_prompted_showrunner_not_yet_attached_still_retires_the_units_row(self) -> None:
        self.started = 1
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        self.assertTrue(plan_cell_is_retired(self.demo_row()["Plan"]))
        self.assertIn("demo-unit is now the showrunner of child", out)
        self.assertNotIn("add_unit.py", out)

    def test_promotion_refuses_unconfigured_showrunners_before_stopping_unit(self) -> None:
        """F004: promotion does not inherit the unit's previous account."""
        with mock.patch.object(accounts, "role_account", side_effect=accounts.AccountsError(
                "showrunners: not configured — accounts.py move showrunners <claudeN>")):
            code, _, error = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("accounts.py move showrunners <claudeN>", error)
        self.assert_nothing_stopped()
        self.assertFalse(accounts.groups_file().exists())

    def test_promotion_uses_showrunners_group_instead_of_retained_unit_account(self) -> None:
        selected = self.ready_showrunner_account()
        directory = selected.dir
        def checked(_child: Production, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, directory)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            self.events.append("check showrunner account")
            return None
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        with (mock.patch.object(accounts, "role_account", side_effect=resolved) as resolve,
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, _, error = self.run_main()
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(resolve.call_args_list)
        self.assertIn(mock.call("showrunner"), resolve.call_args_list)
        self.assertNotIn(mock.call("unit"), resolve.call_args_list)
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn(str(directory), start)
        self.assertNotIn("--config-dir " + str(self.root / ".claude") + " ", start)
        self.assertLess(self.events.index("check showrunner account"),
            next(index for index, event in enumerate(self.events) if event.startswith("stop herdr ")))

    def test_promotion_resolves_showrunners_account_before_stopping_and_passes_it_to_resume(self) -> None:
        """F004: promotion uses group configuration before the stop begins."""
        selected = self.ready_showrunner_account()
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            self.events.append("resolve " + role)
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        def checked(_child: Production, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, selected.dir)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            return None
        with (mock.patch.object(accounts, "role_account", side_effect=resolved),
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, _, error = self.run_main()
        self.assertEqual((code, error), (0, ""))
        kill_at = next(index for index, event in enumerate(self.events) if event.startswith("stop herdr "))
        start_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(self.events.index("resolve showrunner"), kill_at)
        self.assertLess(kill_at, start_at)
        self.assertIn("--config-dir " + str(selected.dir), self.events[start_at])

    def test_failed_promotion_recovery_defers_account_to_units_configuration(self) -> None:
        """F004: returning to unit work also obeys units configuration."""
        selected = self.ready_showrunner_account()
        self.started = 2
        def checked(_child: Production, _caller: str, ending: add_showrunner.EndingTab = add_showrunner.NO_ENDING_TAB, start_in: Path | None = None,
                    *, config_dir: Path | None = None) -> None:
            self.assertEqual(config_dir, selected.dir)
            self.assertEqual((ending, start_in), (add_showrunner.EndingUnitTab(PANE), self.worktree))
            return None
        def resolved(role: str) -> accounts.ClaudeLaunchAccount:
            return selected if role == "showrunner" else accounts.NamedAccount("claude2", self.root / ".claude2")
        with (mock.patch.object(accounts, "role_account", side_effect=resolved),
              mock.patch.object(add_showrunner, "checked", side_effect=checked)):
            code, output, error = self.run_main()
        self.assertEqual((code, error), (1, ""))
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn("--config-dir " + str(selected.dir), start)
        self.assertIn("add_unit.py", output)
        self.assertIn("--resume " + SESSION, output)
        self.assertNotIn("--config-dir", output)
        self.assertNotIn("--account", output)
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))


    def test_a_unit_still_running_after_exit_is_not_restarted(self) -> None:
        self.alive = [True]
        with mock.patch.object(promote_showrunner, "STOP_WAIT_S", 0):
            code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"Claude (pid {PID}) still runs", out)
        self.assertNotIn("stop herdr demo", self.events)
        self.assertFalse(any(event.startswith("start") for event in self.events))
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def native_unit(self) -> None:
        self.marked["demo-unit"] = unit_lookup.MarkedUnit(
            "demo-unit", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("native", "w1:p1"))
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(
            account, "process_config_dir", return_value=accounts.main_config_dir())))

    def test_herdr_promotion_starts_on_the_showrunners_account_on_herdr(self) -> None:
        self.native_unit()
        selected = self.ready_showrunner_account()
        self.unit_account = selected
        with mock.patch.object(herdr, "stop_session") as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 0, err)
        stopped.assert_called_once_with("native")
        start = next(event for event in self.events if event.startswith("start"))
        self.assertIn(str(selected.dir), start)

    def test_herdr_promotion_refuses_unconfigured_showrunners_without_stopping(self) -> None:
        self.native_unit()
        with mock.patch.object(accounts, "role_account", side_effect=accounts.AccountsError(
                "showrunners: not configured — accounts.py move showrunners <claudeN>")), \
                mock.patch.object(herdr, "stop_session") as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("accounts.py move showrunners <claudeN>", err)
        stopped.assert_not_called()
        self.assert_nothing_stopped()

    def test_a_herdr_unit_exits_before_its_named_session_stops_and_restarts(self) -> None:
        self.native_unit()

        def stop_session(label: str) -> None:
            self.assertEqual(self.alive, [False])
            self.events.append(f"stop herdr {label}")

        with mock.patch.object(herdr, "stop_session", side_effect=stop_session) as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 0, err)
        stopped.assert_called_once_with("native")
        calls = (self.root / "herdr.log").read_text().splitlines()
        self.assertEqual(calls, [
            json.dumps(["--session", "native", "pane", "read", "w1:p1", "--source", "recent", "--lines", "60",
                        "--format", "ansi"]),
            json.dumps(["--session", "native", "agent", "prompt", "w1:p1", "/exit"]),
        ])
        start_at = next(index for index, event in enumerate(self.events) if event.startswith("start"))
        self.assertLess(self.events.index("stop herdr native"), start_at)

    def test_another_herdr_unit_does_not_exempt_the_childs_live_destination(self) -> None:
        self.marked["demo-unit"] = unit_lookup.MarkedUnit(
            "demo-unit", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("demo", PANE))
        with mock.patch.object(add_showrunner, "checked", CHILD_PREFLIGHT), \
                mock.patch.object(showrunners, "descendants_of", return_value=[]), \
                mock.patch.object(add_showrunner, "running", return_value=False), \
                mock.patch.object(add_showrunner, "herdr_tab_open", return_value=True) as live, \
                mock.patch.object(trust_reader, "claude_trust", return_value=trust_reader.Trusted(
                    self.worktree, self.root / ".claude.json", self.worktree)):
            code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("a tab named child is already open in herdr session agents", err)
        live.assert_called_once_with("child", add_showrunner.EndingUnitTab(PANE))
        self.assert_nothing_stopped()
        self.assertFalse(any(event.startswith("stop herdr ") for event in self.events))

    def test_promotion_check_exempts_its_own_tab_in_the_shared_session(self) -> None:
        self.marked["demo-unit"] = unit_lookup.MarkedUnit(
            "demo-unit", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("agents", PANE))
        with (mock.patch.object(add_showrunner, "checked", CHILD_PREFLIGHT),
              mock.patch.object(showrunners, "descendants_of", return_value=[]),
              mock.patch.object(add_showrunner, "running", return_value=False),
              mock.patch.object(herdr, "shared_session", return_value="agents"),
              mock.patch.object(herdr, "live_sessions", return_value=[herdr.SessionEntry(
                  name="agents", running=True, default=False, session_dir="/fixture", socket_path="/sock")]),
              mock.patch.object(herdr.Herdr, "panes", return_value=[{"pane_id": PANE, "label": "child"}]),
              mock.patch.object(trust_reader, "claude_trust", return_value=trust_reader.Trusted(
                  self.worktree, self.root / ".claude.json", self.worktree))):
            code, out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("check: ok", out)
        self.assert_nothing_stopped()

    def test_a_blocked_herdr_unit_is_never_stopped_or_restarted(self) -> None:
        self.native_unit()
        refusal: herdr.HerdrRefusal = {"error": {"code": "agent_blocked", "message": "blocked"}}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({
            "--session native agent prompt w1:p1 /exit": {
                "code": 1, "stdout": json.dumps(refusal)},
            "default": {"code": 0, "stdout": ""},
        }))
        with mock.patch.object(herdr, "stop_session") as stopped:
            code, _out, err = self.run_main()
        self.assertEqual(code, 2, err)
        self.assertIn("agent_blocked", err)
        stopped.assert_not_called()
        self.assert_nothing_stopped()

    def test_a_failed_restart_prints_the_way_back_and_leaves_the_row(self) -> None:
        self.started = 2
        code, out, _err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("demo-unit is stopped and did not start as a showrunner; its Units row is unchanged.", out)
        self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py --production "
                      + f"{self.parent_doc} demo --plan docs/demo.md --resume {SESSION} --cwd {self.worktree}", out)
        self.assertFalse(plan_cell_is_retired(self.demo_row()["Plan"]))

    def child_with_open_tabs(self, panes: list[herdr.PaneIdentity]) -> Production:
        child_doc = self.worktree / "builds-production.md"
        _ = child_doc.write_text(doc_text(self.worktree))
        child = read_production(child_doc)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(child.checkout): {"hasTrustDialogAccepted": True}}}))
        _ = self.enterContext(mock.patch.object(add_showrunner, "running", return_value=False))
        _ = self.enterContext(mock.patch.object(add_showrunner, "claude_json", return_value=config))
        _ = self.enterContext(mock.patch.object(showrunners, "descendants_of", return_value=[]))
        _ = self.enterContext(mock.patch.object(herdr, "shared_session", return_value="agents"))
        _ = self.enterContext(mock.patch.object(herdr, "live_sessions", return_value=[herdr.SessionEntry(
            name="agents", running=True, default=False, session_dir="/fixture", socket_path="/sock")]))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "panes", return_value=panes))
        return child

    def test_the_childs_check_lets_the_units_own_tab_carry_its_name(self) -> None:
        ending_place = herdr.HerdrPlace("agents", PANE)
        child = self.child_with_open_tabs([{"pane_id": ending_place.pane, "label": "builds"}])
        ORIGINAL_CHECKED(child, "parent", ending=add_showrunner.EndingUnitTab(ending_place.pane),
                         config_dir=self.unit_account.dir)

    def test_the_childs_check_refuses_another_same_named_tab_beside_the_ending_unit(self) -> None:
        child = self.child_with_open_tabs([
            {"pane_id": PANE, "label": "builds"},
            {"pane_id": "w8:p2", "label": "builds"},
        ])
        with self.assertRaisesRegex(Refusal, "^a tab named builds is already open in herdr session agents$"):
            ORIGINAL_CHECKED(child, "parent", ending=add_showrunner.EndingUnitTab(PANE),
                             config_dir=self.unit_account.dir)

    def test_the_childs_check_is_for_the_units_worktree_which_must_be_trusted(self) -> None:
        self.marked["demo-unit"] = unit_lookup.MarkedUnit(
            "demo-unit", unit_lookup.LiveClaude("demo", SESSION, "/sock", PID),
            herdr.HerdrPlace("agents", PANE))
        code, _out, err = self.run_main("--check")
        self.assertEqual(code, 0, err)
        self.assertIn(f"check child for parent, ending {add_showrunner.EndingUnitTab(PANE)}, "
                      + f"starting in {self.worktree}", self.events)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(self.root.resolve()): {"hasTrustDialogAccepted": True}}}))
        child = read_production(self.child_doc)
        with (mock.patch.dict(os.environ, {"ADD_SHOWRUNNER_CLAUDE_JSON": str(config)}),
              mock.patch.object(add_showrunner, "running", mock.Mock(return_value=False)),
              mock.patch.object(add_showrunner, "herdr_tab_open", mock.Mock(return_value=False)),
              mock.patch.object(showrunners, "descendants_of", mock.Mock(return_value=[]))):
            with self.assertRaisesRegex(Refusal, f"child: Claude is not trusted in {self.worktree.resolve()} on "
                                        + "claude2. Run:"):
                _ = ORIGINAL_CHECKED(child, "parent", ending=add_showrunner.EndingUnitTab(PANE), start_in=self.worktree, config_dir=self.unit_account.dir)


if __name__ == "__main__":
    _ = unittest.main()
