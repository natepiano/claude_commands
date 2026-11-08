"""Unit directors resolve from staff records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.direct import workers
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import (
    StaffTestCase, fixture_runtime_id, fixture_worker, isolated_source_readers,
    showrunner_relationship, unit_membership,
)


SCRIPT = Path(__file__).with_name("workers.py")






def json_object(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise AssertionError(f"expected an object in {path}")
    fields = cast(dict[str, object], value)
    return fields


class WorkerScriptTests(unittest.TestCase):
    def test_worker_script_starts_outside_the_repository(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        with tempfile.TemporaryDirectory(prefix="workers-script-") as directory:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--help"], cwd=directory,
                env=environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


class DirectorResolutionTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = showrunner_relationship("demo")
        _ = self.enterContext(isolated_source_readers())
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(
            os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")},
        )))

    def test_explicit_role_renders_the_title_without_reading_dispatch_history(self) -> None:
        from scripts.direct import progress_history

        parent = unit_membership("demo", "builders")
        member = fixture_worker("writer", parent.id)
        run = self.worker_run("demo", "builders", parent.current_session)
        with patch.object(progress_history, "last_dispatch", side_effect=AssertionError("unexpected history read")):
            title = workers.shown_name(run, member.name, role="impl")
        self.assertEqual(title, "demo · builders · implementer")

    def test_live_unit_director_keeps_a_running_director_without_a_messaging_socket(self) -> None:
        parent = unit_membership("demo", "builders")
        registry = self.session_record(parent.current_session, "builders")
        run = self.worker_run("demo", "builders", parent.current_session)
        self.write_json(run / workers.STATE, {**json_object(run / workers.STATE), "owner_staff_id": parent.id})
        self.assertEqual(json_object(registry)["messagingSocketPath"], "")

        self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(parent.current_session, os.getpid()))

    def test_live_unit_director_prefers_the_newest_running_name_match(self) -> None:
        parent = unit_membership("demo", "builders")
        older = self.session_record(parent.current_session, "builders")
        current = staff.update_current_session(parent.current_session, staff.native_id(fixture_runtime_id("newer-director")))
        newer = self.session_record(current.current_session, "builders")
        record = json_object(newer)
        record["updatedAt"] = 2
        self.write_json(newer, record)
        run = self.worker_run("demo", "builders", "former-director")
        self.write_json(run / workers.STATE, {**json_object(run / workers.STATE), "owner_staff_id": parent.id})
        self.write_json(self.root / "shutdown/account/history/restart.json", {"session": {
            "session_id": "former-director", "name": "builders", "run_dirs": [str(run)],
            "host": {"kind": "unit", "unit": "builders", "session_name": "builders"},
        }})
        registry = sessions.read_registry()
        entries = {item.record["sessionId"]: item for item in registry.entries}
        # Present readable records in the older-first order that caused the defect.
        with patch.object(sessions, "read_registry", return_value=sessions.RegistryRead(
            (entries[older.stem], entries[newer.stem]), registry.unreadable,
        )):
            self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(current.current_session, os.getpid()))

    def test_live_director_resolves_the_renamed_owner_despite_old_state_names(self) -> None:
        parent = unit_membership("demo", "builders")
        run = self.worker_run("demo", "builders", parent.current_session)
        context = json_object(run / workers.STATE)
        context["owner_staff_id"] = parent.id
        self.write_json(run / workers.STATE, context)
        renamed = staff.rename(parent.id, "renamed-builders")
        _ = self.session_record(renamed.current_session, renamed.name)
        self.assertEqual(workers.live_unit_director(run), workers.LiveUnitDirector(renamed.current_session, os.getpid()))




class DetachedWorkerSafetyTests(StaffTestCase):
    def detached_worker(self) -> tuple[staff.StaffRow, staff.StaffRow, Path]:
        parent = unit_membership("demo", "builders")
        member = fixture_worker("w7", parent.id, backend=staff.CodexBackend(self.root / "codex"))
        run = self.worker_run("demo", "builders", parent.current_session)
        return parent, member, run

    def test_reused_supervisor_pid_does_not_retain_detached_work(self) -> None:
        run = self.detached_worker()[2]
        records = run / "mesh_runs"
        records.mkdir()
        record = records / "abandoned.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as unrelated:
            write_text_atomic(record, json.dumps({"worker": "w7", "status": "running", "pid": unrelated.pid,
                                                  "cwd": str(run)}))
            self.assertEqual(workers.live_detached_runs(run), ())
            self.assertEqual(workers.protected_working_directories(run), ())
            self.assertEqual(workers.retained_detached_runs(run), ())
            self.assertIsNone(unrelated.poll())
            result = unrelated.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(unrelated.returncode, 0, result)

    def test_a_supervisor_started_through_a_linked_run_protects_its_resolved_checkout(self) -> None:
        parent, member, run = self.detached_worker()
        linked = run.parent / "linked-run"
        linked.symlink_to(run, target_is_directory=True)
        records = run / "mesh_runs"
        records.mkdir()
        record = records / "linked-command.json"
        checkout = run / "phases" / "5" / "gate"
        checkout.mkdir(parents=True)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
            "mesh_runs.py", "supervise", str(linked / "mesh_runs" / record.name)], stdin=subprocess.PIPE)
        self.addCleanup(child.communicate, timeout=TEST_HANG_GUARD_SECS)
        located = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", checkout), expected=parent.worktree)
        assert isinstance(located.worktree, staff.PresentWorktree)
        write_text_atomic(record, json.dumps({"staff_id": member.id, "worker": member.name,
                                              "status": "running", "pid": child.pid}))
        self.assertEqual([entry[0] for entry in workers.live_detached_runs(run.resolve())], [record])
        self.assertEqual(workers.protected_working_directories(run.resolve()), (located.worktree.path.resolve(),))


if __name__ == "__main__":
    _ = unittest.main()
