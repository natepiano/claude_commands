"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.lib import unit_runs  # noqa: E402
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, showrunner_relationship
from scripts.message import sessions


class UnitRunsTest(StaffTestCase):
    def test_showrunner_run_resolves_both_session_and_fixed_owner_records(self) -> None:
        owner = fixture_member("showrunner", name="production-owner")
        run = self.root / "production-run"
        run.mkdir()
        record = staff.snapshot()
        states = (
            {"main_agent": {"session_id": owner.current_session}},
            {unit_runs.OWNER_FIELD: owner.id},
        )
        for state in states:
            with self.subTest(state=state):
                self.write_json(run / unit_runs.STATE_FILE, state)
                self.assertEqual(unit_runs.director(run, record=record), owner.current_session)

    def test_anomaly_prints_once_across_both_import_names(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        imports = ("from scripts.lib import unit_runs", "import scripts.lib.unit_runs as packaged")
        for statements in (imports, tuple(reversed(imports))):
            with self.subTest(names=statements):
                command = ("; ".join(statements) + "; assert unit_runs is packaged; "
                           + "[module.report_anomaly('anomaly: import test') for module in (unit_runs, packaged)]")
                result = subprocess.run([str(repository / "scripts/lib/py"), "-c", command], cwd=self.root,
                                        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr.splitlines(), ["anomaly: import test"])

    def test_director_prints_each_anomaly_once_but_keeps_changed_causes(self) -> None:
        unit_runs.report_anomaly.cache_clear()
        self.addCleanup(unit_runs.report_anomaly.cache_clear)
        _ = fixture_member("showrunner", name="anomaly-fixture")
        first, second = self.root / "first", self.root / "second"
        for run in (first, second):
            run.mkdir()
            write_text_atomic(run / unit_runs.STATE_FILE, "{}")
            write_text_atomic(run / "fixture-impl_status", "running\n")
        record = staff.snapshot()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            for run in (first, second, first, second):
                self.assertEqual(unit_runs.director(run, record=record), "")
            write_text_atomic(first / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: "gone"}))
            self.assertEqual(unit_runs.director(first, record=record), "")
        self.assertEqual(errors.getvalue().splitlines(), [
            f"anomaly: {first}: this run has no owner because {unit_runs.STATE_FILE} names no staff row",
            f"anomaly: {second}: this run has no owner because {unit_runs.STATE_FILE} names no staff row",
            f"anomaly: {first}: run owner gone: unit staff row is unavailable"])








    def staff_owner(self, run: Path, session_id: str) -> None:
        parent = showrunner_relationship("runs-fixture")
        owner = fixture_member("unit", name="owner-" + session_id[:8], parent=parent.id, runtime_id=session_id)
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({"production": parent.name, "unit": owner.name, "owner_staff_id": owner.id}))

    def test_owner_scans_are_quiet_for_ended_runs_and_runs_without_resumable_workers(self) -> None:
        owner = fixture_member("showrunner", name="quiet-scan-fixture")
        record = staff.StaffSnapshot((owner,))
        session = fixture_runtime_id("quiet-runs")
        for status in ("ended", "active"):
            with self.subTest(status=status):
                root = self.root / status
                runs, active = root / "runs", root / "active"
                run = runs / "run"
                run.mkdir(parents=True)
                active.mkdir()
                write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({"status": status}))
                if status == "ended":
                    write_text_atomic(run / "impl_status_fixture", "implementing\n")
                else:
                    phase = run / "phases/1"
                    phase.mkdir(parents=True)
                    write_text_atomic(phase / "fixture-impl_status", "idle\n")
                write_text_atomic(active / session, str(run))
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    scanned = unit_runs.held_by_session(runs, record=record)
                    self.assertNotIn(session, scanned)
                    self.assertEqual(unit_runs.held(session, runs, active, scanned=scanned, record=record), [])
                self.assertEqual(errors.getvalue(), "")

    def test_director_without_progress_state_has_no_owner_or_output(self) -> None:
        owner = fixture_member("showrunner", name="no-state-production")
        run = self.root / "no-state-run"
        run.mkdir()
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(unit_runs.director(run, record=staff.StaffSnapshot((owner,))), "")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_held_keeps_a_herdr_run_with_an_idle_worker(self) -> None:
        session = fixture_runtime_id("idle-herdr-owner")
        runs, active = self.root / "runs", self.root / "active"
        run = runs / "herdr"
        phase = run / "phases/1"
        phase.mkdir(parents=True)
        self.staff_owner(run, session)
        _ = (phase / "fixture-impl_status").write_text("idle\n")
        self.assertEqual(unit_runs.held(session, runs, active), [str(run)])

    def test_director_reports_a_missing_owner_for_active_pane_workers(self) -> None:
        for folder in ("", "phases/1"):
            with self.subTest(folder=folder):
                run = self.root / ("phase" if folder else "root")
                directory = run / folder
                directory.mkdir(parents=True)
                write_text_atomic(run / unit_runs.STATE_FILE, '{"status":"active"}')
                write_text_atomic(directory / "fixture-impl_status", "running\n")
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(unit_runs.director(run), "")
                self.assertIn(f"anomaly: {run}:", errors.getvalue())

    def test_director_uses_the_recorder_for_an_idle_worker_with_an_active_dispatch(self) -> None:
        from scripts.direct import progress_history

        owner = fixture_member("showrunner", name="dispatch-owner")
        worker = fixture_worker("fixture-impl", owner.id)
        run = self.root / "dispatch"
        run.mkdir()
        _ = (run / unit_runs.STATE_FILE).write_text('{"status":"active"}')
        _ = (run / f"{worker.name}_status").write_text("idle\n")
        with (patch.object(progress_history, "active_dispatch",
                           return_value=progress_history.ActiveDispatch("instance", ("fixture",))) as read,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(unit_runs.director(run), "")
        read.assert_called_once_with(worker.id, run)
        self.assertIn(f"anomaly: {run}:", errors.getvalue())

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
                self.assertEqual((run / unit_runs.ACTIVE_MARKER_FILE).read_text(), f"{current / 'active/moving-session'}\n")
                unit_runs.activate(run, "moving-session")
                self.assertEqual(unit_runs.marker("moving-session").read_text(), f"{run}\n")
            self.assertFalse((old / "moving-session").exists())
            self.assertEqual((old / "other-session").read_text(), "keep other run\n")

    def test_end_removes_only_the_recorded_runs_marker_and_can_repeat(self) -> None:
        for target in ("this run", "another run", "empty", "missing"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run, other, active = root / "run", root / "other", root / "active"
                unit_runs.activate(run, "director", active)
                path = active / "director"
                if target == "another run":
                    _ = path.write_text(f"{other}\n")
                elif target == "empty":
                    _ = path.write_text("")
                elif target == "missing":
                    path.unlink()
                expected = {"director": f"{other}\n"} if target == "another run" else {"director": ""} if target == "empty" else {}
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        unit_runs.end(run)
                        self.assertEqual({entry.name: entry.read_text() for entry in active.iterdir()}, expected)

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

    def test_held_uses_the_override_roots_active_marker(self) -> None:
        root = self.root / "override"
        active = root / "active"
        active.mkdir(parents=True)
        session = fixture_runtime_id("director")
        for name in ("a-side", "z-marked"):
            run = root / name
            run.mkdir()
            self.staff_owner(run, session)
        with (active / session).open("w") as stream:
            stream.writelines([str(root / "z-marked") + "\n"])
        self.assertEqual(unit_runs.held(session, root), [str(root / "z-marked"), str(root / "a-side")])

    def test_held_scan_excludes_marker_folders_from_both_default_roots(self) -> None:
        current, former = self.root / "unit", self.root / "delegate"
        session = fixture_runtime_id("director")
        for root in (current, former):
            (root / "active").mkdir(parents=True)
            for name in ("z-run", "a-run"):
                run = root / name
                run.mkdir()
                self.staff_owner(run, session)
        (former / "duplicate").symlink_to(current / "a-run", target_is_directory=True)
        before = [list((root / "active").iterdir()) for root in (current, former)]
        with patch.object(unit_runs, "RUN_ROOT", current), patch.object(unit_runs, "FORMER_RUN_ROOT", former):
            found = unit_runs.held_by_session()
        self.assertEqual(found, {session: [str(current / "a-run"), str(current / "z-run"),
                                          str(former / "a-run"), str(former / "z-run")]})
        self.assertEqual([list((root / "active").iterdir()) for root in (current, former)], before)

    def test_held_scan_excludes_the_override_roots_marker_folder(self) -> None:
        root = self.root / "override"
        (root / "active").mkdir(parents=True)
        run = root / "run"
        run.mkdir()
        session = fixture_runtime_id("director")
        self.staff_owner(run, session)
        before = list((root / "active").iterdir())
        self.assertEqual(unit_runs.held_by_session(root), {session: [str(run)]})
        self.assertEqual(list((root / "active").iterdir()), before)

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

    def test_shared_scan_omits_folders_without_run_state(self) -> None:
        root = self.root / "runs"
        run = root / "recorded"
        run.mkdir(parents=True)
        self.staff_owner(run, fixture_runtime_id("director"))
        (root / "active").mkdir()
        (root / "solo").mkdir()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            scanned = unit_runs.held_by_session(root)
        self.assertEqual(scanned, {fixture_runtime_id("director"): [str(run)]})
        self.assertEqual(errors.getvalue(), "")

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
