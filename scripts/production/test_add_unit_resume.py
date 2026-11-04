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
from unit_lookup import ProductionMembers, unit_is_retired
from scripts.production import staff, unit_config
import unit_lookup
import sessions
from scripts.shutdown import stop_resume
import herdr
from herdr import HerdrPlace
from production_units import cell_value, plan_cell_is_retired
from unit_lookup import live_unit_table, retired_units
from test_support import ShutdownTestCase


from add_unit_test_support import AddUnitTestCase, configure_test_zone, SCRIPT, OLD_HEADER
from scripts.production.staff_test_support import (
    StaffTestCase, fixture_showrunner_name, showrunner_relationship, unit_membership,
)


def setUpModule() -> None:
    configure_test_zone()


@final
class AddUnitResumeTests(AddUnitTestCase):
    def test_resume_preserves_existing_config_settings_and_launches_with_the_same_staff_id(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        row = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
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
        stopped = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(),
                                        HerdrPlace("old-host", "w2:p3"))
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(stopped)),
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
        stopped = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(),
                                        HerdrPlace("old-host", "w2:p3"))
        running = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/fixture/socket", 4242),
            HerdrPlace("agents", "w1:p1"))
        order = Mock()
        with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(stopped)),
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
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertIn("Run /unit:direct", self.opening_prompt())

    def test_resume_checks_session_cwd_instead_of_worktree(self) -> None:
        prior = self.root / "untrusted-session"
        prior.mkdir()
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(prior), "--account", "claude2")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn(f"alpha-unit: trust anomaly: Claude has not trusted {prior}", result.stderr)
        self.assertIn("alpha-unit started:", result.stdout)
        self.assertEqual(self.opened()[7], str(prior))

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
        row = unit_membership(fixture_showrunner_name("build-followups", self.doc), "stalls-unit")
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "retired-stalls"), expected=row.worktree)
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| stalls-unit | (retired by the user 2026-10-07; run done, worktree removed) follow-up: `/unit:report` shows the same sections every time, the last recorded tables when no window is open (the user, 2026-10-06 22:5x); earlier follow-up: showrunner replies end with the agent usage lines, checked by a hook (the user, 2026-10-06 08:1x), then `docs/as-built/build-followups-fn-length-hook.md (run done; as-built 4ab5e4c)` (follow-up `commands/unit/eta_breakdown.md` merged as a9f35af) (earlier runs' as-builts `docs/as-built/screen-record.md`, merged as b3c069f, `docs/as-built/build-memory-admission.md`, merged as 35151e5, and `docs/as-built/buildlog-memory-stalls.md`) | `/home/natepiano/worktrees/claude-build-followups-stalls` | `build-followups-stalls` | `hook` (resumed in `/etc/nixos`; renamed from `nightly-config` 2026-10-04, then `stalls` → `footer` → `hook` by the user 2026-10-06, herdr session too; its own herdr session `stalls` since 22:13 PDT, after the nightly review's launcher closed the `nightly-config` session it ran in) | — | memory stall recording and its report section; then memory admission, which edited `scripts/buildlog/`, `scripts/lint/` (`invoke.sh`, `memory_gate.sh`, `sweep.py`), `scripts/direct/verify.sh` and `board.sh`, `scripts/build_hold/build_hold.py`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/agents/codex_mesh.py`, `scripts/message/` (`send.py`, `top_level.py`), `scripts/notify/pushover.py`, `scripts/whoami/agent_notes.py`, `scripts/production/dailies_render.py`, `settings.json`, `config/lint.conf`, `pyrightconfig.json`, and `commands/` (`build_hold.md`, `lint_config.md`, `notify_top_level.md`, `showrunner/dailies.md`, `showrunner/produce.md`); promoted by the user 2026-10-04; then the screen recording skill: `commands/screen_record.md`, `scripts/screen_record/`; and the `/unit:report` timer fixes: `scripts/production/unit_status.sh`, `scripts/direct/progress_history.py`; then the Codex `systemError` fix: `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py`; then the kill order: `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py`, `docs/as-built/build-memory-admission.md`; then `/unit:eta_breakdown`: `commands/unit/eta_breakdown.md`; then the function-length hook: `scripts/hooks/fn_length_lib.py`, `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/test_fn_length.py`, `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`, `settings.json`; then the footer: `scripts/production/dailies_render.py`, a showrunner-only Stop hook under `scripts/hooks/`, `settings.json`, `commands/showrunner/produce.md`; `commands/unit/report.md` (the report follow-up) |",
            "| enh-showrunner-unit | `docs/plans/build-followups-enh-showrunner-dailies.md` (follow-up: retired units drop out of the status and a simple dailies groups idle units; the user, 2026-10-07; the earlier run's as-built is `docs/as-built/showrunner-automation.md`) (Phases 7 and 8 of stalls-unit's plan, moved by the user 2026-10-06 14:2x PDT) | `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` | `build-followups-enh-showrunner` | `enh-showrunner` | — | `docs/as-built/showrunner-automation.md`; `scripts/production/add_unit.py`, `test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; `commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests (hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): `commands/unit/direct.md`, `commands/unit/checkpoint.md`, `docs/direct/run_phase_review.md`, `docs/production_format.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/direct/phase_end.md`, `docs/direct_plan_format.md`, `commands/unit/eta_breakdown.md`, `docs/direct/final_gate_commit.md` |",
            "| model-study-unit | `docs/as-built/director-model-study.md` (run done; as-built 16e5ac6) | `/home/natepiano/worktrees/claude-build-followups-model-study` | `build-followups-model-study` | `model-study` | — | `docs/plans/build-followups-model-study.md`; `scripts/model_study/`; `docs/as-built/director-model-study-results.md` |",
        ]
        for unit in ("enh-showrunner-unit", "model-study-unit"):
            _ = unit_membership(fixture_showrunner_name("build-followups", self.doc), unit)
        self.assertEqual(retired_units(lines, "build-followups"), {"stalls-unit"})
        self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "build-followups")],
                         ["enh-showrunner-unit", "model-study-unit"])

    def test_retired_units_include_retired_name_and_exclude_live_name(self) -> None:
        row = unit_membership("show", "old-unit")
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "retired-old"), expected=row.worktree)
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `old-unit` | retired after completion | /tmp/old | old | old | — | — |",
            "| `live-unit` | docs/live.md | /tmp/live | live | live | — | — |",
        ]
        _ = unit_membership("show", "live-unit")
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

    def test_resume_with_unreadable_registry_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / "broken.json").write_text("{broken")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session 62ccbd64-f22e-5647-b20e-7899aa39d766 runs: "
                         + "unreadable session registry record: broken.json\n")
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
        self.assertIn("alpha-unit started:", result.stdout)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertIn(str(prior), self.opened())
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)

    def test_attach_stops_then_launches_once_with_staff_marks_and_finishes(self) -> None:
        prior, trace = self.prepare_attach()
        result = self.successful("alpha", "--permanent", "--attach", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))
        self.assertEqual(trace, ["stop", "launch"])
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "62ccbd64-f22e-5647-b20e-7899aa39d766")
        self.assertEqual(self.pane_environment()[unit_lookup.PRODUCTION_MARK], "build-followups")
        self.assertEqual(self.pane_environment()[unit_lookup.UNIT_MARK], "alpha-unit")
        self.assertIn(str(prior), self.opened())
        self.assertIn("attached 62ccbd64-f22e-5647-b20e-7899aa39d766 as alpha-unit: one process, pid 202", result.stdout)
        row = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
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
        # The fixture marks only alpha-unit, so the launch ends at the mark wait, after the row check.
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
                         f"add_unit: unit trunk-unit resumes in {elsewhere.resolve()}; its staff row names {recorded}\n")
        self.assert_no_launch_change(document, head)

    def test_retired_plan_resume_cannot_bypass_the_retained_row_restore(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.commit_prepared_row(self.unit_row(
            plan=f"(retired 2026-10-28, worktree removed, session 62ccbd64-f22e-5647-b20e-7899aa39d766, folder {prior}) "
                 + "`docs/plans/given.md` (run done)"))
        row = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        before = self.repository_state()
        head = self.git("rev-parse", "HEAD")

        with patch.object(add_unit, "ensure_worktree") as worktree_step:
            result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                              "--cwd", str(prior))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "add_unit: unit alpha-unit is retired; resume that unit, "
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
        self.commit_prepared_row(self.unit_row(
            plan="(retired 2026-10-28, worktree removed, session 62ccbd64-f22e-5647-b20e-7899aa39d766) permanent"))
        _ = self.git("branch", "build-followups-alpha")
        row = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
        row = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.root / "project-alpha"), expected=row.worktree)
        before = self.repository_state()

        result = self.cli("alpha", "--permanent", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "add_unit: unit alpha-unit is retired; resume that unit, "
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
        self.commit_prepared_row(self.unit_row(
            plan=f"(retired 2026-10-28, worktree removed, session 62ccbd64-f22e-5647-b20e-7899aa39d766) `{plan}` (run done)"))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        member = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
        _ = staff.set_worktree(member.id, staff.PresentWorktree("natedev", worktree), expected=member.worktree)
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
        self.commit_prepared_row(self.unit_row(plan=plan))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
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
        self.assertIn(f"unit alpha-unit Plan is {plan!r}; expected {changed_plan!r}", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual([call for call in self.herdr_calls()
                          if call[2:4] not in (["session", "list"], ["pane", "list"], ["pane", "process-info"])], [])

    def test_retired_plan_resume_reuses_its_worktree_after_a_failed_launch(self) -> None:
        self.commit_prepared_row(self.unit_row(
            plan="(retired 2026-10-28, worktree removed, session 62ccbd64-f22e-5647-b20e-7899aa39d766) docs/plans/given.md"))
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "build-followups-alpha")
        member = unit_membership(fixture_showrunner_name("build-followups", self.doc), "alpha-unit")
        _ = staff.set_worktree(member.id, staff.PresentWorktree("natedev", worktree), expected=member.worktree)
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
            result, error, commands, launches, writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning()
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: herdr session attach agents, workspace director, tab alpha\n")

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
                            "--cwd", str(prior), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.opened()[7], str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.agent_arguments(), [
            "--model", "opus", "--effort", "xhigh", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
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


class RetiredRowTests(ShutdownTestCase):
    """Staff membership outlives stale Plan, worktree and pane observations."""

    root: Path = Path()
    linked: Path = Path()
    main: Path = Path()
    missing: Path = Path()

    @override
    def setUp(self) -> None:
        fixture = StaffTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        super().setUp()
        _ = showrunner_relationship("show")
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
            return unit_is_retired(cells, ProductionMembers("show")), asked.call_count

    def test_membership_lasts_until_the_staff_row_is_retired(self) -> None:
        self.assertEqual(self.retired(self.linked, gone=False), (False, 0))
        row = unit_membership("show", "some-unit")
        for worktree in (self.missing, self.linked, self.main):
            for gone in (True, False):
                with self.subTest(worktree=worktree, gone=gone):
                    self.assertEqual(self.retired(worktree, gone=gone), (False, 0))
        self.assertEqual(self.retired(self.linked, gone=False, plan="retired after completion"), (False, 0))
        staff.defer_retire((row.id,))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.linked), expected=row.worktree)
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(self.retired(self.linked, gone=False), (True, 0))

    def test_a_whole_table_reads_one_snapshot_and_no_herdr_membership(self) -> None:
        lines = [*OLD_HEADER, self.row("first", self.missing), self.row("second", self.missing)]
        _ = unit_membership("show", "second-unit")
        first = unit_membership("show", "first-unit")
        _ = staff.set_worktree(first.id, staff.RemovedWorktree("natedev", self.missing), expected=first.worktree)
        with (patch.object(unit_lookup, "marked_units", side_effect=OSError("herdr could not list sessions")) as asked,
              patch.object(staff, "snapshot", wraps=staff.snapshot) as reads):
            self.assertEqual(retired_units(lines, "show"), {"first-unit"})
        asked.assert_not_called()
        reads.assert_called_once_with()

    def test_every_list_drops_a_unit_that_is_gone(self) -> None:
        _ = unit_membership("show", "parked-unit")
        for name in ("marked-unit", "gone-unit"):
            row = unit_membership("show", name)
            _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", self.missing), expected=row.worktree)
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
