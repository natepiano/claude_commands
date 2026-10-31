"""Recover production units with scratch accounts and fake launch and send leaves."""
from __future__ import annotations

import json
import io
import os
import runpy
import signal
import subprocess
import time
import unittest
from datetime import timedelta
from contextlib import contextmanager, redirect_stdout
from collections.abc import Generator
from pathlib import Path
from importlib import import_module
from typing import TYPE_CHECKING, Literal, cast, final, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts import accounts, next_moves, outage_alerts, refusal_watch, role_map, unit_watchdog
from scripts.accounts import test_refusal_watch as fixtures
from scripts.production import showrunners, staff
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship, unit_membership
from scripts.shutdown import folder_trust, stop_resume
from scripts.notify.push_texts import RenderedPush

if TYPE_CHECKING:
    from scripts.shutdown import shutdown_record, inventory, launch_permission, restart, restart_support, pane_record
    from scripts.herdr import herdr
    from scripts.message import sessions
    from scripts.production import unit_lookup
else:
    pane_record = import_module("pane_record")
    shutdown_record = import_module("shutdown_record")
    restart_support = import_module("restart_support")
    restart = import_module("restart")
    herdr = import_module("herdr")
    inventory = import_module("inventory")
    sessions = import_module("sessions")
    launch_permission = import_module("launch_permission")
    unit_lookup = import_module("unit_lookup")
import quota_alert
from scripts.whoami import agent_accounts
NOW = fixtures.NOW


