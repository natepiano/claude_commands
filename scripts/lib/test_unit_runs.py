"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402
if TYPE_CHECKING:
    from scripts.message import sessions
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    import sessions


class UnitRunsTest(unittest.TestCase):
    def test_both_roots_are_read_the_new_name_first(self) -> None:
        self.assertEqual(unit_runs.run_roots(), (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate")))
        self.assertEqual(unit_runs.active_dirs(), (Path("/tmp/claude/unit/active"), Path("/tmp/claude/delegate/active")))

    def test_an_override_replaces_both_names(self) -> None:
        self.assertEqual(unit_runs.run_roots("/x/runs"), (Path("/x/runs"),))
        self.assertEqual(unit_runs.active_dirs("/x/active"), (Path("/x/active"),))

    def test_the_marker_is_the_first_that_exists_else_the_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, old = Path(folder) / "unit", Path(folder) / "delegate"
            with patch.object(unit_runs, "RUN_ROOT", new), patch.object(unit_runs, "FORMER_RUN_ROOT", old):
                self.assertEqual(unit_runs.marker("s1"), new / "active/s1")
                (old / "active").mkdir(parents=True)
                _ = (old / "active/s1").write_text("run\n")
                self.assertEqual(unit_runs.marker("s1"), old / "active/s1")
                (new / "active").mkdir(parents=True)
                _ = (new / "active/s1").write_text("run\n")
                self.assertEqual(unit_runs.marker("s1"), new / "active/s1")

    def test_live_marker_is_kept_and_never_offered_for_former_folder_removal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            former = Path(folder) / "delegate"
            marker = former / "active" / "fixture-live"
            marker.parent.mkdir(parents=True)
            _ = marker.write_text("/missing/run\n")
            with (patch.object(unit_runs, "FORMER_RUN_ROOT", former),
                  patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(123)) as check,
                  contextlib.redirect_stdout(io.StringIO()) as shown):
                self.assertEqual(unit_runs.removal_check({}), 1)
            self.assertEqual(marker.read_text(), "/missing/run\n")
            self.assertNotIn("can go", shown.getvalue())
            self.assertEqual(check.call_args.args, ("fixture-live",))

    def test_gone_marker_can_be_offered_but_unknown_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            former = Path(folder) / "delegate"
            marker = former / "active" / "fixture-gone"
            marker.parent.mkdir(parents=True)
            _ = marker.write_text("/missing/run\n")
            with (patch.object(unit_runs, "FORMER_RUN_ROOT", former),
                  patch.object(sessions, "session_liveness", return_value=sessions.SessionLivenessUnknown("unreadable")),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(unit_runs.removal_check({}), 1)
            with (patch.object(unit_runs, "FORMER_RUN_ROOT", former),
                  patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                  contextlib.redirect_stdout(io.StringIO()) as shown):
                self.assertEqual(unit_runs.removal_check({}), 0)
            self.assertIn("can go", shown.getvalue())
            self.assertTrue(marker.exists(), "removal-check only judges; it never unlinks")

    def test_the_recorded_director_wins_over_the_session_that_started_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run, history = Path(folder) / "run", Path(folder) / "history.jsonl"
            run.mkdir()
            self.assertEqual(unit_runs.director(run), "")
            _ = history.write_text(json.dumps({"event_type": "run_started", "main_agent": {"session_id": "sid-start"}})
                                   + "\n" + json.dumps({"event_type": "phase_started"}) + "\n")
            # A pass opened inside a Codex worker rewrote the state's identity with its own thread.
            _ = (run / unit_runs.STATE_FILE).write_text(json.dumps(
                {"history_file": str(history), "main_agent": {"session_id": "thread-worker"}}))
            self.assertEqual(unit_runs.director(run), "sid-start")
            unit_runs.record_director(run, "sid-now")
            self.assertEqual(unit_runs.director(run), "sid-now")
            self.assertEqual(sorted(path.name for path in run.iterdir()), [unit_runs.DIRECTOR_FILE, unit_runs.STATE_FILE])

    def test_a_history_that_does_not_open_with_the_start_names_no_director(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run, history = Path(folder) / "run", Path(folder) / "history.jsonl"
            run.mkdir()
            _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({"history_file": str(history)}))
            self.assertEqual(unit_runs.director(run), "")
            _ = history.write_text(json.dumps({"event_type": "phase_started", "main_agent": {"session_id": "sid"}}) + "\n")
            self.assertEqual(unit_runs.director(run), "")
            _ = (run / unit_runs.STATE_FILE).write_text("not json")
            self.assertEqual(unit_runs.director(run), "")

    def test_a_claude_worker_of_the_run_is_never_recorded_as_its_director(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            unit_runs.record_director(run, "sid-director")
            _ = (run / "impl_worker_impl.json").write_text(json.dumps(
                {"slot": "impl", "family": "claude", "worker_id": "sid-claude-impl"}))
            _ = (run / "impl_worker_fix.json").write_text(json.dumps(
                {"slot": "fix", "family": "codex", "worker_id": "sid-codex-shaped"}))
            _ = (run / "workers").write_text("sid-clau\tproj-test\n")
            for worker in ("sid-claude-impl", "sid-claude-test"):
                unit_runs.record_director(run, worker)
                self.assertEqual(unit_runs.director(run), "sid-director")
            unit_runs.record_director(run, "sid-codex-shaped")
            self.assertEqual(unit_runs.director(run), "sid-codex-shaped")

    def test_record_director_names_the_shell_session_and_skips_without_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
                self.assertEqual(unit_runs.main(["record-director", str(run)]), 0)
            self.assertFalse((run / unit_runs.DIRECTOR_FILE).exists())
            with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-shell"}):
                self.assertEqual(unit_runs.main(["record-director", str(run)]), 0)
                self.assertEqual(unit_runs.main(["record-director", str(run / "missing")]), 0)
            self.assertEqual((run / unit_runs.DIRECTOR_FILE).read_text(encoding="utf-8"), "sid-shell\n")
            self.assertFalse((run / "missing").exists())

    def test_held_lists_the_marker_run_then_every_run_the_session_directs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, old, active = Path(folder) / "unit", Path(folder) / "delegate", Path(folder) / "active"
            for run in (new / "run-marked", new / "run-side", new / "run-other", old / "run-former"):
                run.mkdir(parents=True)
            active.mkdir()
            _ = (active / "sid-me").write_text(f"{new / 'run-marked'}\n")
            # The marker's run also records its director; it is listed once, first.
            unit_runs.record_director(new / "run-marked", "sid-me")
            unit_runs.record_director(new / "run-side", "sid-me")
            unit_runs.record_director(new / "run-other", "sid-other")
            unit_runs.record_director(old / "run-former", "sid-me")
            with patch.object(unit_runs, "RUN_ROOT", new), patch.object(unit_runs, "FORMER_RUN_ROOT", old):
                self.assertEqual(unit_runs.held("sid-me", active_override=active), [
                    str(new / "run-marked"), str(new / "run-side"), str(old / "run-former")])
                self.assertEqual(unit_runs.held("sid-other", active_override=active), [str(new / "run-other")])
                self.assertEqual(unit_runs.held("sid-none", active_override=active), [])
                self.assertEqual(unit_runs.held("", active_override=active), [])
            self.assertEqual(unit_runs.held("sid-me", new, active), [str(new / "run-marked"), str(new / "run-side")])
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(unit_runs.main(["held", "sid-me", "--root", str(new), "--active", str(active)]), 0)
            self.assertEqual(shown.getvalue().splitlines(), [str(new / "run-marked"), str(new / "run-side")])

    def test_held_without_a_marker_lists_only_the_directed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runs, active = Path(folder) / "runs", Path(folder) / "active"
            (runs / "run-side").mkdir(parents=True)
            unit_runs.record_director(runs / "run-side", "sid-me")
            self.assertEqual(unit_runs.held("sid-me", runs, active), [str(runs / "run-side")])
            self.assertEqual(unit_runs.held("sid-me", Path(folder) / "missing", active), [])

    def test_the_removal_check_refuses_while_a_running_session_owns_a_former_marker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            former = Path(folder) / "delegate"
            (former / "active").mkdir(parents=True)
            _ = (former / "active/sid-open").write_text(f"{former / 'run-open'}\n")
            _ = (former / "active/sid-ended").write_text(f"{former / 'run-ended'}\n")
            with patch.object(unit_runs, "FORMER_RUN_ROOT", former), \
                    contextlib.redirect_stdout(io.StringIO()) as shown, \
                    patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
                self.assertEqual(unit_runs.removal_check({"sid-open": "two-accounts", "sid-other": "elsewhere"}), 1)
                self.assertEqual(unit_runs.removal_check({"sid-other": "elsewhere"}), 0)
            self.assertIn(f"two-accounts (sid-open): {former / 'run-open'}", shown.getvalue())
            self.assertNotIn("sid-ended", shown.getvalue())
            self.assertIn("the former name can go", shown.getvalue())

    def test_the_history_root_is_the_override_else_the_new_name_once_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            new, old = state / "unit-history", state / "plan-delegate"
            with patch.object(unit_runs, "state_dir", lambda: state), patch.dict(os.environ, {"UNIT_HISTORY_DIR": ""}):
                self.assertEqual(unit_runs.history_root(), new)
                old.mkdir()
                self.assertEqual(unit_runs.history_root(), old)
                new.mkdir()
                self.assertEqual(unit_runs.history_root(), new)
                with patch.dict(os.environ, {"UNIT_HISTORY_DIR": f"{folder}/scratch"}):
                    self.assertEqual(unit_runs.history_root(), state / "scratch")

    def test_a_run_variable_is_its_unit_name_else_its_name_before_the_rename(self) -> None:
        with patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": "/runs/former", "PLAN_DELEGATE_TEAM_ROLE": "impl",
                                     "UNIT_TEAM_ROLE": "", "UNIT_RUN_DIR": ""}):
            self.assertEqual(unit_runs.environ("RUN_DIR"), "/runs/former")
            self.assertEqual(unit_runs.environ("TEAM_ROLE"), "impl")
            self.assertEqual(unit_runs.environ("SESSION_DIR"), "")
            with patch.dict(os.environ, {"UNIT_RUN_DIR": "/runs/new"}):
                self.assertEqual(unit_runs.environ("RUN_DIR"), "/runs/new")

    def test_move_history_waits_for_live_runs_then_moves_both_folders_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            new, old, runs = state / "unit-history", state / "plan-delegate", state / "unit"
            (old / "runs").mkdir(parents=True)
            _ = (old / "runs/run-1.jsonl").write_text("{}\n")
            (state / "usage-model/delegate-archive").mkdir(parents=True)
            (runs / "run-1").mkdir(parents=True)
            beat = runs / "run-1" / unit_runs.HEARTBEAT
            _ = beat.write_text("beat\n")
            now = beat.stat().st_mtime
            later = now + unit_runs.LIVE_HEARTBEAT_SECS
            with patch.object(unit_runs, "RUN_ROOT", runs), patch.object(unit_runs, "FORMER_RUN_ROOT", state / "none"), \
                    contextlib.redirect_stdout(io.StringIO()) as shown, contextlib.redirect_stderr(io.StringIO()) as refused:
                self.assertEqual(unit_runs.move_history(now, state), 1)
                self.assertTrue(old.is_dir())
                self.assertIn(str(runs / "run-1"), refused.getvalue())
                self.assertEqual(unit_runs.move_history(later, state), 0)
                self.assertEqual((new / "runs/run-1.jsonl").read_text(), "{}\n")
                self.assertFalse(old.exists())
                self.assertTrue((state / "usage-model/unit-archive").is_dir())
                self.assertFalse((state / "usage-model/delegate-archive").exists())
                self.assertEqual(unit_runs.move_history(later, state), 0)
            self.assertIn("nothing to move", shown.getvalue())

if __name__ == "__main__":
    _ = unittest.main()
