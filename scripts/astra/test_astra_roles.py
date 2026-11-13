"""Astra role records from durable roles and process fixtures, without a live Codex."""
from __future__ import annotations

import json
import contextlib
import io
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
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
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.stop_injection_test_support import stop_in_child

SCRIPT = Path(__file__).with_name("astra_roles.py")


class AstraRolesTests(StaffTestCase):
    parent: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path = Path()
    state: Path = Path()
    run_folder: Path = Path()
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state, self.run_folder, self.home = (self.root / name for name in ("roles", "run", "codex-home"))
        self.run_folder.mkdir()
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


    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)


    def hold_process(self) -> subprocess.CompletedProcess[str]:
        return self.command("hold", "designer", "--staff-id", "5048811c-26db-50f1-bf82-b3c52202e7ff",
                            "--pid", str(os.getpid()))

    def records(self) -> list[dict[str, object]]:
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return cast(list[dict[str, object]], json.loads(shown.stdout))

    def test_durable_lifetime_round_trip_survives_closed_pane_and_ends_only_with_its_instance(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000066"
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 2), "fixture-repo",
            str(self.root), str(self.root / "design.md"), "design", "2026-10-21T12:00:00+00:00",
            design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "lasting-astra-designer", design_roles.RoleNeedsRecovery(staff.StaffId("99340615-2159-5777-b1c8-71572890df55"), "server gone")),
             design_roles.RoleInstance("reviewer", "unused-reviewer", design_roles.RoleNotOpened())))
        with mock.patch.dict(os.environ, self.environment):
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            held = astra_roles.hold("designer", astra_roles.HeldDesignRole(design_id, "designer"),
                                    "99340615-2159-5777-b1c8-71572890df55")
            assert not isinstance(held, astra_roles.HoldWorkerUnavailable)
            self.assertEqual(held["lifetime"], astra_roles.HeldDesignRole(design_id, "designer"))
            record = cast(dict[str, object], json.loads((self.state / "99340615-2159-5777-b1c8-71572890df55.json").read_text()))
            self.assertEqual({key: value for key, value in record.items() if key != "held_at"},
                             {"staff_id": "99340615-2159-5777-b1c8-71572890df55", "design_id": design_id,
                              "role_id": "designer", "pid": None})
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
            for changes in ({"pid": os.getpid()}, {"session_dir": str(self.run_folder), "worker": "project-astra-designer"},
                            {"role_id": "designer"}, {"design_id": ""}, {"extra": "unknown"}):
                with self.subTest(changes=changes), contextlib.redirect_stderr(io.StringIO()) as errors:
                    _ = path.write_text(json.dumps({**valid, **changes}))
                    self.assertEqual(astra_roles.live_roles(staff.snapshot()), [])
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn(str(path), errors.getvalue())
                    self.assertTrue(path.exists())


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



    def test_malformed_records_are_reported_once_and_keep_valid_roles(self) -> None:
        self.assertEqual(self.hold_process().returncode, 0)
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

    def test_live_roles_keep_each_exec_process_lifetime(self) -> None:
        self.assertEqual(self.hold_process().returncode, 0)
        self.assertEqual(self.command("hold", "reviewer", "--staff-id", "2ebb1f2e-e51d-5245-8cbd-8202f40c042e", "--pid", str(os.getpid())).returncode, 0)
        with mock.patch.dict(os.environ, self.environment):
            roles = astra_roles.live_roles(staff.snapshot())
        self.assertCountEqual([role["lifetime"] for role in roles], [
            astra_roles.ExecProcess(os.getpid()), astra_roles.ExecProcess(os.getpid()),
        ])
        for role in roles:
            self.assertTrue({"session_dir", "worker", "pid"}.isdisjoint(role))
        locks: list[bool] = []

        def locked_write(path: Path, text: str) -> None:
            with (self.state / "lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    locks.append(True)
                else:
                    locks.append(False)
                    fcntl.flock(handle, fcntl.LOCK_UN)
            write_text_atomic(path, text)

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(astra_roles, "write_text_atomic", side_effect=locked_write):
            _ = astra_roles.hold("designer", astra_roles.ExecProcess(os.getpid()), "5048811c-26db-50f1-bf82-b3c52202e7ff")
        self.assertEqual(locks, [True])




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
        self.assertEqual(self.hold_process().returncode, 0)
        for _attempt in range(2):
            result = self.command("release", "5048811c-26db-50f1-bf82-b3c52202e7ff")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.state / "5048811c-26db-50f1-bf82-b3c52202e7ff.json").exists())
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    _ = unittest.main()
