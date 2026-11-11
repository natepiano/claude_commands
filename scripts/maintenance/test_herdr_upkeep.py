"""Upkeep restores producers, preserves due state, and stops a hung worker."""
from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from contextlib import redirect_stderr
from typing import Literal, override
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.agents import pane_record, remote, test_stop_resume, worker_lifecycle
from scripts.maintenance import herdr_upkeep as job
from scripts.herdr import herdr, detection_rules, herdr_types
from scripts.lib import operation_deadline
from scripts.lib.test_models_registry import write_registry
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.whoami import account


@dataclass
class ProducerRestoreClock:
    elapsed: float
    interval_time: float = 0

    def monotonic(self) -> float:
        return self.elapsed

    def time(self) -> float:
        return self.interval_time

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


class UpkeepTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
            "HERDR_UPKEEP_CONFIG": str(self.root / "config.toml")}))
        _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=120\ntimeout_seconds=0.2\n")

    def test_runtime_interval_skips_completed_work_without_changing_state(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("from pathlib import Path\nPath(__file__).with_suffix('.ran').touch()\n")
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        completed = self.root / "herdr-upkeep/completed"
        before = completed.read_bytes(), completed.stat().st_mtime_ns
        script.with_suffix(".ran").unlink()
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        self.assertFalse(script.with_suffix(".ran").exists())
        self.assertEqual((completed.read_bytes(), completed.stat().st_mtime_ns), before)
        _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=0.001\ntimeout_seconds=0.2\n")
        time.sleep(0.002)
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)
        self.assertTrue(script.with_suffix(".ran").exists())

    def test_hung_worker_is_killed_and_remains_due_for_retry(self) -> None:
        script = self.root / "worker.py"
        _ = script.write_text("import signal\nsignal.pause()\n")
        started = time.monotonic()
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 124)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse((self.root / "herdr-upkeep/completed").exists())
        _ = script.write_text("pass\n")
        with patch.object(job, "__file__", str(script)):
            self.assertEqual(job.run_job(), 0)

    def test_layout_and_rule_refresh_share_the_current_running_sessions(self) -> None:
        current = herdr.SessionEntry(name="shared", running=True, default=False,
            session_dir="/tmp/shared", socket_path="/tmp/shared.sock")
        with (patch.object(herdr, "live_sessions", return_value=[current]),
              patch.object(herdr, "restore_server"),
              isolated_staff_file(self.root / "staff.json", {}),
              patch.object(herdr, "save_shared_layout") as layout,
              patch.object(detection_rules, "refresh") as rules):
            job.upkeep()
        layout.assert_called_once_with({"shared"})
        rules.assert_called_once_with({"shared"}, herdr.reload_agent_rules)

    def test_producer_restore_retries_failures_and_keeps_live_sessions(self) -> None:
        case = test_stop_resume.StopResumeTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        _ = case.enterContext(patch.object(remote, "local_machine", return_value="natedev"))
        _ = case.enterContext(patch.object(remote, "other_machine", return_value="mac"))
        _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=120\ntimeout_seconds=60\n")
        pane_record.save("agents", {})
        case.client.rows.clear()
        machine = remote.local_machine()
        other = "mac" if machine == "natedev" else "natedev"
        folders = {name: case.root / name for name in
                   ("alpha", "own-codex", "paused", "remote", "invalid", "failed", "exited")}
        for folder in folders.values():
            folder.mkdir()
        rows = {name: fixture_member("producer", name=name,
                    worktree=staff.PresentWorktree(other if name == "remote" else machine, folder))
                for name, folder in folders.items()}
        _ = staff.set_hold(str(rows["paused"].id), staff.Paused("now", "user", "keep stopped"))
        _ = staff.set_hold(str(rows["exited"].id), staff.Exited("now", "user", "keep stopped"))
        _ = write_registry(case.root / "configuration/models.conf", {"producers": {
            name: "codex:gpt-test:high" if name == "own-codex" else
                  "invalid" if name == "invalid" else "claude:opus:max" for name in rows}})
        ready = False
        restores: list[str] = []
        attempts: list[str] = []
        start_deadlines: list[float] = []
        launches: dict[str, tuple[Literal["claude", "codex"], str, dict[str, str]]] = {}
        fail = True
        slow = False
        clock = ProducerRestoreClock(time.monotonic())
        outside_clock_reads = 0

        def restore(name: str, timeout_ms: int, *, session_kind: herdr.SecuritySession
                    ) -> herdr.ServerCreated | herdr.ServerAdopted:
            nonlocal ready
            del session_kind
            for read_clock in (time.monotonic,) * outside_clock_reads:
                _ = read_clock()
            self.assertGreater(timeout_ms, 0)
            restores.append(name)
            result = herdr.ServerAdopted() if ready else herdr.ServerCreated()
            ready = True
            return result

        def prepare(identity: str, cwd: str, environment: dict[str, str],
                    removals: frozenset[str]) -> herdr.PreparedAgentEnvironment:
            del identity, removals
            self.assertTrue(ready)
            return herdr.PreparedAgentEnvironment(Path(cwd), environment)

        def launch(session: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                   prepared: herdr.PreparedAgentEnvironment, kind: Literal["claude", "codex"], arguments: list[str],
                   opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int, *, agent: str,
                   reported_unknown: str = "", codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT
                   ) -> herdr.AgentLaunched:
            del opening, reported_unknown, codex
            self.assertTrue(ready)
            self.assertIsInstance(destination, herdr.NewTab)
            self.assertGreater(timeout_ms, 0)
            self.assertLessEqual(timeout_ms, 20_000)
            row = rows[agent]
            self.assertIn(row.current_session, arguments)
            attempts.append(agent)
            self.assertLessEqual(operation_deadline.remaining(), 20)
            start_deadlines.append(clock.monotonic() + operation_deadline.remaining())
            if slow:
                clock.advance(operation_deadline.remaining())
                operation_deadline.check()
            if agent == "failed" and fail:
                raise herdr_types.HerdrFailure("fixture start failed")
            pane = f"w1:p{len(launches) + 1}"
            launches[row.current_session] = kind, pane, prepared.env
            case.client.rows.append({"pane_id": pane, "terminal_id": pane + "-terminal",
                "tab_id": pane + "-tab", "label": agent, "workspace_id": "w1",
                "agent_session": {"kind": "id", "value": row.current_session}})
            entry = pane_record.launch(pane, str(row.id), str(prepared.directory), prepared.env,
                                       [kind, *arguments], kind=kind)
            entry["producer_binding"] = True
            saved = pane_record.read(session)
            saved[pane] = entry
            pane_record.save(session, saved)
            return herdr.AgentLaunched(pane, herdr.PromptSubmissionNotRequired())

        def conversation(identity: str, kind: str = "") -> herdr.LiveConversation | herdr.StoppedConversation:
            del kind
            return (herdr.LiveConversation(launches[identity][0], (456,)) if identity in launches
                    else herdr.StoppedConversation())

        def select(role: str) -> accounts.NamedAccount | accounts.CodexWorkersAccount:
            return (accounts.CodexWorkersAccount("codex1", case.root / ".codex1") if role == "workers"
                    else accounts.NamedAccount("claude3", case.root / ".claude3"))

        def live(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            return sessions.LiveSessionProcess(456) if identity in launches else sessions.SessionNotLive()

        with (patch.object(job, "time", clock),
              patch.object(operation_deadline, "time", clock),
              patch.object(worker_lifecycle, "time", clock),
              patch.object(staff, "time", clock),
              patch.object(herdr, "restore_server", side_effect=restore),
              patch.object(herdr, "prepare_session_environment", side_effect=prepare),
              patch.object(herdr, "launch_prepared_agent", side_effect=launch),
              patch.object(herdr, "read_conversation", side_effect=conversation),
              patch.object(accounts, "role_account", side_effect=select),
              patch.object(account, "process_codex_home", return_value=account.ProcessCodexHome(case.root / ".codex1")),
              patch.object(accounts, "codex_home_label", return_value="codex1"),
              patch.object(case, "live", side_effect=live),
              patch.object(case.client, "save_layout"), patch.object(detection_rules, "refresh"),
              redirect_stderr(io.StringIO()) as errors):
            job.upkeep()
            self.assertEqual(set(launches), {rows[name].current_session for name in ("alpha", "own-codex")})
            self.assertEqual(attempts.count("failed"), 1)
            self.assertEqual(errors.getvalue().count("fixture start failed"), 1)
            self.assertEqual(errors.getvalue().count("invalid:"), 1)
            self.assertEqual(launches[rows["alpha"].current_session][2]["CLAUDE_CONFIG_DIR"], str(case.root / ".claude3"))
            self.assertEqual(launches[rows["own-codex"].current_session][2]["CODEX_HOME"], str(case.root / ".codex1"))
            before = dict(launches)
            _ = errors.truncate(0)
            _ = errors.seek(0)
            fail = False
            job.upkeep()
            self.assertEqual(attempts.count("failed"), 2)
            self.assertEqual(attempts.count("alpha"), 1)
            self.assertEqual(attempts.count("own-codex"), 1)
            self.assertEqual(set(launches), {*before, rows["failed"].current_session})
            self.assertEqual(errors.getvalue().count("invalid:"), 1)
            _ = errors.truncate(0)
            _ = errors.seek(0)
            with patch.object(account, "process_codex_home", return_value=account.ProcessEnvironmentUnread("fixture account is unreadable")):
                job.upkeep()
            self.assertEqual(errors.getvalue().count("fixture account is unreadable"), 1)
            self.assertEqual(attempts.count("own-codex"), 1)
            self.assertEqual(restores, ["agents"] * 3)

            with self.subTest(case="held producer row"):
                with accounts.settings_report_scope():
                    busy = accounts.producer_upkeep_members()[0]
                launches.clear()
                attempts.clear()
                case.client.rows.clear()
                pane_record.save("agents", {})
                _ = errors.truncate(0)
                _ = errors.seek(0)
                with (staff.row_operation(str(busy.id)),
                      patch.dict(os.environ, {f"STAFF_ROW_FD_{busy.id}": ""})):
                    job.upkeep()
                self.assertEqual(set(launches),
                    {rows[name].current_session for name in ("alpha", "failed", "own-codex")} - {busy.current_session})
                self.assertEqual(errors.getvalue().count(f"{busy.name}: another operation holds this row"), 1)
                _ = errors.truncate(0)
                _ = errors.seek(0)
                job.upkeep()
                self.assertEqual(set(attempts), {"alpha", "failed", "own-codex"})
                self.assertEqual(attempts.count(busy.name), 1)
                self.assertNotIn("another operation holds this row", errors.getvalue())

            slow = True
            for budget, expected, outside_clock_reads in ((60, 3, 0), (15, 1, 0), (20, 1, 0), (60, 3, 1000)):
                with self.subTest(budget=budget, outside_clock_reads=outside_clock_reads):
                    clock.interval_time = 0
                    _ = (self.root / "config.toml").write_text(
                        f"[job]\ninterval_seconds=120\ntimeout_seconds={budget}\n")
                    launches.clear()
                    attempts.clear()
                    case.client.rows.clear()
                    pane_record.save("agents", {})
                    _ = errors.truncate(0)
                    _ = errors.seek(0)
                    job.upkeep()
                    self.assertEqual(len(attempts), expected)
                    self.assertEqual(len(set(attempts)), expected)
                    self.assertLessEqual(set(attempts), {"alpha", "failed", "own-codex"})
                    self.assertEqual(launches, {})
                    self.assertEqual(errors.getvalue().count("deadline passed"), expected)
                    self.assertEqual("producer budget ended" in errors.getvalue(), budget < 60)
                    with self.subTest(case="rotation across intervals"):
                        served = set(attempts)
                        for at in (120, 240):
                            attempts.clear()
                            clock.interval_time = at
                            job.upkeep()
                            served.update(attempts)
                        self.assertEqual(served, {"alpha", "failed", "own-codex"})

            with self.subTest(case="parent deadline after worker startup"):
                _ = (self.root / "config.toml").write_text("[job]\ninterval_seconds=120\ntimeout_seconds=60\n")
                worker = case.root / "deadline_worker.py"
                _ = worker.write_text("import os\nfrom pathlib import Path\n"
                    + "_ = Path(__file__).with_suffix('.deadline').write_text(os.environ.get('HERDR_UPKEEP_DEADLINE', ''))\n")
                parent_started = clock.monotonic()
                with patch.object(job, "__file__", str(worker)):
                    self.assertEqual(job.run_job(), 0)
                inherited_deadline = worker.with_suffix(".deadline").read_text()
                parent_deadline = float(inherited_deadline)
                self.assertLessEqual(parent_deadline, parent_started + 60)
                clock.advance(parent_deadline - 5 - clock.monotonic())
                slow = False
                launches.clear()
                attempts.clear()
                start_deadlines.clear()
                case.client.rows.clear()
                pane_record.save("agents", {})
                with patch.dict(os.environ, {"HERDR_UPKEEP_DEADLINE": inherited_deadline}):
                    job.upkeep()
                self.assertEqual(set(attempts), {"alpha", "failed", "own-codex"})
                self.assertLessEqual(max(start_deadlines), parent_deadline)
