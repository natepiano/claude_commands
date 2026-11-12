"""Recover production units with scratch accounts and fake launch and send leaves."""
from __future__ import annotations

import json
import io
import os
import runpy
import signal
import shutil
import subprocess
from subprocess import run as run_git
import tempfile
import time
import unittest
from datetime import timedelta
from dataclasses import replace
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Generator
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys


GIT: str = shutil.which("git") or "git"

from scripts.accounts import accounts, next_moves, outage_alerts, refusal_watch, role_map, unit_watchdog
from scripts.accounts import test_refusal_watch as fixtures
from scripts.production import fake_unit_host, showrunners, staff
from scripts.message import sessions as reader_sessions
from scripts.agents.remote import local_machine
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship, recorded_unit
from scripts.agents import stop_resume
from scripts.agents.launch_names import LaunchNames, LaunchNamesNotRead
from scripts.agents import pane_record as hook_pane_record

from scripts.agents import inventory, pane_record
from scripts.herdr import herdr, pane_launch
from scripts.message import sessions
from scripts.production import unit_lookup
from scripts.whoami import quota_alert
from scripts.lib.atomic_file import write_text_atomic
from scripts.whoami import agent_accounts
NOW = fixtures.NOW


class RecoveryChildImportTests(unittest.TestCase):
    def test_recovery_child_imports_with_only_home_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            child = subprocess.run(
                [str(Path(unit_watchdog.__file__).resolve().parents[1] / "lib/py"), "-m", "scripts.accounts.unit_watchdog", "--recover"],
                cwd="/",
                env={"HOME": home, "PATH": os.environ["PATH"]},
                capture_output=True, text=True, timeout=10, check=False,
            )
        self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
        self.assertEqual(child.stdout, "")
        self.assertNotIn("ModuleNotFoundError", child.stderr)
        self.assertNotIn("Traceback", child.stderr)
        self.assertEqual(child.stderr, "usage: unit_watchdog --recover <account> <session> <result> <source>\n")


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
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        _ = self.enterContext(redirect_stderr(io.StringIO()))
        self.fixture = fixtures.RefusalWatchTests()
        self.fixture.setUp()
        write_text_atomic(Path(os.environ["CONF_DIR"]) / "shutdown.conf",
            (Path(__file__).resolve().parents[2] / "config/shutdown.conf").read_text())
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
        _ = self.enterContext(patch.object(staff, "local_snapshot", side_effect=self.current_staff))
        _ = self.enterContext(patch.object(staff, "initial_snapshot", side_effect=self.current_staff))
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", self.home / "panes"))
        _ = self.enterContext(patch.object(hook_pane_record, "RECORD_DIR", self.home / "panes"))
        _ = self.enterContext(patch.object(stop_resume, "resume", side_effect=self.resume))
        _ = self.enterContext(patch.object(sessions, "session_liveness", side_effect=self.session_liveness))
        _ = self.enterContext(patch.object(unit_lookup, "runs_dir", return_value=self.runs))
        _ = self.enterContext(patch.object(unit_lookup, "every_labeled_session", side_effect=self.marked))
        _ = self.enterContext(patch.object(showrunners, "registered_showrunners", return_value=showrunners.ShowrunnerDiscovery([
            showrunners.Showrunner(session="fixture-showrunner", socket="/fixture.sock", name="fixture", zone="UTC", doc=str(self.doc))])))
        _ = self.enterContext(patch.object(showrunners, "tell", side_effect=self.tell))
        _ = self.enterContext(patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.command))
        _ = self.enterContext(patch.object(herdr, "herdr_binary", return_value="/nonexistent/herdr"))
        _ = self.enterContext(patch.object(herdr.Herdr, "panes", return_value=[]))
        _ = self.enterContext(patch.object(herdr.Herdr, "agents", return_value=[]))
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
        for name in self.units:
            tree = self.home / name
            member = next((row for row in self.fixture.membership().find(kind="unit") if row.name == name), None)
            if member is None:
                member = recorded_unit("fixture", name, tree, self.doc, plan="docs/plan.md")
            if name in self.retired:
                _ = staff.set_worktree(member.id, staff.RemovedWorktree(local_machine(), tree), expected=member.worktree)
            tree.mkdir(exist_ok=True)
            _ = run_git([GIT, "init", "-b", "fixture", str(tree)], check=True, capture_output=True)
            events: list[dict[str, object]] = [{"event_type": "run_started", "main_agent": {"session_id": member.current_session},
                                              "working_dir": str(tree), "run_started_at": 1}]
            if name in self.finished:
                events.append({"event_type": "run_finished"})
            _ = (self.runs / (name + ".jsonl")).write_text("\n".join(json.dumps(event) for event in events))
        _ = self.doc.write_text("# Fixture\n")

    def add_unit(self, name: str, *, label: str = "claude4") -> inventory.Session:
        owner = self.fixture.owner(name, host="unit", label=label)
        Path(cast(str, owner["cwd"])).mkdir(parents=True, exist_ok=True)
        self.units[name] = owner
        self.write_doc()
        entry = pane_record.launch(name, name, cast(str, owner["cwd"]),
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir(label)), ('' + 'SHOWRUNNER_' + 'UNIT'): "fixture",
             ('' + 'SHOWRUNNER_' + 'UNIT_ID'): name}, ["claude", "-n", owner["name"]])
        write_text_atomic(accounts.account_dir(label) / "projects/fixture" / f"{owner['session_id']}.jsonl", "")
        pane_record.write("shared", entry)
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        return owner

    def marked(self) -> unit_lookup.LabeledProductions:
        known = {entry["sessionId"] for entry in self.fixture.live}
        found: dict[str, unit_lookup.LabeledSession] = {}
        for name, owner in self.units.items():
            claude = (unit_lookup.LiveClaude(owner["name"], owner["session_id"], "/fixture.sock", owner["pid"])
                      if owner["session_id"] in known else unit_lookup.ClaudeNotRunning())
            found[name] = unit_lookup.LabeledSession(name, claude,
                unit_lookup.Place("shared", name), identity=unit_lookup.LiveSession(owner["session_id"]))
        return unit_lookup.group_labeled_sessions((unit for unit in found.values()),
                                                  record=self.current_staff())

    def die(self, *names: str) -> None:
        self.fixture.live[:] = [entry for entry in self.fixture.live if entry["sessionId"] not in names]

    def revive(self, identity: str, label: str) -> None:
        owner = self.units[identity]
        owner["config_dir"] = str(accounts.account_dir(label))
        self.die(identity)
        self.fixture.live.append(sessions.SessionRecord(pid=owner["pid"], sessionId=identity, name=owner["name"],
            procStart=owner["proc_start"], messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]))

    def tell(self, _name: str, _key: str, text: str) -> str:
        self.notices.append(text)
        return "told fixture"

    def resume(self, identity: str, *, why: str, to: str) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed:
        self.assertEqual(why, "unit watchdog recovery")
        self.commands.append(["resume", identity, "--to", to, "--only", identity])
        if not Path(cast(str, self.units[identity]["cwd"])).is_dir():
            return stop_resume.ResumeFailed(identity, "folder no longer exists")
        if to in self.refused:
            return stop_resume.ResumeFailed(identity, "fixture refused")
        self.revive(identity, to)
        return stop_resume.ClaudeUp(identity, identity, to)

    def command(self, argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git":
            return run_git([GIT, *argv[1:]], capture_output=True, text=True, check=False)
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
                with patch.object(staff, "snapshot", side_effect=self.current_staff):
                    unit_watchdog._finish_recovery(argv[offset + 1], argv[offset + 2], Path(argv[offset + 3]), argv[offset + 4])  # pyright: ignore[reportPrivateUsage]

    def pass_at(self, minute: int, *, dry_run: bool = False) -> list[str]:
        if not dry_run:
            self.complete_jobs()
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            return unit_watchdog.run(NOW + timedelta(minutes=minute), dry_run=dry_run)

    def recover_queued(self, source: unit_watchdog.RecoverySource) -> tuple[str, list[list[str]]]:
        self.commands.clear()
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            failure = unit_watchdog._restart(source.label, source.session["session_id"], source.identity)  # pyright: ignore[reportPrivateUsage]
        return failure, self.commands

    def test_partial_discovery_keeps_unobserved_recovery_and_checks_healthy_units(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        _ = self.pass_at(0)
        saved = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        saved["missing"] = unit_watchdog.DownOutage(
            first_seen=NOW.isoformat(), alert_context=unit_watchdog.RecoveryOwner(production="missing", unit="beta"),
            job=unit_watchdog.RecoveryJob(result=str(self.home / "pending-result"), started=NOW.isoformat(), source="fixture"))
        unit_watchdog._write_state(saved)  # pyright: ignore[reportPrivateUsage]
        runner = showrunners.Showrunner(session="fixture-showrunner", socket="/fixture.sock", name="fixture",
                                       zone="UTC", doc=str(self.doc))
        partial = showrunners.ShowrunnerDiscovery([runner], (
            showrunners.ShowrunnerLookupFailure("missing", "fixture lookup unavailable"),))
        with patch.object(showrunners, "registered_showrunners", return_value=partial):
            with self.assertRaisesRegex(OSError, "fixture lookup unavailable"):
                _ = self.pass_at(2)
        current = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(current["missing"], saved["missing"])
        self.assertIn("job", current["alpha"], "the healthy production must still start recovery")
        self.complete_jobs()
        self.assertEqual(len(self.commands), 1)

    def test_interrupted_cleanup_resumes_without_sending_notices_to_live_sessions(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        self.die("alpha", "beta", "gamma")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        write = unit_watchdog._write_state  # pyright: ignore[reportPrivateUsage]
        def interrupted(state: dict[str, unit_watchdog.DownOutage]) -> None:
            write(state)
            raise SystemExit("notes pass killed")
        with patch.object(unit_watchdog, "_write_state", side_effect=interrupted):
            with self.assertRaisesRegex(SystemExit, "notes pass killed"):
                _ = self.pass_at(4)
        self.assertEqual(set(unit_watchdog._read_state()), {"beta", "gamma"})  # pyright: ignore[reportPrivateUsage]
        _ = self.pass_at(6)
        self.assertEqual(self.notices, [])
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(self.commands), 3)

    def test_live_outages_are_cleared_in_one_pass_without_a_notice_queue(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        self.die("alpha", "beta", "gamma")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual(self.notices, [])
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(self.commands), 3)

    def test_idle_rerun_sends_nothing_and_preserves_completed_state(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        path = self.home / ".local/state/accounts/unit_watchdog.json"
        saved, written = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(showrunners, "tell") as told:
            self.assertEqual(self.pass_at(6), [])
            self.assertEqual(self.pass_at(8), [])
        told.assert_not_called()
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(path.stat().st_mtime_ns, written)
        self.assertEqual(self.notices, [])
        self.assertEqual(len(self.commands), 1)

    @contextmanager
    def stop_process_leaves(self) -> Generator[None]:
        proc = self.home / "stop-proc"
        for owner in self.units.values():
            fake_unit_host.process({"HERDR_PROC": str(proc)}, owner["pid"], 1, ["claude"], {}, cwd=self.home)

        def reader_liveness(identity: str) -> reader_sessions.LiveSessionProcess | reader_sessions.SessionNotLive:
            observed = self.session_liveness(identity)
            return (reader_sessions.LiveSessionProcess(observed.pid) if isinstance(observed, sessions.LiveSessionProcess)
                    else reader_sessions.SessionNotLive())

        unread_names = LaunchNamesNotRead()

        def tab(identity: str, *, names: LaunchNames | LaunchNamesNotRead = unread_names) -> herdr.TabSession:
            _ = names
            return herdr.TabSession(identity, self.units[identity]["name"], "fixture", identity)

        def signal_claude(pid: int, requested: int) -> None:
            self.assertEqual(requested, signal.SIGTERM)
            self.die(*(name for name, owner in self.units.items() if owner["pid"] == pid))

        def process(pid: int, started: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            self.assertEqual(started, "fixture-start")
            return (sessions.LiveSessionProcess(pid) if any(entry["pid"] == pid for entry in self.fixture.live)
                    else sessions.SessionNotLive())

        def native_process(pid: int, kind: str) -> herdr.NativeAgentProcess:
            self.assertEqual(kind, "claude")
            owner = next(entry for entry in self.fixture.live if entry["pid"] == pid)
            return herdr.NativeAgentProcess(herdr.EnvironmentThread(owner["sessionId"]), "fixture-start", {})

        def panes() -> list[herdr.PaneIdentity]:
            return [{"pane_id": identity, "terminal_id": "terminal-" + identity, "workspace_id": "fixture",
                     "tab_id": "tab-" + identity, "label": owner["name"], "session_id": identity}
                    for identity, owner in self.units.items()]

        def process_info(*arguments: str) -> dict[str, object]:
            identity = arguments[-1]
            owner = self.units[identity]
            live = isinstance(self.session_liveness(identity), sessions.LiveSessionProcess)
            pid = owner["pid"] if live else 998042
            return {"process_info": {"shell_pid": 998042, "foreground_process_group_id": pid,
                "foreground_processes": [{"pid": pid, "name": "claude" if live else "zsh",
                                          "argv": ["claude"] if live else ["zsh"]}]}}

        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(herdr, "codex_conversations", return_value=herdr.CodexConversations({}, ())),
              patch.object(reader_sessions, "session_liveness", side_effect=reader_liveness),
              patch.object(herdr, "shared_session", return_value="shared"),
              patch.object(herdr, "native_process", side_effect=native_process),
              patch.object(herdr.Herdr, "panes", side_effect=panes),
              patch.object(herdr.Herdr, "result", side_effect=process_info),
              patch.object(pane_record, "seed", return_value=0),
              patch.object(stop_resume, "_tab_of", side_effect=tab),
              patch.object(sessions, "process_start", return_value="fixture-start"),
              patch.object(sessions, "process_liveness", side_effect=process),
              # This fixture supplies signals; shutdown tests check the workspace guard.
              patch.object(stop_resume, "_signal_process", side_effect=signal_claude),
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
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(pane_record.read("shared")["alpha"].get("ended", False))
        self.assertFalse(pane_record.read("shared")["beta"].get("ended", False))

    def test_failed_move_still_allows_watchdog_recovery_on_the_launch_account(self) -> None:
        _ = self.add_unit("alpha")
        with (self.stop_process_leaves(), patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(stop_resume, "resume", return_value=stop_resume.ResumeFailed("alpha", "never started"))):
            self.assertEqual(self.shared_move(["alpha"], "claude2", why="account move"),
                             [stop_resume.ResumePending("alpha", "claude2", "never started")])
        self.assertFalse(pane_record.read("shared")["alpha"].get("ended", False))
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"alpha"})

    def test_explicit_resume_after_public_stop_allows_recovery_of_a_later_crash(self) -> None:
        owner = self.add_unit("alpha")
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        member = next(row for row in self.fixture.membership().find(kind="unit") if row.name == "alpha")
        record = staff.StaffSnapshot(tuple(
            replace(row, current_session=staff.StaffId(owner["session_id"])) if row.id == member.id else row
            for row in staff.snapshot().rows))

        def start(tab: herdr.TabSession, entry: pane_launch.CurrentLaunch, label: str, message: str) -> sessions.LiveSessionProcess:
            self.assertIn("maintenance", message)
            self.assertEqual((tab.pane_id, entry["session_id"], label), ("alpha", "alpha", "claude2"))
            environment = {**entry["environment"], "HERDR_ENV": "1", "HERDR_SESSION": "shared",
                           "HERDR_PANE_ID": tab.pane_id, "CLAUDE_CONFIG_DIR": entry["environment"]["CLAUDE_CONFIG_DIR"]}
            payload = json.dumps({"session_id": "alpha", "cwd": owner["cwd"]})
            with (patch.dict(os.environ, environment),
                  patch.object(os, "getppid", return_value=owner["pid"]),
                  patch.object(staff, "runtime_id", return_value=owner["session_id"]),
                  patch.object(sys, "stdin", io.StringIO(payload)),
                  patch.object(hook_pane_record, "claude_arguments", return_value=stop_resume.resume_arguments(entry))):
                with self.assertRaises(SystemExit) as exited:
                    _ = runpy.run_path(str(stop_resume.SCRIPTS / "hooks/session-start-pane-record.py"), run_name="__main__")
                self.assertEqual(exited.exception.code, 0)
            self.revive("alpha", label)
            return sessions.LiveSessionProcess(owner["pid"])

        config = accounts.account_dir("claude2")
        with self.stop_process_leaves():
            self.assertEqual(stop_resume.stop("alpha"), stop_resume.Stopped("alpha", owner["pid"]))
            self.assertTrue(pane_record.read("shared")["alpha"].get("ended", False))
            with (patch.object(stop_resume, "_start_session", side_effect=start),
                  patch.object(herdr.Herdr, "retitle", return_value=False) as retitle,
                  patch.object(staff, "initial_snapshot", return_value=record),
                  patch.object(stop_resume, "_detach"),
                  patch.object(herdr, "heal_claude_trust", return_value=herdr.Trusted(
                      Path(cast(str, owner["cwd"])), config / ".claude.json", Path(cast(str, owner["cwd"]))))):
                result = self.shared_resume("alpha", why="maintenance complete")
                row = record.by_current_session(owner["session_id"])
                assert row is not None
                retitle.assert_called_once_with("alpha", row.name)
        self.assertIsInstance(result, stop_resume.ClaudeUp)
        entry = pane_record.read("shared")["alpha"]
        self.assertFalse(entry.get("ended", False))
        self.assertEqual((entry.get("staff_id"), str(cast(staff.SessionHome, pane_launch.config_home(entry)).path)), (member.id, str(config)))
        self.assertFalse(any(name.startswith("SHOWRUNNER_") for name in entry["environment"]))
        self.die("alpha")
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude2", "--only", "alpha"]])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"alpha"})

    def test_crashed_unit_without_any_record_resumes_in_its_tab_on_the_launch_account(self) -> None:
        _ = self.add_unit("alpha")
        for path in (self.home / "shutdown").rglob("*.json"):
            path.unlink()
        self.die("alpha")
        with redirect_stderr(io.StringIO()):
            _ = self.pass_at(0)
            self.assertEqual(self.commands, [])
            _ = self.pass_at(2)
            _ = self.pass_at(4)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertEqual(self.notices, [])
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, {"alpha"})

    def test_crashed_unit_without_a_pane_launch_record_stays_down(self) -> None:
        _ = self.add_unit("alpha")
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            self.assertEqual(pane_record.forget_unit("alpha"), ["shared:alpha"])
        self.die("alpha")
        with patch.object(pane_record, "launch_for_session", wraps=pane_record.launch_for_session) as launches:
            lines = self.pass_at(0)
        launches.assert_called_once_with("alpha")
        self.assertEqual(self.pass_at(2, dry_run=True), [])
        self.assertEqual(self.pending, [])
        for minute in (2, 4):
            lines.extend(self.pass_at(minute))
        self.assertEqual(lines, [])
        self.assertEqual(self.commands, [])
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, set())
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]

    def test_missing_folder_without_a_pane_launch_record_stays_down(self) -> None:
        owner = self.add_unit("alpha")
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            self.assertEqual(pane_record.forget_unit("alpha"), ["shared:alpha"])
        self.die("alpha")
        folder = owner["cwd"]
        assert isinstance(folder, str)
        Path(folder).rmdir()
        lines: list[str] = []
        for minute in (0, 2, 4):
            lines.extend(self.pass_at(minute))
        self.assertEqual(lines, [])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.fixture.live, [])

    def test_crashed_unit_with_a_live_launch_record_restarts(self) -> None:
        _ = self.add_unit("alpha")
        self.assertFalse(pane_record.read("shared")["alpha"].get("ended", False))
        self.die("alpha")
        for minute in (0, 2, 4):
            _ = self.pass_at(minute)
        self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
        self.assertEqual({item["sessionId"] for item in self.fixture.live}, {"alpha"})
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]

    def test_newest_ended_launch_keeps_a_unit_down_and_removes_its_outage(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        self.assertEqual(self.pass_at(0), [])
        entry = pane_record.read("shared")["alpha"]
        entry["written_at"] = "2026-10-30T01:00:00+00:00"
        pane_record.write("shared", entry)
        ended = entry.copy()
        ended.update(pane_id="new-alpha", ended=True, written_at="2026-10-31T01:00:00+00:00")
        pane_record.write("shared", ended)
        for minute in (2, 4):
            self.assertEqual(self.pass_at(minute), [])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.pending, [])
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]

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
        self.assertEqual(self.notices, [])

    def test_recordless_crash_alerts_once_and_a_new_live_generation_allows_the_next_alert(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.add("claude4")
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4, 6, 8):
                _ = self.pass_at(minute)
            phone.assert_not_called()
            self.assertNotIn("alerted", unit_watchdog._read_state()["alpha"])  # pyright: ignore[reportPrivateUsage]
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.die("alpha")
            for minute in (12, 14, 16, 18):
                _ = self.pass_at(minute)
            self.assertEqual(phone.call_count, 0)

    def queued_unit(self) -> unit_watchdog.RecoverySource:
        _ = self.add_unit("alpha")
        self.die("alpha")
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            source = unit_watchdog._sources()[("fixture", "alpha")]  # pyright: ignore[reportPrivateUsage]
        assert isinstance(source, unit_watchdog.RecoverySource)
        return source

    def test_newest_moved_launch_decides_recovery_account_after_one_tick(self) -> None:
        _ = self.add_unit("alpha")
        old = pane_record.resume_launch(pane_record.read("shared")["alpha"])
        old["written_at"] = "2026-10-30T01:00:00+00:00"
        pane_record.write("shared", old)
        moved = pane_record.launch("new-alpha", "alpha", old["cwd"],
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2")), ('' + 'SHOWRUNNER_' + 'UNIT'): "fixture",
             ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha"}, ["claude", "-n", "alpha"])
        pane_record.write("shared", moved)
        self.die("alpha")
        self.assertEqual(pane_record.launch_for_session("alpha"), pane_record.read("shared")["new-alpha"])
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
                    self.assertEqual(self.pending[0][self.pending[0].index("--recover") + 2], str(next(row for row in self.fixture.membership().find(kind="unit") if row.name == "alpha").id))
                    _ = self.pass_at(4)
                self.assertEqual(self.commands, [["resume", "alpha", "--to", "claude4", "--only", "alpha"]])
                self.commands.clear()
                unit_watchdog._state_file().unlink(missing_ok=True)  # pyright: ignore[reportPrivateUsage]

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
        entry = pane_record.resume_launch(pane_record.read("shared")["alpha"])
        entry = pane_record.launch(entry["pane_id"], entry["session_id"], entry["cwd"],
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2"))}, ["claude", "-n", entry["name"]])
        pane_record.write("shared", entry)
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            source = unit_watchdog._sources()[("fixture", "alpha")]  # pyright: ignore[reportPrivateUsage]
        assert isinstance(source, unit_watchdog.RecoverySource)
        failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--to") + 1], "claude2")


    def test_queued_recovery_delegates_missing_pane_creation_to_shared_resume(self) -> None:
        source = self.queued_unit()
        with patch.object(stop_resume, "resume", wraps=self.resume) as resume:
            failure, launches = self.recover_queued(source)
        self.assertEqual(failure, "")
        self.assertEqual(len(launches), 1)
        resume.assert_called_once_with("alpha", why="unit watchdog recovery", to="claude4")

    def test_live_units_share_one_liveness_read_and_receive_nothing_on_either_pass(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        with (patch.object(unit_lookup, "every_labeled_session", wraps=self.marked) as reads,
              patch.object(unit_watchdog, "_fresh_units", side_effect=AssertionError("live units need no recovery scan")),
              patch.object(showrunners, "tell") as tell,
              patch.object(quota_alert, "tell_user") as phone):
            self.assertEqual(self.pass_at(0), [])
            self.assertEqual(reads.call_count, 1)
            self.assertEqual(self.pass_at(2), [])
            self.assertEqual(reads.call_count, 2)
        tell.assert_not_called()
        phone.assert_not_called()
        self.assertEqual(self.pending, [])

    def test_recovery_rechecks_a_reported_outage_and_sees_the_new_unit_pane(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {('' + 'SHOWRUNNER_' + 'UNIT'): "fixture", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.LabeledProductions:
            if herdr.herdr_processes():
                return self.marked()
            return unit_lookup.LabeledProductions({"fixture": {"unit": unit_lookup.LabeledSession(
                "unit", unit_lookup.ClaudeNotRunning(), unit_lookup.Place("shared", "unit"),
                identity=unit_lookup.LiveSession(owner["session_id"]))}}, self.marked().members)
        with (patch.object(herdr, "_scan_herdr_processes", side_effect=[[], [fresh]]) as scans,
              patch.object(unit_lookup, "every_labeled_session", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(self.pass_at(0), [])
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pending, [])

    def test_recovery_completion_reads_new_unit_pane_after_report_snapshot(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "new-pane"),
            {('' + 'SHOWRUNNER_' + 'UNIT'): "fixture", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "unit"}, "claude", str(self.home))
        processes: list[herdr.HerdrProcess] = []

        def observed() -> unit_lookup.LabeledProductions:
            return self.marked() if herdr.herdr_processes() else unit_lookup.LabeledProductions({})

        with (patch.object(herdr, "_scan_herdr_processes", side_effect=lambda: list(processes)),
              patch.object(unit_lookup, "every_labeled_session", side_effect=observed),
              herdr.process_snapshot()):
            self.assertEqual(herdr.herdr_processes(), [])
            processes.append(fresh)
            self.assertEqual(unit_watchdog._live_account("fixture", "unit"), "claude4")  # pyright: ignore[reportPrivateUsage]

    def test_live_unit_report_reuses_the_scan_without_starting_recovery(self) -> None:
        owner = self.add_unit("unit")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"), {}, "claude", str(self.home))
        def observed() -> unit_lookup.LabeledProductions:
            self.assertEqual(herdr.herdr_processes(), [fresh])
            return self.marked()
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_labeled_session", side_effect=observed),
              herdr.process_snapshot()):
            _ = herdr.herdr_processes()
            self.assertEqual(self.pass_at(0), [])
        self.assertEqual(scans.call_count, 1)
        self.assertEqual(self.pending, [])

    def test_idle_real_watcher_and_watchdog_share_the_report_scan(self) -> None:
        owner = self.add_unit("unit", label="claude2")
        fresh = herdr.HerdrProcess(owner["pid"], herdr.HerdrPlace("agents", "pane"),
            {('' + 'SHOWRUNNER_' + 'UNIT'): "fixture", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "unit"}, "claude", str(self.home))
        def observed() -> unit_lookup.LabeledProductions:
            _ = herdr.herdr_processes()
            return self.marked()
        def listing(login: str | None = None, *,
                    registrations: inventory.ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[inventory.ListedSession]:
            _ = herdr.herdr_processes()
            return self.fixture.listing(login, registrations=registrations)
        with (patch.object(herdr, "_scan_herdr_processes", return_value=[fresh]) as scans,
              patch.object(unit_lookup, "every_labeled_session", side_effect=observed),
              patch.object(inventory, "listing", side_effect=listing),
              patch.object(staff, "snapshot", side_effect=self.current_staff),
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
        with (patch.object(unit_lookup, "every_labeled_session", side_effect=[live, absent]),
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


    def test_finished_recovery_without_a_source_removes_the_outage_and_result(self) -> None:
        state, result = self.finished_recovery()
        unit_watchdog._write_state(state)  # pyright: ignore[reportPrivateUsage]
        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            _ = pane_record.forget_unit("unit")
        self.die("unit")
        with patch.object(unit_watchdog, "_restart") as resumed:
            lines = self.pass_at(3)
        resumed.assert_not_called()
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(result.exists())
        self.assertEqual(lines, [])
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

    def test_auto_move_recovers_every_unit(self) -> None:
        _ = self.fixture.groups(units="claude4")
        for name in ("alpha", "beta", "gamma"):
            _ = self.add_unit(name)
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("10-26-outage"))
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


    def test_move_allows_later_crash_recovery(self) -> None:
        _ = self.fixture.groups(units="claude4")
        _ = self.add_unit("alpha")
        self.fixture.reading("four@example.invalid", 1)
        self.fixture.next_entries(self.fixture.low_rule("held-target"))
        _ = refusal_watch.run(NOW)
        self.assertEqual(len(self.fixture.moves.calls), 1)
        self.assertEqual(accounts.group_accounts().get("units"), "claude2")
        entry = pane_record.resume_launch(pane_record.read("shared")["alpha"])
        entry = pane_record.launch(entry["pane_id"], entry["session_id"], entry["cwd"],
            {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2"))}, ["claude", "-n", entry["name"]])
        pane_record.write("shared", entry)
        self.die("alpha")
        _ = self.pass_at(2)
        self.assertEqual(self.commands, [])
        _ = self.pass_at(4)
        _ = self.pass_at(6)
        self.assertEqual(self.commands[0][3], "claude2")
        self.assertEqual(self.notices, [])

    def test_crash_command_uses_recovery_owner_and_reports_actual_account(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.add("claude2")
        self.die("alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        _ = self.pass_at(4)
        self.assertEqual([argv[3] for argv in self.commands], ["claude4"])
        self.assertEqual(self.notices, [])

    def test_standing_by_unit_recovers_after_grace_while_finished_run_stays_down(self) -> None:
        _ = self.add_unit("standing")
        _ = self.add_unit("finished")
        self.finished.add("finished")
        self.write_doc()
        (self.runs / "standing.jsonl").unlink()
        self.assertIs(unit_lookup.run_state(self.home / "standing"), unit_lookup.UnitState.STANDING_BY)
        self.assertIs(unit_lookup.run_state(self.home / "finished"), unit_lookup.UnitState.RUN_FINISHED)
        self.die("standing", "finished")

        _ = self.pass_at(0)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(set(state), {"standing", "finished"})
        self.assertEqual(state["standing"]["first_seen"], NOW.isoformat())
        self.assertEqual(self.pending, [])
        self.assertEqual(self.commands, [])

        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 2)
        self.assertEqual(self.commands, [])
        _ = self.pass_at(4)
        self.assertCountEqual(self.commands, [["resume", name, "--to", "claude4", "--only", name]
                                              for name in ("standing", "finished")])
        self.assertEqual({entry["sessionId"] for entry in self.fixture.live}, {"standing", "finished"})
        self.assertEqual(unit_watchdog._read_state(), {})  # pyright: ignore[reportPrivateUsage]


    def test_both_crash_accounts_refuse_one_push_per_outage_and_showrunner_each_pass(self) -> None:
        _ = self.add_unit("alpha")
        self.refused.update(("claude2", "claude4"))
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            for minute in (0, 2, 4, 6, 8):
                _ = self.pass_at(minute)
            phone.assert_not_called()
            self.assertEqual(self.notices, [])
            self.assertEqual(len(self.commands), 3)
            self.revive("alpha", "claude4")
            _ = self.pass_at(10)
            self.die("alpha")
            _ = self.pass_at(12)
            _ = self.pass_at(14)
            _ = self.pass_at(16)
            self.assertEqual(phone.call_count, 0)

    def test_failed_launch_read_keeps_the_running_job_without_duplicate_recovery(self) -> None:
        _ = self.add_unit("alpha")
        self.finish_jobs = False
        self.die("alpha")
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
        self.assertIn("job", state["alpha"])
        self.assertEqual(len(self.pending), 1)
        with patch.object(pane_record, "launch_for_session", side_effect=OSError("launch unreadable")):
            lines = self.pass_at(3)
        self.assertEqual(lines, ["watchdog: fixture/alpha session alpha: launch unreadable"])
        with self.subTest(pass_kind="failed read"):
            self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.pass_at(4), [])
        self.assertEqual(self.pass_at(5), [])
        self.assertEqual(unit_watchdog._read_state(), state)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(self.pending), 1)
        self.assertEqual(self.commands, [])

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
        Path(cast(str, owner["cwd"])).rmdir()
        self.die("alpha")
        with patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()) as phone:
            lines: list[str] = []
            for minute in (0, 2, 4):
                lines.extend(self.pass_at(minute))
            Path(cast(str, owner["cwd"])).mkdir()
            lines.extend(self.pass_at(6))
            phone.assert_not_called()
        self.assertEqual(self.pending, [])
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(any("no longer exists" in line for line in lines))
        self.assertTrue(any("restarted on claude4" in line for line in lines))

    def test_folder_lost_after_dispatch_keeps_outage_context_and_retries_its_alert(self) -> None:
        owner = self.add_unit("alpha")
        self.die("alpha")
        self.finish_jobs = False
        _ = self.pass_at(0)
        _ = self.pass_at(2)
        self.assertEqual(len(self.pending), 1)
        Path(cast(str, owner["cwd"])).rmdir()
        command = self.pending[0]
        offset = command.index("--recover")

        with patch.object(staff, "snapshot", side_effect=self.current_staff):
            unit_watchdog._finish_recovery(command[offset + 1], command[offset + 2],  # pyright: ignore[reportPrivateUsage]
                Path(command[offset + 3]), command[offset + 4])
        result = Path(command[offset + 3])
        finished = result.read_text()
        self.assertIn("folder no longer exists", finished)
        result.unlink()
        # The outage owns its alert even before the job result arrives.
        with patch.object(quota_alert, "tell_user", side_effect=[
                quota_alert.PushFailed("fixture delivery failed"), quota_alert.PushSent()]) as phone:
            _ = self.pass_at(3)
            phone.assert_not_called()
            state = unit_watchdog._read_state()  # pyright: ignore[reportPrivateUsage]
            self.assertNotIn("alerted", state["alpha"])
            self.assertIn("job", state["alpha"])
            _ = result.write_text(finished)
            lines = self.pass_at(4)
            self.assertEqual(phone.call_count, 0)
            lines.extend(self.pass_at(6))
            self.assertEqual(phone.call_count, 0)
            lines.extend(self.pass_at(8))
            self.assertEqual(phone.call_count, 0)
        self.assertEqual(sum("retry from claude4" in line for line in lines), 1)
        self.assertFalse(result.exists())
        self.assertIn("job", unit_watchdog._read_state()["alpha"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(len(self.pending), 2)


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
        assert isinstance(first, outage_alerts.AlertClaim)
        with redirect_stdout(io.StringIO()) as output:
            self.assertIsInstance(outage_alerts.claim("live-owner"), outage_alerts.AlertInFlight)
        self.assertEqual(output.getvalue(), f"already claimed by {first.token}\n")

    def test_alert_claim_past_the_lease_is_replaced(self) -> None:
        with patch.object(time, "time", return_value=1000):
            first = outage_alerts.claim("expired-owner")
        with patch.object(time, "time", return_value=2801):
            second = outage_alerts.claim("expired-owner")
        self.assertIsInstance(second, outage_alerts.AlertClaim)
        assert isinstance(first, outage_alerts.AlertClaim) and isinstance(second, outage_alerts.AlertClaim)
        self.assertNotEqual(first.token, second.token)


    def test_watchdog_notice_uses_the_users_zone(self) -> None:
        _ = self.add_unit("alpha")
        self.die("alpha")
        with patch.object(unit_watchdog, "user_zone", return_value=ZoneInfo("Asia/Tokyo")):
            _ = self.pass_at(0)
            _ = self.pass_at(2)
            _ = self.pass_at(4)
        self.assertEqual(self.notices, [])

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
            marked.units["fixture"]["alpha"] = unit_lookup.LabeledSession("alpha",
                unit_lookup.ClaudeUnknown("pane unavailable"), unit_lookup.Place("shared", "alpha"),
                identity=unit_lookup.LiveSession(self.units["alpha"]["session_id"]))
            with patch.object(unit_lookup, "every_labeled_session", return_value=marked):
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
            self.assertEqual(pane_record.read("shared")["alpha"]["model"], "sonnet")
            self.assertEqual(command[offset + 4], str(next(row for row in self.fixture.membership().find(kind="unit") if row.name == "alpha").id))
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
            if argv[0] != "git":
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

    def current_staff(self) -> staff.StaffSnapshot:
        record = self.fixture.membership()
        return replace(record, rows=tuple(
            replace(row, current_session=staff.StaffId(self.units[row.name]["session_id"]))
            if row.kind == "unit" and row.name in self.units else row for row in record.rows))


if __name__ == "__main__":
    _ = unittest.main()
