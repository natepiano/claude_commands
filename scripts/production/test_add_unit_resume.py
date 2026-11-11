"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest.mock import Mock, patch

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import add_unit
from scripts.production import staff, unit_config
from scripts.production import unit_lookup
from scripts.message import sessions
from scripts.agents import stop_resume
from scripts.herdr import herdr
from scripts.herdr.herdr import HerdrPlace
from scripts.agents.test_support import ShutdownTestCase


from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone, SCRIPT
from scripts.production.staff_test_support import fixture_runtime_id, unit_membership


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitResumeTests(AddUnitTestCase):
    def test_resume_preserves_existing_config_settings_and_launches_with_the_same_staff_id(self) -> None:
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        row = unit_membership("build-followups", "alpha")
        fixed_id = row.id
        row = staff.update_current_session(row.current_session, staff.native_id("3001165e-ca76-59c2-9490-878224585fad"))
        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", worktree), expected=row.worktree)
        saved = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/given.md")),
                                      unit_config.AppPort(8123), ("nightly-review", "build-check"))
        _ = unit_config.write(worktree, saved)
        path = worktree / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = self.successful("alpha", "--resume", "3001165e-ca76-59c2-9490-878224585fad", "--cwd", str(worktree))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(unit_config.read(worktree), saved)
        self.assertEqual(staff.by_id(fixed_id), row)
        self.assertEqual(row.id, fixed_id)
        self.assertNotEqual(row.id, row.current_session)
        self.assertNotIn("STAFF_ID", self.pane_environment())
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], row.current_session)
        self.assertEqual(self.git("ls-files", ".unit.toml", cwd=worktree), "")

    def test_unreadable_staff_refuses_resume_before_closing_its_stopped_pane(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.staff.staff_file.parent.mkdir(parents=True, exist_ok=True)
        _ = self.staff.staff_file.write_text("broken record", encoding="utf-8")
        before = (self.doc.read_bytes(), self.git("rev-parse", "HEAD"), self.staff.staff_file.read_bytes())
        stopped = unit_lookup.LabeledSession("alpha", unit_lookup.ClaudeNotRunning(),
                                        HerdrPlace("old-host", "w2:p3"), identity=unit_lookup.LiveSession(fixture_runtime_id("unreadable-stopped-session")))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(stopped)),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(herdr, "close_agent") as closed):
            result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("StaffUnreadable", result.stderr)
        closed.assert_not_called()
        self.assertEqual((self.doc.read_bytes(), self.git("rev-parse", "HEAD"), self.staff.staff_file.read_bytes()), before)
        self.assertFalse(any(row[2:4] == ["agent", "start"] for row in self.herdr_calls()))

    def test_new_row_resume_closes_the_stopped_marked_tab_then_launches(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        old_head = self.git("rev-parse", "build-followups")
        row = unit_membership("build-followups", "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        stopped = unit_lookup.LabeledSession("alpha", unit_lookup.ClaudeNotRunning(),
                                        HerdrPlace("old-host", "w2:p3"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        running = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/fixture/socket", 4242),
            HerdrPlace("agents", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))
        order = Mock()
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(stopped)),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(herdr, "close_agent") as closed,
              patch.object(herdr, "start_agent", wraps=herdr.start_agent) as started,
              patch.object(add_unit, "wait_for_remote_control", return_value=running) as waited):
            order.attach_mock(closed, "close")
            order.attach_mock(started, "start")
            result = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                                     "--cwd", str(prior))

        closed.assert_called_once_with("old-host", "w2:p3")
        started.assert_called_once()
        self.assertEqual(order.mock_calls[0][0], "close")
        self.assertEqual(order.mock_calls[1][0], "start")
        waited.assert_called_once()
        self.assertIn("alpha started:", result.stdout)
        row = unit_membership("build-followups", "alpha")
        self.assertEqual(row.worktree, staff.PresentWorktree("natedev", self.root / "project-alpha"))
        self.assertEqual(self.git("rev-parse", "build-followups"), old_head)
        self.assertTrue((self.root / "project-alpha").is_dir())
        self.assertEqual(self.git("rev-parse", "build-followups-alpha"),
                         self.git("rev-parse", "build-followups"))
        self.assertEqual(self.opened()[7], str(prior))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertIn("Run /unit:direct", self.opening_prompt())

    def test_resume_writes_trust_for_session_cwd_instead_of_worktree(self) -> None:
        prior = self.root / "untrusted-session"
        prior.mkdir()
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(prior), "--account", "claude2")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        with patch.dict(os.environ, self.env):
            self.assertIsInstance(herdr.claude_trust(prior, Path(self.env["HOME"]) / ".claude2/.claude.json"), herdr.Trusted)
        self.assertNotIn("trust anomaly:", result.stderr)
        self.assertIn("alpha started:", result.stdout)
        self.assertEqual(self.opened()[7], str(prior))

    def test_running_resume_refuses_before_any_launch_write(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode):
                result = self.cli("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stderr, f"add_unit: session 62ccbd64-f22e-5647-b20e-7899aa39d766 already runs as pid {os.getpid()}; "
                                 + "to make it this unit, run add_unit with --attach 62ccbd64-f22e-5647-b20e-7899aa39d766\n")
                self.assert_no_launch_change(document, head)
                self.assertEqual([row for row in self.herdr_calls() if row[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))
        # Brief launches must refuse before their stub is written too.
        result = self.cli("alpha", "--brief", "Repair the unit", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])

    def test_resume_with_unreadable_process_identity_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("recorded-start")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        with patch.object(sessions, "process_start", return_value=None):
            result = self.cli("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session 62ccbd64-f22e-5647-b20e-7899aa39d766 runs: "
                         + f"process identity unreadable for pid {os.getpid()}\n")
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])

    def test_resume_with_stale_registry_identity_still_launches(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("a-previous-process-start")
        result = self.successful("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertIn("alpha started:", result.stdout)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertIn(str(prior), self.opened())
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)

    def test_attach_stops_then_launches_once_with_staff_marks_and_finishes(self) -> None:
        prior, trace = self.prepare_attach()
        result = self.successful("alpha", "--unit-type", "merge", "--permanent", "--attach",
                                 "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(trace, ["stop", "launch"])
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertFalse(any(key.startswith("SHOWRUNNER_") for key in self.pane_environment()))
        self.assertIn(str(prior), self.opened())
        self.assertIn("attached 62ccbd64-f22e-5647-b20e-7899aa39d766 as alpha: one process, pid 202", result.stdout)
        row = unit_membership("build-followups", "alpha")
        self.assertEqual(row.unit_type, staff.UnitDetails("merge"))
        self.assertIn("Load /unit:merge before waiting for orders.", self.opening_prompt())
        self.assertEqual(row.worktree, staff.PresentWorktree("natedev", self.root / "project-alpha"))
        self.assertNotIn("STAFF_ID", self.pane_environment())
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], row.current_session)
        self.assertEqual(unit_config.read(self.root / "project-alpha"),
                         unit_config.UnitConfig(unit_config.PermanentPlan(), unit_config.NoAppPort(), ()))

    def test_attach_requires_cwd_supported_mode_and_exclusive_session_flag(self) -> None:
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        cases = ((["--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766"], "--attach and --cwd"),
                 (["--brief", "repair", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.root)], "--attach requires"),
                 (["--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--resume", "other", "--cwd", str(self.root)], "not allowed"))
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = self.cli("alpha", *arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(message, result.stderr)
                self.assert_no_launch_change(document, head)

    def test_attach_check_validates_without_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        _ = self.successful("alpha", "--standby", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--check")
        self.assertEqual(trace, [])

    def test_attach_refuses_absent_and_unknown_sessions_before_writes(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        for liveness, reason in ((sessions.SessionNotLive(), "session 62ccbd64-f22e-5647-b20e-7899aa39d766 does not run; use --resume"),
                                 (sessions.SessionLivenessUnknown("registry permission denied"), "registry permission denied")):
            with (self.subTest(reason=reason), patch.object(sessions, "session_liveness", return_value=liveness),
                  patch.object(stop_resume, "stop") as shutdown):
                result = self.cli("alpha", "--standby", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
                shutdown.assert_not_called()
                self.assert_no_launch_change(document, head)


    def test_attach_refuses_process_still_registered_after_stop(self) -> None:
        prior, trace = self.prepare_attach(after_stop=(303,))
        result = self.cli("alpha", "--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("processes still run after shutdown; pids found: [303]", result.stderr)
        self.assertEqual(trace, ["stop"])

    def test_attach_checks_old_pid_even_after_registry_record_disappears(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(sessions, "process_liveness", return_value=sessions.LiveSessionProcess(os.getpid())):
            result = self.cli("alpha", "--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"pids found: [{os.getpid()}]", result.stderr)
        self.assertEqual(trace, ["stop"])


    def test_attach_reports_duplicate_processes_after_launch_without_touching_them(self) -> None:
        prior, trace = self.prepare_attach(after_launch=(202, 303))
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["stop", "launch"])
        self.assertIn("ATTACH UNPROVEN", result.stderr)
        self.assertIn("pids found: [202, 303]", result.stderr)
        self.assertNotIn("attached 62ccbd64-f22e-5647-b20e-7899aa39d766 as", result.stdout)
        self.assertEqual(len([row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]), 1)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] in (["agent", "stop"], ["server", "stop"])], [])

    def test_a_resume_in_its_row_worktree_off_the_naming_pattern_launches_there(self) -> None:
        """hana's startup-polish row predates the naming rule (2026-10-19); its resume was refused."""
        recorded = self.commit_row_off_the_naming_pattern()
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(recorded), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        # The fixture marks only alpha, so the launch ends at the mark wait, after the row check.
        self.assertNotIn("Worktree is", result.stderr)
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))

    def test_a_resume_outside_its_row_worktree_is_refused_with_exit_two(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(elsewhere), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr,
                         f"add_unit: unit trunk resumes in {elsewhere.resolve()}; its staff row names {recorded}\n")
        self.assert_no_launch_change(document, head)

    def test_retired_plan_resume_cannot_bypass_the_retained_row_restore(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        row = unit_membership("build-followups", "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        before = self.repository_state()
        head = self.git("rev-parse", "HEAD")

        with patch.object(add_unit, "ensure_worktree") as worktree_step:
            result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                              "--cwd", str(prior))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "add_unit: unit alpha is retired; resume that unit, "
                         + "or remove it for good to free the name\n")
        worktree_step.assert_not_called()
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(starts, [])
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse((self.root / "project-alpha").exists())

    def test_retired_permanent_resume_keeps_the_existing_branch_and_refuses_admission(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.git("branch", "build-followups-alpha")
        row = unit_membership("build-followups", "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        before = self.repository_state()

        result = self.cli("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "add_unit: unit alpha is retired; resume that unit, "
                         + "or remove it for good to free the name\n")
        self.assertFalse(any(call[2:4] == ["agent", "start"] for call in self.herdr_calls()))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(self.repository_state(), before)
        self.assertFalse((self.root / "project-alpha").exists())

    def test_retired_as_built_plan_resumes_without_a_production_header(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        _ = self.git("add", plan)
        _ = self.git("commit", "-m", "add the as-built plan")
        _ = self.git("push", "origin", "build-followups")
        worktree = self.root / "project-alpha"
        _ = self.prepare_restored_worktree("alpha", worktree, plan=plan)
        document = self.doc.read_bytes()

        _ = self.successful("alpha", "--plan", plan, "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(worktree))

        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(worktree))
        self.assertEqual(self.doc.read_bytes(), document)

    def test_active_as_built_plan_resume_check_accepts_its_recorded_plan_without_a_header(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n", encoding="utf-8")
        _ = self.git("add", plan)
        worktree = self.root / "project-alpha"
        _ = self.prepare_restored_worktree("alpha", worktree, plan=plan)
        before = self.repository_state()

        _ = self.successful("alpha", "--plan", plan, "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                            "--cwd", str(worktree), "--check")

        self.assertEqual(self.repository_state(), before)
        self.assertEqual([call for call in self.herdr_calls()
                          if call[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])

        changed_plan = "docs/as-built/other.md"
        result = self.cli("alpha", "--plan", changed_plan, "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(worktree), "--check")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"unit alpha Plan is {plan!r}; expected {changed_plan!r}", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual([call for call in self.herdr_calls()
                          if call[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])

    def test_retired_plan_resume_reuses_its_worktree_after_a_failed_launch(self) -> None:
        worktree = self.root / "project-alpha"
        _ = self.prepare_restored_worktree("alpha", worktree)
        before = self.repository_state()

        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                            "--cwd", str(worktree))

        self.assertEqual(self.repository_state(), before)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(worktree))

    def test_rerun_keeps_marked_herdr_and_prints_its_herdr_attach_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, writes = self.run_with_labeled_session(unit_lookup.ClaudeNotRunning())
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)
        self.assertEqual(output.getvalue(), "alpha started: herdr session attach agents, workspace build-followups, tab alpha\n")

    def test_resume_from_a_brief_is_refused(self) -> None:
        result = self.cli("alpha", "--brief", "Do it.", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume requires --plan, --standby, or --permanent", result.stdout + result.stderr)

    def test_check_resume_preflights_without_any_launch_side_effect(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_no_launch_change(document, head)

    def test_resume_uses_session_id_cwd_and_promote_prompt(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                            "--cwd", str(prior), "--unit-type", "style",
                            "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.opened()[7], str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.agent_arguments(), [
            "--model", "opus", "--effort", "xhigh", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
            "--remote-control", "alpha", "-n", "alpha", "--settings", '{"disableAgentView": true}',
        ])
        self.assert_director_flags("opus", "xhigh")
        command = self.opening_prompt()
        self.assertIn("You are now alpha in production build-followups", command)
        promoted_plan = self.root / "project-alpha/docs/plans/given.md"
        self.assertTrue(promoted_plan.exists())
        self.assertIn(f"Run /unit:style {promoted_plan}", command)
        selected = staff.unit_command(unit_membership("build-followups", "alpha"))
        self.assertEqual(selected, "/unit:style")
        contract = SCRIPT.parents[2] / "commands/unit/style.md"
        self.assertTrue(contract.is_file())
        self.assertIn("<RestartTakeover/>", contract.read_text())

    def test_excluded_standing_plan_launches_and_keeps_the_units_copy(self) -> None:
        plan = Path("docs/style-production/units/alpha-unit.md")
        source = self.checkout / plan
        source.parent.mkdir(parents=True)
        header = "# Standing plan\n\n> **Production: build-followups** — unit `alpha`\n"
        standing = header + "> **Standing:** style_plan.py\n"
        _ = source.write_text(standing, encoding="utf-8")
        initial = self.repository_state()
        refusal = f"plan {plan} is missing on build-followups"

        with self.subTest(plan="untracked but not excluded"):
            result = self.cli("alpha", "--plan", plan.as_posix(), "--check")
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertIn(refusal, result.stderr)
            self.assertEqual(self.repository_state(), initial)

        exclude = self.checkout / ".git/info/exclude"
        _ = exclude.write_text(exclude.read_text() + "\n/docs/style-production/\n", encoding="utf-8")
        _ = source.write_text(header, encoding="utf-8")
        with self.subTest(plan="excluded without Standing header"):
            result = self.cli("alpha", "--plan", plan.as_posix(), "--check")
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertIn(refusal, result.stderr)
            self.assertEqual(self.repository_state(), initial)

        _ = source.write_text(standing, encoding="utf-8")
        _ = self.successful("alpha", "--plan", plan.as_posix(), "--check")
        self.assertEqual(self.repository_state(), initial)
        _ = self.successful("alpha", "--plan", plan.as_posix())
        worktree = self.root / "project-alpha"
        copied = worktree / plan
        self.assertEqual(copied.read_text(encoding="utf-8"), standing)
        self.assertEqual(self.git("check-ignore", "--", plan.as_posix(), cwd=worktree), plan.as_posix())
        self.assertEqual(self.git("ls-tree", "HEAD", "--", plan.as_posix()), "")
        self.assertIn(f"/unit:direct {plan}", self.opening_prompt())

        progress = standing + "\n### Phase 1 — ticket in progress\n"
        _ = copied.write_text(progress, encoding="utf-8")
        _ = source.write_text(standing + "\nShowrunner template changed\n", encoding="utf-8")
        _ = self.successful("alpha", "--plan", plan.as_posix())
        self.assertEqual(copied.read_text(encoding="utf-8"), progress)

        with self.subTest(plan="committed only on the unit branch"):
            _ = self.git("add", "-f", "--", plan.as_posix(), cwd=worktree)
            _ = self.git("commit", "-q", "-m", "commit the unit plan", cwd=worktree)
            write_text_atomic(source, header)
            row = unit_membership("build-followups", "alpha")
            state = self.repository_state()
            for flag, observed in (("--resume", sessions.SessionNotLive()),
                                   ("--attach", sessions.LiveSessionProcess(os.getpid()))):
                with (self.subTest(flag=flag),
                      patch.object(sessions, "session_liveness", return_value=observed),
                      patch.object(add_unit, "attach_source", return_value=add_unit.RunningSessionHandoff(
                          row.current_session, os.getpid(), "fixture-start", "fixture-login")),
                      patch.object(add_unit, "stop_for_attach") as stopped):
                    _ = self.successful("alpha", "--plan", plan.as_posix(), flag, row.current_session,
                                        "--cwd", str(worktree), "--check")
                    stopped.assert_not_called()
                    self.assertEqual(self.repository_state(), state)
                    self.assertEqual(copied.read_text(encoding="utf-8"), progress)

    def test_transcript_model_reads_newest_file_and_crosses_chunk_boundaries(self) -> None:
        root = Path(self.env["HOME"]) / ".claude/projects"
        old = root / "old/62ccbd64-f22e-5647-b20e-7899aa39d766.jsonl"
        newest = root / "new/62ccbd64-f22e-5647-b20e-7899aa39d766.jsonl"
        for path in (old, newest):
            path.parent.mkdir(parents=True)
        _ = old.write_text(json.dumps({"type": "assistant", "message": {"model": "old-model"}}))
        _ = newest.write_text(json.dumps({"type": "assistant", "message": {"model": "opus", "content": "é" * 70000}})
                              + '\n' + json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + '\n{partial')
        os.utime(old, (1, 1))
        with patch.dict(os.environ, self.env):
            self.assertEqual(add_unit.transcript_model("62ccbd64-f22e-5647-b20e-7899aa39d766"), "opus")
            self.assertEqual(add_unit.transcript_model("absent"), "")


class PythonExecutionEnvironment(TypedDict):
    root: str
    extraPaths: list[str]


class PythonImportConfiguration(TypedDict):
    executionEnvironments: list[PythonExecutionEnvironment]


@final
class UnitLaunchProtocolTests(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        home = self.enterContext(tempfile.TemporaryDirectory())
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": home})
        ))
        del home_context

    def resume_instructions(self) -> str:
        command = SCRIPT.parents[2] / "commands/showrunner/produce.md"
        return command.read_text(encoding="utf-8").split("**Resume.**", 1)[1].split(
            "</LaunchUnits>", 1
        )[0]

    def test_manual_unit_resume_uses_add_unit_without_selecting_the_current_unit_role(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("add_unit.py", instructions)
        self.assertIn("--resume <session-id>", instructions)
        self.assertNotIn("env unit", instructions)
        self.assertNotIn("claude --resume", instructions)

    def test_manual_unit_resume_uses_configured_units_account_and_names_missing_configuration_command(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("starts it on the configured units account", instructions)
        self.assertIn("units account is not configured", instructions)
        self.assertIn("accounts.py move units", instructions)

    def test_production_import_paths_resolve_the_accounts_package(self) -> None:
        """F005: production's configured import search can find scripts.accounts."""
        root = SCRIPT.parents[2]
        configuration = cast(
            PythonImportConfiguration,
            cast(object, json.loads((root / "pyrightconfig.json").read_text(encoding="utf-8"))),
        )
        environment = next(
            candidate for candidate in configuration["executionEnvironments"]
            if candidate["root"] == "scripts/production"
        )

        self.assertTrue(any(
            (root / search_path / "scripts/accounts/accounts.py").is_file()
            for search_path in [environment["root"], *environment["extraPaths"]]
        ))


if __name__ == "__main__":
    _ = unittest.main()
