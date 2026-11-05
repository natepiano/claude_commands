"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import atomic_file, unit_runs  # noqa: E402
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, showrunner_relationship
if TYPE_CHECKING:
    from scripts.message import sessions
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    from scripts.message import sessions


class RunOwnerMigrationTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        lock = fcntl.flock

        def fail_on_lock_wait(descriptor: int | IO[str], operation: int) -> None:
            try:
                lock(descriptor, operation | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AssertionError(f"run-owner migration test would block on a file lock: {descriptor}") from error

        _ = self.enterContext(patch.object(fcntl, "flock", side_effect=fail_on_lock_wait))
        super().setUp()

    def test_interrupted_owner_migration_repeats_after_clear_without_changing_history_or_finished_runs(self) -> None:
        owner = fixture_member("showrunner", name="production")
        row = fixture_member("unit", name="tools", parent=owner.id)
        runs = self.root / "runs"
        history = self.root / "history.jsonl"
        _ = history.write_text('{"production":"former","unit":"tools"}\n')
        for name, status in (("a", "active"), ("b", "active"), ("old", "completed")):
            run = runs / name
            run.mkdir(parents=True)
            _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({
                "status": status, "production": "production", "unit": "tools",
                "main_agent": {"session_id": row.current_session}, "history_file": str(history)}))
        finished = runs / "old" / unit_runs.STATE_FILE
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (history, finished)]
        publish = atomic_file.write_text_atomic
        changed_session = fixture_runtime_id("run-owner-clear")
        interrupted_once = False

        def interrupted(path: Path, text: str) -> None:
            nonlocal interrupted_once
            publish(path, text)
            if path != runs / "a" / unit_runs.STATE_FILE or interrupted_once:
                return
            interrupted_once = True
            _ = staff.update_current_session(row.id, staff.StaffId(changed_session))
            raise RuntimeError("migration interrupted")

        with (patch.object(atomic_file, "write_text_atomic", side_effect=interrupted),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            with self.assertRaisesRegex(RuntimeError, "migration interrupted"):
                _ = unit_runs.migrate_owners(runs)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(unit_runs.migrate_owners(runs), 0)
        paths = [runs / name / unit_runs.STATE_FILE for name in ("a", "b")]
        for path in paths:
            fields = cast(dict[str, object], json.loads(path.read_text()))
            self.assertEqual(fields["owner_staff_id"], row.id)
            self.assertNotIn("production", fields)
            self.assertNotIn("unit", fields)
        published = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(unit_runs.migrate_owners(runs), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], published)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (history, finished)], before)

    def test_unproved_owner_keeps_the_active_run_and_reports_unavailable(self) -> None:
        _ = fixture_member("showrunner", name="production")
        run = self.root / "runs/unknown"
        run.mkdir(parents=True)
        path = run / unit_runs.STATE_FILE
        _ = path.write_text('{"status":"active","main_agent":{"session_id":"gone"}}')
        before = path.read_bytes(), path.stat().st_mtime_ns
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(unit_runs.migrate_owners(run.parent), 1)
        self.assertIn("run owner is unavailable", errors.getvalue())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


class UnitRunsTest(StaffTestCase):
    def staff_owner(self, run: Path, session_id: str) -> None:
        parent = showrunner_relationship("runs-fixture")
        owner = fixture_member("unit", name="owner-" + session_id[:8], parent=parent.id, runtime_id=session_id)
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({"production": parent.name, "unit": owner.name}))

    def test_director_without_progress_state_resolves_roster_parent_without_output(self) -> None:
        production = fixture_member("showrunner", name="roster-production")
        owner = fixture_member("unit", name="roster-unit", parent=production.id)
        worker = fixture_worker("impl", owner.id)
        run = self.root / "roster-run"
        run.mkdir()
        _ = (run / "mesh_roster.json").write_text(json.dumps({"impl": {"staff_id": worker.id}}))
        self.assertFalse((run / unit_runs.STATE_FILE).exists())
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(unit_runs.director(run), owner.current_session)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_director_reports_existing_invalid_or_unreadable_progress_state(self) -> None:
        _ = fixture_member("showrunner", name="state-diagnostics")
        for kind in ("invalid", "unreadable"):
            with self.subTest(kind=kind):
                run = self.root / kind
                run.mkdir()
                path = run / unit_runs.STATE_FILE
                if kind == "invalid":
                    _ = path.write_text("{")
                else:
                    path.mkdir()
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    _ = unit_runs.director(run)
                self.assertIn("anomaly:", errors.getvalue())
                self.assertIn(str(path), errors.getvalue())

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
