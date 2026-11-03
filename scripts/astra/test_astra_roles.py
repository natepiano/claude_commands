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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_roles  # noqa: E402
from scripts.astra import design_roles  # noqa: E402
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).with_name("astra_roles.py")


class AstraRolesTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    mesh_run: Path = Path()
    home: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state, self.mesh_run, self.home = (self.root / name for name in ("roles", "mesh", "codex-home"))
        self.mesh_run.mkdir()
        self.home.mkdir()
        self.environment = {**os.environ, "ASTRA_ROLES_STATE": str(self.state), "HOME": str(self.root),
                            "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                            "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID", None)
        self.roster("running")
        _ = (self.mesh_run / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.home)}))

    def roster(self, status: str, thread: str = "thread-designer") -> None:
        _ = (self.mesh_run / "mesh_roster.json").write_text(json.dumps({
            "project-design": {"thread_id": thread, "status": status, "role": "impl"},
        }))

    def command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def hold_mesh(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.command("hold", "designer", "--session-dir", str(self.mesh_run),
                            "--worker", "project-design", *arguments)

    def records(self) -> list[dict[str, object]]:
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        return cast(list[dict[str, object]], json.loads(shown.stdout))

    def test_durable_lifetime_round_trip_survives_failed_mesh_and_ends_only_with_its_instance(self) -> None:
        design_id = "00000000-0000-4000-8000-000000000066"
        design = design_roles.HeldDesign(design_roles.DesignPlacement(design_id, "fixture-host", 2), "fixture-repo",
            str(self.root), str(self.root / "design.md"), "design", "2026-10-21T12:00:00+00:00",
            design_roles.UnitHolder("demo", "design-unit"), design_roles.DesignUnbound(),
            (design_roles.RoleInstance("designer", "lasting-alias", design_roles.RoleNeedsRecovery("thread-lasting", "server gone")),
             design_roles.RoleInstance("reviewer", "unused-reviewer", design_roles.RoleNotOpened())))
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(design_roles, "resolve_holder", return_value=design_roles.HolderReach("restarted-holder")):
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = design_roles.mesh_directory(design_id)
            mesh.mkdir(parents=True)
            _ = (mesh / "mesh_roster.json").write_text(json.dumps({"lasting-alias": {
                "thread_id": "thread-lasting", "status": "failed"}}))
            held = astra_roles.hold("designer", "thread-lasting", str(self.home), manager="old-holder",
                                    design_id=design_id, role_id="designer")
            self.assertEqual(held["lifetime"], astra_roles.HeldDesignRole(design_id, "designer"))
            record = cast(dict[str, object], json.loads((self.state / "thread-lasting.json").read_text()))
            self.assertEqual({key: record[key] for key in ("design_id", "role_id", "session_dir", "worker", "pid")},
                             {"design_id": design_id, "role_id": "designer", "session_dir": None, "worker": None, "pid": None})
            live = astra_roles.live_roles()
            self.assertEqual([(entry["thread_id"], entry["manager"], entry["state"]) for entry in live],
                             [("thread-lasting", "restarted-holder", "idle")])
            ended = replace(design, roles=(replace(design.roles[0], lifecycle=design_roles.RoleEnded("thread-lasting")), design.roles[1]))
            design_roles._save(ended)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(astra_roles.live_roles(), [])
            self.assertFalse((self.state / "thread-lasting.json").exists())

    def test_durable_record_refuses_mixed_lifetimes_wrong_role_and_unknown_keys(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            _ = astra_roles.hold("reviewer", "thread-durable", str(self.home), manager="holder",
                                 design_id="00000000-0000-4000-8000-000000000067", role_id="reviewer")
            path = self.state / "thread-durable.json"
            valid = cast(dict[str, object], json.loads(path.read_text()))
            for changes in ({"pid": os.getpid()}, {"session_dir": str(self.mesh_run), "worker": "project-design"},
                            {"role_id": "designer"}, {"design_id": ""}, {"extra": "unknown"}):
                with self.subTest(changes=changes), contextlib.redirect_stderr(io.StringIO()) as errors:
                    _ = path.write_text(json.dumps({**valid, **changes}))
                    self.assertEqual(astra_roles.live_roles(), [])
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn(str(path), errors.getvalue())
                    self.assertTrue(path.exists())

    def test_mesh_hold_reads_the_thread_and_account_and_defaults_to_the_calling_manager(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        held = self.hold_mesh()
        self.assertEqual(held.returncode, 0, held.stderr)
        path = self.state / "thread-designer.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual({key: value for key, value in record.items() if key != "held_at"}, {
            "role": "designer", "thread_id": "thread-designer", "codex_home": str(self.home),
            "manager": "sid-director", "session_dir": str(self.mesh_run), "worker": "project-design", "pid": None,
        })
        self.assertEqual(datetime.fromisoformat(str(record["held_at"])).utcoffset(), timedelta(0))
        self.assertEqual([entry.name for entry in self.state.iterdir()], [path.name])

    def test_a_missing_worker_or_thread_is_refused_without_a_record(self) -> None:
        for roster in ({}, {"project-design": {"status": "running"}},
                       {"project-design": {"thread_id": "", "status": "running"}}):
            with self.subTest(roster=roster):
                _ = (self.mesh_run / "mesh_roster.json").write_text(json.dumps(roster))
                result = self.hold_mesh("--manager", "sid-director")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn("project-design", result.stderr)
                self.assertFalse(list(self.state.glob("*.json")))

    def test_an_empty_manager_is_refused_without_a_record(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = ""
        result = self.hold_mesh()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Claude session id", result.stderr)
        self.assertFalse(list(self.state.glob("*.json")))

    def test_an_explicit_manager_overrides_the_calling_session(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "sid-caller"
        result = self.hold_mesh("--manager", "sid-director")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[0]["manager"], "sid-director")

    def test_live_mesh_state_tracks_busy_and_idle_statuses(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        for status, state in (("running", "busy"), ("waiting_capacity", "busy"), ("idle", "idle"), ("done", "idle")):
            with self.subTest(status=status):
                self.roster(status)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assertEqual((records[0]["thread_id"], records[0]["state"]), ("thread-designer", state))

    def test_ended_and_failed_workers_remove_their_records(self) -> None:
        for status in ("ended", "failed"):
            with self.subTest(status=status):
                self.roster("running")
                self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
                self.roster(status)
                self.assertEqual(self.records(), [])
                self.assertFalse((self.state / "thread-designer.json").exists())

    def test_malformed_records_are_reported_once_and_keep_valid_roles(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        valid = cast(dict[str, object], json.loads((self.state / "thread-designer.json").read_text()))
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
        self.assertEqual([entry["thread_id"] for entry in records], ["thread-designer"])
        lines = shown.stderr.splitlines()
        self.assertEqual(len(lines), len(paths))
        for path in paths:
            self.assertEqual(sum(str(path) in line for line in lines), 1)
            self.assertTrue(path.exists())

    def test_live_roles_name_the_mesh_worker_and_exec_process_lifetimes(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        self.assertEqual(self.command("hold", "reviewer", "--thread", "thread-process", "--codex-home",
                                      str(self.home), "--pid", str(os.getpid()), "--manager", "sid-runner").returncode, 0)
        with mock.patch.dict(os.environ, self.environment):
            roles = astra_roles.live_roles()
        self.assertEqual([role["lifetime"] for role in roles], [
            astra_roles.MeshWorker(str(self.mesh_run), "project-design"), astra_roles.ExecProcess(os.getpid()),
        ])
        for role in roles:
            self.assertTrue({"session_dir", "worker", "pid"}.isdisjoint(role))

    def test_a_missing_run_or_worker_removes_the_hold_silently(self) -> None:
        for missing in ("run", "worker"):
            with self.subTest(missing=missing):
                self.mesh_run.mkdir(exist_ok=True)
                self.roster("running")
                _ = (self.mesh_run / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.home)}))
                self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
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
                self.assertFalse((self.state / "thread-designer.json").exists())

    def test_an_unreadable_roster_keeps_the_hold_and_reports_its_file(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        for text in ("{not json", "[]"):
            with self.subTest(text=text):
                _ = (self.mesh_run / "mesh_roster.json").write_text(text)
                shown = self.command("list", "--json")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual(cast(object, json.loads(shown.stdout)), [])
                self.assertEqual(len(shown.stderr.splitlines()), 1)
                self.assertIn("thread-designer.json", shown.stderr)
                self.assertTrue((self.state / "thread-designer.json").exists())
        roster = self.mesh_run / "mesh_roster.json"
        roster.unlink()
        roster.mkdir()
        shown = self.command("list", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(cast(object, json.loads(shown.stdout)), [])
        self.assertEqual(len(shown.stderr.splitlines()), 1)
        self.assertIn("thread-designer.json", shown.stderr)
        self.assertTrue((self.state / "thread-designer.json").exists())

    def test_a_starting_mesh_worker_is_busy(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        self.roster("starting")
        self.assertEqual(self.records()[0]["state"], "busy")

    def test_a_process_role_is_busy_until_its_pid_exits_then_its_record_is_removed(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        try:
            held = self.command("hold", "reviewer", "--thread", "thread-reviewer", "--codex-home", str(self.home),
                                "--pid", str(process.pid), "--manager", "sid-showrunner")
            self.assertEqual(held.returncode, 0, held.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertEqual({key: value for key, value in records[0].items() if key != "held_at"}, {
                "role": "reviewer", "thread_id": "thread-reviewer", "codex_home": str(self.home),
                "manager": "sid-showrunner", "session_dir": None, "worker": None,
                "pid": process.pid, "state": "busy",
            })
        finally:
            if process.stdin is not None:
                process.stdin.close()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.records(), [])
        self.assertFalse((self.state / "thread-reviewer.json").exists())

    def test_release_removes_a_record_and_is_safe_to_repeat(self) -> None:
        self.assertEqual(self.hold_mesh("--manager", "sid-director").returncode, 0)
        for _attempt in range(2):
            result = self.command("release", "thread-designer")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.state / "thread-designer.json").exists())
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    _ = unittest.main()
