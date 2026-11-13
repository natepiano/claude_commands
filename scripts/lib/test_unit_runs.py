"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from dataclasses import replace
from typing import override
from unittest.mock import patch

from scripts.lib import unit_runs  # noqa: E402
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.unit_runs_test_support import bind_private_run_roots
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, showrunner_relationship


class UnitRunsTest(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        bind_private_run_roots(self, self.root)

    def set_open_run(self, session: str, path: Path) -> None:
        row = staff.by_current_session(session)
        if row is None:
            row = fixture_member("showrunner", name=f"owner-{session}", runtime_id=session)
        _ = staff.set_run(row.id, staff.OpenRun(path))

    def test_open_run_reads_the_current_session_and_supplied_record(self) -> None:
        owner = fixture_member("showrunner", name="open-owner")
        path = self.root / "open-run"
        self.assertIsNone(unit_runs.open_run(owner.current_session))
        _ = staff.set_run(owner.id, staff.OpenRun(path))
        record = staff.local_snapshot()
        current = staff.native_id(fixture_runtime_id("open-current"))
        _ = staff.update_current_session(owner.current_session, current)
        self.assertEqual(unit_runs.open_run(current), path)
        self.assertIsNone(unit_runs.open_run(owner.current_session))
        self.assertEqual(unit_runs.open_run(owner.current_session, record=record), path)

        path.mkdir()
        with patch.object(unit_runs, "RUN_ROOT", self.root):
            stale = replace(record, rows=tuple(replace(row, run=staff.NoRun()) for row in record.rows))
            self.write_json(path / unit_runs.STATE_FILE, {unit_runs.OWNER_FIELD: owner.id})
            with patch.object(staff, "local_snapshot", return_value=stale):
                self.assertFalse(unit_runs.run_is_open(path))
                self.assertTrue(unit_runs.run_is_open(path, record=record))
            for fields, pointer, expected in (
                ({unit_runs.OWNER_FIELD: owner.id}, staff.OpenRun(path), True),
                ({"main_agent": {"session_id": "unrelated-saved-session"}}, staff.OpenRun(path), True),
                ({"main_agent": {"session_id": owner.current_session}}, staff.NoRun(), False),
                ({unit_runs.OWNER_FIELD: owner.id, "status": "active"}, staff.NoRun(), False),
                ({unit_runs.OWNER_FIELD: owner.id, "status": "completed"}, staff.OpenRun(path), True),
            ):
                with self.subTest(fields=fields, pointer=pointer):
                    self.write_json(path / unit_runs.STATE_FILE, fields)
                    if isinstance(pointer, staff.OpenRun):
                        _ = staff.set_run(owner.id, pointer)
                    else:
                        _ = staff.clear_run(owner.id, path)
                    self.assertEqual(unit_runs.run_is_open(path), expected)

    def test_showrunner_run_uses_only_fixed_owner_records(self) -> None:
        owner = fixture_member("showrunner", name="production-owner")
        run = self.root / "production-run"
        run.mkdir()
        record = staff.snapshot()
        states = (
            ({"main_agent": {"session_id": owner.current_session}}, ""),
            ({unit_runs.OWNER_FIELD: owner.id}, owner.current_session),
        )
        for state, expected in states:
            with self.subTest(state=state):
                self.write_json(run / unit_runs.STATE_FILE, state)
                self.assertEqual(unit_runs.director(run, record=record), expected)

        from scripts.astra import design_roles

        for root in (unit_runs.ASTRA_RUN_ROOT, design_roles.state_root(), self.root / "other-helpers"):
            helper = root / "helper"
            helper.mkdir(parents=True, exist_ok=True)
            for fields, expected in (({}, True), ({unit_runs.OWNER_FIELD: owner.id}, True),
                                     ({"status": "completed", "finished_at": 1}, False)):
                with self.subTest(helper=helper, fields=fields):
                    self.write_json(helper / unit_runs.STATE_FILE, fields)
                    self.assertEqual(unit_runs.run_is_open(helper), expected)

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
                runs = root / "runs"
                run = runs / "run"
                run.mkdir(parents=True)
                write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({"status": status}))
                if status == "ended":
                    write_text_atomic(run / "impl_status_fixture", "implementing\n")
                else:
                    phase = run / "phases/1"
                    phase.mkdir(parents=True)
                    write_text_atomic(phase / "fixture-impl_status", "idle\n")
                record = staff.StaffSnapshot((replace(owner, current_session=staff.StaffId(session), run=staff.OpenRun(run)),))
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    scanned = unit_runs.held_by_session(runs, record=record)
                    self.assertNotIn(session, scanned)
                    self.assertEqual(unit_runs.held(session, runs, scanned=scanned, record=record), [])
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
        runs = self.root / "runs"
        run = runs / "herdr"
        phase = run / "phases/1"
        phase.mkdir(parents=True)
        self.staff_owner(run, session)
        _ = (phase / "fixture-impl_status").write_text("idle\n")
        self.assertEqual(unit_runs.held(session, runs), [str(run)])

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
        repository = Path(__file__).resolve().parents[2]
        environment = {name: value for name, value in os.environ.items()
                       if name not in {"SCRATCH_ROOT", "ASTRA_REVIEW_DIR"}}
        result = subprocess.run(
            [str(repository / "scripts/lib/py"), "-c",
             "from scripts.lib import unit_runs; print(*unit_runs.run_roots(), sep='\\n')"],
            env=environment, capture_output=True, text=True, check=True,
        )
        self.assertEqual(tuple(Path(value) for value in result.stdout.splitlines()),
                         (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate")))

    def test_an_override_replaces_both_names(self) -> None:
        self.assertEqual(unit_runs.run_roots("/x/runs"), (Path("/x/runs"),))

    def test_held_lists_the_open_run_then_every_run_the_session_directs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            new, old = Path(folder) / "unit", Path(folder) / "delegate"
            for run in (new / "run-marked", new / "run-side", new / "run-other", old / "run-former"):
                run.mkdir(parents=True)
            # Staff identifies the owner and places the open run first.
            self.staff_owner(new / "run-marked", fixture_runtime_id("sid-me"))
            self.staff_owner(new / "run-side", fixture_runtime_id("sid-me"))
            self.staff_owner(new / "run-other", fixture_runtime_id("sid-other"))
            self.staff_owner(old / "run-former", fixture_runtime_id("sid-me"))
            self.set_open_run(fixture_runtime_id("sid-me"), new / "run-marked")
            with patch.object(unit_runs, "RUN_ROOT", new), patch.object(unit_runs, "FORMER_RUN_ROOT", old):
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me")), [
                    str(new / "run-marked"), str(new / "run-side"), str(old / "run-former")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-other")), [str(new / "run-other")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("sid-none")), [])
                self.assertEqual(unit_runs.held(""), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), new), [str(new / "run-marked"), str(new / "run-side")])
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                self.assertEqual(unit_runs.main(["held", fixture_runtime_id("sid-me"), "--root", str(new)]), 0)
            self.assertEqual(shown.getvalue().splitlines(), [str(new / "run-marked"), str(new / "run-side")])

    def test_held_uses_the_staff_pointer_with_override_roots(self) -> None:
        root = self.root / "override"
        root.mkdir(parents=True)
        session = fixture_runtime_id("director")
        for name in ("a-side", "z-marked"):
            run = root / name
            run.mkdir()
            self.staff_owner(run, session)
        self.set_open_run(session, root / "z-marked")
        self.assertEqual(unit_runs.held(session, root), [str(root / "z-marked"), str(root / "a-side")])

    def test_held_scan_lists_unique_runs_from_both_default_roots(self) -> None:
        current, former = self.root / "unit", self.root / "delegate"
        session = fixture_runtime_id("director")
        for root in (current, former):
            root.mkdir(parents=True)
            for name in ("z-run", "a-run"):
                run = root / name
                run.mkdir()
                self.staff_owner(run, session)
        (former / "duplicate").symlink_to(current / "a-run", target_is_directory=True)
        with patch.object(unit_runs, "RUN_ROOT", current), patch.object(unit_runs, "FORMER_RUN_ROOT", former):
            found = unit_runs.held_by_session()
        self.assertEqual(found, {session: [str(current / "a-run"), str(current / "z-run"),
                                          str(former / "a-run"), str(former / "z-run")]})

    def test_shared_scan_keeps_the_open_run_first_without_rereading_directors(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runs"
            for name in ("a-run", "z-run", "other-run"):
                run = root / name
                run.mkdir(parents=True)
                self.staff_owner(run, fixture_runtime_id("other") if name == "other-run" else fixture_runtime_id("director"))
            self.set_open_run(fixture_runtime_id("director"), root / "z-run")
            self.set_open_run(fixture_runtime_id("other"), root / "other-run")
            with patch.object(unit_runs, "director", wraps=unit_runs.director) as read:
                scanned = unit_runs.held_by_session(root)
                self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root, scanned=scanned),
                                 [str(root / "z-run"), str(root / "a-run")])
                self.assertEqual(unit_runs.held(fixture_runtime_id("other"), root, scanned=scanned), [str(root / "other-run")])
                self.assertEqual(read.call_count, 3)
                self.assertEqual({call.args[0] for call in read.call_args_list}, set(root.iterdir()))

    def test_shared_scan_omits_folders_without_run_state(self) -> None:
        root = self.root / "runs"
        run = root / "recorded"
        run.mkdir(parents=True)
        self.staff_owner(run, fixture_runtime_id("director"))
        (root / "solo").mkdir()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            scanned = unit_runs.held_by_session(root)
        self.assertEqual(scanned, {fixture_runtime_id("director"): [str(run)]})
        self.assertEqual(errors.getvalue(), "")

    def test_open_run_outside_the_scan_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "runs", Path(folder) / "outside"
            (root / "a-run").mkdir(parents=True)
            outside.mkdir()
            self.staff_owner(root / "a-run", fixture_runtime_id("director"))
            self.staff_owner(outside, fixture_runtime_id("director"))
            target = str(outside / ".." / "outside")
            self.set_open_run(fixture_runtime_id("director"), Path(target))
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root, scanned=scanned),
                             [target, str(root / "a-run")])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director"), root), [target, str(root / "a-run")])

    def test_a_stale_pointer_does_not_assign_another_directors_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runs"
            run = root / "run"
            run.mkdir(parents=True)
            self.staff_owner(run, fixture_runtime_id("replacement"))
            self.set_open_run(fixture_runtime_id("original"), run)
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("original"), root, scanned=scanned), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("original"), root), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("replacement"), root, scanned=scanned), [str(run)])

    def test_a_foreign_outside_pointer_does_not_change_another_directors_shared_list(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root, outside = Path(folder) / "runs", Path(folder) / "outside"
            root.mkdir()
            outside.mkdir()
            self.staff_owner(outside, fixture_runtime_id("director-b"))
            self.set_open_run(fixture_runtime_id("director-a"), outside)
            scanned = unit_runs.held_by_session(root)
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-a"), root, scanned=scanned), [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-a"), root), [])
            independent = unit_runs.held(fixture_runtime_id("director-b"), root)
            self.assertEqual(independent, [])
            self.assertEqual(unit_runs.held(fixture_runtime_id("director-b"), root, scanned=scanned), independent)

    def test_held_without_a_pointer_lists_only_the_directed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runs = Path(folder) / "runs"
            (runs / "run-side").mkdir(parents=True)
            self.staff_owner(runs / "run-side", fixture_runtime_id("sid-me"))
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), runs), [str(runs / "run-side")])
            self.assertEqual(unit_runs.held(fixture_runtime_id("sid-me"), Path(folder) / "missing"), [])

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
