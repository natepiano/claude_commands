"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
import production_units
import launch_permission
import unit_lookup
import sessions
import record as shutdown_store
import restart
import herdr
from herdr import HerdrPlace
from production_units import cell_value, live_unit_table, plan_cell_is_retired, retired_units
from test_support import ShutdownTestCase


from add_unit_test_support import AddUnitTestCase, configure_test_zone, SCRIPT, OLD_HEADER


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitResumeTests(AddUnitTestCase):
    def test_new_row_resume_closes_the_stopped_marked_tab_then_launches(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        old_head = self.git("rev-parse", "build-followups")
        stopped = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(),
                                        HerdrPlace("old-host", "w2:p3"))
        running = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "session-123", "/fixture/socket", 4242),
            HerdrPlace("agents", "w1:p1"))
        order = Mock()
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(stopped)),
              patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
              patch.object(herdr, "close_agent") as closed,
              patch.object(herdr, "start_agent", wraps=herdr.start_agent) as started,
              patch.object(add_unit, "wait_for_remote_control", return_value=running) as waited):
            order.attach_mock(closed, "close")
            order.attach_mock(started, "start")
            result = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                                     "--cwd", str(prior))

        closed.assert_called_once_with("old-host", "w2:p3")
        started.assert_called_once()
        self.assertEqual(order.mock_calls[0][0], "close")
        self.assertEqual(order.mock_calls[1][0], "start")
        waited.assert_called_once()
        self.assertIn("alpha-unit started:", result.stdout)
        self.assertIn("| alpha-unit |", self.doc.read_text().replace("`", ""))
        self.assertNotEqual(self.git("rev-parse", "build-followups"), old_head)
        self.assertEqual(self.git("log", "-1", "--format=%s", "build-followups"),
                         "production(build-followups): add unit alpha-unit (plan)")
        self.assertTrue((self.root / "project-alpha").is_dir())
        self.assertEqual(self.git("rev-parse", "build-followups-alpha"),
                         self.git("rev-parse", "build-followups"))
        self.assertEqual(self.opened()[7], str(prior))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")
        self.assertIn("Run /unit:direct", self.opening_prompt())

    def test_resume_checks_session_cwd_instead_of_worktree(self) -> None:
        prior = self.root / "untrusted-session"
        prior.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(prior), "--account", "claude2")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"Claude is not trusted in {prior}", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_retired_marker_only_applies_at_start_of_plan_cell(self) -> None:
        cases = (
            ("retired by the user", True),
            ("(retired by the user)", True),
            ("docs/plan.md; retired by the user", False),
            ("(run done; retired by the user)", False),
            ("retiredness", False),
            ("Retired by the user", False),
        )
        for plan_cell, expected in cases:
            with self.subTest(plan_cell=plan_cell):
                self.assertEqual(plan_cell_is_retired(plan_cell), expected)

    def test_retired_readers_use_marker_on_production_rows(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| stalls-unit | (retired by the user 2026-10-07; run done, worktree removed) follow-up: `/unit:report` shows the same sections every time, the last recorded tables when no window is open (the user, 2026-10-06 22:5x); earlier follow-up: showrunner replies end with the agent usage lines, checked by a hook (the user, 2026-10-06 08:1x), then `docs/as-built/build-followups-fn-length-hook.md (run done; as-built 4ab5e4c)` (follow-up `commands/unit/eta_breakdown.md` merged as a9f35af) (earlier runs' as-builts `docs/as-built/screen-record.md`, merged as b3c069f, `docs/as-built/build-memory-admission.md`, merged as 35151e5, and `docs/as-built/buildlog-memory-stalls.md`) | `/home/natepiano/worktrees/claude-build-followups-stalls` | `build-followups-stalls` | `hook` (resumed in `/etc/nixos`; renamed from `nightly-config` 2026-10-04, then `stalls` → `footer` → `hook` by the user 2026-10-06, herdr session too; its own herdr session `stalls` since 22:13 PDT, after the nightly review's launcher closed the `nightly-config` session it ran in) | — | memory stall recording and its report section; then memory admission, which edited `scripts/buildlog/`, `scripts/lint/` (`invoke.sh`, `memory_gate.sh`, `sweep.py`), `scripts/direct/verify.sh` and `board.sh`, `scripts/build_hold/build_hold.py`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/agents/codex_mesh.py`, `scripts/message/` (`send.py`, `top_level.py`), `scripts/notify/pushover.py`, `scripts/whoami/agent_notes.py`, `scripts/production/dailies_render.py`, `settings.json`, `config/lint.conf`, `pyrightconfig.json`, and `commands/` (`build_hold.md`, `lint_config.md`, `notify_top_level.md`, `showrunner/dailies.md`, `showrunner/produce.md`); promoted by the user 2026-10-04; then the screen recording skill: `commands/screen_record.md`, `scripts/screen_record/`; and the `/unit:report` timer fixes: `scripts/production/unit_status.sh`, `scripts/direct/progress_history.py`; then the Codex `systemError` fix: `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py`; then the kill order: `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py`, `docs/as-built/build-memory-admission.md`; then `/unit:eta_breakdown`: `commands/unit/eta_breakdown.md`; then the function-length hook: `scripts/hooks/fn_length_lib.py`, `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/test_fn_length.py`, `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`, `settings.json`; then the footer: `scripts/production/dailies_render.py`, a showrunner-only Stop hook under `scripts/hooks/`, `settings.json`, `commands/showrunner/produce.md`; `commands/unit/report.md` (the report follow-up) |",
            "| enh-showrunner-unit | `docs/plans/build-followups-enh-showrunner-dailies.md` (follow-up: retired units drop out of the status and a simple dailies groups idle units; the user, 2026-10-07; the earlier run's as-built is `docs/as-built/showrunner-automation.md`) (Phases 7 and 8 of stalls-unit's plan, moved by the user 2026-10-06 14:2x PDT) | `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` | `build-followups-enh-showrunner` | `enh-showrunner` | — | `docs/as-built/showrunner-automation.md`; `scripts/production/add_unit.py`, `test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; `commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests (hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): `commands/unit/direct.md`, `commands/unit/checkpoint.md`, `docs/direct/run_phase_review.md`, `docs/production_format.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/direct/phase_end.md`, `docs/direct_plan_format.md`, `commands/unit/eta_breakdown.md`, `docs/direct/final_gate_commit.md` |",
            "| model-study-unit | `docs/as-built/director-model-study.md` (run done; as-built 16e5ac6) | `/home/natepiano/worktrees/claude-build-followups-model-study` | `build-followups-model-study` | `model-study` | — | `docs/plans/build-followups-model-study.md`; `scripts/model_study/`; `docs/as-built/director-model-study-results.md` |",
        ]
        self.assertEqual(retired_units(lines, "build-followups"), {"stalls-unit"})
        self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "build-followups")],
                         ["enh-showrunner-unit", "model-study-unit"])

    def test_retired_units_include_retired_name_and_exclude_live_name(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `old-unit` | retired after completion | /tmp/old | old | old | — | — |",
            "| `live-unit` | docs/live.md | /tmp/live | live | live | — | — |",
        ]
        self.assertEqual(retired_units(lines, "show"), {"old-unit"})

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
                result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior), *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stderr, f"add_unit: session session-123 already runs as pid {os.getpid()}; "
                                 + "to make it this unit, run add_unit with --attach session-123\n")
                self.assert_no_launch_change(document, head)
                self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))
        # Brief launches must refuse before their stub is written too.
        result = self.cli("alpha", "--brief", "Repair the unit", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_resume_with_unreadable_registry_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / "broken.json").write_text("{broken")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session session-123 runs: "
                         + "unreadable session registry record: broken.json\n")
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_resume_with_unreadable_process_identity_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("recorded-start")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        with patch.object(sessions, "process_start", return_value=None):
            result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session session-123 runs: "
                         + f"process identity unreadable for pid {os.getpid()}\n")
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_resume_with_stale_registry_identity_still_launches(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("a-previous-process-start")
        result = self.successful("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertIn("alpha-unit started:", result.stdout)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")
        self.assertIn(str(prior), self.opened())
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)

    def test_attach_stops_then_launches_once_with_unit_marks_and_finishes(self) -> None:
        prior, trace = self.prepare_attach()
        result = self.successful("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(trace, ["down", "follow", "launch", "finish"])
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")
        self.assertEqual(self.pane_environment()[unit_lookup.PRODUCTION_MARK], "build-followups")
        self.assertEqual(self.pane_environment()[unit_lookup.UNIT_MARK], "alpha-unit")
        self.assertIn(str(prior), self.opened())
        self.assertIn("attached session-123 as alpha-unit: one process, pid 202", result.stdout)

    def test_attach_requires_cwd_supported_mode_and_exclusive_session_flag(self) -> None:
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        cases = ((["--permanent", "--attach", "session-123"], "--attach and --cwd"),
                 (["--brief", "repair", "--attach", "session-123", "--cwd", str(self.root)], "--attach requires"),
                 (["--permanent", "--attach", "session-123", "--resume", "other", "--cwd", str(self.root)], "not allowed"))
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = self.cli("alpha", *arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(message, result.stderr)
                self.assert_no_launch_change(document, head)

    def test_attach_check_validates_without_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        _ = self.successful("alpha", "--standby", "--attach", "session-123", "--cwd", str(prior), "--check")
        self.assertEqual(trace, [])

    def test_attach_refuses_absent_and_unknown_sessions_before_writes(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        for liveness, reason in ((sessions.SessionNotLive(), "session session-123 does not run; use --resume"),
                                 (sessions.SessionLivenessUnknown("registry permission denied"), "registry permission denied")):
            with (self.subTest(reason=reason), patch.object(sessions, "session_liveness", return_value=liveness),
                  patch.object(add_unit, "run_attach_shutdown") as shutdown):
                result = self.cli("alpha", "--standby", "--attach", "session-123", "--cwd", str(prior))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
                shutdown.assert_not_called()
                self.assert_no_launch_change(document, head)

    def test_attach_checks_launch_hold_before_requesting_shutdown(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("units account is held")):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("units account is held", result.stderr)
        self.assertEqual(trace, [])

    def test_attach_timeout_leaves_shutdown_state_and_prints_cancel(self) -> None:
        prior, trace = self.prepare_attach(stop_progress="waiting", timeout=True)
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow"])
        self.assertIn("cancel owner@example.com --here --only session-123", result.stderr)
        with patch.dict(os.environ, self.env):
            self.assertEqual(shutdown_store.session_records("owner@example.com")[0]["progress"]["kind"], "waiting")

    def test_attach_requires_stopped_record_even_when_follower_returns_success(self) -> None:
        prior, trace = self.prepare_attach(stop_progress="waiting")
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("record does not confirm a completed stop", result.stderr)
        self.assertIn("cancel owner@example.com --here --only session-123", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_refuses_process_still_registered_after_stop(self) -> None:
        prior, trace = self.prepare_attach(after_stop=(303,))
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("processes still run after shutdown; pids found: [303]", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_checks_old_pid_even_after_registry_record_disappears(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(sessions, "process_liveness", return_value=sessions.LiveSessionProcess(os.getpid())):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"pids found: [{os.getpid()}]", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_refuses_unfinished_shutdown_record_after_launch(self) -> None:
        prior, trace = self.prepare_attach()
        def unfinished(records: list[shutdown_store.SessionShutdown]) -> restart.TargetCheck:
            return restart.TargetCheck(records, [])

        with patch.object(restart, "clear_running_on_target", side_effect=unfinished):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow", "launch"])
        self.assertIn("shutdown record did not finish", result.stderr)
        self.assertNotIn("attached session-123 as", result.stdout)

    def test_attach_reports_duplicate_processes_after_launch_without_touching_them(self) -> None:
        prior, trace = self.prepare_attach(after_launch=(202, 303))
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow", "launch", "finish"])
        self.assertIn("ATTACH UNPROVEN", result.stderr)
        self.assertIn("pids found: [202, 303]", result.stderr)
        self.assertNotIn("attached session-123 as", result.stdout)
        self.assertEqual(len([row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]), 1)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] in (["agent", "stop"], ["server", "stop"])], [])

    def test_a_resume_in_its_row_worktree_off_the_naming_pattern_launches_there(self) -> None:
        """hana's startup-polish row predates the naming rule (2026-10-19); its resume was refused."""
        recorded = self.commit_row_off_the_naming_pattern()
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(recorded), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        # The fixture marks only alpha-unit, so the launch ends at the mark wait, after the row check.
        self.assertNotIn("Worktree is", result.stderr)
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))

    def test_a_resume_outside_its_row_worktree_is_refused_with_exit_two(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(elsewhere), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr,
                         f"add_unit: unit trunk-unit resumes in {elsewhere.resolve()}; its Units row names {recorded}\n")
        self.assert_no_launch_change(document, head)

    def test_retired_plan_resumes_in_its_start_folder_without_recreating_the_worktree(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.commit_prepared_row(self.unit_row(
            plan=f"(retired 2026-10-28, worktree removed, session session-123, folder {prior}) "
                 + "`docs/plans/given.md` (run done)"))
        before = self.repository_state()
        head = self.git("rev-parse", "HEAD")

        with patch.object(add_unit, "ensure_worktree") as worktree_step:
            _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                                "--cwd", str(prior))

        worktree_step.assert_not_called()
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--resume") + 1], "session-123")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(prior))
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse((self.root / "project-alpha").exists())

    def test_retired_permanent_resume_ignores_an_existing_branch_without_a_worktree(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.commit_prepared_row(self.unit_row(
            plan="(retired 2026-10-28, worktree removed, session session-123) permanent"))
        _ = self.git("branch", "build-followups-alpha")
        before = self.repository_state()

        _ = self.successful("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))

        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(prior))
        self.assertEqual(self.repository_state(), before)
        self.assertFalse((self.root / "project-alpha").exists())

    def test_retired_as_built_plan_resumes_without_a_production_header(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        _ = self.git("add", plan)
        self.commit_prepared_row(self.unit_row(
            plan=f"(retired 2026-10-28, worktree removed, session session-123) `{plan}` (run done)"))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        document = self.doc.read_bytes()

        _ = self.successful("alpha", "--plan", plan, "--resume", "session-123", "--cwd", str(worktree))

        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--resume") + 1], "session-123")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(worktree))
        self.assertEqual(self.doc.read_bytes(), document)

    def test_active_as_built_plan_resume_check_accepts_its_recorded_plan_without_a_header(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n", encoding="utf-8")
        _ = self.git("add", plan)
        self.commit_prepared_row(self.unit_row(plan=plan))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        before = self.repository_state()

        _ = self.successful("alpha", "--plan", plan, "--resume", "session-123",
                            "--cwd", str(worktree), "--check")

        self.assertEqual(self.repository_state(), before)
        self.assertEqual([call for call in self.herdr_calls()
                          if call != ["--session", "default", "session", "list", "--json"]], [])

        changed_plan = "docs/as-built/other.md"
        result = self.cli("alpha", "--plan", changed_plan, "--resume", "session-123",
                          "--cwd", str(worktree), "--check")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"unit alpha-unit Plan is {plan!r}; expected {changed_plan!r}", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual([call for call in self.herdr_calls()
                          if call != ["--session", "default", "session", "list", "--json"]], [])

    def test_retired_plan_resume_reuses_its_worktree_after_a_failed_launch(self) -> None:
        self.commit_prepared_row(self.unit_row(
            plan="(retired 2026-10-28, worktree removed, session session-123) docs/plans/given.md"))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        before = self.repository_state()

        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(worktree))

        self.assertEqual(self.repository_state(), before)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--resume") + 1], "session-123")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(worktree))

    def test_rerun_keeps_marked_herdr_and_prints_its_herdr_attach_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning()
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: herdr session attach agents, workspace director, tab alpha\n")

    def test_resume_from_a_brief_is_refused(self) -> None:
        result = self.cli("alpha", "--brief", "Do it.", "--resume", "session-123", "--cwd", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume requires --plan, --standby, or --permanent", result.stdout + result.stderr)

    def test_check_resume_preflights_without_any_launch_side_effect(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_no_launch_change(document, head)

    def test_resume_uses_session_id_cwd_and_promote_prompt(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.opened()[7], str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.agent_arguments(), [
            "--model", "opus", "--effort", "xhigh", "--resume", "session-123",
            "--remote-control", "alpha", "-n", "alpha", "--settings", '{"disableAgentView": true}',
        ])
        self.assert_director_flags("opus", "xhigh")
        command = self.opening_prompt()
        self.assertIn("You are now alpha-unit in production build-followups", command)
        promoted_plan = self.root / "project-alpha/docs/plans/given.md"
        self.assertTrue(promoted_plan.exists())
        self.assertIn(f"Run /unit:direct {promoted_plan}", command)

    def test_excluded_standing_plan_launches_and_keeps_the_units_copy(self) -> None:
        plan = Path("docs/style-production/units/alpha-unit.md")
        source = self.checkout / plan
        source.parent.mkdir(parents=True)
        header = "# Standing plan\n\n> **Production: build-followups** — unit `alpha-unit`\n"
        standing = header + "> **Standing:** style_plan.py\n"
        _ = source.write_text(standing, encoding="utf-8")
        initial = self.repository_state()
        refusal = f"plan {plan} is missing on build-followups or lacks the Production header"

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

    def test_new_unit_as_built_plan_still_requires_a_production_header(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n", encoding="utf-8")
        _ = self.git("add", plan)
        _ = self.git("commit", "-m", "record completed plan")
        before = self.repository_state()

        result = self.cli("alpha", "--plan", plan, "--check")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr,
                         f"add_unit: plan {plan} is missing on build-followups or lacks the Production header\n")
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_transcript_model_reads_newest_file_and_crosses_chunk_boundaries(self) -> None:
        root = Path(self.env["HOME"]) / ".claude/projects"
        old = root / "old/session-123.jsonl"
        newest = root / "new/session-123.jsonl"
        for path in (old, newest):
            path.parent.mkdir(parents=True)
        _ = old.write_text(json.dumps({"type": "assistant", "message": {"model": "old-model"}}))
        _ = newest.write_text(json.dumps({"type": "assistant", "message": {"model": "opus", "content": "é" * 70000}})
                              + '\n' + json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + '\n{partial')
        os.utime(old, (1, 1))
        with patch.dict(os.environ, self.env):
            self.assertEqual(add_unit.transcript_model("session-123"), "opus")
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


class RetiredRowTests(ShutdownTestCase):
    """A unit is retired by its Plan cell, or when its worktree and its herdr session are both gone."""

    root: Path = Path()
    linked: Path = Path()
    main: Path = Path()
    missing: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.linked = self.root / "linked"
        self.linked.mkdir()
        _ = (self.linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.main = self.root / "main"
        (self.main / ".git").mkdir(parents=True)
        self.missing = self.root / "missing"

    def row(self, unit: str, worktree: Path, plan: str = "`docs/plan.md`") -> str:
        return f"| `{unit}-unit` | {plan} | `{worktree}` | `branch` | `{unit}` (resumed elsewhere) | — | — |"

    def session(self, unit: str) -> unit_lookup.MarkedUnit:
        return unit_lookup.MarkedUnit(unit, unit_lookup.ClaudeNotRunning(), HerdrPlace("fixture", "w1:p1"))

    def retired(self, worktree: Path, *, gone: bool, plan: str = "`docs/plan.md`") -> tuple[bool, int]:
        """Whether the row is retired, and how many times herdr was asked."""
        found = {} if gone else {"some-unit": self.session("some-unit")}
        cells = production_units.unit_table([*OLD_HEADER, self.row("some", worktree, plan)])[0]
        with patch.object(unit_lookup, "marked_units", return_value=found) as asked:
            return production_units.unit_is_retired(cells, production_units.SessionMarks("show")), asked.call_count

    def test_a_unit_is_gone_only_without_both_its_worktree_and_its_session(self) -> None:
        self.assertEqual(self.retired(self.missing, gone=True), (True, 1))
        self.assertEqual(self.retired(self.missing, gone=False), (False, 1))
        # A session that is only restarting keeps its row: its worktree is there, and herdr is not asked.
        self.assertEqual(self.retired(self.linked, gone=True), (False, 0))
        # A main checkout is shared, so it holds no unit once the session is gone.
        self.assertEqual(self.retired(self.main, gone=True), (True, 1))
        self.assertEqual(self.retired(self.main, gone=False), (False, 1))
        self.assertEqual(self.retired(self.linked, gone=False, plan="retired after completion"), (True, 0))

    def test_herdr_must_say_the_session_is_gone_and_is_asked_once_for_a_whole_table(self) -> None:
        lines = [*OLD_HEADER, self.row("first", self.missing), self.row("second", self.missing)]
        with patch.object(unit_lookup, "marked_units", side_effect=OSError("herdr could not list sessions")) as asked:
            self.assertEqual(retired_units(lines, "show"), set())
        self.assertEqual(asked.call_count, 1)
        with patch.object(unit_lookup, "marked_units", return_value={"second-unit": self.session("second-unit")}) as asked:
            self.assertEqual(retired_units(lines, "show"), {"first-unit"})
        self.assertEqual(asked.call_count, 1)

    def test_every_list_drops_a_unit_that_is_gone(self) -> None:
        lines = [
            *OLD_HEADER,
            self.row("marked", self.linked, "retired after completion"),
            self.row("gone", self.missing),
            self.row("parked", self.linked),
        ]
        with patch.object(unit_lookup, "marked_units", return_value={}):
            self.assertEqual(retired_units(lines, "show"), {"marked-unit", "gone-unit"})
            self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "show")], ["parked-unit"])


if __name__ == "__main__":
    _ = unittest.main()
