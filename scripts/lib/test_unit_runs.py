"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, showrunner_relationship
if TYPE_CHECKING:
    from scripts.message import sessions
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    import sessions


class UnitRunsTest(StaffTestCase):
    def staff_owner(self, run: Path, session_id: str) -> None:
        parent = showrunner_relationship("runs-fixture")
        owner = fixture_member("unit", name="owner-" + session_id[:8], parent=parent.id, runtime_id=session_id)
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({"production": parent.name, "unit": owner.name}))

    def test_both_roots_are_read_the_new_name_first(self) -> None:
        self.assertEqual(unit_runs.run_roots(), (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate")))
        self.assertEqual(unit_runs.active_dirs(), (Path("/tmp/claude/unit/active"), Path("/tmp/claude/delegate/active")))

    def test_activate_moves_only_its_marker_to_the_current_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current, former = root / "unit", root / "delegate"
            old = former / "active"
            old.mkdir(parents=True)
            _ = (old / "moving-session").write_text("old run\n")
            _ = (old / "other-session").write_text("keep other run\n")
            run = current / "private-run"
            with patch.object(unit_runs, "RUN_ROOT", current), patch.object(unit_runs, "FORMER_RUN_ROOT", former):
                unit_runs.activate(run, "moving-session")
                self.assertEqual(unit_runs.marker("moving-session").read_text(), f"{run}\n")
                unit_runs.activate(run, "moving-session")
                self.assertEqual(unit_runs.marker("moving-session").read_text(), f"{run}\n")
            self.assertFalse((old / "moving-session").exists())
            self.assertEqual((old / "other-session").read_text(), "keep other run\n")

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


    def test_held_lists_the_marker_run_then_every_run_the_session_directs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, old, active = Path(folder) / "unit", Path(folder) / "delegate", Path(folder) / "active"
            for run in (new / "run-marked", new / "run-side", new / "run-other", old / "run-former"):
                run.mkdir(parents=True)
            active.mkdir()
            _ = (active / fixture_runtime_id("sid-me")).write_text(f"{new / 'run-marked'}\n")
            # Staff identifies the owner. The marker places the run first.
            self.staff_owner(new / "run-marked", fixture_runtime_id("sid-me"))
            self.staff_owner(new / "run-side", fixture_runtime_id("sid-me"))
            self.staff_owner(new / "run-other", fixture_runtime_id("sid-other"))
            self.staff_owner(old / "run-former", fixture_runtime_id("sid-me"))
            with patch.object(unit_runs, "RUN_ROOT", new), patch.object(unit_runs, "FORMER_RUN_ROOT", old):
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), active_override=active), [
                    str(new / "run-marked"), str(new / "run-side"), str(old / "run-former")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-other"), active_override=active), [str(new / "run-other")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-none"), active_override=active), [])
                self.assertEqual(unit_runs.held("", active_override=active), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), new, active), [str(new / "run-marked"), str(new / "run-side")])
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(unit_runs.main(["held", fixture_runtime_id("sid-me"), "--root", str(new), "--active", str(active)]), 0)
            self.assertEqual(shown.getvalue().splitlines(), [str(new / "run-marked"), str(new / "run-side")])

    def test_shared_scan_keeps_a_late_marker_first_without_rereading_directors(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, active = Path(folder) / "runs", Path(folder) / "active"
            active.mkdir()
            for name in ("a-run", "z-run", "other-run"):
                run = root / name
                run.mkdir(parents=True)
                self.staff_owner(run, fixture_runtime_id("other") if name == "other-run" else fixture_runtime_id("director"))
            _ = (active / fixture_runtime_id("director")).write_text(str(root / "z-run") + "\n")
            _ = (active / fixture_runtime_id("other")).write_text(str(root / "other-run") + "\n")
            with patch.object(unit_runs, "director", wraps=unit_runs.director) as read:
                scanned = unit_runs.held_by_session(root)
                self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root, active, scanned=scanned),
                                 [str(root / "z-run"), str(root / "a-run")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("other"), root, active, scanned=scanned), [str(root / "other-run")])
                self.assertEqual(read.call_count, 3)
                self.assertEqual({call.args[0] for call in read.call_args_list}, set(root.iterdir()))

    def test_marker_outside_the_scan_is_verified_and_keeps_its_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, active, outside = Path(folder) / "runs", Path(folder) / "active", Path(folder) / "outside"
            (root / "a-run").mkdir(parents=True)
            outside.mkdir()
            active.mkdir()
            self.staff_owner(root / "a-run", fixture_runtime_id("director"))
            self.staff_owner(outside, fixture_runtime_id("director"))
            target = str(outside) + "/."
            _ = (active / fixture_runtime_id("director")).write_text(target + "\n")
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root, active, scanned=scanned),
                             [target, str(root / "a-run")])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root, active), [target, str(root / "a-run")])

    def test_a_stale_marker_does_not_assign_another_directors_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, active = Path(folder) / "runs", Path(folder) / "active"
            run = root / "run"
            run.mkdir(parents=True)
            active.mkdir()
            self.staff_owner(run, fixture_runtime_id("replacement"))
            _ = (active / fixture_runtime_id("original")).write_text(str(run) + "\n")
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("original"), root, active, scanned=scanned), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("original"), root, active), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("replacement"), root, active, scanned=scanned), [str(run)])

    def test_a_foreign_outside_marker_does_not_change_another_directors_shared_list(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, active, outside = Path(folder) / "runs", Path(folder) / "active", Path(folder) / "outside"
            root.mkdir()
            active.mkdir()
            outside.mkdir()
            self.staff_owner(outside, fixture_runtime_id("director-b"))
            _ = (active / fixture_runtime_id("director-a")).write_text(str(outside) + "\n")
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-a"), root, active, scanned=scanned), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-a"), root, active), [])
            independent = unit_runs.held(fixture_runtime_id("director-b"), root, active)
            self.assertEqual(independent, [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-b"), root, active, scanned=scanned), independent)

    def test_held_without_a_marker_lists_only_the_directed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runs, active = Path(folder) / "runs", Path(folder) / "active"
            (runs / "run-side").mkdir(parents=True)
            self.staff_owner(runs / "run-side", fixture_runtime_id("sid-me"))
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), runs, active), [str(runs / "run-side")])
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), Path(folder) / "missing", active), [])

    def test_the_removal_check_refuses_while_a_running_session_owns_a_former_marker(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            former = Path(folder) / "delegate"
            (former / "active").mkdir(parents=True)
            _ = (former / "active/sid-open").write_text(f"{former / 'run-open'}\n")
            _ = (former / "active/sid-ended").write_text(f"{former / 'run-ended'}\n")
            with patch.object(unit_runs, "FORMER_RUN_ROOT", former), \
                    contextlib.redirect_stdout(io.StringIO()) as shown, \
                    patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
                self.assertEqual(unit_runs.removal_check({"sid-open": "two-accounts", fixture_runtime_id("sid-other"): "elsewhere"}), 1)
                self.assertEqual(unit_runs.removal_check({fixture_runtime_id("sid-other"): "elsewhere"}), 0)
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
    import unittest

    _ = unittest.main()