@final
class UnitWatchdogTests(unittest.TestCase):
    shared_move = staticmethod(stop_resume.move_sessions)
    shared_resume = staticmethod(stop_resume.resume)
    fixture: fixtures.RefusalWatchTests = fixtures.RefusalWatchTests()
    home: Path = Path()
    doc: Path = Path()
    runs: Path = Path()
    units: dict[str, inventory.Session] = {}
    finished: set[str] = set()
    retired: set[str] = set()
    commands: list[list[str]] = []
    notices: list[str] = []
    refused: set[str] = set()
    pending: list[list[str]] = []
    finish_jobs: bool = True

    @override
    def setUp(self) -> None:
        self.fixture = fixtures.RefusalWatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        _ = self.enterContext(isolated_staff_file(self.home / "staff.json", {}))
        _ = showrunner_relationship("fixture")
        self.doc = self.home / "fixture-production.md"
        self.units = {}
        self.runs = self.home / "runs"
        self.runs.mkdir()
        self.finished = set()
        self.retired = set()
        self.commands = []
        self.notices = []
        self.refused = set()
        self.pending = []
        self.finish_jobs = True
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.home / "panes"))
        _ = self.enterContext(patch.object(stop_resume, "resume", side_effect=self.resume))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.session_liveness))
        _ = self.enterContext(patch.object(unit_lookup, "runs_dir", return_value=self.runs))
        _ = self.enterContext(patch.object(unit_lookup, "every_marked_unit", side_effect=self.marked))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=[
            showrunners.Showrunner(session="fixture-showrunner", socket="/fixture.sock", slug="fixture", zone="UTC", doc=str(self.doc))]))
        _ = self.enterContext(patch.object(showrunners, "tell", side_effect=self.tell))
        _ = self.enterContext(patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.command))
        _ = self.enterContext(patch.object(herdr, "herdr_binary", return_value="/nonexistent/herdr"))
        _ = self.enterContext(patch.object(herdr.Herdr, "agents", return_value=[]))
        _ = self.enterContext(patch.object(restart, "_prepare_restore_server"))
        _ = self.enterContext(patch.object(herdr.Herdr, "close_restore_placeholders"))
        _ = self.fixture.groups(units="claude2")
        # Every account read, launch permission and message leaf is provided by the fixture.
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "four@example.invalid"}}))
        _ = (self.home / "notes/claude4.md").write_text("---\nlogin: four@example.invalid\n---\n")
        self.fixture.reading("four@example.invalid", 80)
        self.write_doc()

    def session_liveness(self, identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
        return (sessions.LiveSessionProcess(self.units[identity]["pid"]) if identity in
                {entry["sessionId"] for entry in self.fixture.live} else sessions.SessionNotLive())

    def write_doc(self) -> None:
        rows = ["# Fixture", "- **Production slug:** fixture", "## Units", "| Unit | Plan | Worktree |", "| --- | --- | --- |"]
        for name in self.units:
            member = unit_membership("fixture", name)
            if name in self.retired:
                _ = staff.retire(member.id)
            tree = self.home / name
            tree.mkdir(exist_ok=True)
            _ = (tree / ".git").write_text("gitdir: scratch\n")
            events: list[dict[str, object]] = [{"event_type": "run_started", "working_dir": str(tree), "run_started_at": 1}]
            if name in self.finished:
                events.append({"event_type": "run_finished"})
            _ = (self.runs / (name + ".jsonl")).write_text("\n".join(json.dumps(event) for event in events))
            plan = "(retired)" if name in self.retired else "docs/plan.md"
            rows.append(f"| {name} | {plan} | {tree} |")
        _ = self.doc.write_text("\n".join(rows) + "\n")

    def add_unit(self, name: str, *, label: str = "claude4") -> inventory.Session:
        owner = self.fixture.owner(name, host="unit", label=label)
        Path(owner["cwd"]).mkdir(parents=True, exist_ok=True)
        self.units[name] = owner
        self.write_doc()
        entry = pane_record.launch(name, name, owner["cwd"],
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir(label)), "SHOWRUNNER_UNIT": "fixture",
             "SHOWRUNNER_UNIT_ID": name}, ["claude", "-n", owner["name"]])
        pane_record.write("shared", entry)
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        shutdown_record.write_snapshot(shutdown_record.RecoverySnapshot(machine="natedev", login=identity.login, label=label,
            taken_at=NOW.isoformat(), sessions=[shutdown_record.SnapshotClaudeSession(agent="claude", session=item,
                workers=[], timers=[], last_seen=NOW.isoformat(), state="unresolved", crash="host gone")
                for item in self.units.values() if item.get("config_dir") == str(accounts.account_dir(label))]))
        return owner

    def marked(self) -> unit_lookup.MarkedProductions:
        known = {entry["sessionId"] for entry in self.fixture.live}
        found: dict[str, unit_lookup.MarkedUnit] = {}
        for name, owner in self.units.items():
            claude = (unit_lookup.LiveClaude(owner["name"], owner["session_id"], "/fixture.sock", owner["pid"])
                      if owner["session_id"] in known else unit_lookup.ClaudeNotRunning())
            found[name] = unit_lookup.MarkedUnit(name, claude,
                unit_lookup.Place("shared", name))
        return unit_lookup.group_marked_units(("fixture", unit) for unit in found.values())

    def die(self, *names: str) -> None:
        self.fixture.live[:] = [entry for entry in self.fixture.live if entry["sessionId"] not in names]

    def revive(self, identity: str, label: str) -> None:
        owner = self.units[identity]
        owner["config_dir"] = str(accounts.account_dir(label))
        self.die(identity)
        self.fixture.live.append(sessions.SessionRecord(pid=owner["pid"], sessionId=identity, name=owner["name"],
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))

    def tell(self, _slug: str, _key: str, text: str) -> str:
        self.notices.append(text)
        return "told fixture"

    def resume(self, identity: str, *, why: str, to: str, restore_missing: bool) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed:
        self.assertEqual(why, "unit watchdog recovery")
        self.assertTrue(restore_missing)
        self.commands.append(["resume", identity, "--to", to, "--only", identity])
        if to in self.refused:
            return stop_resume.ResumeFailed(identity, "fixture refused")
        self.revive(identity, to)
        return stop_resume.ClaudeUp(identity, identity, to)

    def command(self, argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        self.assertIn("--recover", argv)
        self.assertLessEqual(cast(float, options["timeout"]), 5)
        self.assertIn("--no-block", argv)
        self.assertIn("--user", argv)
        self.pending.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def complete_jobs(self) -> None:
        if self.finish_jobs:
            pending, self.pending = self.pending, []
            for argv in pending:
                offset = argv.index("--recover")
                unit_watchdog._finish_recovery(argv[offset + 1], argv[offset + 2], Path(argv[offset + 3]), argv[offset + 4])  # pyright: ignore[reportPrivateUsage]

    def pass_at(self, minute: int, *, dry_run: bool = False) -> list[str]:
        if not dry_run:
            self.complete_jobs()
        return unit_watchdog.run(NOW + timedelta(minutes=minute), dry_run=dry_run)

    def recover_queued(self, source: unit_watchdog.RecoverySource) -> tuple[str, list[list[str]]]:
        self.commands.clear()
        failure = unit_watchdog._restart(source.label, source.session["session_id"], source.identity)  # pyright: ignore[reportPrivateUsage]
        return failure, self.commands

    @contextmanager
    def stop_process_leaves(self) -> Generator[None]:
        def tab(identity: str) -> herdr.TabSession:
            return herdr.TabSession(identity, self.units[identity]["name"], "fixture", identity)

        def signal_claude(pid: int, requested: int) -> None:
            self.assertEqual(requested, signal.SIGTERM)
            self.die(*(name for name, owner in self.units.items() if owner["pid"] == pid))

        def process(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertEqual(started, "fixture-start")
            return (sessions.LiveSessionProcess(pid) if any(entry["pid"] == pid for entry in self.fixture.live)
                    else sessions.SessionNotLive())

        with (patch.object(herdr, "shared_session", return_value="shared"),
              patch.object(pane_record, "seed", return_value=0),
              patch.object(stop_resume, "_tab_of", side_effect=tab),
              patch.object(sessions, "process_start", return_value="fixture-start"),
              patch.object(sessions, "process_liveness", side_effect=process),
              patch.object(os, "kill", side_effect=signal_claude)):
            yield

    def test_public_stop_keeps_a_unit_down_across_watchdog_ticks(self) -> None:
        owner = self.add_unit("alpha")
        _ = self.add_unit("beta")
        with self.stop_process_leaves():
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("alpha", owner["pid"]))
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.pending, [])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"beta"})
        self.assertTrue(pane_record.read("shared")["alpha"].get("ended", False))
        self.assertFalse(pane_record.read("shared")["beta"].get("ended", False))

    def test_failed_move_still_allows_watchdog_recovery_on_the_launch_account(self) -> None:
        _ = self.add_unit("alpha")
        with (self.stop_process_leaves(), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed("alpha", "never started"))):
            self.assertEqual(self.shared_move(["alpha"], "claude2", why="account move"),
                             [stop_resume.ResumeFailed("alpha", "never started")])
        self.assertFalse(pane_record.read("shared")["alpha"].get("ended", False))
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"alpha"})

    def test_explicit_resume_after_public_stop_allows_recovery_of_a_later_crash(self) -> None:
        owner = self.add_unit("alpha")

        def start(tab: herdr.TabSession, entry: pane_record.PaneLaunch, label: str, message: str) -> None:
            self.assertIn("maintenance", message)
            self.assertEqual((tab.pane_id, entry["session_id"], label), ("alpha", "alpha", "claude4"))
            environment = {**entry["environment"], "HERDR_ENV": "1", "HERDR_SESSION": "shared",
                           "HERDR_PANE_ID": tab.pane_id, "CLAUDE_CONFIG_DIR": entry["config_dir"]}
            payload = json.dumps({"session_id": "alpha", "cwd": owner["cwd"]})
            with (patch.dict(os.environ, environment, clear=True),
                  patch.object(sys, "stdin", io.StringIO(payload)),
                  patch.object(pane_record, "claude_arguments", return_value=stop_resume.resume_arguments(entry))):
                with self.assertRaises(SystemExit) as exited:
                    _ = runpy.run_path(str(stop_resume.SCRIPTS / "hooks/session-start-pane-record.py"), run_name="__main__")
                self.assertEqual(exited.exception.code, 0)
            self.revive("alpha", label)

        config = accounts.account_dir("claude4")
        with self.stop_process_leaves():
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("alpha", owner["pid"]))
            self.assertTrue(pane_record.read("shared")["alpha"].get("ended", False))
            with (patch.object(stop_resume, "_start_session", side_effect=start),
                  patch.object(stop_resume, "_detach"), patch("unit_runs.held", return_value=[]),
                  patch.object(folder_trust, "heal", return_value=herdr.Trusted(
                      Path(owner["cwd"]), config / ".claude.json", Path(owner["cwd"])))):
                result = self.shared_resume("alpha", why="maintenance complete")
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        entry = pane_record.read("shared")["alpha"]
        self.assertFalse(entry.get("ended", False))
        self.assertEqual((entry["session_id"], entry["config_dir"]), ("alpha", str(config)))
        self.die("alpha")
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"alpha"})


    def test_crashed_unit_without_any_record_resumes_in_its_tab_on_the_launch_account(self) -> None:
        _ = self.add_unit("alpha")
        for path in (self.home / "shutdown").rglob("*.json"):
            path.unlink()
        self.die("alpha")
        with (patch.object(shutdown_record, "all_session_records", side_effect=AssertionError("no record read")),
              patch.object(shutdown_record, "all_snapshots", side_effect=AssertionError("no snapshot read")),
              patch.object(shutdown_record, "lookup_account_hold", side_effect=AssertionError("no hold read"))):
            _ = self.pass_at(0)
            self.assertEqual(self.commands, [])
            _ = self.pass_at(2)
            _ = self.pass_at(4)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertIn("restarted on claude4", "\n".join(self.notices))
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, {"alpha"})

    def test_older_launch_without_an_end_mark_still_recovers_its_conversation(self) -> None:
        _ = self.add_unit("alpha")
        path = pane_record.record_path("shared")
        entries = cast(dict[str, dict[str, object]], json.loads(path.read_text()))
        _ = entries["alpha"].pop("ended")
        _ = path.write_text(json.dumps(entries))
        self.die("alpha")
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertIn("restarted on claude4", "\n".join(self.notices))

    def test_recordless_crash_alerts_once_and_a_new_live_generation_allows_the_next_alert(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.add("claude4")
        self.die("alpha")
        with (patch.object(shutdown_record, "all_session_records", side_effect=AssertionError("no record read")),
              patch.object(shutdown_record, "all_snapshots", side_effect=AssertionError("no snapshot read")),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone):
            for minute in (0, 2, 4, 6, 8):
                _ = self.pass_at(minute)
            phone.assert_called_once()
            self.assertTrue(unit_watchdog._read_state()["alpha"]["alerted"])  # pyright: ignore[reportPrivateUsage]
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.die("alpha")
            for minute in (12, 14, 16, 18):
                _ = self.pass_at(minute)
            self.assertEqual(phone.call_count, 2)

    def queued_unit(self) -> unit_watchdog.RecoverySource:
        _ = self.add_unit("alpha")
        self.die("alpha")
        return unit_watchdog._sources()[("fixture", "alpha")]  # pyright: ignore[reportPrivateUsage]

    def test_newest_moved_launch_decides_recovery_account_after_one_tick(self) -> None:
        _ = self.add_unit("alpha")
        old = pane_record.read("shared")["alpha"]
        old["written_at"] = "2026-10-30T01:00:00+00:00"
        pane_record.write("shared", old)
        moved = pane_record.launch("new-alpha", "alpha", old["cwd"],
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2")), "SHOWRUNNER_UNIT": "fixture",
             "SHOWRUNNER_UNIT_ID": "alpha"}, ["claude", "-n", "alpha"])
        pane_record.write("shared", moved)
        self.die("alpha")
        self.assertEqual(pane_record.launch_for_session("alpha"), moved)
        self.assertEqual(self.pass_at(0), [])
        self.assertEqual(self.pending, [])
        self.assertEqual(unit_watchdog._read_state()["alpha"]["first_seen"], NOW.isoformat())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pass_at(2), [])
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.pending[0][self.pending[0].index("--recover") + 1], "claude2")
        _ = self.pass_at(4)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude2", "--only", "alpha"]])

    def test_bad_launch_beside_crashed_unit_does_not_stop_recovery(self) -> None:
        for failure in (OSError("launch unreadable"), ValueError("launch invalid")):
            with self.subTest(failure=type(failure).__name__):
                _ = self.add_unit("bad")
                _ = self.add_unit("alpha")
                self.die("bad", "alpha")
                original = pane_record.launch_for_session

                def launch(identity: str) -> pane_record.PaneLaunch | None:
                    if identity == "bad":
                        raise failure
                    return original(identity)

                with patch.object(pane_record, "launch_for_session", side_effect=launch):
                    first = self.pass_at(0)
                    self.assertTrue(any("bad" in line and str(failure) in line for line in first), first)
                    self.assertIn("alpha", unit_watchdog._read_state())  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(self.pending, [])
                    _ = self.pass_at(2)
                    self.assertEqual(len(self.pending), 1)
                    self.assertEqual(self.pending[0][self.pending[0].index("--recover") + 2], "alpha")
                    _ = self.pass_at(4)
                self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
                self.commands.clear()
                unit_watchdog._state_file().unlink(missing_ok=True)  # pyright: ignore[reportPrivateUsage]

    def test_bad_unit_observation_does_not_skip_later_units_in_the_production(self) -> None:
        _ = self.add_unit("bad")
        _ = self.add_unit("alpha")
        self.die("bad", "alpha")
        original = unit_lookup.RunStates.state

        def state(runs: unit_lookup.RunStates, worktree: Path) -> unit_lookup.UnitState:
            if worktree.name == "bad":
                raise ValueError("unit run state unreadable")
            return original(runs, worktree)

        with patch.object(unit_lookup.RunStates, "state", autospec=True, side_effect=state):
            first = self.pass_at(0)
            self.assertIn("watchdog: fixture/bad: unit run state unreadable", first)
            self.assertIn("alpha", unit_watchdog._read_state())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(self.pending, [])
            _ = self.pass_at(2)
            self.assertEqual(len(self.pending), 1)
            _ = self.pass_at(4)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])


    def test_queued_recovery_uses_the_pane_launch_account(self) -> None:
        source = self.queued_unit()
        failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--to") + 1], "claude4")


    def test_queued_recovery_uses_its_launch_even_when_the_units_group_is_unavailable(self) -> None:
        source = self.queued_unit()
        with patch.object(accounts, "account_for_group_launch", side_effect=accounts.AccountsError("units unavailable")):
            failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--to") + 1], "claude4")


    def test_queued_recovery_keeps_the_account_of_the_latest_move_launch(self) -> None:
        _ = self.queued_unit()
        entry = pane_record.read("shared")["alpha"]
        entry["config_dir"] = str(accounts.account_dir("claude2"))
        pane_record.write("shared", entry)
        source = unit_watchdog._sources()[("fixture", "alpha")]  # pyright: ignore[reportPrivateUsage]
        failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--to") + 1], "claude2")


    def test_queued_recovery_refuses_a_changed_or_ended_pane_launch(self) -> None:
        source = self.queued_unit()
        original = pane_record.read("shared")["alpha"]
        for change in ("written_at", "ended", "account", "cwd"):
            with self.subTest(change=change):
                entry = original.copy()
                if change == "written_at":
                    entry["written_at"] = (NOW + timedelta(days=100)).isoformat()
                elif change == "ended":
                    entry["ended"] = True
                elif change == "account":
                    entry["config_dir"] = str(accounts.account_dir("claude2"))
                else:
                    entry["cwd"] += "/changed"
                pane_record.write("shared", entry)
                failure, launches = self.recover_queued(source)
                self.assertTrue(failure)
                self.assertEqual(launches, [])


    def test_queued_recovery_delegates_missing_pane_creation_to_shared_resume(self) -> None:
        source = self.queued_unit()
        with patch.object(stop_resume, "resume", wraps=self.resume) as resume:
            failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        resume.assert_called_once_with("alpha", why="unit watchdog recovery", to="claude4", restore_missing=True)


    def test_several_units_read_run_records_once_per_pass(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        with patch.object(unit_lookup, "read_run_states", wraps=unit_lookup.read_run_states) as reads:
            self.assertEqual(self.pass_at(0, dry_run=True), [])
            reads.assert_called_once_with()

    def test_recovery_rechecks_a_reported_outage_and_sees_the_new_unit_pane(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            if herdr.herdr_processes():
                return self.marked()
            return unit_lookup.MarkedProductions({"fixture": {"unit": unit_lookup.MarkedUnit(
                "unit", unit_lookup.ClaudeNotRunning(), unit_lookup.Place("shared", "unit"))}}, {}, self.marked().members)
        with (patch.object(herdr, "_scan_herdr_processes", side_effect=[[], [fresh]]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(unit_watchdog.run(NOW, dry_run=False), [])
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pending, [])

    def test_recovery_completion_reads_new_unit_pane_after_report_snapshot(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        processes: list[herdr.HerdrProcess] = []

        def observed() -> unit_lookup.MarkedProductions:
            return self.marked() if herdr.herdr_processes() else unit_lookup.MarkedProductions({}, {})

        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)),
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            processes.append(fresh)
            self.assertEqual(unit_watchdog._live_account(self.doc, "unit"), "claude4")  # pyright: ignore[reportPrivateUsage]

    def test_live_unit_report_reuses_the_scan_without_starting_recovery(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"), {}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            self.assertEqual(herdr.herdr_processes(), [fresh])
            return self.marked()
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              herdr.process_snapshot()):
            _ = herdr.herdr_processes()
            self.assertEqual(unit_watchdog.run(NOW, dry_run=False), [])
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.pending, [])

    def test_idle_real_watcher_and_watchdog_share_the_report_scan(self) -> None:
        owner = self.add_unit("unit", label="claude2")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"),
            {"SHOWRUNNER_UNIT": "fixture", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.MarkedProductions:
            _ = herdr.herdr_processes()
            return self.marked()
        def listing(login: str | None = None, *,
                    registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            _ = herdr.herdr_processes()
            return self.fixture.listing(login, registrations=registrations)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_marked_unit", side_effect=observed),
              patch.object(inventory, "listing", side_effect=listing),
              refusal_watch.session_observation(), herdr.process_snapshot()):
            _ = herdr.herdr_processes()  # The earlier session-table report.
            self.assertEqual(refusal_watch.run(NOW), [])
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.pending, [])
        self.assertEqual(self.fixture.moves.calls, [])

    def test_live_report_without_fresh_confirmation_keeps_recovery_evidence(self) -> None:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        result = Path(outage["job"]["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        live = self.marked()
        self.die("unit")
        absent = self.marked()
        with (patch.object(unit_lookup, "every_marked_unit", side_effect=[live, absent]),
              patch.object(unit_watchdog, "_recovery_result", return_value=unit_watchdog.RecoveryStillRunning()),
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertTrue(result.exists())
        self.assertIn("job", unit_watchdog._read_state()["unit"])  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any("restarted on" in line for line in [*lines, *self.notices]))
        cleared.assert_not_called()

    def test_unreadable_fresh_confirmation_preserves_recovery_result_and_job(self) -> None:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        result = Path(outage["job"]["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        with (patch.object(unit_watchdog, "_live_account", side_effect=OSError("fixture unreadable")),
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertEqual(result.read_text(), '"fixture evidence"')
        self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("fixture unreadable" in line for line in lines))
        self.assertFalse(any("restarted on" in line for line in self.notices))
        cleared.assert_not_called()

    def finished_recovery(self) -> tuple[dict[str, unit_watchdog.DownOutage], Path]:
        _ = self.add_unit("unit")
        self.finish_jobs = False
        self.die("unit")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        outage = state["unit"]
        assert "job" in outage
        job = outage["job"]
        result = Path(job["result"])
        _ = result.write_text('"fixture evidence"')
        self.revive("unit", "claude4")
        return state, result

    def test_finished_recovery_keeps_evidence_when_the_second_fresh_read_is_unreadable(self) -> None:
        state, result = self.finished_recovery()
        with (patch.object(unit_watchdog, "_live_account", side_effect=["", OSError("fixture unreadable")]) as live,
              patch.object(outage_alerts, "clear") as cleared):
            lines = self.pass_at(3)
        self.assertEqual(live.call_count, 2)
        self.assertEqual(result.read_text(), '"fixture evidence"')
        self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertIn("watchdog: fixture/unit: fixture unreadable", lines)
        self.assertFalse(any("restarted on" in line or "retry from" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 1)
        cleared.assert_not_called()

    def test_finished_recovery_consumes_evidence_only_after_live_confirmation(self) -> None:
        state, result = self.finished_recovery()
        checks: list[str] = []

        def confirm(_doc: Path, _unit: str) -> str:
            checks.append("checked")
            self.assertEqual(result.read_text(), '"fixture evidence"')
            self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
            return "" if len(checks) == 1 else "claude4"

        with patch.object(unit_watchdog, "_live_account", side_effect=confirm):
            lines = self.pass_at(3)
        self.assertEqual(len(checks), 2)
        self.assertFalse(result.exists())
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("restarted on claude4" in line for line in lines))
        self.assertEqual(len(self.pending), 1)

    def test_finished_recovery_consumes_evidence_after_absent_confirmation_and_retries(self) -> None:
        state, result = self.finished_recovery()
        checks: list[str] = []

        def confirm(_doc: Path, _unit: str) -> str:
            checks.append("checked")
            self.assertEqual(result.read_text(), '"fixture evidence"')
            return ""

        with patch.object(unit_watchdog, "_live_account", side_effect=confirm):
            lines = self.pass_at(3)
        self.assertEqual(len(checks), 2)
        self.assertFalse(result.exists())
        after = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
        before = state["unit"]
        assert "job" in after and "job" in before
        self.assertNotEqual(after["job"], before["job"])
        self.assertTrue(any("fixture evidence; retry from claude4's pane launch" in line for line in lines))
        self.assertFalse(any("restarted on" in line for line in [*lines, *self.notices]))
        self.assertEqual(len(self.pending), 2)

    def test_obsolete_account_hold_does_not_discard_recovery_evidence_or_retry(self) -> None:
        _, result = self.finished_recovery()
        _ = self.hold("claude4", terminal=True)
        with (patch.object(unit_watchdog, "_live_account", return_value="") as live,
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("terminal stop"))):
            lines = self.pass_at(3)
        self.assertEqual(live.call_count, 2)
        self.assertFalse(result.exists())
        self.assertIn("job", unit_watchdog._read_state()["unit"])  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("retry from" in line for line in lines))
        self.assertEqual(len(self.pending), 2)


    def test_obsolete_session_hold_does_not_discard_recovery_evidence_or_retry(self) -> None:
        _, result = self.finished_recovery()
        saved = shutdown_record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=NOW.isoformat(), request_id="terminal-stop", requested_by=shutdown_record.FromTerminal(kind="terminal"),
            hold="session", then="stop", force="wait for ready", alerts="none", probe=False,
            session=self.units["unit"], workers=[], timers=[], settle_message=shutdown_record.SettleMessageNotSent(kind="not sent"),
            where=shutdown_record.WhereNotSaid(kind="not said"), progress=shutdown_record.SessionStopped(kind="stopped", at=NOW.isoformat()),
            stop_issues=[], trail=[])
        self.assertTrue(shutdown_record.create_session(saved))
        with patch.object(unit_watchdog, "_live_account", return_value="") as live:
            lines = self.pass_at(3)
        self.assertEqual(live.call_count, 2)
        self.assertFalse(result.exists())
        self.assertIn("job", unit_watchdog._read_state()["unit"])  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(any("retry from" in line for line in lines))
        self.assertEqual(len(self.pending), 2)


    def test_finished_recovery_without_a_source_records_failure_and_consumes_the_result(self) -> None:
        state, result = self.finished_recovery()
        unit_watchdog._write_state(state)  # pyright: ignore[reportPrivateUsage]
        _ = pane_record.forget_unit("unit")
        self.die("unit")
        lines = self.pass_at(3)
        outage = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
        assert "dropped" in outage
        self.assertEqual(outage["dropped"], "fixture evidence")
        self.assertNotIn("job", outage)
        self.assertFalse(result.exists())
        self.assertIn("watchdog: unit: fixture evidence", lines)
        self.assertIn("watchdog: unit: no pane launch entry", lines)
        self.assertEqual(len(self.pending), 1)

    def test_failed_result_cleanup_retries_when_a_recovery_source_returns(self) -> None:
        state, result = self.finished_recovery()
        self.pending.clear()
        unit_watchdog._write_state(state)  # pyright: ignore[reportPrivateUsage]
        entry = pane_record.read("shared")["unit"]
        _ = pane_record.forget_unit("unit")
        self.die("unit")
        unlink = Path.unlink
        failures: list[Path] = []

        def remove(path: Path, *, missing_ok: bool = False) -> None:
            if path == result and not failures:
                failures.append(path)
                raise PermissionError("fixture result cleanup denied")
            unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", remove):
            lines = self.pass_at(3)
            self.assertIn("watchdog: fixture/unit: fixture result cleanup denied", lines)
            self.assertTrue(result.exists())
            after_failure = unit_watchdog._read_state()["unit"]  # pyright: ignore[reportPrivateUsage]
            self.assertIn("job", after_failure)
            pane_record.write("shared", entry)
            lines = self.pass_at(4)
        self.assertFalse(result.exists())
        self.assertTrue(any("fixture evidence; retry from claude4's pane launch" in line for line in lines))
        self.assertFalse(any("no longer exists" in line for line in lines))
        self.assertEqual(len(self.pending), 1)
        self.finish_jobs = True
        lines = self.pass_at(5)
        self.assertTrue(any("restarted on claude4" in line for line in lines))
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertIn("unit", {entry["sessionId"] for entry in self.fixture.live})

    def test_10_26_auto_move_ignores_a_stale_hold_and_every_unit_recovers(self) -> None:
        _ = self.fixture.groups(units="claude4")
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("10-26-outage"))
        _ = self.hold("claude2", terminal=False)
        _ = refusal_watch.run(NOW)
        self.assertEqual(self.commands, [])
        _ = refusal_watch.run(NOW + timedelta(minutes=2))
        self.complete_jobs()
        _ = refusal_watch.run(NOW + timedelta(minutes=4))
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, set(self.units))
        for owner in self.units.values():
            self.assertEqual(owner.get("config_dir"), str(accounts.account_dir("claude2")))
        self.assertEqual(len(self.fixture.moves.calls), 3)


    def hold(self, label: str, *, terminal: bool) -> shutdown_record.AccountHold:
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        held = shutdown_record.AccountHold(login=identity.login, label=label, machine="natedev", requested_at=NOW.isoformat(),
            request_id="hold-" + label, requested_by=shutdown_record.FromTerminal(kind="terminal") if terminal else
            shutdown_record.FromSession(kind="session", session_id="b1f5ca98-gone"), then="stop")
        shutdown_record.set_account_hold(held)
        return held

    def test_old_target_hold_does_not_block_a_move_or_later_crash_recovery(self) -> None:
        _ = self.fixture.groups(units="claude4")
        _ = self.add_unit("alpha")
        _ = self.hold("claude2", terminal=True)
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("held-target"))
        _ = refusal_watch.run(NOW)
        self.assertEqual(len(self.fixture.moves.calls), 1)
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        entry = pane_record.read("shared")["alpha"]
        entry["config_dir"] = str(accounts.account_dir("claude2"))
        pane_record.write("shared", entry)
        self.die("alpha")
        _ = self.pass_at(2)
        self.assertEqual(self.commands, [])
        _ = self.pass_at(4)
        _ = self.pass_at(6)
        self.assertEqual(self.commands[0][3], "claude2")
        self.assertIn("restarted on claude2", self.notices[0])


    def test_crash_command_uses_recovery_owner_and_reports_actual_account(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.add("claude2")
        self.die("alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual([argv[3] for argv in self.commands], ["claude4"])
        self.assertIn("restarted on claude4", self.notices[0])

    def test_user_stop_retired_finished_and_in_progress_units_are_not_restarted(self) -> None:
        for name in ("user-stop", "retired", "finished", "waiting"):
            _ = self.add_unit(name)
        self.retired.add("retired")
        self.finished.add("finished")
        self.write_doc()
        for name in ("user-stop", "waiting"):
            entry = pane_record.read("shared")[name]
            entry["ended"] = True
            pane_record.write("shared", entry)
        self.die(*self.units)
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [])

    def test_obsolete_account_stop_does_not_hold_a_crashed_unit(self) -> None:
        _ = self.add_unit("held")
        _ = self.hold("claude4", terminal=True)
        self.die("held")
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("terminal stop")):
            for minute in (0, 2, 4):
                _ = self.pass_at(minute)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("restarted on claude4", self.notices[0])

    def test_both_crash_accounts_refuse_one_push_per_outage_and_showrunner_each_pass(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.update(("claude2", "claude4"))
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4, 6, 8):
                _ = self.pass_at(minute)
            phone.assert_called_once()
            push = cast(RenderedPush, phone.call_args.args[0])
            self.assertEqual(push.id, "shutdown.restart_failed")
            self.assertEqual(push.values["account"], "claude4")
            self.assertIn("retry from claude4's pane launch", self.notices[0])
            self.assertEqual(len(self.notices), 3)
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.die("alpha")
            _ = self.pass_at(12)
            _ = self.pass_at(14)
            _ = self.pass_at(16)
            self.assertEqual(phone.call_count, 2)

    def test_pending_detached_restarts_do_not_wait_or_start_duplicate_commands(self) -> None:
        self.finish_jobs = False
        for index in range(30):
            _ = self.add_unit("down-" + str(index))
        self.die(*self.units)
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 30)
        self.assertEqual(self.commands, [])
        state = cast(object, json.loads((self.home / ".local/state/accounts/unit_watchdog.json").read_text()))
        assert isinstance(state, dict)
        self.assertEqual(len(cast(dict[str, object], state)), 30)
        _ = self.pass_at(4)
        self.assertEqual(len(self.pending), 30)
        self.assertEqual(self.commands, [])

    def test_gone_folder_ends_recovery_and_alerts_once(self) -> None:
        owner = self.add_unit("alpha")
        Path(owner["cwd"]).rmdir()
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4):
                _ = self.pass_at(minute)
            Path(owner["cwd"]).mkdir()
            _ = self.pass_at(6)
            phone.assert_called_once()
        self.assertEqual(self.pending, [])
        self.assertEqual(self.commands, [])
        self.assertTrue(any("no longer exists; restore dropped" in line for line in self.notices))

    def test_folder_lost_after_dispatch_keeps_outage_context_and_retries_its_alert(self) -> None:
        saved = self.moved_outage()
        # An empty legacy snapshot cannot hide the unit's pane launch.
        shutdown_record.write_snapshot(shutdown_record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 1)
        Path(saved["session"]["cwd"]).rmdir()
        command = self.pending[0]
        offset = command.index("--recover")

        failure = self.retained_record_failure(saved)
        with patch.object(unit_watchdog, "_restart", return_value=failure.reason):
            unit_watchdog._finish_recovery(command[offset + 1], command[offset + 2],  # pyright: ignore[reportPrivateUsage]
                Path(command[offset + 3]), command[offset + 4])
        result = Path(command[offset + 3])
        finished = result.read_text()
        result.unlink()
        # The outage owns its alert even before the job result arrives.
        with patch.object(quota_alert, "tell_user", side_effect=[
                quota_alert.PushFailed("fixture delivery failed"), quota_alert.PushSent()]) as phone:
            _ = self.pass_at(3)
            phone.assert_called_once()
            push = cast(RenderedPush, phone.call_args.args[0])
            self.assertEqual(push.id, "shutdown.restart_failed")
            self.assertEqual(push.values["account"], "claude4")
            self.assertEqual(push.values["stopped"], saved["session"]["name"])
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            self.assertIn("job", state["alpha"])
            _ = result.write_text(finished)
            _ = self.pass_at(4)
            self.assertEqual(phone.call_count, 2)
            _ = self.pass_at(6)
            self.assertEqual(phone.call_count, 2)
            _ = self.pass_at(8)
            self.assertEqual(phone.call_count, 2)
        self.assertTrue(any("no longer exists" in line for line in self.notices))

    def fail_moved_restart(self, saved: shutdown_record.SessionShutdown) -> None:
        with (patch.object(restart, "_retire"),
              patch.object(restart, "_live_session_ids", return_value=frozenset()),
              patch.object(restart, "_owner_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()),
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
              patch.object(restart, "_launch_session", side_effect=OSError("both accounts refused")) as launch,
              redirect_stdout(io.StringIO())):
            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=restart.DesktopLaunchState())
        self.assertEqual(launch.call_count, 2)

    def retained_record_failure(self, saved: shutdown_record.SessionShutdown) -> stop_resume.ResumeFailed:
        # Legacy recovery owns the record. The watchdog only observes its outcome.
        output = io.StringIO()
        with redirect_stdout(output):
            _ = restart.up(saved["login"], only=(saved["session"]["session_id"],))
        return stop_resume.ResumeFailed(saved["session"]["session_id"], output.getvalue())

    def fail_pending_restore_with_missing_folder(self, saved: shutdown_record.SessionShutdown) -> None:
        command = self.pending[-1]
        offset = command.index("--recover")
        Path(saved["session"]["cwd"]).rmdir()

        failure = self.retained_record_failure(saved)
        with patch.object(unit_watchdog, "_restart", return_value=failure.reason):
            unit_watchdog._finish_recovery(command[offset + 1], command[offset + 2],  # pyright: ignore[reportPrivateUsage]
                Path(command[offset + 3]), command[offset + 4])

    def test_clear_removes_only_claims_before_the_live_observation(self) -> None:
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                identity = "delivered" if delivered else "claimed"
                with patch.object(time, "time", return_value=1000.0):
                    claimed = outage_alerts.claim(identity)
                assert isinstance(claimed, outage_alerts.AlertClaim)
                if delivered:
                    outage_alerts.delivered(claimed)
                self.assertFalse(outage_alerts.clear(identity, 999.0))
                self.assertFalse(outage_alerts.clear(identity, 1000.0))
                with patch.object(time, "time", return_value=1001.0):
                    self.assertIsInstance(outage_alerts.claim(identity),
                                          outage_alerts.AlertDelivered if delivered else outage_alerts.AlertInFlight)
                self.assertTrue(outage_alerts.clear(identity, time.time() + 1))
                self.assertIsInstance(outage_alerts.claim(identity), outage_alerts.AlertClaim)

    def test_old_live_observation_preserves_a_new_record_alert_mark(self) -> None:
        saved = self.moved_outage()
        with patch.object(time, "time", return_value=1000.0):
            claimed = outage_alerts.claim("alpha")
            assert isinstance(claimed, outage_alerts.AlertClaim)
            self.assertTrue(shutdown_record.claim_restore_alert(saved, "new outage"))
            outage_alerts.delivered(claimed)
        restart._clear_live_outage(saved, 999.0)  # pyright: ignore[reportPrivateUsage]
        current = shutdown_record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertEqual(current.get("restore_alerted"), "new outage")
        restart._clear_live_outage(saved, 1001.0)  # pyright: ignore[reportPrivateUsage]
        current = shutdown_record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertNotIn("restore_alerted", current)

    def test_slow_snapshot_probe_preserves_a_new_watchdog_alert(self) -> None:
        beta = self.add_unit("beta")
        _ = self.add_unit("alpha")
        self.die("beta")
        self.finish_jobs = False
        sessions.sessions_dir().mkdir(parents=True, exist_ok=True)
        clock = [1000.0]

        delayed = [False]

        def slow_probe(pid: int, _start: str) -> restart.SessionNotLive:
            if pid == beta["pid"] and not delayed[0]:
                delayed[0] = True
                clock[0] += 10
                self.die("alpha")
                for minute in (0, 2, 8):
                    _ = self.pass_at(minute)
                self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            return restart.SessionNotLive()

        with (patch.object(time, "time", side_effect=lambda: clock[0]),
              patch.object(restart, "_process_liveness", side_effect=slow_probe) as probe,
              patch.object(restart, "_launch_session", side_effect=OSError("fixture refused")),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              redirect_stdout(io.StringIO())):
            _ = restart.up("four@example.invalid", only=("beta", "alpha"))
            self.assertGreater(probe.call_count, 0)
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            _ = self.pass_at(14)
            alpha_pushes = [call for call in phone.call_args_list
                           if cast(RenderedPush, call.args[0]).values.get("stopped") == "owner alpha"]
            self.assertEqual(len(alpha_pushes), 1)

    def test_slow_batch_archive_preserves_a_new_outage_alert(self) -> None:
        self.slow_confirmation_outage(conductor=False)

    def test_slow_conductor_archive_preserves_a_new_outage_alert(self) -> None:
        self.slow_confirmation_outage(conductor=True)

    def slow_confirmation_outage(self, *, conductor: bool) -> None:
        alpha = self.moved_outage()
        beta_owner = self.add_unit("beta")
        beta = alpha.copy()
        beta.update(session=beta_owner, request_id="beta-move",
            timers=[shutdown_record.TimerRestore(instance="slow-beta", was_enabled=True)])
        self.assertTrue(shutdown_record.create_session(beta))
        self.die("beta")
        clock = [time.time() + 10]
        old = outage_alerts.claim("alpha")
        assert isinstance(old, outage_alerts.AlertClaim)
        outage_alerts.delivered(old)

        def recipient(identity: str) -> herdr.PromptRecipientProcess:
            owner = self.units[identity]
            return herdr.PromptRecipientProcess(owner["pid"], owner["proc_start"])

        def launch(saved: shutdown_record.SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState,
                   *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            identity = saved["session"]["session_id"]
            self.revive(identity, "claude2")
            return restart.SessionLaunched(herdr.PromptSubmitted(NOW, recipient(identity)))

        def slow_timer(_verb: str, instance: str) -> None:
            self.assertEqual(instance, "slow-beta")
            current = shutdown_record.find_session(alpha["login"], "alpha")
            assert current is not None
            self.assertEqual(current["progress"]["kind"], "restarted")
            clock[0] += 10
            self.die("alpha")
            for minute in (0, 2, 8):
                _ = self.pass_at(minute)
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)

        with (patch.object(time, "time", side_effect=lambda: clock[0]),
              patch.object(restart, "_retire"),
              patch.object(restart, "_launch_session", side_effect=launch),
              patch.object(herdr, "prompt_recipient_process", side_effect=recipient),
              patch.object(sessions, "remote_control", return_value="active"),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              patch.object(restart_support, "run_notifier", side_effect=slow_timer)):
            launched = [restart.relaunch_session(saved, dry_run=False,
                desktop_state=restart.DesktopLaunchState()) for saved in (alpha, beta)]
            if conductor:
                for saved in launched:
                    progress = saved["progress"]
                    assert progress["kind"] == "restarting"
                    restart.confirm_entry(saved, progress["at"], True, observed_at=time.time())
                for saved in launched:
                    self.assertTrue(restart_support.archive_finished(saved))
            else:
                restart._confirm_entries(launched)  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(shutdown_record.find_session(alpha["login"], "alpha"))
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            _ = self.pass_at(14)
            phone.assert_called_once()
            clock[0] += 10
            self.revive("alpha", "claude2")
            _ = self.pass_at(16)
            # The live snapshot pass provides recovery for the next outage.
            seen_at = (NOW + timedelta(minutes=16)).isoformat()
            shutdown_record.write_snapshot(shutdown_record.RecoverySnapshot(machine="natedev", login="main@example.invalid",
                label="claude2", taken_at=seen_at, sessions=[shutdown_record.SnapshotClaudeSession(agent="claude",
                    session=self.units["alpha"], workers=[], timers=[], last_seen=seen_at,
                    state="unresolved", crash="agent exited")]))
            self.die("alpha")
            for minute in (18, 20, 26):
                _ = self.pass_at(minute)
            self.assertEqual(phone.call_count, 2)

    def test_dead_alert_owner_is_replaced_and_its_old_token_cannot_finish(self) -> None:
        first = outage_alerts.claim("dead-owner")
        assert isinstance(first, outage_alerts.AlertClaim)
        with patch.object(os, "kill", side_effect=ProcessLookupError):
            second = outage_alerts.claim("dead-owner")
        self.assertIsInstance(second, outage_alerts.AlertClaim)
        assert isinstance(second, outage_alerts.AlertClaim)
        self.assertNotEqual(first.token, second.token)
        outage_alerts.delivered(first)
        outage_alerts.release(first)
        self.assertIsInstance(outage_alerts.claim("dead-owner"), outage_alerts.AlertInFlight)
        outage_alerts.delivered(second)
        self.assertIsInstance(outage_alerts.claim("dead-owner"), outage_alerts.AlertDelivered)

    def test_live_alert_owner_keeps_its_claim_inside_the_lease(self) -> None:
        first = outage_alerts.claim("live-owner")
        self.assertIsInstance(first, outage_alerts.AlertClaim)
        self.assertIsInstance(outage_alerts.claim("live-owner"), outage_alerts.AlertInFlight)

    def test_alert_claim_past_the_lease_is_replaced(self) -> None:
        with patch.object(time, "time", return_value=1000):
            first = outage_alerts.claim("expired-owner")
        with patch.object(time, "time", return_value=2801):
            second = outage_alerts.claim("expired-owner")
        self.assertIsInstance(second, outage_alerts.AlertClaim)
        assert isinstance(first, outage_alerts.AlertClaim) and isinstance(second, outage_alerts.AlertClaim)
        self.assertNotEqual(first.token, second.token)

    def test_conductor_delivery_and_archive_share_the_watchdogs_alert(self) -> None:
        for archived in (False, True):
            with self.subTest(archived=archived):
                saved = self.moved_outage()
                if archived:
                    saved["requested_at"] = (NOW + timedelta(seconds=1)).isoformat()
                    _ = shutdown_record.update_session(saved["login"], "alpha", lambda current: current.update(
                        requested_at=saved["requested_at"]))
                _ = self.pass_at(0)
                _ = self.pass_at(2)
                saved["alerts"] = "user"
                saved["progress"] = shutdown_record.SessionRestartFailed(kind="restart failed", at=NOW.isoformat(), reason="both refused")
                _ = shutdown_record.update_session(saved["login"], "alpha", lambda current: current.update(
                    alerts=saved["alerts"], progress=saved["progress"]))
                with (patch.object(restart, "restart_failed_liveness", return_value=restart.SessionNotLive()),
                      patch.object(restart_support, "run_remote", return_value=(0, "[]")),
                      patch.object(restart_support, "send_message", return_value=restart_support.MessageSent(kind="sent")),
                      patch.object(restart_support, "tell_showrunners_outcome"),
                      patch.object(restart_support, "_deliver_message", return_value=restart_support.MessageSent(kind="sent")) as report_phone,
                      patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as watchdog_phone,
                      redirect_stdout(io.StringIO())):
                    restart_support.report_stop([saved])
                    report_phone.assert_called_once()
                    if archived:
                        self.fail_pending_restore_with_missing_folder(saved)
                    _ = self.pass_at(10)
                    _ = self.pass_at(12)
                    watchdog_phone.assert_not_called()
                self.revive("alpha", "claude4")
                _ = self.pass_at(14)
                if archived:
                    Path(saved["session"]["cwd"]).mkdir(exist_ok=True)
                else:
                    shutdown_record.archive_session(saved["login"], "alpha")

    def test_failed_conductor_push_allows_one_later_watchdog_delivery(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        saved["alerts"] = "user"
        saved["progress"] = shutdown_record.SessionRestartFailed(kind="restart failed", at=NOW.isoformat(), reason="both refused")
        _ = shutdown_record.update_session(saved["login"], "alpha", lambda current: current.update(
                    alerts=saved["alerts"], progress=saved["progress"]))
        with (patch.object(restart, "restart_failed_liveness", return_value=restart.SessionNotLive()),
              patch.object(restart_support, "run_remote", return_value=(0, "[]")),
              patch.object(restart_support, "send_message", return_value=restart_support.MessageSent(kind="sent")),
              patch.object(restart_support, "tell_showrunners_outcome"),
              patch.object(restart_support, "_deliver_message", side_effect=RuntimeError("fixture push failed")),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone,
              redirect_stdout(io.StringIO())):
            restart_support.report_stop([saved])
            _ = self.pass_at(8)
            _ = self.pass_at(14)
            phone.assert_called_once()

    def test_worker_delivery_survives_a_later_retained_missing_folder(self) -> None:
        saved = self.moved_outage()
        shutdown_record.write_snapshot(shutdown_record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            self.fail_moved_restart(saved)
            phone.assert_called_once()
            self.assertEqual(cast(RenderedPush, phone.call_args.args[0]).id, "shutdown.move_failed")
            self.fail_pending_restore_with_missing_folder(saved)
            _ = self.pass_at(10)
            _ = self.pass_at(12)
            phone.assert_called_once()
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(state["alpha"]["alerted"])

    def test_watchdog_during_failed_worker_delivery_does_not_remember_a_claim_as_sent(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        attempts: list[RenderedPush] = []

        def deliver(push: RenderedPush) -> quota_alert.PushSent | quota_alert.PushFailed:
            attempts.append(push)
            if len(attempts) == 1:
                current = shutdown_record.find_session(saved["login"], "alpha")
                assert current is not None
                self.assertIn("restore_alerted", current)
                # Run the real watcher while the worker's delivery is unfinished.
                _ = self.pass_at(8)
                return quota_alert.PushFailed("fixture delivery failed")
            return quota_alert.PushSent()

        with patch.object(quota_alert, "tell_user", side_effect=deliver):
            self.fail_moved_restart(saved)
            self.assertEqual(len(attempts), 1)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            _ = self.pass_at(14)
            _ = self.pass_at(20)
        self.assertEqual(len(attempts), 2)

    def test_failed_worker_delivery_allows_one_move_alert_after_retained_failure(self) -> None:
        saved = self.moved_outage()
        shutdown_record.write_snapshot(shutdown_record.RecoverySnapshot(machine="natedev", login=saved["login"],
            label=saved["label"], taken_at=NOW.isoformat(), sessions=[]))
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushFailed("delivery failed")) as worker_phone:
            self.fail_moved_restart(saved)
            worker_phone.assert_called_once()
        # The pass observes the failed delivery while the worker is still pending.
        _ = self.pass_at(3)
        self.fail_pending_restore_with_missing_folder(saved)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            _ = self.pass_at(4)
            _ = self.pass_at(6)
            phone.assert_called_once()
            self.assertEqual(cast(RenderedPush, phone.call_args.args[0]).id, "shutdown.restart_failed")

    def test_worker_delivery_then_live_unit_allows_one_new_outage_alert(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            self.fail_moved_restart(saved)
            _ = self.pass_at(8)
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
            current = shutdown_record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertClaim)
            _ = outage_alerts.clear("alpha", time.time() + 1)
            self.die("alpha")
            _ = self.pass_at(12)
            _ = self.pass_at(14)
            self.fail_moved_restart(current)
            _ = self.pass_at(20)
            _ = self.pass_at(26)
            self.assertEqual(phone.call_count, 2)

    def test_watchdog_notice_uses_the_users_zone(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        with patch.object(unit_watchdog, "user_zone", return_value=ZoneInfo("Asia/Tokyo")):
            _ = self.pass_at(0)
            _ = self.pass_at(2)
            _ = self.pass_at(4)
        self.assertIn("down since 21:00 JST", self.notices[0])

    def test_grace_keeps_the_launch_then_recovery_resumes_its_conversation(self) -> None:
        self.grace_launch_recovery(probe_unreadable=False)

    def test_unreadable_grace_probe_keeps_recovery_until_a_later_readable_probe(self) -> None:
        self.grace_launch_recovery(probe_unreadable=True)

    def test_newer_pane_launch_replaces_the_configuration_after_unreadable_grace(self) -> None:
        self.grace_launch_recovery(probe_unreadable=True, newer_launch=True)

    def grace_launch_recovery(self, *, probe_unreadable: bool, newer_launch: bool = False) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        self.finish_jobs = False
        if probe_unreadable:
            marked = self.marked()
            marked.units["fixture"]["alpha"] = unit_lookup.MarkedUnit("alpha",
                unit_lookup.ClaudeUnknown("pane unavailable"), unit_lookup.Place("shared", "alpha"))
            with patch.object(unit_lookup, "every_marked_unit", return_value=marked):
                _ = self.pass_at(0)
                _ = self.pass_at(2)
            self.assertEqual(self.pending, [])
            _ = self.pass_at(3)
        else:
            self.assertEqual(self.pass_at(0), [])
        if newer_launch:
            entry = pane_record.read("shared")["alpha"]
            entry["model"] = "sonnet"
            pane_record.write("shared", entry)
        _ = self.pass_at(4 if probe_unreadable else 2)
        self.assertEqual(len(self.pending), 1)
        command = self.pending[0]
        offset = command.index("--recover")
        if newer_launch:
            self.assertEqual(json.loads(command[offset + 4])["model"], "sonnet")
        self.finish_jobs = True
        _ = self.pass_at(6)
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, {"alpha"})
        self.assertEqual(len(self.commands), 1)


    def test_all_detach_calls_share_one_deadline_and_leave_the_rest_due(self) -> None:
        self.finish_jobs = False
        for index in range(20):
            _ = self.add_unit("down-" + str(index))
        self.die(*self.units)
        _ = self.pass_at(0)
        elapsed = [0.0]
        def slow(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            elapsed[0] += cast(float, options["timeout"])
            return self.command(argv, **options)
        with (patch.object(time, "monotonic", side_effect=lambda: elapsed[0]),
              patch.object(subprocess, "run", side_effect=slow)):
            _ = self.pass_at(2)
            self.assertLessEqual(elapsed[0], 30)
            self.assertEqual(len(self.pending), 6)
            _ = self.pass_at(4)
            self.assertEqual(len(self.pending), 12)
        self.assertEqual(self.commands, [])

    def test_expired_job_consumed_before_its_stale_restarting_record_blocks(self) -> None:
        owner = self.add_unit("alpha")
        self.die("alpha")
        self.finish_jobs = False
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        snapshot = shutdown_record.read_snapshot("four@example.invalid")
        assert not isinstance(snapshot, shutdown_record.NoRecoverySnapshot)
        entry = snapshot["sessions"][0]
        assert entry["agent"] == "claude"
        saved = shutdown_record.materialize_snapshot(snapshot, entry)
        def interrupted(current: shutdown_record.SessionShutdown) -> None:
            prior = current["progress"]
            assert prior["kind"] != "restarting"
            current["progress"] = shutdown_record.SessionRestarting(kind="restarting", at=NOW.isoformat(), prior=prior)
        _ = shutdown_record.update_session(saved["login"], owner["session_id"], interrupted)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            lines = self.pass_at(8)
            self.assertTrue(any("did not finish within 5 minutes" in line for line in lines))
            self.assertEqual(len(self.pending), 2)
            phone.assert_called_once()
            _ = self.pass_at(14)
            self.assertEqual(phone.call_count, 1)
            self.assertEqual(len(self.pending), 3)

    def test_expired_move_worker_alerts_once_unless_restart_already_pushed(self) -> None:
        for marked in (False, True):
            with self.subTest(restore_alerted=marked):
                name = "marked" if marked else "unmarked"
                owner = self.add_unit(name)
                saved = shutdown_record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
                    requested_at=NOW.isoformat(), request_id=name, requested_by=shutdown_record.FromTerminal(kind="terminal"),
                    hold="session", then="restart", force="now", alerts="none", probe=False,
                    session=owner, workers=[], timers=[], settle_message=shutdown_record.SettleMessageNotSent(kind="not sent"),
                    where=shutdown_record.WhereNotSaid(kind="not said"), progress=shutdown_record.SessionStopped(kind="stopped", at=NOW.isoformat()),
                    stop_issues=[], trail=[], target="claude2", source_label="claude4")
                self.assertTrue(shutdown_record.create_session(saved))
                if marked:
                    claimed = outage_alerts.claim(name)
                    assert isinstance(claimed, outage_alerts.AlertClaim)
                    outage_alerts.delivered(claimed)
                self.die(name)
                self.finish_jobs = False
                _ = self.pass_at(0)
                _ = self.pass_at(2)
                def interrupted(current: shutdown_record.SessionShutdown) -> None:
                    prior = current["progress"]
                    assert prior["kind"] != "restarting"
                    current["progress"] = shutdown_record.SessionRestarting(kind="restarting", at=NOW.isoformat(), prior=prior)
                _ = shutdown_record.update_session(saved["login"], name, interrupted)
                with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
                    _ = self.pass_at(8)
                    self.assertEqual(phone.call_count, 0 if marked else 1)
                    if not marked:
                        push = cast(RenderedPush, phone.call_args.args[0])
                        self.assertEqual(push.id, "shutdown.restart_failed")
                    _ = self.pass_at(14)
                    self.assertEqual(phone.call_count, 0 if marked else 1)
                self.revive(name, "claude4")
                _ = self.pass_at(16)

    def moved_outage(self) -> shutdown_record.SessionShutdown:
        owner = self.add_unit("alpha")
        saved = shutdown_record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=NOW.isoformat(), request_id="alpha-move", requested_by=shutdown_record.FromTerminal(kind="terminal"),
            hold="session", then="restart", force="now", alerts="none", probe=False,
            session=owner, workers=[], timers=[], settle_message=shutdown_record.SettleMessageNotSent(kind="not sent"),
            where=shutdown_record.WhereNotSaid(kind="not said"), progress=shutdown_record.SessionStopped(kind="stopped", at=NOW.isoformat()),
            stop_issues=[], trail=[], target="claude2", source_label="claude4")
        self.assertTrue(shutdown_record.create_session(saved))
        self.die("alpha")
        self.finish_jobs = False
        return saved

    def test_worker_alert_after_source_read_prevents_watchdog_duplicate(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        def worker_alert(slug: str, key: str, text: str) -> str:
            # The worker finishes after the watcher has read sources, before its alert.
            current = shutdown_record.find_session(saved["login"], "alpha")
            assert current is not None
            self.fail_moved_restart(current)
            return self.tell(slug, key, text)
        with (patch.object(showrunners, "tell", side_effect=worker_alert),
              patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone):
            _ = self.pass_at(8)
            phone.assert_called_once()

    def test_failed_move_push_releases_claim_and_retries_until_sent(self) -> None:
        saved = self.moved_outage()
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        with patch.object(quota_alert, "tell_user", side_effect=[
                quota_alert.PushFailed("fixture delivery failed"), quota_alert.PushSent()]) as phone:
            _ = self.pass_at(8)
            current = shutdown_record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertNotIn("restore_alerted", current)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(state["alpha"]["alerted"])
            _ = self.pass_at(14)
            self.assertEqual(phone.call_count, 2)
            current = shutdown_record.find_session(saved["login"], "alpha")
            assert current is not None
            self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertDelivered)
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(state["alpha"]["alerted"])
            _ = self.pass_at(20)
            self.assertEqual(phone.call_count, 2)

    def test_watchdog_live_observation_clears_move_alert_for_the_next_outage(self) -> None:
        saved = self.moved_outage()
        restart.restart_support.mark_not_back_pushed(saved)
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.revive("alpha", "claude4")
        _ = self.pass_at(4)
        current = shutdown_record.find_session(saved["login"], "alpha")
        assert current is not None
        self.assertIsInstance(outage_alerts.claim("alpha"), outage_alerts.AlertClaim)
        _ = outage_alerts.clear("alpha", time.time() + 1)
        self.die("alpha")
        _ = self.pass_at(6)
        _ = self.pass_at(8)
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            _ = self.pass_at(14)
            _ = self.pass_at(20)
            phone.assert_called_once()

    def test_old_terminal_stop_does_not_hold_a_newer_session_for_the_same_unit(self) -> None:
        old = self.fixture.owner("old-alpha", host="unit", label="claude4")
        host = old["host"]
        assert host["kind"] == "unit"
        host["unit"] = "alpha"
        earlier = (NOW - timedelta(minutes=10)).isoformat()
        saved = shutdown_record.SessionShutdown(login="four@example.invalid", label="claude4", machine="natedev",
            requested_at=earlier, request_id="old-stop", requested_by=shutdown_record.FromTerminal(kind="terminal"),
            hold="session", then="stop", force="wait for ready", alerts="none", probe=False,
            session=old, workers=[], timers=[], settle_message=shutdown_record.SettleMessageNotSent(kind="not sent"),
            where=shutdown_record.WhereNotSaid(kind="not said"), progress=shutdown_record.SessionStopped(kind="stopped", at=earlier),
            stop_issues=[], trail=[])
        self.assertTrue(shutdown_record.create_session(saved))
        _ = self.add_unit("alpha")
        self.die("alpha", "old-alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][self.commands[0].index("--only") + 1], "alpha")
        self.assertIn("restarted on claude4", self.notices[0])

    def test_dry_run_and_mac_write_nothing_and_launch_nothing(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        _ = self.pass_at(0)
        before = self.fixture.snapshot()
        lines = self.pass_at(2, dry_run=True)
        self.assertIn("would restart alpha from claude4's pane launch", "\n".join(lines))
        self.assertEqual(self.fixture.snapshot(), before)
        with patch.object(role_map, "is_role_map_host", return_value=False):
            self.assertEqual(self.pass_at(2), [])
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    _ = unittest.main()
