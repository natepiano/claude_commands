"""Astra role records from local mesh and process fixtures, without a live Codex."""
from __future__ import annotations

import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker, fixture_runtime_id
from scripts.astra import astra_roles  # noqa: E402
from scripts.astra import design_roles  # noqa: E402
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import process_start
from scripts.lib.stop_injection_test_support import stop_in_child

SCRIPT = Path(__file__).with_name("astra_roles.py")


class AstraRolesTests(StaffTestCase):
    parent: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path = Path()
    state: Path = Path()
    mesh_run: Path = Path()
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state, self.mesh_run, self.home = (self.root / name for name in ("roles", "mesh", "codex-home"))
        self.mesh_run.mkdir()
        self.home.mkdir()
        self.environment = {**os.environ, "ASTRA_ROLES_STATE": str(self.state), "HOME": str(self.root), "CODEX_HOME": str(self.home),
                            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                            "USAGE_STATE_DIR": str(self.root / "usage"),
                            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID", None)
        runner = fixture_member("showrunner", name="astra")
        self.parent = fixture_member("unit", name="director", parent=runner.id,
                                     runtime_id=fixture_runtime_id("sid-director"))
        for number, label in enumerate(("5048811c-26db-50f1-bf82-b3c52202e7ff", "99340615-2159-5777-b1c8-71572890df55",
                            "0e3914a9-be3f-5be4-bbeb-c3f72d8e0ec8", "2ebb1f2e-e51d-5245-8cbd-8202f40c042e",
                            "4f832112-25a1-5946-bf2c-52640f8daba4")):
            _ = fixture_worker(("project-astra-designer", "lasting-astra-designer", "worker-a-astra-reviewer", "worker-b-astra-reviewer", "worker-c-astra-reviewer")[number], self.parent.id, runtime_id=label, backend=staff.CodexBackend(self.home))
        self.roster("running")
        _ = (self.mesh_run / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.home)}))

    def roster(self, status: str, thread: str = "5048811c-26db-50f1-bf82-b3c52202e7ff", *, launcher_pid: int = 0) -> None:
        row = staff.local_snapshot().by_current_session(thread)
        assert row is not None
        _ = (self.mesh_run / "mesh_roster.json").write_text(json.dumps({
            "project-astra-designer": {"staff_id": row.id,
                               "status": status, "launcher_pid": launcher_pid},
        }))

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def hold_mesh(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.command("hold", "designer", "--session-dir", str(self.mesh_run),
                            "--worker", "project-astra-designer", *arguments)

    def records(self) -> list[dict[str, object]]:
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return cast(list[dict[str, object]], json.loads(shown.stdout))

    def test_durable_lifetime_round_trip_survives_failed_mesh_and_ends_only_with_its_instance(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000066"
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 2), "fixture-repo",
            str(self.root), str(self.root / "design.md"), "design", "2026-10-21T12:00:00+00:00",
            design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "lasting-astra-designer", design_roles.RoleNeedsRecovery(staff.StaffId("99340615-2159-5777-b1c8-71572890df55"), "server gone")),
             design_roles.RoleInstance("reviewer", "unused-reviewer", design_roles.RoleNotOpened())))
        with mock.patch.dict(os.environ, self.environment):
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = design_roles.mesh_directory(design_id)
            mesh.mkdir(parents=True)
            _ = (mesh / "mesh_roster.json").write_text(json.dumps({"lasting-astra-designer": {
                "staff_id": "99340615-2159-5777-b1c8-71572890df55", "status": "failed"}}))
            held = astra_roles.hold("designer", astra_roles.HeldDesignRole(design_id, "designer"),
                                    "99340615-2159-5777-b1c8-71572890df55")
            assert not isinstance(held, astra_roles.HoldWorkerUnavailable)
            self.assertEqual(held["lifetime"], astra_roles.HeldDesignRole(design_id, "designer"))
            record = cast(dict[str, object], json.loads((self.state / "99340615-2159-5777-b1c8-71572890df55.json").read_text()))
            self.assertEqual({key: record[key] for key in ("design_id", "role_id", "session_dir", "worker", "pid")},
                             {"design_id": design_id, "role_id": "designer", "session_dir": None, "worker": None, "pid": None})
            world = PaneWorld()
            row = staff.by_id(staff.StaffId("99340615-2159-5777-b1c8-71572890df55"))
            assert row is not None
            world.add(row, "w1:p1", live=False)
            with mock.patch("scripts.herdr.herdr.Herdr", return_value=world):
                live = astra_roles.live_roles(staff.snapshot())
            self.assertEqual([(entry["thread_id"], entry["manager"], entry["state"]) for entry in live],
                             [("99340615-2159-5777-b1c8-71572890df55", self.parent.current_session, "idle")])
            ended = replace(design, roles=(replace(design.roles[0], lifecycle=design_roles.RoleEnded(staff.StaffId("99340615-2159-5777-b1c8-71572890df55"))), design.roles[1]))
            design_roles._save(ended)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(astra_roles.live_roles(staff.snapshot()), [])
            self.assertFalse((self.state / "99340615-2159-5777-b1c8-71572890df55.json").exists())

    def test_durable_record_refuses_mixed_lifetimes_wrong_role_and_unknown_keys(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            _ = astra_roles.hold("reviewer", astra_roles.HeldDesignRole(
                "00000000-0000-4000-8000-000000000067", "reviewer"), "0e3914a9-be3f-5be4-bbeb-c3f72d8e0ec8")
            path = self.state / "0e3914a9-be3f-5be4-bbeb-c3f72d8e0ec8.json"
            valid = cast(dict[str, object], json.loads(path.read_text()))
            for changes in ({"pid": os.getpid()}, {"session_dir": str(self.mesh_run), "worker": "project-astra-designer"},
                            {"role_id": "designer"}, {"design_id": ""}, {"extra": "unknown"}):
                with self.subTest(changes=changes), contextlib.redirect_stderr(io.StringIO()) as errors:
                    _ = path.write_text(json.dumps({**valid, **changes}))
                    self.assertEqual(astra_roles.live_roles(staff.snapshot()), [])
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn(str(path), errors.getvalue())
                    self.assertTrue(path.exists())

    def test_mesh_hold_reads_the_thread_and_account_and_defaults_to_the_calling_manager(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "7dae38b9-c962-5494-a4ac-57639298e4d1"
        held = self.hold_mesh()
        self.assertEqual(held.returncode, 0, held.stderr)
        path = self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual({key: value for key, value in record.items() if key != "held_at"}, {
            "staff_id": "5048811c-26db-50f1-bf82-b3c52202e7ff", "session_dir": str(self.mesh_run), "worker": "project-astra-designer", "pid": None,
        })
        self.assertEqual(datetime.fromisoformat(str(record["held_at"])).utcoffset(), timedelta(0))
        self.assertEqual([entry.name for entry in self.state.iterdir()], [path.name])

    def test_a_missing_worker_or_thread_is_refused_without_a_record(self) -> None:
        for roster in ({}, {"project-astra-designer": {"status": "running"}},
                       {"project-astra-designer": {"thread_id": "", "status": "running"}}):
            with self.subTest(roster=roster):
                _ = (self.mesh_run / "mesh_roster.json").write_text(json.dumps(roster))
                result = self.hold_mesh()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn("project-astra-designer", result.stderr)
                self.assertFalse(list(self.state.glob("*.json")))
    def test_durable_hold_without_a_worker_returns_the_unavailable_reason(self) -> None:
        identity = fixture_runtime_id("absent-held-worker")
        with mock.patch.dict(os.environ, self.environment), contextlib.redirect_stderr(io.StringIO()) as errors:
            held = astra_roles.hold("reviewer", astra_roles.HeldDesignRole(
                "00000000-0000-4000-8000-000000000068", "reviewer"), identity)
        self.assertIsInstance(held, astra_roles.HoldWorkerUnavailable)
        assert isinstance(held, astra_roles.HoldWorkerUnavailable)
        self.assertIn(identity, held.reason)
        self.assertIn(held.reason, errors.getvalue())
        self.assertEqual(list(self.state.glob("*.json")), [])

    def test_live_mesh_state_tracks_busy_and_idle_statuses(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        for status, state in (("running", "busy"), ("waiting_capacity", "busy"), ("idle", "idle"), ("done", "idle")):
            with self.subTest(status=status):
                self.roster(status, launcher_pid=os.getpid() if state == "busy" else 0)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assertEqual((records[0]["staff_id"], records[0]["state"]), ("5048811c-26db-50f1-bf82-b3c52202e7ff", state))

    def test_ended_and_failed_workers_remove_their_records(self) -> None:
        for status in ("ended", "failed"):
            with self.subTest(status=status):
                self.roster("running")
                self.assertEqual(self.hold_mesh().returncode, 0)
                self.roster(status)
                self.assertEqual(self.records(), [])
                self.assertFalse((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())

    def test_malformed_records_are_reported_once_and_keep_valid_roles(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        valid = cast(dict[str, object], json.loads((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").read_text()))
        malformed: list[dict[str, object]] = []
        for key in valid:
            missing = dict(valid)
            del missing[key]
            malformed.append(missing)
            malformed.append({**valid, key: []})
        malformed.append({**valid, "state": "busy"})
        paths: list[Path] = []
        for number, record in enumerate(malformed):
            thread = f"thread-broken-{number}"
            if "thread_id" in record and isinstance(record["thread_id"], str):
                record["thread_id"] = thread
            path = self.state / f"{thread}.json"
            _ = path.write_text(json.dumps(record))
            paths.append(path)
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        records = cast(list[dict[str, object]], json.loads(shown.stdout))
        self.assertEqual([entry["staff_id"] for entry in records], ["5048811c-26db-50f1-bf82-b3c52202e7ff"])
        lines = shown.stderr.splitlines()
        self.assertEqual(len(lines), len(paths))
        for path in paths:
            self.assertEqual(sum(str(path) in line for line in lines), 1)
            self.assertTrue(path.exists())

    def test_live_roles_name_the_mesh_worker_and_exec_process_lifetimes(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        self.assertEqual(self.command("hold", "reviewer", "--staff-id", "2ebb1f2e-e51d-5245-8cbd-8202f40c042e", "--pid", str(os.getpid())).returncode, 0)
        with mock.patch.dict(os.environ, self.environment):
            roles = astra_roles.live_roles(staff.snapshot())
        self.assertCountEqual([role["lifetime"] for role in roles], [
            astra_roles.MeshWorker(str(self.mesh_run), "project-astra-designer"), astra_roles.ExecProcess(os.getpid()),
        ])
        for role in roles:
            self.assertTrue({"session_dir", "worker", "pid"}.isdisjoint(role))

    def test_a_missing_run_or_worker_removes_the_hold_silently(self) -> None:
        for missing in ("run", "worker"):
            with self.subTest(missing=missing):
                self.mesh_run.mkdir(exist_ok=True)
                self.roster("running")
                _ = (self.mesh_run / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.home)}))
                self.assertEqual(self.hold_mesh().returncode, 0)
                if missing == "run":
                    for path in self.mesh_run.iterdir():
                        path.unlink()
                    self.mesh_run.rmdir()
                else:
                    _ = (self.mesh_run / "mesh_roster.json").write_text("{}")
                shown = self.command("list", "--json")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual(shown.stderr, "")
                self.assertEqual(cast(object, json.loads(shown.stdout)), [])
                self.assertFalse((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())

    def test_an_unreadable_roster_keeps_the_hold_and_reports_its_file(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        for text in ("{not json", "[]"):
            with self.subTest(text=text):
                _ = (self.mesh_run / "mesh_roster.json").write_text(text)
                shown = self.command("list", "--json")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual(cast(object, json.loads(shown.stdout)), [])
                self.assertEqual(len(shown.stderr.splitlines()), 1)
                self.assertIn("5048811c-26db-50f1-bf82-b3c52202e7ff.json", shown.stderr)
                self.assertTrue((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())
        roster = self.mesh_run / "mesh_roster.json"
        roster.unlink()
        roster.mkdir()
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(cast(object, json.loads(shown.stdout)), [])
        self.assertEqual(len(shown.stderr.splitlines()), 1)
        self.assertIn("5048811c-26db-50f1-bf82-b3c52202e7ff.json", shown.stderr)
        self.assertTrue((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())

    def test_a_starting_mesh_worker_is_busy(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        self.roster("starting", launcher_pid=os.getpid())
        self.assertEqual(self.records()[0]["state"], "busy")

    def test_a_process_role_is_busy_until_its_pid_exits_then_its_record_is_removed(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            held = self.command("hold", "reviewer", "--staff-id", "4f832112-25a1-5946-bf2c-52640f8daba4",
                                "--pid", str(process.pid))
            self.assertEqual(held.returncode, 0, held.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            started = process_start.process_start(process.pid)
            self.assertIsInstance(started, process_start.Started)
            assert isinstance(started, process_start.Started)
            self.assertEqual({key: value for key, value in records[0].items() if key != "held_at"}, {
                "staff_id": "4f832112-25a1-5946-bf2c-52640f8daba4",
                "session_dir": None, "worker": None,
                "pid": process.pid, "pid_start": started.start, "state": "busy",
            })
        finally:
            if process.stdin is not None:
                process.stdin.close()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.records(), [])
        self.assertFalse((self.state / "4f832112-25a1-5946-bf2c-52640f8daba4.json").exists())

    def test_exec_hold_rerun_after_stop_does_not_attach_to_a_reused_pid(self) -> None:
        identity = "4f832112-25a1-5946-bf2c-52640f8daba4"
        original_pid = os.getpid()
        with mock.patch.dict(os.environ, self.environment):
            stop_in_child(lambda: astra_roles.hold("reviewer", astra_roles.ExecProcess(original_pid), identity),
                          os, "replace", argument=1,
                          matches=lambda destination: Path(str(destination)) == self.state / (identity + ".json"))
            with mock.patch.object(process_start, "process_start", return_value=process_start.Started("replacement-lifetime")):
                roles = astra_roles.live_roles(staff.snapshot())
        self.assertEqual(roles, [])

    def test_release_removes_a_record_and_is_safe_to_repeat(self) -> None:
        self.assertEqual(self.hold_mesh().returncode, 0)
        for _attempt in range(2):
            result = self.command("release", "5048811c-26db-50f1-bf82-b3c52202e7ff")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    _ = unittest.main()
