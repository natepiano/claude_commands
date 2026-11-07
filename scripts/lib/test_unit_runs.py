"""Run folders under the new name and the former one; nothing here touches /tmp/claude."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import unit_runs  # noqa: E402
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker, showrunner_relationship
if TYPE_CHECKING:
    from scripts.message import sessions
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    from scripts.message import sessions


class UnitRunsTest(StaffTestCase):
    def test_anomaly_prints_once_across_both_import_names(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repository / "scripts/lib"), str(repository)))}
        for names in (("unit_runs", "scripts.lib.unit_runs"), ("scripts.lib.unit_runs", "unit_runs")):
            with self.subTest(names=names):
                command = f"import importlib; [importlib.import_module(name).report_anomaly('anomaly: import test') for name in {names!r}]"
                result = subprocess.run([sys.executable, "-c", command], cwd=self.root, env=environment,
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
        record = staff.snapshot()
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            for run in (first, second, first, second):
                self.assertEqual(unit_runs.director(run, record=record), "")
            write_text_atomic(first / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: "gone"}))
            self.assertEqual(unit_runs.director(first, record=record), "")
        self.assertEqual(errors.getvalue().splitlines(), [
            f"anomaly: {first}: this run has no owner because {unit_runs.STATE_FILE} and mesh_roster.json name no unit, production or staff row",
            f"anomaly: {second}: this run has no owner because {unit_runs.STATE_FILE} and mesh_roster.json name no unit, production or staff row",
            f"anomaly: {first}: run owner gone: unit staff row is unavailable"])

    def test_parent_transport_reads_the_first_held_pin_without_writing(self) -> None:
        runs, config = self.root / "runs", self.root / "config"
        config.mkdir()
        session = fixture_runtime_id("transport-parent")
        first, second = runs / "a-first", runs / "b-second"
        first.mkdir(parents=True)
        second.mkdir()
        self.staff_owner(first, session)
        self.staff_owner(second, session)
        runner = fixture_member("showrunner", name="roster-parent")
        worker = fixture_worker("impl", runner.id)
        roster_run = runs / "c-roster"
        roster_run.mkdir()
        write_text_atomic(roster_run / "mesh_roster.json", json.dumps({"impl": {"staff_id": worker.id}}))
        with (patch.object(unit_runs, "RUN_ROOT", runs),
              patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former"),
              patch.dict(os.environ, {"CONF_DIR": str(config), "ROSTER_RUN_ROOT": str(runs)})):
            for transport in ("herdr", "mesh"):
                write_text_atomic(config / "direct.conf", f"# one of: mesh, herdr\nWORKER_TRANSPORT={transport}\n")
                before = sorted(path.relative_to(runs) for path in runs.rglob("*"))
                self.assertEqual(unit_runs.parent_transport(session), transport)
                self.assertEqual(unit_runs.parent_transport(runner.current_session), transport)
                self.assertEqual(unit_runs.parent_transport(fixture_runtime_id("no-held-run")), transport)
                self.assertEqual(sorted(path.relative_to(runs) for path in runs.rglob("*")), before)
            write_text_atomic(first / "worker_transport", "herdr\n")
            write_text_atomic(second / "worker_transport", "mesh\n")
            before_pin = (first / "worker_transport").read_bytes(), (first / "worker_transport").stat().st_mtime_ns
            self.assertEqual(unit_runs.parent_transport(session), "herdr")
            self.assertEqual(((first / "worker_transport").read_bytes(),
                              (first / "worker_transport").stat().st_mtime_ns), before_pin)
            unit_runs.activate(second, session)
            self.assertEqual(unit_runs.parent_transport(session), "mesh")
            (second / "worker_transport").unlink()
            self.assertEqual(unit_runs.parent_transport(session), "mesh")
            self.assertFalse((second / "worker_transport").exists())
            record = staff.snapshot()
            with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as reads:
                self.assertEqual(unit_runs.parent_transport(session, record=record), "mesh")
            self.assertEqual(reads.call_count, 0)

    def test_roster_only_run_keeps_its_mesh_pin_with_herdr_configured(self) -> None:
        owner = fixture_member("showrunner", name="roster-owner")
        worker = fixture_worker("impl", owner.id)
        run, config = self.root / "runs/roster-only", self.root / "config"
        run.mkdir(parents=True)
        config.mkdir()
        write_text_atomic(run / "mesh_roster.json", json.dumps({"impl": {"staff_id": worker.id}}))
        write_text_atomic(run / "worker_transport", "mesh\n")
        write_text_atomic(config / "direct.conf", "WORKER_TRANSPORT=herdr\n")
        with patch.dict(os.environ, {"CONF_DIR": str(config), "ROSTER_RUN_ROOT": str(run.parent)}):
            self.assertEqual(unit_runs.parent_transport(owner.current_session, record=staff.snapshot()), "mesh")

    def test_run_pin_reads_absent_valid_and_invalid_values_without_writing(self) -> None:
        run = self.root / "read-pin"
        run.mkdir()
        pin = run / "worker_transport"
        self.assertIs(unit_runs.run_pin(run), unit_runs.NoRunPin.ABSENT)
        self.assertEqual(list(run.iterdir()), [])
        for selected in ("mesh", "herdr"):
            with self.subTest(selected=selected):
                write_text_atomic(pin, selected + "\n")
                before = pin.stat().st_mtime_ns
                self.assertEqual(unit_runs.run_pin(run), selected)
                self.assertEqual(pin.read_text(), selected + "\n")
                self.assertEqual(pin.stat().st_mtime_ns, before)
        write_text_atomic(pin, "junk\n")
        with self.assertRaisesRegex(ValueError, "invalid worker transport: junk") as error:
            _ = unit_runs.run_pin(run)
        self.assertIn(str(pin), str(error.exception))
        self.assertEqual(pin.read_text(), "junk\n")

    def test_run_transport_remains_pinned_after_configuration_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config"
            config.mkdir()
            run = root / "first"
            run.mkdir()
            write_text_atomic(run / "worker_transport", "mesh\n")
            with patch.dict(os.environ, {"CONF_DIR": str(config)}):
                write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=mesh\n")
                self.assertEqual(unit_runs.worker_transport(run), "mesh")
                pin = run / "worker_transport"
                unchanged = pin.stat().st_mtime_ns
                write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=herdr\n")
                self.assertEqual(unit_runs.worker_transport(run), "mesh")
                self.assertEqual(pin.read_text(), "mesh\n")
                self.assertEqual(pin.stat().st_mtime_ns, unchanged)
                self.assertEqual(unit_runs.worker_transport(root / "second"), "herdr")
                second_pin = root / "second/worker_transport"
                self.assertEqual(second_pin.read_text(), "herdr\n")
                second_before = second_pin.read_bytes(), second_pin.stat().st_mtime_ns
                self.assertEqual(unit_runs.worker_transport(root / "second"), "herdr")
                self.assertEqual((second_pin.read_bytes(), second_pin.stat().st_mtime_ns), second_before)

    def test_new_mesh_run_exits_two_with_one_error_line_and_no_pin(self) -> None:
        config = self.root / "config"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=mesh\n")
        run = self.root / "new-mesh"
        with (patch.dict(os.environ, {"CONF_DIR": str(config)}),
              contextlib.redirect_stdout(io.StringIO()) as output,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            with self.assertRaises(SystemExit) as failure:
                _ = unit_runs.main(["worker-transport", str(run)])
        self.assertEqual(failure.exception.code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("mesh", errors.getvalue())
        self.assertFalse((run / "worker_transport").exists())

    def test_unreadable_transport_configuration_does_not_replace_a_saved_pin(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "run"
            run.mkdir()
            write_text_atomic(run / "worker_transport", "herdr\n")
            with patch.dict(os.environ, {"CONF_DIR": str(root / "missing")}):
                self.assertEqual(unit_runs.worker_transport(run), "herdr")
            self.assertEqual((run / "worker_transport").read_text(), "herdr\n")

    def test_invalid_saved_transport_stays_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            write_text_atomic(run / "worker_transport", "unknown\n")
            with self.assertRaisesRegex(ValueError, "invalid worker transport"):
                _ = unit_runs.worker_transport(run)
            self.assertEqual((run / "worker_transport").read_text(), "unknown\n")

    def staff_owner(self, run: Path, session_id: str) -> None:
        parent = showrunner_relationship("runs-fixture")
        owner = fixture_member("unit", name="owner-" + session_id[:8], parent=parent.id, runtime_id=session_id)
        _ = (run / unit_runs.STATE_FILE).write_text(json.dumps({"production": parent.name, "unit": owner.name, "owner_staff_id": owner.id}))

    def test_director_without_progress_state_resolves_roster_parent_without_output(self) -> None:
        production = fixture_member("showrunner", name="roster-production")
        owner = fixture_member("unit", name="roster", parent=production.id)
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
