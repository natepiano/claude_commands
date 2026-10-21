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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402


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
                    contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(unit_runs.removal_check({"sid-open": "two-accounts", "sid-other": "elsewhere"}), 1)
                self.assertEqual(unit_runs.removal_check({"sid-other": "elsewhere"}), 0)
            self.assertIn(f"two-accounts (sid-open): {former / 'run-open'}", shown.getvalue())
            self.assertNotIn("sid-ended", shown.getvalue())
            self.assertIn("the former name can go", shown.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
