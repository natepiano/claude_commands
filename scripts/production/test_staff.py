"""Exercise the staff contract with isolated state and an actual fake SSH process."""
from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_worker, fixture_runtime_id

import fcntl
import io
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, chdir, redirect_stderr
from dataclasses import FrozenInstanceError, replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import staff
from scripts.agents import shutdown, stop_resume, restart_support
from scripts.production.cli_fixture import invoke_cli
from scripts.herdr import worker_panes
from scripts.lib import unit_runs
from scripts.message import sessions
from scripts.production.staff_test_support import CHECKOUT, STAFF_SCRIPT, StaffTestCase, isolated_staff_file
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


MISSING_ID = staff.StaffId("s00000000000")


class StaffCommandTestCase(StaffTestCase):
    """Run local CLI logic with the real parser, handlers, and private state."""

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "ROSTER_RUN_ROOT": str(self.root / "unit"),
            "UNIT_RUN_ROOT": str(self.root / "unit"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
        })))
        _ = self.enterContext(mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"))

    @override
    def cli(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        result = invoke_cli(staff, lambda: staff.main(list(arguments)), list(arguments),
                            dict(os.environ), CHECKOUT)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def agents(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        with (mock.patch.object(stop_resume, "stop_unit_workers", return_value=[]),
              mock.patch.object(stop_resume, "resume", return_value=stop_resume.ClaudeUp("fixture", "w1:p1", "claude2")),
              mock.patch.object(restart_support, "send_message", return_value={"kind": "sent", "how": "fixture"})):
            result = invoke_cli(shutdown, lambda: shutdown.main(list(arguments)), list(arguments), dict(os.environ), CHECKOUT)
        self.assertEqual(result.returncode, code, result.stderr)
        return result


KILLED_WRITER = """import os, sys
sys.platform = 'linux'
from scripts.production import staff
os.replace = lambda *args, **kwargs: os._exit(73)
staff.ensure('showrunner', name='interrupted', runtime_id='196bcb82-c226-5d2c-8b39-94b20dd50f8d')
"""



def concurrent_ensure(
    ready: Barrier,
    result: Connection,
    kind: staff.Kind,
    name: str,
    parent: staff.StaffId | None,
) -> None:
    """Release both real writer processes together and return an id or failure."""
    with result:
        try:
            _ = ready.wait(timeout=TEST_HANG_GUARD_SECS)
            row = fixture_member(kind, name=name, parent=parent)
            result.send((True, str(row.id)))
        except Exception as error:
            result.send((False, repr(error)))


class NativeRuntimeIdentityTests(StaffCommandTestCase):
    def test_old_row_keeps_unknown_role_and_home_absent(self) -> None:
        _, _, member = self.tree()
        value = member.to_json()
        self.assertNotIn("role", value)
        self.assertNotIn("home", value)
        decoded = staff.StaffRow.from_json(value)
        self.assertEqual(decoded.to_json(), value)
        self.assertEqual(staff.snapshot().by_id(member.id), decoded)
        before = self.staff_file.read_bytes()
        with redirect_stderr(io.StringIO()):
            staff._transaction(lambda rows: None)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_run_pointer_edits_preserve_other_fields_and_a_newer_run(self) -> None:
        _, unit, _ = self.tree()
        old = self.root / "old-run"
        new = self.root / "new-run"
        cases = (
            ("set", old, staff.OpenRun(old)),
            ("clear", self.root / "other-run", staff.OpenRun(old)),
            ("set", new, staff.OpenRun(new)),
            ("clear", old, staff.OpenRun(new)),
            ("clear", new / ".." / "new-run", staff.NoRun()),
            ("clear", new, staff.NoRun()),
        )
        self.assertIsInstance(unit.run, staff.NoRun)
        self.assertNotIn("run", unit.to_json())
        for action, path, expected in cases:
            with self.subTest(action=action, path=path):
                result = (staff.set_run(unit.id, staff.OpenRun(path)) if action == "set"
                          else staff.clear_run(unit.id, path))
                self.assertEqual(result, replace(unit, run=expected))
                self.assertEqual(staff.local_by_current_session(unit.current_session), result)
                self.assertEqual(staff.StaffRow.from_json(result.to_json()), result)
                self.assertEqual(staff.by_id(unit.id), result)
                if isinstance(expected, staff.OpenRun):
                    self.assertEqual(result.to_json()["run"], str(expected.path))
                else:
                    self.assertNotIn("run", result.to_json())

    def test_run_pointer_cli_uses_the_current_session_and_reports_missing_rows(self) -> None:
        _, unit, _ = self.tree()
        current = staff.native_id(fixture_runtime_id("run-current"))
        unit = staff.update_current_session(unit.current_session, current)
        run = self.root / "run"
        for verb, expected in (("set-run", staff.OpenRun(run)), ("clear-run", staff.NoRun())):
            with self.subTest(verb=verb):
                _ = self.cli(verb, current, str(run))
                self.assertEqual(staff.by_id(unit.id), replace(unit, run=expected))
                result = self.cli(verb, unit.id, str(run), code=1)
                self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_staff_admission_does_not_read_pane_publications(self) -> None:
        from scripts.herdr import pane_launch

        _, parent, member = self.tree()
        with mock.patch.object(pane_launch, "staff_admitted", wraps=pane_launch.staff_admitted) as publication:
            _ = staff.ensure("agent", name="TEST-no-pane-read", runtime_id=fixture_runtime_id("TEST-no-pane-read"))
            _ = staff.ensure_worker(staff.native_id(fixture_runtime_id("TEST-no-worker-pane-read")), "TEST-new", parent.id)
            _ = staff.update_current_session(member.current_session, staff.native_id(fixture_runtime_id("TEST-next-native")))
        publication.assert_not_called()

    def test_invalid_role_and_home_preserve_the_staff_file_and_raw_values(self) -> None:
        runner, _, member = self.tree()
        document = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        rows = cast(list[dict[str, object]], document["rows"])
        fields = next(row for row in rows if row["id"] == member.id)
        fields.update(role="future-worker-role", home="relative-home")
        self.write_json(self.staff_file, document)
        with redirect_stderr(io.StringIO()) as errors:
            record = staff.snapshot()
        current = record.by_id(member.id)
        assert current is not None
        self.assertIsInstance(current.role, staff.WorkerRoleUnknown)
        self.assertIsInstance(current.home, staff.HomeUnknown)
        self.assertEqual(current.unknown_fields, {"role": "future-worker-role", "home": "relative-home"})
        self.assertEqual(current.to_json(), fields)
        self.assertEqual(record.by_id(runner.id), runner)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        with redirect_stderr(io.StringIO()):
            _ = staff.rename(member.id, "TEST-renamed")
        saved_document = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        saved_rows = cast(list[dict[str, object]], saved_document["rows"])
        saved = next(row for row in saved_rows if row["id"] == member.id)
        self.assertEqual(saved, {**fields, "name": "TEST-renamed"})

    def test_running_claude_registry_identifies_a_worker_before_its_first_transcript(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id)
        native = staff.native_id(fixture_runtime_id("claude-before-first-message"))
        worker = staff.ensure_worker(native, "impl", unit.id)
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json"
        _ = registry.write_text(json.dumps({
            "pid": os.getpid(), "sessionId": native, "name": worker.name,
            "messagingSocketPath": "", "procStart": sessions.process_start(os.getpid()), "updatedAt": 1,
        }))
        self.assertEqual(sessions.transcript_locations(native), [])
        self.assertEqual(staff.backend(worker), staff.ClaudeBackend())
        registry.unlink()
        self.assertIsInstance(staff.backend(worker), staff.BackendUnavailable)

    def test_agent_environment_removes_launch_and_editor_configuration(self) -> None:
        environment = {"MODELS_LAUNCH_CONFIGURATION": "launch-generation",
                       "MODELS_REGISTRY_EDIT_BASE": "editor-generation", "KEEP": "agent-value"}
        self.assertEqual(staff.child_environment(environment), {"KEEP": "agent-value"})
        self.assertEqual(environment["MODELS_LAUNCH_CONFIGURATION"], "launch-generation")
        self.assertEqual(environment["MODELS_REGISTRY_EDIT_BASE"], "editor-generation")

    def test_worker_rename_cli_keeps_identity_for_each_backend_and_holder(self) -> None:
        from scripts.production.rename_test_support import RenameDisplays

        runner, unit, _ = self.tree()
        for backend in ("claude", "codex"):
            for holder in (runner, unit):
                for live in (False, True):
                    with self.subTest(backend=backend, holder=holder.kind, live=live):
                        name = f"cli-{backend}-{holder.kind}-{'live' if live else 'stopped'}"
                        worker = fixture_worker(name, holder.id)
                        worker = staff.update_current_session(worker.current_session,
                            staff.native_id(fixture_runtime_id(name + "-current")))
                        displays = RenameDisplays(worker, live=live, backend=backend)
                        arguments = ["rename", worker.id, "--name", name + "-renamed", "--json"]
                        with displays.installed():
                            result = invoke_cli(staff, lambda: staff.main(arguments), arguments, dict(os.environ), self.root)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            renamed = staff.StaffRow.from_json(cast(object, json.loads(result.stdout)))
                            self.assertEqual(renamed, replace(worker, name=name + "-renamed"))
                            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
                            rerun = invoke_cli(staff, lambda: staff.main(arguments), arguments, dict(os.environ), self.root)
                        self.assertEqual(rerun.returncode, 0, rerun.stderr)
                        self.assertIn("unchanged", rerun.stderr)
                        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
                        self.assertEqual(displays.commands,
                                         [("pane", "rename", "probe-pane", worker_panes.agent_name(holder, renamed.name))] if live else [])
                        self.assertEqual(displays.tab, "previous")
                        self.assertEqual(staff.by_id(holder.id), holder)


    def test_worker_rename_keeps_identity_for_each_backend_and_holder(self) -> None:
        from scripts.production.rename_test_support import RenameDisplays

        runner, unit, _ = self.tree()
        for backend in ("claude", "codex"):
            for holder in (runner, unit):
                for live in (False, True):
                    with self.subTest(backend=backend, holder=holder.kind, live=live):
                        name = f"{backend}-{holder.kind}-{'live' if live else 'stopped'}"
                        worker = fixture_worker(name, holder.id)
                        current = staff.native_id(fixture_runtime_id(name + "-current"))
                        worker = staff.update_current_session(worker.current_session, current)
                        displays = RenameDisplays(worker, live=live, backend=backend)
                        layout = self.root / (name + "-layout.json")
                        self.write_json(layout, {"row_id": worker.id, "terminal_id": "probe-terminal"})
                        before = layout.read_bytes(), layout.stat().st_mtime_ns
                        with displays.installed():
                            renamed = staff.rename(worker.id, name + "-renamed")
                            self.assertEqual(renamed, replace(worker, name=name + "-renamed"))
                            self.assertEqual(displays.pane, worker_panes.agent_name(holder, renamed.name) if live else "previous")
                            settled = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
                            self.assertEqual(staff.rename(worker.id, renamed.name), renamed)
                        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), settled)
                        self.assertEqual((layout.read_bytes(), layout.stat().st_mtime_ns), before)
                        self.assertEqual(displays.commands,
                                         [("pane", "rename", "probe-pane", worker_panes.agent_name(holder, renamed.name))] if live else [])
                        self.assertEqual(displays.tab, "previous")
                        self.assertEqual(staff.by_id(holder.id), holder)


    def test_guarded_cli_adds_an_agent_without_a_parent_and_reads_its_locations(self) -> None:
        identity = fixture_runtime_id("morty-agent")
        for field, state in (("worktree", "present"), ("doc", "live")):
            with self.subTest(field=field):
                self.staff_file.unlink(missing_ok=True)
                result = self.cli("ensure", "agent", "--name", "morty", "--runtime-id", identity,
                                  "--" + field, "natedev", str(self.root / "morty"), state, "--json")
                payload = cast(object, json.loads(result.stdout))
                row = staff.StaffRow.from_json(payload)
                self.assertEqual((row.id, row.current_session, row.kind, row.name, row.parent),
                                 (identity, identity, "agent", "morty", staff.StaffRoot()))
                self.assertEqual(staff.snapshot().rows, (row,))
                self.assertEqual(staff.by_path("morty"), row)
                shown = self.cli("show", "--kind", "agent")
                self.assertIn("morty", shown.stdout)
                self.assertIn("agent", shown.stdout)
                self.assertEqual(staff.StaffRow.from_json(row.to_json()), row)

    def test_unit_lookup_keeps_exact_removed_worktrees_and_ignores_showrunners(self) -> None:
        runner, unit, _ = self.tree()
        location = self.root / "retired"
        unit = staff.set_worktree(unit.id, staff.RemovedWorktree("natedev", location), expected=unit.worktree)
        record = staff.snapshot()
        self.assertEqual(record.unit_by_worktree("natedev", location), unit)
        self.assertIsNone(record.unit_by_worktree("mac", location))
        self.assertIsNone(record.unit_by_worktree("natedev", location / "src"))
        self.assertIsNone(record.by_worktree("natedev", location))
        self.assertIsNone(record.unit_by_worktree("natedev", self.root))
        self.assertEqual(record.by_id(runner.id), runner)

    def test_a_present_worktree_wins_over_a_removed_worktree_in_either_order(self) -> None:
        runner, unit, _ = self.tree()
        location = self.root / "reused"
        removed = replace(unit, worktree=staff.RemovedWorktree("natedev", location))
        present = replace(unit, id=runner.id, worktree=staff.PresentWorktree("natedev", location))
        for rows in ((removed, present), (present, removed)):
            with self.subTest(rows=rows):
                self.assertEqual(staff.StaffSnapshot(rows).unit_by_worktree("natedev", location), present)

    def test_the_latest_removed_worktree_wins_for_a_reused_path(self) -> None:
        runner, unit, _ = self.tree()
        location = self.root / "reused"
        older = replace(unit, worktree=staff.RemovedWorktree("natedev", location))
        newer = replace(older, id=runner.id)
        self.assertEqual(staff.StaffSnapshot((older, newer)).unit_by_worktree("natedev", location), newer)

    def test_the_latest_present_worktree_wins_for_a_reused_path(self) -> None:
        runner, unit, _ = self.tree()
        location = self.root / "reused"
        older = replace(unit, worktree=staff.PresentWorktree("natedev", location))
        newer = replace(older, id=runner.id)
        self.assertEqual(staff.StaffSnapshot((older, newer)).unit_by_worktree("natedev", location), newer)

    def test_schema_two_decoder_keeps_native_sessions_and_wire_parent_fields(self) -> None:
        owner = fixture_runtime_id("stored-owner")
        unit = fixture_runtime_id("stored")
        current = fixture_runtime_id("stored-unit-after-clear")
        rows: list[dict[str, object]] = [
            {"id": owner, "kind": "showrunner", "name": "owner", "parent": None,
             "current_session": owner},
            {"id": unit, "kind": "unit", "name": "member", "parent": owner,
             "current_session": current,
             "worktree": {"machine": "mac", "path": str(self.root / "member"), "state": "removed"}},
        ]
        before = self.write_document({"schema": 2, "rows": rows, "pending_retire": [unit]})
        record = staff.snapshot()
        self.assertEqual(record.schema, 2)
        self.assertEqual([row.to_json() for row in record.rows], rows)
        self.assertEqual(record.pending_retire, (unit,))
        self.assertIsNone(record.by_current_session(unit))
        self.assertEqual(record.by_current_session(current), record.by_id(staff.StaffId(unit)))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_worker_name_checks_shape_without_requiring_a_role_suffix(self) -> None:
        for name in ("impl2", "reader", "one.two-three_4", "astra"):
            with self.subTest(name=name):
                self.assertEqual(staff.WorkerName.parse(name).value, name)
        for name in ("", ".", "..", "bad worker", "bad/worker", "bad\nworker"):
            with self.subTest(name=name), self.assertRaises(staff.StaffRefused):
                _ = staff.WorkerName.parse(name)

    def test_new_members_use_only_the_native_id_supplied_by_the_backend(self) -> None:
        runner_id = "00000000-0000-4000-8000-000000000001"
        unit_id = "00000000-0000-4000-8000-000000000002"
        thread_id = "019b3a40-4250-7000-8000-000000000003"
        runner = staff.ensure("showrunner", name="demo", runtime_id=runner_id)
        unit = staff.ensure("unit", name="models", parent=staff.ReportsTo(runner.id), runtime_id=unit_id)
        worker = staff.ensure_worker(staff.native_id(thread_id), "impl", unit.id)
        self.assertEqual([runner.id, unit.id, worker.id], [runner_id, unit_id, thread_id])
        self.assertEqual([runner.current_session, unit.current_session, worker.current_session],
                         [runner_id, unit_id, thread_id])
        self.assertEqual([unit.parent, worker.parent], [staff.ReportsTo(staff.StaffId(runner_id)), staff.ReportsTo(staff.StaffId(unit_id))])
        self.assertEqual(staff.by_id(staff.StaffId(thread_id)), worker)
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "invalid native runtime id"):
            _ = staff.ensure_worker(staff.StaffId(""), "unstarted", unit.id)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_removing_a_worker_refuses_its_live_native_session_and_names_that_session(self) -> None:
        _, _, worker = self.tree()
        _ = self.session_record(worker.id, worker.name)
        before = self.staff_file.read_bytes()
        with mock.patch.object(sessions, "session_liveness", return_value=sessions.LiveSessionProcess(os.getpid())):
            with self.assertRaisesRegex(staff.StaffRefused, worker.id):
                _ = staff.retire(worker.id)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(worker.id), worker)

    def test_runtime_session_changes_only_current_session_in_one_write_and_rerun_changes_no_byte(self) -> None:
        runner, unit, worker = self.tree()
        new_id = staff.StaffId("00000000-0000-4000-8000-000000000004")
        before_bytes = self.staff_file.read_bytes()
        calls: list[bytes] = []
        original = os.replace

        def record_publication(source: str | Path, destination: str | Path) -> None:
            if Path(destination) == self.staff_file:
                calls.append(Path(source).read_bytes())
            original(source, destination)

        with mock.patch.object(os, "replace", side_effect=record_publication):
            changed = staff.update_current_session(unit.id, new_id)
        self.assertEqual(changed, replace(unit, current_session=new_id))
        self.assertEqual(staff.by_id(runner.id), runner)
        self.assertEqual(staff.by_id(unit.id), changed)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(len(calls), 1)
        published = cast(dict[str, object], json.loads(calls[0]))
        rows = cast(list[dict[str, object]], published["rows"])
        self.assertTrue(any(row["id"] == worker.id and row["parent"] == unit.id for row in rows))
        self.assertTrue(any(row["id"] == unit.id and row["current_session"] == new_id for row in rows))
        self.assertEqual(self.staff_file.read_bytes(), before_bytes.replace(
            f'"current_session": "{unit.current_session}"'.encode(),
            f'"current_session": "{new_id}"'.encode(), 1))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(staff.update_current_session(unit.id, new_id), changed)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_running_session_lookup_after_clear_does_not_match_the_fixed_first_session(self) -> None:
        _, unit, worker = self.tree()
        live = staff.native_id(fixture_runtime_id("current-unit-after-clear"))
        changed = staff.update_current_session(unit.current_session, live)
        self.assertEqual(staff.by_id(unit.id), changed)
        self.assertEqual(staff.by_current_session(live), changed)
        self.assertIsNone(staff.by_current_session(unit.id))
        snapshot = staff.snapshot()
        self.assertEqual(snapshot.by_current_session(live), changed)
        self.assertIsNone(snapshot.by_current_session(unit.id))
        self.assertEqual(snapshot.by_id(worker.id), worker)

    def test_failed_runtime_publication_keeps_the_old_graph_and_all_other_stores(self) -> None:
        _, unit, worker = self.tree()
        untouched = self.root / "panes.json"
        _ = untouched.write_text('{"session_id":"old"}\n')
        parked = self.root / "units" / (unit.id + ".toml")
        parked.parent.mkdir()
        _ = parked.write_text('plan = "plan.md"\n')
        before = self.staff_file.read_bytes(), untouched.read_bytes(), parked.read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError("publication interrupted")):
            with self.assertRaises((OSError, staff.StaffUnreadable)):
                _ = staff.update_current_session(unit.id, staff.StaffId("00000000-0000-4000-8000-000000000005"))
        self.assertEqual((self.staff_file.read_bytes(), untouched.read_bytes(), parked.read_bytes()), before)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_id(unit.id), unit)

    def test_runtime_session_refuses_another_members_identity_without_a_write(self) -> None:
        _, unit, worker = self.tree()
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "already belongs"):
            _ = staff.update_current_session(worker.id, unit.id)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(worker.id), worker)


class RecordedWorkerRoleTests(StaffCommandTestCase):

    def test_rows_share_one_publication_for_admission_replacement_and_retirement(self) -> None:
        for action in ("admit", "replace", "retire"):
            for stage in ("before", "after"):
                with self.subTest(action=action, stage=stage), mock.patch.dict(
                    os.environ, {"STAFF_FILE": str(self.root / action / stage / "staff.json")},
                ):
                    owner = fixture_member("showrunner", name="owner")
                    identity = staff.native_id(fixture_runtime_id(f"atomic-{action}-{stage}"))
                    if action != "admit":
                        _ = staff.ensure_worker(identity, "impl2", owner.id)
                    path = Path(os.environ["STAFF_FILE"])
                    before = path.read_bytes()
                    current = staff.native_id(fixture_runtime_id(f"atomic-current-{action}-{stage}"))
                    proposals: list[dict[str, object]] = []
                    publish = os.replace

                    def interrupt(source: str | Path, destination: str | Path) -> None:
                        if Path(destination) != path:
                            publish(source, destination)
                            return
                        proposals.append(cast(dict[str, object], json.loads(Path(source).read_text())))
                        self.assertEqual(path.read_bytes(), before)
                        if stage == "after":
                            publish(source, destination)
                        raise OSError(f"{stage} atomic publication")

                    def change() -> object:
                        if action == "admit":
                            return staff.ensure_worker(identity, "impl2", owner.id)
                        if action == "replace":
                            return staff.update_current_session(identity, current)
                        return staff.retire(identity)

                    with mock.patch.object(os, "replace", side_effect=interrupt):
                        with self.assertRaises(staff.StaffUnreadable):
                            _ = change()
                    self.assertEqual(len(proposals), 1)
                    proposed = proposals[0]
                    rows = cast(list[dict[str, object]], proposed["rows"])
                    matching = [row for row in rows if row["id"] == identity]
                    if action == "retire":
                        self.assertEqual(matching, [])
                    else:
                        self.assertEqual(len(matching), 1)
                        self.assertEqual(matching[0]["current_session"], current if action == "replace" else identity)
                    if stage == "before":
                        self.assertEqual(path.read_bytes(), before)
                    else:
                        self.assertEqual(cast(object, json.loads(path.read_bytes())), proposed)
                    _ = change()
                    record = staff.snapshot()
                    if action == "retire":
                        self.assertIsNone(record.by_id(identity))
                    else:
                        row = record.by_id(identity)
                        assert row is not None
                        self.assertEqual(row.current_session, current if action == "replace" else identity)




class DocumentLookupTests(StaffCommandTestCase):
    def git(self, checkout: Path, *arguments: str) -> None:
        result = subprocess.run(["git", *arguments], cwd=checkout, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def checkouts(self) -> tuple[Path, Path, Path]:
        main = self.root / "main"
        main.mkdir()
        self.git(main, "init", "-b", "main")
        self.git(main, "config", "maintenance.auto", "false")
        self.git(main, "config", "user.name", "Document Test")
        self.git(main, "config", "user.email", "document@example.invalid")
        document = main / "docs/plans/example-production.md"
        document.parent.mkdir(parents=True)
        _ = document.write_text("# Example production\n")
        self.git(main, "add", ".")
        self.git(main, "commit", "-m", "Add production document")
        owner = self.root / "owner-checkout"
        unit = self.root / "unit-checkout"
        self.git(main, "worktree", "add", "-b", "owner", str(owner))
        self.git(main, "worktree", "add", "-b", "unit", str(unit))
        return main, owner, unit

    def test_document_lookup_joins_checkouts_without_an_external_command(self) -> None:
        main, owner, unit = self.checkouts()
        relative = Path("docs/plans/example-production.md")
        runner = fixture_member("showrunner", name="example", doc=staff.LiveDoc("natedev", owner / relative))
        git_file = unit / ".git"
        git_directory = Path(git_file.read_text().removeprefix("gitdir: ").strip())
        _ = git_file.write_text(f"gitdir: {os.path.relpath(git_directory, unit)}\n")
        separate = self.root / "separate-repository"
        separate.mkdir()
        self.git(separate, "init", "-b", "main")
        (separate / relative).parent.mkdir(parents=True)
        _ = (separate / relative).write_text((main / relative).read_text())
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")):
            self.assertEqual(record.by_doc("natedev", main / relative), runner)
            self.assertEqual(record.by_doc("natedev", unit / relative), runner)
            with chdir(unit):
                self.assertEqual(record.by_doc("natedev", relative), runner)
            self.assertIsNone(record.by_doc("mac", unit / relative))
            self.assertIsNone(record.by_doc("natedev", unit / "docs/other-production.md"))
            self.assertIsNone(record.by_doc("natedev", separate / relative))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_document_outside_git_matches_only_its_exact_path(self) -> None:
        document = self.root / "outside/example-production.md"
        document.parent.mkdir()
        _ = document.write_text("# Example production\n")
        copy = self.root / "another/example-production.md"
        copy.parent.mkdir()
        _ = copy.write_text(document.read_text())
        runner = fixture_member("showrunner", name="example", doc=staff.LiveDoc("natedev", document))
        record = staff.snapshot()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")):
            self.assertEqual(record.by_doc("natedev", document), runner)
            self.assertIsNone(record.by_doc("natedev", copy))

    def test_document_lookup_refuses_unreadable_git_metadata_after_no_exact_match(self) -> None:
        document = self.root / "owner/example-production.md"
        document.parent.mkdir()
        _ = document.write_text("# Example production\n")
        _ = fixture_member("showrunner", name="example", doc=staff.LiveDoc("natedev", document))
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        for name, metadata, unreadable in (("missing-gitdir", b"gitdir: missing-folder\n", "missing-folder"),
                                           ("invalid-encoding", b"gitdir: \xff\n", ".git")):
            with self.subTest(metadata=name):
                checkout = self.root / name
                checkout.mkdir()
                _ = (checkout / ".git").write_bytes(metadata)
                copy = checkout / "example-production.md"
                _ = copy.write_text(document.read_text())
                with (mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")),
                      self.assertRaises(staff.StaffRefused) as refusal):
                    _ = record.by_doc("natedev", copy)
                self.assertIn(str(copy), str(refusal.exception))
                self.assertIn(str(checkout / unreadable), str(refusal.exception))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_document_lookup_skips_an_unreadable_candidate_checkout(self) -> None:
        stale = self.root / "stale-checkout"
        stale.mkdir()
        _ = (stale / ".git").write_text("gitdir: missing-folder\n")
        relative = Path("docs/plans/example-production.md")
        _ = fixture_member("showrunner", name="stale", doc=staff.LiveDoc("natedev", stale / relative))
        _, owner, unit = self.checkouts()
        runner = fixture_member("showrunner", name="example", doc=staff.LiveDoc("natedev", owner / relative))
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")):
            self.assertEqual(record.by_doc("natedev", unit / relative), runner)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_exact_document_match_does_not_require_readable_git_metadata(self) -> None:
        checkout = self.root / "unreadable-checkout"
        checkout.mkdir()
        _ = (checkout / ".git").write_text("gitdir: missing-folder\n")
        document = checkout / "example-production.md"
        _ = document.write_text("# Example production\n")
        runner = fixture_member("showrunner", name="example", doc=staff.LiveDoc("natedev", document))
        record = staff.snapshot()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("No external command expected")):
            self.assertEqual(record.by_doc("natedev", document), runner)

    def test_document_lookup_refuses_multiple_repository_matches(self) -> None:
        main, owner, unit = self.checkouts()
        relative = Path("docs/plans/example-production.md")
        _ = fixture_member("showrunner", name="first", doc=staff.LiveDoc("natedev", main / relative))
        _ = fixture_member("showrunner", name="second", doc=staff.LiveDoc("natedev", owner / relative))
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = record.by_doc("natedev", unit / relative)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_exact_document_match_wins_over_a_repository_match(self) -> None:
        main, owner, unit = self.checkouts()
        relative = Path("docs/plans/example-production.md")
        _ = fixture_member("showrunner", name="first", doc=staff.LiveDoc("natedev", main / relative))
        exact = fixture_member("showrunner", name="second", doc=staff.LiveDoc("natedev", owner / relative))
        record = staff.snapshot()
        self.assertEqual(record.by_doc("natedev", owner / relative), exact)
        self.assertIsNone(record.by_doc("natedev", unit / "docs/other-production.md"))


class StaffTests(StaffCommandTestCase):
    def test_owner_lookup_returns_the_unit_for_a_worker_reference(self) -> None:
        showrunner, unit, worker = self.tree()
        record = staff.local_snapshot()
        for reference in (worker.id, worker.current_session, unit.id, unit.current_session):
            with self.subTest(reference=reference):
                self.assertEqual(record.owner_of(reference), unit)
        self.assertEqual(record.owner_of(showrunner.id), showrunner)
        self.assertIsNone(record.owner_of("absent"))

    def test_caller_identity_uses_codex_before_claude_and_allows_no_identity(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "codex", "CLAUDE_CODE_SESSION_ID": "claude"}):
            self.assertEqual(staff.caller_identity(), "codex")
            del os.environ["CODEX_THREAD_ID"]
            self.assertEqual(staff.caller_identity(), "claude")
            del os.environ["CLAUDE_CODE_SESSION_ID"]
            self.assertEqual(staff.caller_identity(), "")

    def test_pause_and_resume_one_unit_preserve_membership_and_allow_reruns(self) -> None:
        from scripts.production.fake_showrunner import write_session

        owner, unit, worker = self.tree()
        before = unit.to_json()
        caller = fixture_member("showrunner", name="fixture-director")
        listener = write_session(sessions.sessions_dir(), "registry-name", caller.current_session)
        self.addCleanup(listener.close)
        self.enterContext(mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}))
        _ = self.enterContext(mock.patch.object(staff, "runtime_id", return_value=str(caller.current_session)))
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.agents("pause", f"{owner.name}/{unit.name}", "--why", "review the change")
                self.assertEqual(result.returncode, 0, result.stderr)
                current = staff.by_id(unit.id)
                assert current is not None
                paused = cast(dict[str, object], current.to_json()["paused"])
                self.assertIsInstance(paused, dict)
                self.assertEqual(paused["why"], "review the change")
                self.assertTrue(paused["since"])
                self.assertEqual(paused["by"], "fixture-director")
                self.assertEqual({key: value for key, value in current.to_json().items() if key != "paused"}, before)
                self.assertEqual(staff.by_id(worker.id), worker)
        for attempt in range(2):
            with self.subTest(resume=attempt):
                result = self.agents("resume", f"{owner.name}/{unit.name}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(staff.by_id(unit.id), unit)

    def test_pause_records_the_codex_callers_staff_name(self) -> None:
        owner, unit, worker = self.tree()
        caller = fixture_worker("codex-director", owner.id, backend=staff.CodexBackend(self.root / "codex"))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": caller.current_session}):
            result = self.agents("pause", f"{owner.name}/{unit.name}", "--why", "review the change")
        self.assertEqual(result.returncode, 0, result.stderr)
        current = staff.by_id(unit.id)
        assert current is not None
        self.assertEqual(cast(dict[str, object], current.to_json()["paused"])["by"], caller.name)
        self.assertEqual(staff.by_id(worker.id), worker)

    def test_production_pause_and_resume_change_only_its_units(self) -> None:
        owner, first, worker = self.tree()
        second = fixture_member("unit", name="registry", parent=owner.id)
        other_owner = fixture_member("showrunner", name="hana")
        other = fixture_member("unit", name="other", parent=other_owner.id)
        _ = self.agents("pause", "--production", owner.name, "--why", "review the production")
        for original in (first, second):
            current = staff.by_id(original.id)
            assert current is not None
            self.assertEqual(cast(dict[str, object], current.to_json()["paused"])["why"], "review the production")
        self.assertEqual(staff.by_id(other.id), other)
        self.assertEqual(staff.by_id(worker.id), worker)
        _ = self.agents("resume", "--production", owner.name)
        self.assertEqual(staff.by_id(first.id), first)
        self.assertEqual(staff.by_id(second.id), second)

    def test_production_pause_except_keeps_each_named_unit_active(self) -> None:
        owner, selected, worker = self.tree()
        first = fixture_member("unit", name="registry", parent=owner.id)
        second = fixture_member("unit", name="builds", parent=owner.id)
        _ = self.agents("pause", "--production", owner.name, "--except", first.name, "--except", second.name,
                 "--why", "review the selected unit")
        current = staff.by_id(selected.id)
        assert current is not None
        self.assertEqual(cast(dict[str, object], current.to_json()["paused"])["why"], "review the selected unit")
        for original in (first, second, worker):
            self.assertEqual(staff.by_id(original.id), original)
        _ = self.agents("resume", "--production", owner.name)
        self.assertEqual(staff.by_id(selected.id), selected)

    def test_production_pause_except_accepts_the_unit_suffix(self) -> None:
        owner, selected, worker = self.tree()
        excluded = fixture_member("unit", name="fastest", parent=owner.id)
        result = self.agents("pause", "--production", owner.name, "--except", "fastest-unit",
                          "--why", "review the selected unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staff.by_id(excluded.id), excluded)
        self.assertEqual(staff.by_id(worker.id), worker)
        current = staff.by_id(selected.id)
        assert current is not None
        self.assertIsInstance(current.paused, staff.Paused)
        self.assertEqual(cast(staff.Paused, current.paused).why, "review the selected unit")

    def test_production_pause_except_accepts_the_unit_id(self) -> None:
        owner, selected, worker = self.tree()
        excluded = fixture_member("unit", name="fastest", parent=owner.id)
        result = self.agents("pause", "--production", owner.name, "--except", str(excluded.id),
                          "--why", "review the selected unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staff.by_id(excluded.id), excluded)
        self.assertEqual(staff.by_id(worker.id), worker)
        current = staff.by_id(selected.id)
        assert current is not None
        self.assertIsInstance(current.paused, staff.Paused)
        self.assertEqual(cast(staff.Paused, current.paused).why, "review the selected unit")

    def test_production_pause_except_prefers_the_exact_staff_name(self) -> None:
        owner, selected, worker = self.tree()
        alias = fixture_member("unit", name="a", parent=owner.id)
        exact = fixture_member("unit", name="a-unit", parent=owner.id)
        result = self.agents("pause", "--production", owner.name, "--except", exact.name,
                          "--why", "review the selected units")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staff.by_id(exact.id), exact)
        self.assertEqual(staff.by_id(worker.id), worker)
        for original in (selected, alias):
            current = staff.by_id(original.id)
            assert current is not None
            self.assertIsInstance(current.paused, staff.Paused)

    def test_production_pause_except_accepts_a_staff_name_with_spaces(self) -> None:
        owner, selected, worker = self.tree()
        excluded = fixture_member("unit", name="phase 9", parent=owner.id)
        result = self.agents("pause", "--production", owner.name, "--except", excluded.name,
                          "--why", "review the selected unit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staff.by_id(excluded.id), excluded)
        self.assertEqual(staff.by_id(worker.id), worker)
        current = staff.by_id(selected.id)
        assert current is not None
        self.assertIsInstance(current.paused, staff.Paused)

    def test_production_pause_refuses_an_unmatched_exclusion_without_a_write(self) -> None:
        owner = self.tree()[0]
        excluded = fixture_member("unit", name="kept", parent=owner.id)
        before = self.staff_state()
        result = self.agents("pause", "--production", owner.name, "--except", excluded.name,
                          "--except", "unknown-unit", "--why", "review the selected units", code=2)
        self.assertIn("StaffRefused", result.stderr)
        self.assertIn("unknown-unit", result.stderr)
        self.assertIn(owner.name, result.stderr)
        self.assertEqual(self.staff_state(), before)

    def test_production_pause_refuses_another_productions_unit_without_a_write(self) -> None:
        owner = self.tree()[0]
        other = fixture_member("showrunner", name="other")
        foreign = fixture_member("unit", name="foreign", parent=other.id)
        before = self.staff_state()
        for reference in (foreign.name, str(foreign.id)):
            with self.subTest(reference=reference):
                result = self.agents("pause", "--production", owner.name, "--except", reference,
                                  "--why", "review the selected units", code=2)
                self.assertIn("StaffRefused", result.stderr)
                self.assertIn(reference, result.stderr)
                self.assertIn(owner.name, result.stderr)
                self.assertEqual(self.staff_state(), before)

    def test_rename_and_move_preserve_the_pause_record(self) -> None:
        owner, unit, worker = self.tree()
        destination = fixture_member("showrunner", name="hana")
        _ = self.agents("pause", f"{owner.name}/{unit.name}", "--why", "review before moving")
        paused = staff.by_id(unit.id)
        assert paused is not None
        changed = staff.rename(unit.id, "registry")
        self.assertEqual(changed.to_json()["paused"], paused.to_json()["paused"])
        moved = staff.move(changed.id, staff.ReportsTo(destination.id))
        self.assertEqual(moved.to_json()["paused"], paused.to_json()["paused"])
        self.assertEqual(staff.by_id(worker.id), worker)

    def test_staff_fixture_keeps_registry_paths_under_its_private_folder(self) -> None:
        from scripts.lib.session_paths import sessions_directory
        from scripts.production import showrunners
        from scripts.agents import pane_record

        keys = ("SHOWRUNNER_STATE_DIR", "NOTIFIER_SESSIONS_DIR", "HERDR_PANE_RECORD_DIR", "XDG_CONFIG_HOME")
        root = self.root / "registry-fixture"
        environment: dict[str, str] = {}
        with (mock.patch.dict(os.environ, {key: "/fixture/live" for key in keys}),
              isolated_staff_file(root / "staff.json", environment)):
            for key in keys:
                with self.subTest(key=key):
                    self.assertTrue(Path(environment[key]).is_relative_to(root))
                    self.assertEqual(os.environ[key], environment[key])
            self.assertEqual(showrunners.SHOWRUNNER_STATE_DIR, Path(environment["SHOWRUNNER_STATE_DIR"]))
            self.assertEqual(sessions_directory(), Path(environment["NOTIFIER_SESSIONS_DIR"]))
            self.assertEqual(pane_record.record_directory(), Path(environment["HERDR_PANE_RECORD_DIR"]))

    def test_snapshots_keep_their_production_record_source(self) -> None:
        value: dict[str, object] = {"schema": 2, "rows": []}
        _ = self.write_document(value)
        self.assertFalse(staff.snapshot().production)
        self.assertFalse(staff.local_snapshot().production)
        self.assertFalse(staff.initial_snapshot().production)
        production_file = self.root / "production-staff.json"
        _ = production_file.write_text(json.dumps(value), encoding="utf-8")
        with (mock.patch.dict(os.environ),
              mock.patch.object(staff, "_default_file", return_value=production_file)):
            del os.environ["STAFF_FILE"]
            self.assertTrue(staff.snapshot().production)
            self.assertFalse(staff.local_snapshot().production)
            self.assertFalse(staff.initial_snapshot().production)
            self.assertEqual(staff.snapshot().inputs, (str(production_file.absolute()),))
            with (mock.patch.object(staff, "is_owner", return_value=False),
                  mock.patch.object(staff, "_remote", return_value=value)):
                self.assertTrue(staff.snapshot().production)

    def test_gate_module_writes_keep_the_inherited_staff_and_launch_folders_unchanged(self) -> None:
        from scripts.herdr import pane_launch
        from scripts.production import merge_gate

        inherited_state = self.root / "inherited-state"
        inherited_staff = inherited_state / "staff"
        inherited_launches = inherited_state / "herdr-panes"
        inherited_staff.mkdir(parents=True)
        inherited_launches.mkdir(parents=True)
        _ = (inherited_staff / "staff.json").write_text('{"rows": []}\n', encoding="utf-8")
        _ = (inherited_launches / "agents.json").write_text('{}\n', encoding="utf-8")

        def folder_bytes(folder: Path) -> dict[Path, bytes]:
            return {path.relative_to(folder): path.read_bytes()
                    for path in folder.rglob("*") if path.is_file()}

        before = folder_bytes(inherited_staff), folder_bytes(inherited_launches)
        module_root = self.root / "gate-module"
        module_root.mkdir()
        environment = merge_gate.test_module_environment(module_root, {
            **os.environ,
            "STAFF_FILE": str(inherited_staff / "staff.json"),
            "HERDR_PANE_RECORD_DIR": str(inherited_launches),
        })
        self.assertEqual(environment["STAFF_FILE"], str(module_root / "staff.json"))
        self.assertEqual(environment["HERDR_PANE_RECORD_DIR"], str(module_root / "herdr-panes"))
        with mock.patch.dict(os.environ, environment):
            owner = fixture_member("showrunner", name="gate-private")
            launch = pane_launch.launch("w1:p1", owner.id, str(module_root), {}, ["claude"])
            path = pane_launch.record_directory() / "agents.json"
            pane_launch.write(path, launch)
            self.assertEqual(staff.snapshot().by_id(owner.id), replace(owner,
                home=staff.SessionHome(Path.home() / ".claude")))
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"]["session_id"], owner.id)
        self.assertTrue((module_root / "staff.json").is_file())
        self.assertEqual((folder_bytes(inherited_staff), folder_bytes(inherited_launches)), before)

    def test_stopped_test_owner_removal_is_unchanged_on_a_second_run(self) -> None:
        row = fixture_member("showrunner", name="fixture",
            doc=staff.LiveDoc("natedev", self.root / "merge-gate-fixture/production.md"))
        with mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
            self.assertEqual(staff._remove_test_fixture(row.id), staff.FixtureRemoved(row))  # pyright: ignore[reportPrivateUsage]
            self.assertIsNone(staff.snapshot().by_id(row.id))
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            self.assertEqual(staff._remove_test_fixture(row.id), staff.FixtureAlreadyAbsent())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_primary_staff_write_refuses_test_root_documents_and_worktrees(self) -> None:
        runner, unit, _ = self.tree()
        before = self.staff_file.read_bytes()
        primary_home = self.root / "primary-home"
        primary_file = primary_home / ".local/state/staff/staff.json"
        primary_file.parent.mkdir(parents=True)
        _ = primary_file.write_bytes(before)
        for field in ("doc", "worktree"):
            path = Path("/dev/shm/merge-gate-fixture") / field
            with (self.subTest(field=field),
                  mock.patch.dict(os.environ, {"STAFF_FILE": "", "HOME": str(primary_home)}),
                  mock.patch.object(staff, "_file", return_value=primary_file)):
                with self.assertRaises(staff.StaffRefused) as refusal:
                    if field == "doc":
                        _ = staff.set_doc(runner.id, staff.LiveDoc("natedev", path))
                    else:
                        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
                self.assertIn(str(path), str(refusal.exception))
            self.assertEqual(primary_file.read_bytes(), before)

    def test_primary_staff_write_refuses_generic_linux_and_mac_temporary_roots(self) -> None:
        runner, unit, _ = self.tree()
        before = self.staff_file.read_bytes()
        primary_home = self.root / "primary-home"
        primary_file = primary_home / ".local/state/staff/staff.json"
        primary_file.parent.mkdir(parents=True)
        _ = primary_file.write_bytes(before)
        mac_temp = "/var/folders/fixture/T"
        for root in (Path("/tmp/lint-scope-test-case/case"), Path(mac_temp) / "tmp-fixture"):
            for field in ("doc", "worktree"):
                path = root / field
                with (self.subTest(path=path),
                      mock.patch.dict(os.environ, {"STAFF_FILE": "", "HOME": str(primary_home)}),
                      mock.patch.object(staff, "_file", return_value=primary_file),
                      mock.patch("tempfile.gettempdir", return_value=mac_temp),
                      self.assertRaises(staff.StaffRefused) as refusal):
                    if field == "doc":
                        _ = staff.set_doc(runner.id, staff.LiveDoc("natedev", path))
                    else:
                        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
                self.assertIn(str(path), str(refusal.exception))
                self.assertEqual(primary_file.read_bytes(), before)

    def test_primary_staff_write_refuses_fixture_ids_and_accepts_native_ids(self) -> None:
        primary_home = self.root / "guard-home"
        primary_file = primary_home / ".local/state/staff/staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": "", "HOME": str(primary_home)}):
            with self.assertRaisesRegex(staff.StaffRefused, "fixture runtime id cannot enter the real staff record"):
                _ = fixture_member("showrunner", name="fixture")
            self.assertFalse(primary_file.exists())

            native = staff.ensure("showrunner", name="native",
                                  runtime_id="019b3a40-4250-7000-8000-000000000101")
            before = primary_file.read_bytes()
            with self.assertRaisesRegex(staff.StaffRefused, "fixture runtime id cannot enter the real staff record"):
                _ = staff.update_current_session(native.current_session, staff.StaffId(fixture_runtime_id("restarted")))
            self.assertEqual(primary_file.read_bytes(), before)
            self.assertEqual(staff.snapshot().rows, (native,))

    def test_primary_staff_write_preserves_unchanged_fixture_rows_and_allows_cleanup(self) -> None:
        fixture = fixture_member("showrunner", name="fixture",
                                 doc=staff.LiveDoc("natedev", self.root / "fixture/production.md"))
        native = staff.ensure("showrunner", name="native",
                              runtime_id="019b3a40-4250-7000-8000-000000000101")
        primary_home = self.root / "guard-home"
        primary_file = primary_home / ".local/state/staff/staff.json"
        primary_file.parent.mkdir(parents=True)
        _ = primary_file.write_bytes(self.staff_file.read_bytes())
        with mock.patch.dict(os.environ, {"STAFF_FILE": "", "HOME": str(primary_home)}):
            changed = staff.rename(native.id, "renamed")
            self.assertEqual(changed.name, "renamed")
            self.assertEqual(staff.snapshot().by_id(fixture.id), fixture)
            before = primary_file.read_bytes()

            with self.assertRaisesRegex(staff.StaffRefused, "fixture runtime id cannot enter the real staff record"):
                _ = fixture_member("showrunner", name="new-fixture")
            self.assertEqual(primary_file.read_bytes(), before)
            with self.assertRaisesRegex(staff.StaffRefused, "fixture runtime id cannot enter the real staff record"):
                _ = staff.set_doc(fixture.id, staff.LiveDoc("natedev", Path("/srv/staff-guard/production.md")))
            self.assertEqual(primary_file.read_bytes(), before)

            with mock.patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
                self.assertEqual(staff._remove_test_fixture(fixture.id), staff.FixtureRemoved(fixture))  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(staff.snapshot().rows, (changed,))

    def test_primary_staff_update_keeps_unchanged_test_root_rows(self) -> None:
        runner = fixture_member("showrunner", name="stray",
            doc=staff.LiveDoc("natedev", Path("/dev/shm/merge-gate-existing/production.md")))
        unrelated = fixture_member("showrunner", name="other", runtime_id="019b3a40-4250-7000-8000-000000000102")
        primary_home = self.root / "primary-home"
        primary_file = primary_home / ".local/state/staff/staff.json"
        primary_file.parent.mkdir(parents=True)
        _ = primary_file.write_bytes(self.staff_file.read_bytes())
        with (mock.patch.dict(os.environ, {"STAFF_FILE": "", "HOME": str(primary_home)}),
              mock.patch.object(staff, "_file", return_value=primary_file)):
            changed = staff.rename(unrelated.id, "renamed")
            self.assertEqual(changed.name, "renamed")
            self.assertEqual(staff.snapshot().by_id(runner.id), runner)
        self.assertIn("/dev/shm/merge-gate-existing/production.md", primary_file.read_text())

    def test_worktree_guard_changes_only_the_recorded_expected_location(self) -> None:
        _, unit, _ = self.tree()
        initial = staff.PresentWorktree("natedev", self.root / "initial")
        settled = staff.RemovedWorktree("natedev", initial.path)
        filled = staff.set_worktree(unit.id, initial, expected=unit.worktree)
        changed = staff.set_worktree(unit.id, settled, expected=filled.worktree)
        self.assertEqual(changed.worktree, settled)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(staff.set_worktree(unit.id, settled, expected=initial), changed)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        for expected in (initial, staff.WorktreeUnfilled(),
                         staff.RemovedWorktree("mac", initial.path),
                         staff.RemovedWorktree("natedev", self.root / "different")):
            with self.subTest(expected=expected), self.assertRaises(staff.StaffRefused):
                _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", self.root / "third"), expected=expected)
            self.assertEqual(staff.by_id(unit.id), changed)
            self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_remote_worktree_guard_transports_the_full_expected_location(self) -> None:
        runner, _, _ = self.tree()
        initial = staff.PresentWorktree("natedev", self.root / "initial")
        unit = fixture_member("unit", name="remote-guard", parent=runner.id, worktree=initial)
        settled = staff.PresentWorktree("mac", self.root / "settled")
        with self.machine("mac"):
            changed = staff.set_worktree(unit.id, settled, expected=initial)
            self.assertEqual(changed.worktree, settled)
            self.assert_refused(lambda: staff.set_worktree(unit.id,
                staff.RemovedWorktree("mac", settled.path), expected=initial))
        self.assertEqual(staff.by_id(unit.id), changed)
        command = self.ssh_commands()[0]
        for flag, value in (("--expected-state", "present"), ("--expected-machine", "natedev"),
                            ("--expected-path", str(initial.path))):
            self.assertEqual(command[command.index(flag) + 1], value)

    def test_restored_unit_survives_its_previous_pending_retirement(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        location = staff.PresentWorktree("natedev", self.root / "models")
        unit = fixture_member("unit", name="models", parent=parent.id, worktree=location)
        staff.defer_retire((unit.id,))
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(location.machine, location.path)))
        staff.defer_retire((unit.id,))
        self.assertEqual(staff.snapshot().pending_retire, (unit.id,))

        restored = staff.restore_unit(unit.id, location)
        self.assertEqual(staff.snapshot().pending_retire, ())
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(unit.id), restored)
        self.assertEqual((restored.id, restored.kind, restored.name, restored.parent),
                         (unit.id, "unit", unit.name, staff.ReportsTo(parent.id)))
        self.assertEqual(restored.worktree, location)

    def test_restore_unit_keeps_the_retained_id_and_parent_and_rerun_writes_nothing(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        location = staff.PresentWorktree("natedev", self.root / "models")
        removed = fixture_member("unit", name="models", parent=parent.id,
                               worktree=staff.RemovedWorktree(location.machine, location.path))
        unit = staff.restore_unit(removed.id, location)
        self.assertEqual((unit.id, unit.kind, unit.name, unit.parent), (removed.id, "unit", "models", staff.ReportsTo(parent.id)))
        self.assertEqual(unit.worktree, location)
        self.assertEqual(staff.by_id(removed.id), unit)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(staff.restore_unit(removed.id, location), unit)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        document = staff.LiveDoc("natedev", self.root / "child-production.md")
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                promoted = staff.promote_unit(unit.id, name=unit.name, parent=parent.id, doc=document)
                self.assertEqual(promoted, replace(unit, kind="showrunner", worktree=staff.WORKTREE_UNFILLED, doc=document))
                child = fixture_member("unit", name="child-" + machine, parent=promoted.id)
                retained = fixture_member("worker", name="worker-" + machine, parent=promoted.id)
                destination = staff.UnitRole(unit.name, parent.id, location)
                demoted = staff.change_director(unit.id, destination)
                self.assertEqual(demoted, unit)
                self.assertEqual(staff.by_id(child.id), replace(child, parent=staff.ReportsTo(parent.id)))
                self.assertEqual(staff.by_id(retained.id), retained)
                unchanged = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
                self.assertEqual(staff.change_director(unit.id, destination), unit)
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), unchanged)
                self.assert_refused(lambda: staff.change_director(retained.id, destination))
                self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), unchanged)

    def test_restore_unit_refuses_missing_identity_or_another_member_without_writing(self) -> None:
        parent, unit, worker = self.tree()
        location = staff.PresentWorktree("natedev", self.root / "models")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for held_id in (parent.id, worker.id, unit.id):
            with self.subTest(held_id=held_id):
                self.assert_refused(lambda: staff.restore_unit(held_id, location))
        with self.assertRaises(staff.StaffNotFound):
            _ = staff.restore_unit(MISSING_ID, location)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_restore_unit_refuses_a_different_retained_location_without_writing(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        removed = fixture_member("unit", name="models", parent=parent.id,
                               worktree=staff.RemovedWorktree("natedev", self.root / "models"))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        for proposed in (staff.PresentWorktree("mac", self.root / "models"),
                         staff.PresentWorktree("natedev", self.root / "elsewhere")):
            with self.subTest(proposed=proposed):
                self.assert_refused(lambda: staff.restore_unit(removed.id, proposed))
        self.assertEqual(staff.by_id(removed.id), removed)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_restore_unit_from_mac_uses_the_owner_and_is_idempotent_through_the_cli(self) -> None:
        parent = fixture_member("showrunner", name="sdlc")
        location = staff.PresentWorktree("mac", self.root / "models")
        removed = fixture_member("unit", name="models", parent=parent.id,
                               worktree=staff.RemovedWorktree(location.machine, location.path))
        with self.machine("mac"):
            restored = staff.restore_unit(removed.id, location)
            before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
            self.assertEqual(staff.restore_unit(removed.id, location), restored)
        self.assertEqual((restored.id, restored.kind, restored.name, restored.parent),
                         (removed.id, "unit", "models", staff.ReportsTo(parent.id)))
        self.assertEqual(restored.worktree, location)
        self.assertEqual(staff.by_id(removed.id), restored)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(self.ssh_commands(), [
            ["restore-unit", removed.id,
             "--machine", location.machine, "--path", str(location.path), "--json"],
            ["restore-unit", removed.id,
             "--machine", location.machine, "--path", str(location.path), "--json"],
        ])
        self.assertFalse(self.mac_file.exists())
        _ = self.assert_rerun("restore-unit", removed.id,
                              "--machine", location.machine, "--path", str(location.path))
        self.assertEqual(staff.by_id(removed.id), restored)

    def test_admission_publishes_identity_and_location_once_without_a_setter_window(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        location = staff.PresentWorktree("natedev", self.root / "models")
        original = os.replace
        publications: list[dict[str, object]] = []

        def observe(temporary: Path, destination: Path) -> None:
            original(temporary, destination)
            if destination == self.staff_file:
                publications.append(cast(dict[str, object], json.loads(self.staff_file.read_text())))

        with (mock.patch.object(os, "replace", side_effect=observe),
              mock.patch.object(staff, "set_worktree", side_effect=AssertionError("second write"))):
            unit = fixture_member("unit", name="models", parent=runner.id, worktree=location)
        self.assertEqual(len(publications), 1)
        published = cast(list[dict[str, object]], publications[0]["rows"])
        self.assertEqual(next(row for row in published if row["id"] == unit.id), unit.to_json())
        self.assertEqual(unit.worktree, location)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(fixture_member("unit", name="models", parent=runner.id, worktree=location), unit)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_admission_fills_unfilled_locations_but_refuses_wrong_kind_duplicates_and_resurrection(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.root / "doc.md"))
        blank = fixture_member("unit", name="models", parent=runner.id)
        location = staff.PresentWorktree("natedev", self.root / "models")
        filled = fixture_member("unit", name="models", parent=runner.id, worktree=location)
        self.assertEqual(filled, replace(blank, worktree=location))
        self.assert_refused(lambda: fixture_member("showrunner", name="wrong", worktree=location))
        self.assert_refused(lambda: fixture_member("unit", name="wrong", parent=runner.id, doc=runner.doc))
        self.assert_refused(lambda: fixture_member("unit", name="duplicate", parent=runner.id, worktree=location))
        self.assert_refused(lambda: fixture_member("unit", name="models", parent=runner.id,
                                               worktree=staff.PresentWorktree("natedev", self.root / "other")))
        removed = staff.set_worktree(filled.id, staff.RemovedWorktree(location.machine, location.path), expected=filled.worktree)
        self.assert_refused(lambda: fixture_member("unit", name="models", parent=runner.id, worktree=location))
        self.assertEqual(staff.by_id(removed.id), removed)

    def test_remote_admission_sends_the_location_in_the_owner_transaction(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        with self.machine("mac"):
            location = staff.PresentWorktree("mac", self.root / "mac-models")
            unit = fixture_member("unit", name="models", parent=runner.id, worktree=location)
            self.assertEqual(unit.worktree, location)
            self.assertEqual(staff.by_id(unit.id), unit)
            self.assertFalse(self.mac_file.exists())
        self.assertEqual(staff.by_id(unit.id), unit)

    def test_active_filters_removed_and_wrapped_rows_but_raw_identity_keeps_every_state(self) -> None:
        runners = [fixture_member("showrunner", name=name, doc=doc) for name, doc in (
            ("unfilled", staff.DocUnfilled()),
            ("live", staff.LiveDoc("natedev", self.root / "live.md")),
            ("wrapped", staff.WrappedDoc("natedev", self.root / "archive.md")))]
        units = [fixture_member("unit", name=name, parent=runners[0].id, worktree=worktree)
                 for name, worktree in (("unfilled", staff.WorktreeUnfilled()),
                     ("present", staff.PresentWorktree("natedev", self.root / "present")),
                     ("removed", staff.RemovedWorktree("natedev", self.root / "removed")))]
        record = staff.snapshot()
        self.assertCountEqual(staff.active(record, "unit"), units[:2])
        self.assertCountEqual(staff.active(record, "showrunner"), runners[:2])
        for row in (*runners, *units):
            self.assertEqual(record.by_id(row.id), row)
        self.assertEqual(record.by_path("unfilled/removed"), units[2])
        self.assertCountEqual(record.find(), [*runners, *units])

    def test_local_and_remote_retire_keep_owners_and_remove_selected_workers_and_agents(self) -> None:
        runner, unit, worker = self.tree()
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                for row in (runner, unit):
                    self.assert_refused(lambda: staff.retire(row.id))
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                agent = fixture_member("agent", name="test-agent-" + machine)
                self.assertEqual(staff.retire(agent.id), agent)
                self.assertIsNone(staff.by_id(agent.id))
        self.assertEqual(staff.retire(worker.id), worker)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(runner.id), runner)

    def test_pending_retirement_keeps_ids_and_edges_and_waits_for_workers_and_known_locations(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.root / "doc.md"))
        child = fixture_member("showrunner", name="child", parent=runner.id)
        unit = fixture_member("unit", name="models", parent=runner.id,
                            worktree=staff.PresentWorktree("natedev", self.root / "models"))
        worker = fixture_worker("impl1", unit.id)
        staff.defer_retire((runner.id, unit.id, child.id))
        self.assertCountEqual(staff.retire_pending(), (runner.id, unit.id, child.id))
        self.assertEqual(staff.by_id(worker.id), worker)
        _ = staff.retire(worker.id)
        self.assertEqual(staff.retire_pending(), (child.id,))
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree("natedev", self.root / "models")))
        self.assertEqual(staff.by_id(runner.id), replace(runner, doc=staff.WrappedDoc("natedev", self.root / "doc.md")))
        self.assertEqual(staff.by_id(child.id), child)
        before = self.staff_file.read_bytes()
        self.assertEqual(staff.retire_pending(), (child.id,))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_retirement_retains_the_removed_row_and_cannot_reactivate_it_from_stale_membership(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id,
                            worktree=staff.PresentWorktree("natedev", self.root / "models"))
        retirement = staff.SeedInput((), (staff.SeedRetirement(unit.id, runner.id),))
        record = staff.apply_seed(retirement)
        removed = replace(unit, worktree=staff.RemovedWorktree("natedev", self.root / "models"))
        self.assertEqual(record.by_id(unit.id), removed)
        self.assertEqual(record.by_id(runner.id), runner)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(staff.apply_seed(retirement).by_id(unit.id), removed)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        stale = staff.SeedInput((staff.SeedMember("unit", "unit", unit.name, unit.id, staff.KeepSeedParent()),))
        self.assertEqual(staff.apply_seed(stale).by_id(unit.id), removed)
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_schema_one_rewrites_as_three_without_inventing_worktrees_or_docs(self) -> None:
        runner, unit, worker = self.tree()
        _ = self.write_document({"schema": 1, "rows": [row.to_json() for row in (runner, unit, worker)]})
        record = staff.snapshot()
        read_unit, read_runner = record.by_id(unit.id), record.by_id(runner.id)
        assert read_unit is not None and read_runner is not None
        self.assertIsInstance(read_unit.worktree, staff.WorktreeUnfilled)
        self.assertIsInstance(read_runner.doc, staff.DocUnfilled)
        _ = staff.rename(unit.id, "renamed")
        document = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        self.assertEqual(document["schema"], 2)
        rows = cast(list[dict[str, object]], document["rows"])
        self.assertEqual({row["id"] for row in rows}, {runner.id, unit.id, worker.id})
        self.assertTrue(all("worktree" not in row and "doc" not in row for row in rows))

    def test_staff_writes_keep_unknown_row_location_and_pause_fields(self) -> None:
        runner, unit, worker = self.tree()
        for row, other, key, state in ((unit, runner, "worktree", "present"),
                                       (unit, runner, "worktree", "removed"),
                                       (runner, unit, "doc", "live"),
                                       (runner, unit, "doc", "wrapped")):
            with self.subTest(key=key, state=state), redirect_stderr(io.StringIO()) as errors:
                value = {**row.to_json(), "future_row": {"items": [False, None, 3]},
                         key: {"machine": "natedev", "path": str(self.root / state), "state": state,
                               "future_location": ["retained", {"revision": 2}]},
                         "paused": {"since": "2026-11-06T16:54:00-05:00", "by": "owner", "why": "review",
                                    "future_pause": {"enabled": True}}}
                _ = self.write_document({"schema": 2, "rows": [value, other.to_json(), worker.to_json()]})
                current = staff.snapshot().by_id(row.id)
                assert current is not None
                self.assertEqual(current.unknown_fields, {"future_row": value["future_row"]})
                location = current.worktree if key == "worktree" else current.doc
                assert isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc))
                self.assertEqual(location.unknown_fields, {"future_location": ["retained", {"revision": 2}]})
                assert isinstance(current.paused, staff.Paused)
                self.assertEqual(current.paused.unknown_fields, {"future_pause": {"enabled": True}})
                _ = staff.rename(other.id, "other-" + state)
                stored = cast(list[dict[str, object]], json.loads(self.staff_file.read_text())["rows"])
                self.assertEqual(next(item for item in stored if item["id"] == row.id), value)
                changed = staff.rename(row.id, "changed-" + state)
                expected = {**value, "name": changed.name}
                stored = cast(list[dict[str, object]], json.loads(self.staff_file.read_text())["rows"])
                self.assertEqual(next(item for item in stored if item["id"] == row.id), expected)
                self.assertEqual(errors.getvalue(), "")

    def test_owner_location_changes_keep_unknown_fields(self) -> None:
        runner, unit = self.tree()[:2]
        kept = {"future_location": {"items": [False, None, 3]}}
        for action in ("set_worktree", "set_doc", "restore", "retire_unit", "retire_showrunner"):
            with self.subTest(action=action):
                worktree = (staff.RemovedWorktree("natedev", self.root / "unit")
                            if action in ("restore", "retire_showrunner") else
                            staff.PresentWorktree("natedev", self.root / "unit"))
                doc = staff.LiveDoc("natedev", self.root / "doc.md")
                unit_value = replace(unit, worktree=worktree).to_json()
                runner_value = replace(runner, doc=doc).to_json()
                unit_value["worktree"] = {**cast(dict[str, object], unit_value["worktree"]), **kept}
                runner_value["doc"] = {**cast(dict[str, object], runner_value["doc"]), **kept}
                _ = self.write_document({"schema": 2, "rows": [runner_value, unit_value]})
                target, key, path, state = unit, "worktree", worktree.path, "present"
                if action == "set_worktree":
                    path = self.root / "new-unit"
                    _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=worktree)
                elif action == "set_doc":
                    target, key, path, state = runner, "doc", self.root / "new-doc.md", "live"
                    _ = staff.set_doc(runner.id, staff.LiveDoc("natedev", path))
                elif action == "restore":
                    _ = staff.restore_unit(unit.id, staff.PresentWorktree("natedev", path))
                else:
                    if action == "retire_showrunner":
                        target, key, path, state = runner, "doc", doc.path, "wrapped"
                    else:
                        state = "removed"
                    staff.defer_retire((target.id,))
                    self.assertEqual(staff.retire_pending(), ())
                stored = cast(list[dict[str, object]], json.loads(self.staff_file.read_text())["rows"])
                location = next(item for item in stored if item["id"] == target.id)[key]
                self.assertEqual(location, {"machine": "natedev", "path": str(path), "state": state, **kept})

    def test_staff_read_requires_known_row_location_and_pause_fields(self) -> None:
        runner, unit, worker = self.tree()
        for key in ("kind", "id", "name", "parent"):
            with self.subTest(row_field=key):
                value = unit.to_json()
                del value[key]
                _ = self.write_document({"schema": 2, "rows": [runner.to_json(), value, worker.to_json()]})
                with self.assertRaisesRegex(staff.StaffUnreadable, "staff row must contain id, kind, name and parent"):
                    _ = staff.snapshot()
        for key, required in (("worktree", {"machine": "natedev", "path": str(self.root), "state": "present"}),
                              ("paused", {"since": "today", "by": "owner", "why": "review"})):
            for missing in required:
                with self.subTest(record=key, missing=missing):
                    value = {**unit.to_json(), key: {name: item for name, item in required.items() if name != missing}}
                    _ = self.write_document({"schema": 2, "rows": [runner.to_json(), value, worker.to_json()]})
                    with self.assertRaises(staff.StaffUnreadable):
                        _ = staff.snapshot()
        for key in ("since", "by", "why"):
            with self.subTest(pause_string=key):
                pause: dict[str, object] = {"since": "today", "by": "owner", "why": "review", key: False}
                _ = self.write_document({"schema": 2, "rows": [runner.to_json(),
                    {**unit.to_json(), "paused": pause}, worker.to_json()]})
                with self.assertRaisesRegex(staff.StaffUnreadable, "paused must contain since, by and why as strings"):
                    _ = staff.snapshot()

    def test_partial_fill_survives_an_ordinary_staff_write(self) -> None:
        runner, unit, _ = self.tree()
        other = fixture_member("unit", name="unfilled", parent=runner.id)
        location = staff.PresentWorktree("natedev", self.root / "filled")
        _ = staff.set_worktree(unit.id, location, expected=unit.worktree)
        worker = fixture_worker("test", other.id)
        record = staff.snapshot()
        filled = record.by_id(unit.id)
        assert filled is not None
        self.assertEqual(filled.worktree, location)
        for row_id in (runner.id, other.id, worker.id):
            row = record.by_id(row_id)
            assert row is not None
            self.assertNotIn("worktree", row.to_json())
            self.assertNotIn("doc", row.to_json())
        self.assertEqual(json.loads(self.staff_file.read_text())["schema"], 2)

    def test_worktree_and_doc_fields_are_admitted_only_on_their_row_kinds(self) -> None:
        runner, unit, worker = self.tree()
        for row, key, state in ((runner, "worktree", "present"), (worker, "worktree", "removed"),
                                (unit, "doc", "live"), (worker, "doc", "wrapped")):
            with self.subTest(kind=row.kind, key=key), self.assertRaises(staff.StaffUnreadable):
                _ = staff.StaffRow.from_json({**row.to_json(), key: {
                    "machine": "natedev", "path": str(self.root / "location"), "state": state,
                }})
        for row, key, state in ((unit, "worktree", "present"), (runner, "doc", "live")):
            for location in ({"machine": "natedev", "path": "relative", "state": state},
                             {"machine": "natedev", "path": str(self.root), "state": "guessed"}):
                with self.subTest(key=key, location=location), self.assertRaises(staff.StaffUnreadable):
                    _ = staff.StaffRow.from_json({**row.to_json(), key: location})

    def test_worktree_and_doc_setters_are_idempotent_and_refuse_duplicate_live_locations(self) -> None:
        runner, unit, worker = self.tree()
        other = fixture_member("unit", name="other", parent=runner.id)
        path, doc = self.root / "worktree", self.root / "production.md"
        _ = self.assert_rerun("set-worktree", unit.id, "--machine", "natedev", "--path", str(path), "--state", "present",
                              "--expected-state", "unfilled")
        _ = self.assert_rerun("set-doc", runner.id, "--machine", "natedev", "--path", str(doc), "--state", "live")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
        _ = staff.set_doc(runner.id, staff.LiveDoc("natedev", doc))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assert_refused(lambda: staff.set_worktree(other.id, staff.PresentWorktree("natedev", path), expected=other.worktree))
        for wrong in (runner, worker):
            self.assert_refused(lambda: staff.set_worktree(wrong.id, staff.PresentWorktree("natedev", path), expected=wrong.worktree))
        for wrong in (unit, worker):
            self.assert_refused(lambda: staff.set_doc(wrong.id, staff.LiveDoc("natedev", doc)))
        _ = staff.set_worktree(unit.id, staff.RemovedWorktree("natedev", path), expected=staff.PresentWorktree("natedev", path))
        claimed = staff.set_worktree(other.id, staff.PresentWorktree("natedev", path), expected=other.worktree)
        self.assertEqual(staff.snapshot().by_worktree("natedev", path), claimed)
        wrapped = staff.set_doc(runner.id, staff.WrappedDoc("natedev", doc))
        self.assertEqual(staff.StaffRow.from_json(wrapped.to_json()), wrapped)

    def test_snapshot_finds_present_worktrees_by_machine_and_containing_path(self) -> None:
        runner, unit, _ = self.tree()
        path = self.root / "checkout"
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
        _ = staff.set_doc(runner.id, staff.LiveDoc("natedev", path / "production.md"))
        record = staff.snapshot()
        self.assertEqual(record.by_worktree("natedev", path), record.by_id(unit.id))
        self.assertEqual(record.by_worktree("natedev", path / "src/file.py"), record.by_id(unit.id))
        self.assertIsNone(record.by_worktree("mac", path))
        self.assertIsNone(record.by_worktree("natedev", self.root / "checkout-other"))
        self.assertEqual(record.by_doc("natedev", path / "production.md"), record.by_id(runner.id))
        self.assertIsNone(record.by_doc("mac", path / "production.md"))
        self.assertIsNone(record.by_doc("natedev", path / "other.md"))
        _ = staff.set_worktree(unit.id, staff.RemovedWorktree("natedev", path), expected=staff.PresentWorktree("natedev", path))
        self.assertIsNone(staff.snapshot().by_worktree("natedev", path))

    def test_remote_setters_use_one_owner_write_and_preserve_the_other_field(self) -> None:
        runner, unit, _ = self.tree()
        path = self.root / "mac" / "worktree"
        with self.machine("mac"):
            self.assertNotIn("STAFF_FILE", os.environ)
            changed = staff.set_worktree(unit.id, staff.PresentWorktree("mac", path), expected=unit.worktree)
            self.assertEqual(changed.worktree, staff.PresentWorktree("mac", path))
            changed_doc = staff.set_doc(runner.id, staff.LiveDoc("mac", path / "production.md"))
        commands = self.ssh_commands()
        self.assertEqual([command[0] for command in commands], ["set-worktree", "set-doc"])
        self.assertEqual(staff.by_id(unit.id), changed)
        self.assertEqual(staff.by_id(runner.id), changed_doc)
        self.assertFalse(self.mac_file.exists())

    def test_launcher_names_are_accepted_without_a_fixed_role_suffix(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id)
        for prefix, slot in (("bevy_brp", "impl"), ("hana_catalyst-lfo", "review"),
                             ("cargo-liner", "berth-hook"), ("cargo-liner-style", "modules-judge"),
                             ("claude-build-followups-two-accounts", "writer"),
                             ("cargo-liner-tile", "review_m73"), ("3D.Project", "audit")):
            with self.subTest(prefix=prefix, slot=slot):
                result = subprocess.run(["bash", str(CHECKOUT / "scripts/direct/worker_name.sh"),
                                         str(self.root), slot, "impl", prefix],
                                        capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                name = result.stdout.strip()
                _ = staff.WorkerName.parse(name)
                row = fixture_worker(name, unit.id)
                self.assertEqual(row.name, name)
                self.assertEqual(staff.by_id(row.id), row)

    def test_pending_showrunner_retirement_waits_for_direct_workers_without_rolling_back_other_rows(self) -> None:
        runner = fixture_member("showrunner", name="sdlc", doc=staff.LiveDoc("natedev", self.root / "sdlc.md"))
        worker = fixture_worker("sdlc-handler-astra-reviewer", runner.id)
        other = fixture_member("showrunner", name="unheld", doc=staff.LiveDoc("natedev", self.root / "unheld.md"))
        staff.defer_retire((runner.id, other.id))
        self.assertEqual(staff.retire_pending(), (runner.id,))
        self.assertEqual(staff.by_id(other.id), replace(other, doc=staff.WrappedDoc("natedev", self.root / "unheld.md")))
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assert_refused(lambda: staff.retire(runner.id))
        _ = staff.retire(worker.id)
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(runner.id), replace(runner, doc=staff.WrappedDoc("natedev", self.root / "sdlc.md")))

    def test_row_write_transport_refuses_unit_and_showrunner_retirement(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id)
        before = self.staff_file.read_bytes()
        refused = self.cli("retire", unit.id, "--row-write", "--json", code=2)
        self.assertIn("worker", refused.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)
        empty = fixture_member("showrunner", name="empty")
        before = self.staff_file.read_bytes()
        refused = self.cli("retire", empty.id, "--row-write", "--json", code=2)
        self.assertIn("worker", refused.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_showrunner_worker_paths_printed_by_show_round_trip_through_get(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        worker = fixture_worker("sdlc-handler-astra-reviewer", runner.id)
        line = next(line for line in self.cli("show").stdout.splitlines() if line.startswith(worker.id))
        path = line.split()[2]
        self.assertEqual(staff.by_path(path), worker)
        self.assertEqual(staff.snapshot().by_path(path), worker)
        self.assertEqual(cast(object, json.loads(self.cli("get", path, "--json").stdout)), worker.to_json())

    def assert_refused(self, action: Callable[[], object]) -> None:
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = action()
        self.assertEqual(self.staff_file.read_bytes(), before)

    def assert_rerun(self, *arguments: str) -> object:
        first = self.cli(*arguments, "--json")
        before = self.staff_file.read_bytes()
        second = self.cli(*arguments, "--json")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn("unchanged", second.stderr)
        initial = cast(object, json.loads(first.stdout))
        repeated = cast(object, json.loads(second.stdout))
        if arguments[0] == "retire":
            self.assertIsNone(repeated)
        else:
            self.assertEqual(repeated, initial)
        return initial

    def run_writers(self, kind: staff.Kind, names: tuple[str, str], parent: staff.StaffId | None) -> list[str]:
        context = multiprocessing.get_context("fork")
        ready = context.Barrier(3)
        processes: list[BaseProcess] = []
        results: list[Connection] = []
        try:
            for name in names:
                receive, send = context.Pipe(duplex=False)
                # fork runs on Unix, where Pipe returns Connection; typeshed also
                # includes the Windows-only PipeConnection alternative here.
                results.append(cast(Connection, cast(object, receive)))
                process = context.Process(target=concurrent_ensure, args=(ready, send, kind, name, parent))
                processes.append(process)
                process.start()
                send.close()
            _ = ready.wait(timeout=TEST_HANG_GUARD_SECS)
            for process in processes:
                process.join(timeout=TEST_HANG_GUARD_SECS)
                self.assertFalse(process.is_alive(), "concurrent staff writer did not finish")
                self.assertEqual(process.exitcode, 0)
            ids: list[str] = []
            for result in results:
                success, value = cast(tuple[bool, str], result.recv())
                self.assertTrue(success, value)
                ids.append(value)
            return ids
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=TEST_HANG_GUARD_SECS)
                process.close()
            for result in results:
                result.close()

    def test_worker_names_accept_numbered_and_custom_slots_and_refuse_invalid_shapes(self) -> None:
        for role in ("impl", "test", "fix", "review", "designer", "reviewer"):
            for number in (1, 12):
                with self.subTest(role=role, number=number):
                    name = staff.WorkerName.parse(f"{role}{number}")
                    self.assertEqual(name.value, f"{role}{number}")
        for text in ("impl0", "bob1", "impl-1", "impl1x"):
            with self.subTest(name=text):
                self.assertEqual(staff.WorkerName.parse(text).value, text)
        for invalid in ("", " impl1", "impl1 ", "impl 1", "impl/1", "impl1\n", "impl\t1", "impl\x001"):
            with self.subTest(invalid=invalid), self.assertRaises(staff.StaffRefused):
                _ = staff.WorkerName.parse(invalid)

    def test_live_worker_names_preserve_their_full_identity(self) -> None:
        names = (
            "impl", "impl2", "test", "review2", "fix3", "code1", "ui2",
            "widget-impl", "widget-test2", "sdlc-handler-astra-designer",
            "sdlc-handler-astra-reviewer",
        )
        for text in names:
            with self.subTest(name=text):
                parsed = staff.WorkerName.parse(text)
                self.assertEqual(parsed.value, text)

    def test_ensure_worker_reuses_its_row_after_unit_move_and_rename(self) -> None:
        runner = fixture_member("showrunner", name="sdlc")
        unit = fixture_member("unit", name="models", parent=runner.id)
        runtime_id = fixture_runtime_id("moved-worker-backend")
        worker = fixture_worker("impl", unit.id, runtime_id=runtime_id)
        before = self.staff_file.read_bytes()
        self.assertEqual(fixture_worker("impl", unit.id), worker)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(worker.id, runtime_id)
        self.assertNotIn(worker.id, (runner.id, unit.id))
        destination = fixture_member("showrunner", name="tools")
        _ = staff.move(unit.id, staff.ReportsTo(destination.id))
        _ = staff.rename(unit.id, "registry")
        self.assertEqual(fixture_worker("impl", unit.id), worker)
        self.assertEqual(staff.by_path("tools/registry/impl"), worker)
        self.assertEqual(set(worker.to_json()), {"id", "kind", "name", "parent", "current_session"})
        _ = staff.retire(worker.id)
        self.assertIsNone(staff.by_id(worker.id))

    def test_workers_accept_unit_and_showrunner_parents_and_refuse_invalid_parents(self) -> None:
        runner, unit, ordinary = self.tree()
        designer = fixture_worker("sdlc-handler-astra-designer", runner.id)
        reviewer = fixture_worker("sdlc-handler-astra-reviewer", runner.id)
        self.assertEqual((designer.parent, reviewer.parent), (staff.ReportsTo(runner.id), staff.ReportsTo(runner.id)))
        self.assertEqual(fixture_worker(designer.name, runner.id), designer)
        direct_test = fixture_worker("test", runner.id)
        self.assertEqual(staff.by_path("sdlc/test"), direct_test)
        for parent, name in ((ordinary.id, "fix"), (MISSING_ID, "impl")):
            with self.subTest(parent=parent, name=name):
                self.assert_refused(lambda: fixture_worker(name, parent))
        self.assert_refused(lambda: fixture_member("worker", name="impl"))
        unit_reviewer = fixture_worker("review", unit.id)
        self.assertEqual(unit_reviewer.parent, staff.ReportsTo(unit.id))

    def test_row_round_trip_is_frozen_and_stores_only_membership(self) -> None:
        _, _, worker = self.tree()
        self.assertEqual(set(worker.to_json()), {"id", "kind", "name", "parent", "current_session"})
        self.assertEqual(staff.StaffRow.from_json(worker.to_json()), worker)
        with self.assertRaises(FrozenInstanceError):
            setattr(worker, "name", "test1")

    def test_write_verbs_are_rerun_safe_and_cli_reports_unchanged(self) -> None:
        _ = self.assert_rerun("ensure", "showrunner", "--name", "sdlc",
                              "--runtime-id", fixture_runtime_id("cli-sdlc"))
        showrunner = staff.by_path("sdlc")
        assert showrunner is not None
        _ = self.assert_rerun("ensure", "unit", "--name", "models", "--parent", showrunner.id,
                              "--runtime-id", fixture_runtime_id("cli-models"))
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.assert_rerun("ensure-worker", fixture_runtime_id("cli-impl1"), "--name", "impl1",
                              "--parent", unit.id)
        self.assertEqual(len(staff.find(kind="worker", parent=unit.id)), 1)
        _ = self.assert_rerun("rename", unit.id, "--name=registry")
        other = fixture_member("showrunner", name="natedev")
        _ = self.assert_rerun("move", unit.id, f"--parent={other.id}")
        _ = self.assert_rerun("move", other.id, f"--parent={showrunner.id}")
        _ = self.assert_rerun("move", other.id, "--parent=none")
        worker = staff.find(kind="worker", parent=unit.id)[0]
        _ = self.assert_rerun("retire", worker.id)
        before = self.staff_file.read_bytes()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertIsNone(staff.retire(worker.id))
        self.assertIn("unchanged", errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_api_noops_return_the_existing_row_and_preserve_bytes(self) -> None:
        showrunner, unit, worker = self.tree()
        for row in (showrunner, unit, worker):
            before = self.staff_file.read_bytes()
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(fixture_member(row.kind, name=row.name, parent=row.parent), row)
            self.assertIn("unchanged", errors.getvalue())
            self.assertEqual(self.staff_file.read_bytes(), before)
        for action in (lambda: staff.rename(unit.id, unit.name), lambda: staff.move(unit.id, unit.parent)):
            before = self.staff_file.read_bytes()
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(action(), unit)
            self.assertIn("unchanged", errors.getvalue())
            self.assertEqual(self.staff_file.read_bytes(), before)

    def test_names_are_unique_within_kind_and_parent(self) -> None:
        showrunner, unit, first = self.tree()
        other = fixture_member("unit", name="other", parent=showrunner.id)
        second = fixture_member("worker", name="impl1", parent=other.id)
        self.assertNotEqual(first.id, second.id)
        self.assert_refused(lambda: staff.rename(other.id, unit.name))
        another_showrunner = fixture_member("showrunner", name="natedev")
        conflict = fixture_member("unit", name="models", parent=another_showrunner.id)
        self.assert_refused(lambda: staff.move(conflict.id, staff.ReportsTo(showrunner.id)))
        self.assert_refused(lambda: staff.rename(another_showrunner.id, showrunner.name))
        # Promotion needs both kinds to coexist briefly under the same short name.
        promoted = fixture_member("showrunner", name=unit.name)
        self.assertNotEqual(promoted.id, unit.id)

    def test_valid_worker_shapes_and_holders_coexist_with_refused_graph_and_worker_mutations(self) -> None:
        showrunner, unit, worker = self.tree()
        child = fixture_member("showrunner", name="child", parent=showrunner.id)
        direct_worker = fixture_member("worker", name="test1", parent=showrunner.id)
        custom_worker = fixture_member("worker", name="impl0", parent=unit.id)
        self.assertEqual(staff.by_path("sdlc/test1"), direct_worker)
        self.assertEqual(staff.by_path("sdlc/models/impl0"), custom_worker)
        before = self.staff_file.read_bytes()
        self.assertEqual(staff.move(worker.id, staff.ReportsTo(unit.id)), worker)
        self.assertEqual(self.staff_file.read_bytes(), before)
        rejected: tuple[Callable[[], object], ...] = (
            lambda: fixture_member("worker", name="test1"),
            lambda: fixture_member("unit", name="bad", parent=unit.id),
            lambda: fixture_member("unit", name="bad", parent=MISSING_ID),
            lambda: fixture_member("unit", name="bad"),
            lambda: fixture_member("showrunner", name="bad", parent=unit.id),
            lambda: staff.move(showrunner.id, staff.ReportsTo(child.id)),
            lambda: staff.move(showrunner.id, staff.ReportsTo(showrunner.id)),
            lambda: staff.move(unit.id, staff.ReportsTo(worker.id)),
            lambda: staff.move(unit.id, staff.ReportsTo(MISSING_ID)),
            lambda: staff.rename(worker.id, "impl0"),
            lambda: staff.retire(showrunner.id),
        )
        for index, action in enumerate(rejected):
            with self.subTest(case=index):
                self.assert_refused(action)

    def test_short_names_cannot_alias_staff_ids(self) -> None:
        showrunner, unit, worker = self.tree()
        actions: tuple[Callable[[], object], ...] = (
            lambda: fixture_member("showrunner", name=unit.id),
            lambda: fixture_member("unit", name=worker.id, parent=showrunner.id),
            lambda: staff.rename(showrunner.id, worker.id),
            lambda: staff.rename(unit.id, showrunner.id),
        )
        for action in actions:
            with self.subTest(action=action):
                self.assert_refused(action)

    def test_promoted_showrunner_and_original_unit_have_distinct_paths(self) -> None:
        showrunner, unit, worker = self.tree()
        promoted = fixture_member("showrunner", name=unit.name, parent=showrunner.id)
        promoted_unit = fixture_member("unit", name="registry", parent=promoted.id)
        promoted_worker = fixture_member("worker", name="impl1", parent=promoted_unit.id)
        expected = {
            showrunner.id: "sdlc", unit.id: "sdlc/models", worker.id: "sdlc/models/impl1",
            promoted.id: "models", promoted_unit.id: "models/registry",
            promoted_worker.id: "models/registry/impl1",
        }
        rows = staff.find()
        for row in rows:
            with self.subTest(row=row):
                path = expected[row.id]
                self.assertEqual(staff.by_path(path), row)
                self.assertEqual(cast(object, json.loads(self.cli("get", path, "--json").stdout)), row.to_json())
        self.assertEqual({line.split()[0]: line.split()[2] for line in self.cli("show").stdout.splitlines()}, expected)
        for invalid in ("sdlc/models/registry", "sdlc/models/registry/impl1", "sdlc//models", "/sdlc", "sdlc/"):
            with self.subTest(path=invalid):
                self.assertIsNone(staff.by_path(invalid))

    def test_lookup_filters_and_unit_retirement_follow_membership(self) -> None:
        showrunner, unit, worker = self.tree()
        other_unit = fixture_member("unit", name="other", parent=showrunner.id)
        other_worker = fixture_member("worker", name="test1", parent=other_unit.id)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_path("sdlc"), showrunner)
        self.assertEqual(staff.by_path("sdlc/models"), unit)
        self.assertEqual(staff.by_path("sdlc/models/impl1"), worker)
        self.assertCountEqual(staff.find(), [showrunner, unit, worker, other_unit, other_worker])
        self.assertCountEqual(staff.find(kind="unit"), [unit, other_unit])
        self.assertCountEqual(staff.find(parent=showrunner.id), [unit, other_unit])
        self.assertEqual(staff.find(kind="worker", parent=unit.id), [worker])
        self.assertEqual(staff.find(kind="showrunner", parent=unit.id), [])
        self.assertIsNone(staff.by_id(MISSING_ID))
        self.assertIsNone(staff.by_path("sdlc/missing/impl1"))
        self.assert_refused(lambda: staff.retire(unit.id))
        location = staff.PresentWorktree("natedev", self.root / "models")
        _ = staff.set_worktree(unit.id, location, expected=unit.worktree)
        staff.defer_retire((unit.id,))
        self.assertEqual(staff.retire_pending(), (unit.id,))
        self.assertEqual(staff.retire(worker.id), worker)
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(location.machine, location.path)))
        self.assertIsNone(staff.by_id(worker.id))
        self.assertIsNone(staff.by_path("sdlc/models/impl1"))
        self.assertEqual(staff.by_id(other_worker.id), other_worker)

    def test_rename_and_move_keep_identity_and_descendant_paths(self) -> None:
        showrunner, unit, worker = self.tree()
        renamed = staff.rename(unit.id, "registry")
        self.assertEqual(renamed, replace(unit, name="registry"))
        showrunner_renamed = staff.rename(showrunner.id, "tools")
        self.assertEqual(showrunner_renamed, replace(showrunner, name="tools"))
        self.assertIsNone(staff.by_path("sdlc/models/impl1"))
        self.assertEqual(staff.by_path("tools/registry/impl1"), worker)
        other = fixture_member("showrunner", name="natedev")
        moved = staff.move(unit.id, staff.ReportsTo(other.id))
        self.assertEqual(moved, replace(renamed, parent=staff.ReportsTo(other.id)))
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertIsNone(staff.by_path("tools/registry/impl1"))
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)
        attached = staff.move(other.id, staff.ReportsTo(showrunner.id))
        self.assertEqual(attached, replace(other, parent=staff.ReportsTo(showrunner.id)))
        self.assertIsNone(staff.by_path("tools/natedev/registry/impl1"))
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)
        self.assertEqual(staff.move(other.id, staff.StaffRoot()), other)
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)

    def test_retire_showrunner_detaches_child_showrunners(self) -> None:
        parent = fixture_member("showrunner", name="parent", doc=staff.LiveDoc("natedev", self.root / "parent.md"))
        child = fixture_member("showrunner", name="child", parent=parent.id)
        unit = fixture_member("unit", name="models", parent=child.id)
        self.assert_refused(lambda: staff.retire(parent.id))
        staff.defer_retire((parent.id,))
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(parent.id), replace(parent, doc=staff.WrappedDoc("natedev", self.root / "parent.md")))
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(staff.by_path("child/models"), unit)

    def test_retired_ids_are_not_reused_and_all_ids_fit_herdr(self) -> None:
        showrunner, unit, worker = self.tree()
        self.assertEqual(staff.retire(worker.id), worker)
        replacement_id = fixture_runtime_id("replacement-native-thread")
        replacement = fixture_member("worker", name=worker.name, parent=unit.id, runtime_id=replacement_id)
        self.assertEqual(replacement.id, replacement_id)
        self.assertNotEqual(replacement.id, worker.id)
        rows = [showrunner, unit, worker, replacement]
        for role in ("test", "fix", "review", "designer", "reviewer"):
            rows.append(fixture_member("worker", name=f"{role}1", parent=unit.id))
        self.assertEqual(len({row.id for row in rows}), len(rows))
        for row in rows:
            self.assertEqual(staff.native_id(row.id), row.id)
            self.assertRegex(row.name, r"^[a-z][a-z0-9_-]{0,31}$")

    def test_file_uses_canonical_sorted_rows_and_no_cached_path(self) -> None:
        _ = self.tree()
        rows = staff.find()
        expected = {"schema": 2, "rows": [row.to_json() for row in sorted(rows, key=lambda row: row.id)]}
        self.assertEqual(self.staff_file.read_text(), json.dumps(expected, indent=2, sort_keys=True) + "\n")
        original = self.staff_file.read_bytes()
        elsewhere = self.root / "elsewhere" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(elsewhere)}):
            row = fixture_member("showrunner", name="separate")
            self.assertEqual(staff.find(), [row])
        self.assertEqual(self.staff_file.read_bytes(), original)
        self.assertCountEqual(staff.find(), rows)

    def test_missing_file_reads_as_empty_and_bootstraps_on_write(self) -> None:
        record = staff.initial_snapshot()
        self.assertIsNone(record.by_id(MISSING_ID))
        self.assertIsNone(record.by_path("sdlc"))
        self.assertEqual(record.find(), [])
        self.assertFalse(self.staff_file.exists())
        created = fixture_member("showrunner", name="sdlc")
        self.assertEqual(staff.by_id(created.id), created)

    def test_malformed_or_unsupported_records_cannot_be_read_or_overwritten(self) -> None:
        showrunner, unit, worker = self.tree()
        valid = showrunner.to_json()
        malformed: list[object] = [
            [], {"schema": 999, "rows": []}, {"schema": 1, "rows": {}},
            {"schema": 1, "rows": [{**valid, "kind": "visitor"}]},
            {"schema": 1, "rows": [{**valid, "id": 4}]},
            {"schema": 1, "rows": [{**valid, "name": False}]},
            {"schema": 1, "rows": [{**valid, "parent": []}]},
            {"schema": 1, "rows": [valid, valid]},
            {"schema": 1, "rows": [valid, {**valid, "id": "s11111111111"}]},
            {"schema": 1, "rows": [unit.to_json(), worker.to_json()]},
        ]
        operations: tuple[Callable[[], object], ...] = (
            lambda: staff.by_id(showrunner.id), lambda: staff.by_path("sdlc"), staff.find,
            lambda: fixture_member("showrunner", name="new"),
            lambda: staff.rename(showrunner.id, "new"),
            lambda: staff.move(showrunner.id, staff.StaffRoot()), lambda: staff.retire(showrunner.id),
        )
        for index, document in enumerate([*malformed, "unparseable"]):
            before = self.write_document(document)
            if index == 3:
                child = replace(worker, parent=staff.ReportsTo(showrunner.id))
                unknown = {**valid, "kind": "visitor", "current_session": fixture_runtime_id("unknown-session")}
                _ = self.write_document({"schema": 2, "rows": [unknown, child.to_json()]})
                with redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(staff.snapshot().rows, (child,))
                    created = staff.ensure("showrunner", name="new", runtime_id=fixture_runtime_id("new"))
                    self.assertCountEqual(staff.snapshot().rows, (child, created))
                self.assertEqual(output.getvalue(), f"staff anomaly: row {showrunner.id}: unknown kind visitor; skipped\n" * 3)
                self.assertEqual(cast(dict[str, object], json.loads(self.staff_file.read_text()))["rows"],
                    [row.to_json() for row in sorted((child, created), key=lambda row: row.id)] + [unknown])
                before = self.staff_file.read_bytes()
                for runtime_id, reason in ((str(showrunner.id), "staff id"), (unknown["current_session"], "current session")):
                    with redirect_stderr(io.StringIO()), self.assertRaisesRegex(staff.StaffRefused, "duplicate " + reason):
                        _ = staff.ensure("showrunner", name="duplicate", runtime_id=str(runtime_id))
                    self.assertEqual(self.staff_file.read_bytes(), before)
                continue
            if document == "unparseable":
                before = b"{truncated"
                _ = self.staff_file.write_bytes(before)
            for operation_index, operation in enumerate(operations):
                with self.subTest(document=index, operation=operation_index):
                    with self.assertRaises(staff.StaffUnreadable):
                        _ = operation()
                    self.assertEqual(self.staff_file.read_bytes(), before)

    def test_missing_mutation_targets_raise_not_found_without_a_write(self) -> None:
        _ = self.tree()
        for action in (lambda: staff.rename(MISSING_ID, "new"), lambda: staff.move(MISSING_ID, staff.StaffRoot())):
            before = self.staff_file.read_bytes()
            with self.assertRaises(staff.StaffNotFound):
                _ = action()
            self.assertEqual(self.staff_file.read_bytes(), before)

    def test_concurrent_ensures_of_same_member_return_one_id(self) -> None:
        ids = self.run_writers("showrunner", ("sdlc", "sdlc"), None)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual([row.id for row in staff.find()], [ids[0]])

    def test_concurrent_different_writes_both_land(self) -> None:
        _, unit, existing = self.tree()
        ids = self.run_writers("worker", ("test1", "fix1"), unit.id)
        workers = staff.find(kind="worker", parent=unit.id)
        self.assertEqual({row.id for row in workers}, {existing.id, *ids})
        self.assertEqual({row.name for row in workers}, {"impl1", "test1", "fix1"})

    def test_exception_before_replace_preserves_the_readable_old_file(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        with mock.patch("os.replace", side_effect=RuntimeError("injected before replace")):
            with self.assertRaisesRegex(RuntimeError, "injected before replace"):
                _ = fixture_member("showrunner", name="interrupted")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_path("interrupted"))
        self.assertIsNotNone(staff.by_path("sdlc/models/impl1"))
        self.assertEqual(list(self.staff_file.parent.glob(".staff.json.*")), [])

    def test_unchanged_rerun_syncs_folder_after_replace_sync_failure(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        sync = os.fsync
        synced_versions: list[bytes] = []

        def fail_first_folder_sync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                synced_versions.append(self.staff_file.read_bytes())
                if len(synced_versions) == 1:
                    raise OSError("injected folder sync failure")
            sync(descriptor)

        with mock.patch("scripts.lib.atomic_file.os.fsync", side_effect=fail_first_folder_sync):
            with self.assertRaisesRegex(staff.StaffUnreadable, "injected folder sync failure"):
                _ = fixture_member("showrunner", name="recovered")
            after = self.staff_file.read_bytes()
            self.assertNotEqual(after, before)
            created = staff.by_path("recovered")
            self.assertIsNotNone(created)
            self.assertEqual(synced_versions, [after])
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(fixture_member("showrunner", name="recovered"), created)
            self.assertIn("unchanged", errors.getvalue())
            self.assertEqual(synced_versions, [after, after])
        self.assertEqual(self.staff_file.read_bytes(), after)
        self.assertEqual(list(self.staff_file.parent.glob(".staff.json.*")), [])

    def test_next_write_removes_temp_left_by_a_killed_writer(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        killed = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", KILLED_WRITER], cwd=CHECKOUT,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(killed.returncode, 73, killed.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)
        orphans = list(self.staff_file.parent.glob(".staff.json.*"))
        self.assertTrue(orphans, "writer did not leave a temp file before os.replace")
        unrelated = self.staff_file.parent / "keep.tmp"
        _ = unrelated.write_text("unrelated")
        created = fixture_member("showrunner", name="recovered")
        self.assertTrue(all(not path.exists() for path in orphans))
        self.assertEqual(unrelated.read_text(), "unrelated")
        self.assertEqual(staff.by_id(created.id), created)

    def test_cli_show_and_get_sort_paths_and_apply_filters(self) -> None:
        showrunner, unit, worker = self.tree()
        other = fixture_member("showrunner", name="aaa")
        rendered = self.cli("show").stdout.splitlines()
        expected = [(other, "aaa"), (showrunner, "sdlc"), (unit, "sdlc/models"), (worker, "sdlc/models/impl1")]
        self.assertEqual([line.split() for line in rendered], [[row.id, row.kind, path] for row, path in expected])
        starts = [[line.index(field) for field in line.split()] for line in rendered]
        self.assertTrue(all(start == starts[0] for start in starts), rendered)
        shown = self.cli("show", "--kind", "worker", "--under", "sdlc/models", "--json")
        self.assertEqual(cast(object, json.loads(shown.stdout)), [worker.to_json()])
        shown_by_id = self.cli("show", "--under", unit.id, "--json")
        self.assertEqual(cast(object, json.loads(shown_by_id.stdout)), [worker.to_json()])
        for identifier in (worker.id, "sdlc/models/impl1"):
            found = self.cli("get", identifier, "--json")
            self.assertEqual(cast(object, json.loads(found.stdout)), worker.to_json())
        missing = self.cli("get", "sdlc/missing", "--json", code=1)
        self.assertTrue(missing.stderr.strip())

    def test_get_finds_a_member_by_its_worktree_root(self) -> None:
        _, unit, _ = self.tree()
        path = self.root / "checkout"
        unit = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
        found = self.cli("get", str(path), "--json")
        self.assertEqual(json.loads(found.stdout), unit.to_json())

    def test_get_finds_the_longest_worktree_for_an_inside_folder(self) -> None:
        runner, unit, _ = self.tree()
        path = self.root / "checkout"
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
        nested = fixture_member("unit", name="nested", parent=runner.id)
        nested = staff.set_worktree(nested.id, staff.PresentWorktree("natedev", path / "nested"), expected=nested.worktree)
        found = self.cli("get", str(path / "nested/src"), "--json")
        self.assertEqual(json.loads(found.stdout), nested.to_json())

    def test_get_reports_an_unknown_worktree_path_as_not_found(self) -> None:
        _, unit, _ = self.tree()
        path = self.root / "checkout"
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", path), expected=unit.worktree)
        missing = self.cli("get", str(self.root / "checkout-other"), code=1)
        self.assertIn("staff member not found", missing.stderr)
        self.assertEqual(missing.stdout, "")

    def test_get_finds_a_member_by_its_current_session(self) -> None:
        _, unit, _ = self.tree()
        current = staff.native_id(fixture_runtime_id("get-after-clear"))
        unit = staff.update_current_session(unit.id, current)
        found = self.cli("get", current, "--json")
        self.assertEqual(json.loads(found.stdout), unit.to_json())

    def test_cli_maps_refused_unreadable_and_missing_mutations(self) -> None:
        _, unit, worker = self.tree()
        before = self.staff_file.read_bytes()
        refused = self.cli("rename", worker.id, f"--name={worker.id}", "--json", code=2)
        self.assertTrue(refused.stderr.strip())
        self.assertEqual(self.staff_file.read_bytes(), before)
        missing = self.cli("move", MISSING_ID, f"--parent={unit.parent.id if isinstance(unit.parent, staff.ReportsTo) else 'none'}", "--json", code=1)
        self.assertTrue(missing.stderr.strip())
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = self.staff_file.write_text("{truncated")
        unreadable = self.cli("show", "--json", code=2)
        self.assertTrue(unreadable.stderr.strip())
        self.assertEqual(self.staff_file.read_text(), "{truncated")

    def test_move_cli_requires_the_explicit_parent_option(self) -> None:
        showrunner, unit, _ = self.tree()
        other = fixture_member("showrunner", name="other")
        before = self.staff_file.read_bytes()
        _ = self.cli("move", unit.id, other.id, "--json", code=2)
        _ = self.cli("move", showrunner.id, "--json", code=2)
        self.assertEqual(self.staff_file.read_bytes(), before)
        attached = self.cli("move", other.id, f"--parent={showrunner.id}", "--json")
        self.assertEqual(cast(object, json.loads(attached.stdout)), replace(other, parent=staff.ReportsTo(showrunner.id)).to_json())
        detached = self.cli("move", other.id, "--parent=none", "--json")
        self.assertEqual(cast(object, json.loads(detached.stdout)), other.to_json())

    def test_mac_reads_and_writes_use_owner_via_fake_ssh_without_a_local_copy(self) -> None:
        showrunner, unit, worker = self.tree()
        owner_show = self.script(STAFF_SCRIPT, "show", "--json")
        owner_display = self.script(STAFF_SCRIPT, "show").stdout
        reads: tuple[Callable[[], object], ...] = (
            lambda: staff.by_id(worker.id),
            lambda: staff.by_path("sdlc/models/impl1"),
            lambda: staff.find(kind="worker", parent=unit.id),
            staff.find,
            lambda: staff.by_path(worker.id),
            lambda: staff.by_id(staff.StaffId(showrunner.name)),
            lambda: staff.find(parent=MISSING_ID),
            lambda: staff.by_id(MISSING_ID),
            lambda: staff.by_path("sdlc/missing"),
        )
        owner_results = [read() for read in reads]
        self.assertCountEqual(staff.find(), [staff.StaffRow.from_json(row) for row in cast(list[object], json.loads(owner_show.stdout))])
        local_file = self.mac_file
        with self.machine("mac"):
            for read, expected in zip(reads, owner_results):
                with self.subTest(read=read):
                    self.assertEqual(read(), expected)
            self.assertEqual(self.script(STAFF_SCRIPT, "show").stdout, owner_display)
            created = staff.ensure_worker(staff.native_id(fixture_runtime_id("mac-native-worker")),
                                          "test1", unit.id)
            moved_name = staff.rename(unit.id, "registry")
            self.assertEqual(moved_name, replace(unit, name="registry"))
            self.assertEqual(staff.move(showrunner.id, staff.StaffRoot()), showrunner)
            self.assertEqual(staff.retire(created.id), created)
            self.assertIsNone(staff.by_id(created.id))
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(staff.by_path("sdlc/registry/impl1"), worker)
        commands = self.ssh_commands()
        self.assertEqual(commands[:len(reads) + 1],
                         [["show", "--snapshot", "--json"]] * len(reads) + [["show", "--json"]])
        self.assertEqual([command[0] for command in commands[len(reads) + 1:]], ["ensure-worker", "rename", "move", "retire", "show"])

    def test_mac_preserves_names_that_begin_with_a_dash(self) -> None:
        expected = fixture_member("showrunner", name="-scratch")
        local_file = self.mac_file
        with self.machine("mac"):
            self.assertEqual(staff.ensure("showrunner", name="-scratch", runtime_id=expected.id), expected)
            renamed = staff.rename(expected.id, "-renamed")
        self.assertEqual(renamed, staff.rename(expected.id, "-renamed"))
        self.assertEqual(staff.by_path("-renamed"), renamed)
        self.assertFalse(local_file.parent.exists())
        self.assertIn("--name=-scratch", self.ssh_commands()[0])
        self.assertIn("--name=-renamed", self.ssh_commands()[1])

    def test_owner_selection_uses_the_shared_remote_module(self) -> None:
        showrunner, _, _ = self.tree()
        local_file = self.mac_file
        with self.machine("mac"):
            self.assertEqual(staff.by_id(showrunner.id), showrunner)
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"]])

    def test_explicit_staff_file_is_local_even_on_a_mac(self) -> None:
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(self.mac_file)}), mock.patch(
            "scripts.agents.remote.other_machine", return_value="natedev",
        ):
            row = fixture_member("showrunner", name="private")
            self.assertEqual(staff.snapshot().rows, (row,))
            self.assertTrue(self.mac_file.is_file())
            worker = fixture_worker("review", row.id)
            self.assertEqual(staff.retire(worker.id), worker)
            self.assertEqual(staff.snapshot().rows, (row,))
        self.assertFalse(self.staff_file.exists())
        self.assertEqual(self.ssh_log.read_text() if self.ssh_log.exists() else "", "")

    def test_remote_errors_keep_application_reasons_and_identify_ssh_failures(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        local_file = self.mac_file
        cases = (
            (2, "usage: staff.py: invalid option", "usage: staff.py: invalid option"),
            (1, "remote interpreter failed", "remote interpreter failed"),
            (17, "remote command interrupted", "remote command interrupted"),
            (255, "ssh: connection refused", "natedev not reached"),
            (2, "", "natedev not reached"),
        )
        with self.machine("mac"):
            for code, reason, expected in cases:
                with self.subTest(code=code, reason=reason), mock.patch.dict(os.environ, {
                    "STAFF_TEST_SSH_FAIL": "1",
                    "STAFF_TEST_SSH_EXIT": str(code), "STAFF_TEST_SSH_REASON": reason,
                }):
                    with self.assertRaises(staff.StaffUnreadable) as raised:
                        _ = staff.find()
                    self.assertEqual(str(raised.exception), expected)
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreachable_owner_raises_unreadable_and_writes_nothing_locally(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        local_file = self.mac_file
        with self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}):
            actions: tuple[Callable[[], object], ...] = (
                lambda: staff.by_id(MISSING_ID), lambda: staff.by_path("sdlc"), staff.find,
                lambda: staff.ensure("showrunner", name="blocked", runtime_id=fixture_runtime_id("showrunner::blocked")),
            )
            for action in actions:
                with self.subTest(action=action), self.assertRaisesRegex(staff.StaffUnreadable, "^natedev not reached$"):
                    _ = action()
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual([command[0] for command in self.ssh_commands()], ["show", "show", "show", "ensure"])

    def test_mac_snapshot_reads_once_and_all_lookups_use_its_rows(self) -> None:
        showrunner, unit, worker = self.tree()
        expected = (staff.by_id(worker.id), staff.by_path("sdlc/models/impl1"), staff.find(kind="unit", parent=showrunner.id))
        with self.machine("mac"):
            record = staff.snapshot()
            self.assertEqual(record.by_id(worker.id), expected[0])
            self.assertEqual(record.by_path("sdlc/models/impl1"), expected[1])
            self.assertEqual(record.find(kind="unit", parent=showrunner.id), expected[2])
            self.assertIsNone(record.by_id(MISSING_ID))
            self.assertIsNone(record.by_path("sdlc/missing"))
            self.assertEqual(record.find(kind="worker", parent=unit.id), [worker])
            self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"]])
            self.assertFalse(self.mac_file.exists())
        _ = staff.rename(unit.id, "panes")
        self.assertEqual(record.by_path("sdlc/models"), unit)
        self.assertIsNone(record.by_path("sdlc/panes"))
        self.assertIsNotNone(staff.snapshot().by_path("sdlc/panes"))
        with self.assertRaises(FrozenInstanceError):
            setattr(record, "rows", ())

    def test_local_snapshot_reads_retained_membership_without_contacting_the_owner(self) -> None:
        _, unit, _ = self.tree()
        retained = self.staff_file.read_bytes()
        _ = staff.rename(unit.id, "renamed-on-owner")
        with self.machine("mac"):
            self.mac_file.parent.mkdir(parents=True, exist_ok=True)
            _ = self.mac_file.write_bytes(retained)
            _ = self.ssh_log.write_text("")
            with mock.patch.object(subprocess, "run", side_effect=AssertionError("no remote owner read")):
                record = staff.local_snapshot()
            self.assertEqual(record.by_id(unit.id), unit)
            self.assertEqual(record.inputs, (str(self.mac_file),))
            self.assertEqual(self.ssh_commands(), [])

    def test_mac_snapshot_refuses_an_unreachable_owner(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        with self.machine("mac"), mock.patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}):
            with self.assertRaisesRegex(staff.StaffUnreadable, "^natedev not reached$"):
                _ = staff.snapshot()
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"]])
        self.assertFalse(self.mac_file.exists())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_get_renders_the_same_complete_path_and_columns_as_show(self) -> None:
        showrunner, unit, worker = self.tree()
        attached = fixture_member("showrunner", name="style", parent=showrunner.id)
        lines = {line.split()[0]: line for line in self.cli("show").stdout.splitlines()}
        for row, path in ((unit, "sdlc/models"), (worker, "sdlc/models/impl1"), (attached, "style")):
            with self.subTest(path=path):
                self.assertEqual(self.cli("get", path).stdout.rstrip("\n"), lines[row.id])
                self.assertEqual(lines[row.id].split()[2], path)

    def test_mac_write_waits_for_owner_operation_lock(self) -> None:
        _, unit, _ = self.tree()
        destination = fixture_member("showrunner", name="other")
        lock = self.staff_file.parent / "operation.lock"
        with self.lock_notice(lock) as listener:
            with staff.operation():
                with self.machine("mac"):
                    pending = self.start_script(STAFF_SCRIPT, "move", unit.id, f"--parent={destination.id}", "--json")
                    self.assert_operation_blocks(pending, listener)
                self.assertEqual(staff.by_id(unit.id), unit)
            output, errors = pending.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(pending.returncode, 0, errors)
        self.assertEqual(cast(object, json.loads(output)), replace(unit, parent=staff.ReportsTo(destination.id)).to_json())
        self.assertEqual(staff.by_id(unit.id), replace(unit, parent=staff.ReportsTo(destination.id)))
        self.assertFalse(self.mac_file.exists())

    def test_operation_preserves_body_errors_and_releases_the_lock(self) -> None:
        failure = OSError("source update failed")
        with self.assertRaises(OSError) as raised:
            with staff.operation():
                raise failure
        self.assertIs(raised.exception, failure)
        with staff.operation():
            row = fixture_member("showrunner", name="after-failure")
        self.assertEqual(staff.by_id(row.id), row)

    def test_operation_lock_errors_have_the_staff_unreadable_type(self) -> None:
        with mock.patch.object(fcntl, "flock", side_effect=OSError("lock failed")):
            with self.assertRaisesRegex(staff.StaffUnreadable, "staff operation: lock failed"):
                with staff.operation():
                    self.fail("entered an operation without acquiring its lock")

    def test_absent_records_are_empty_on_owner_and_mac(self) -> None:
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                self.assertEqual(staff.initial_snapshot(), staff.StaffSnapshot(()))
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"]])
        self.assertFalse(self.staff_file.exists())
        self.assertFalse(self.mac_file.exists())

    def test_operation_joins_inherited_descriptor_and_keeps_the_outer_lock(self) -> None:
        with staff.operation():
            with staff.operation():
                _ = fixture_member("showrunner", name="outer")
            child = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c",
                "from scripts.production import staff; " +
                "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='child', runtime_id='02fc3c9a-b345-56a6-ac99-fdd5685739a6')\")"],
                cwd=CHECKOUT, pass_fds=staff.operation_fds(), capture_output=True,
                text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(child.returncode, 0, child.stderr)
            with self.lock_notice(self.staff_file.parent / "operation.lock") as listener:
                seed = self.start_script(STAFF_SCRIPT, "ensure", "showrunner", "--name=after",
                                         "--runtime-id", fixture_runtime_id("cli-after"))
                self.assert_operation_blocks(seed, listener)
        output, errors = seed.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(seed.returncode, 0, output + errors)
        self.assertEqual({row.name for row in staff.snapshot().rows}, {"outer", "child", "after"})

    def test_operation_joins_a_valid_inherited_lock_after_its_owner_exits(self) -> None:
        path = self.staff_file.parent / "operation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as inherited:
            exited = subprocess.Popen([sys.executable, "-c",
                "import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX)", str(inherited.fileno())],
                pass_fds=(inherited.fileno(),))
            self.assertEqual(exited.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            environment = {**os.environ, "STAFF_OPERATION_FD": str(inherited.fileno())}
            child = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c",
                "from scripts.production import staff; " +
                "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='orphan', runtime_id='bace30cb-17a4-5156-a98f-c0d41e973849')\")"],
                cwd=CHECKOUT, env=environment, pass_fds=(inherited.fileno(),), capture_output=True,
                text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual([row.name for row in staff.find()], ["orphan"])

    def test_seed_result_preserves_pending_retirement_on_owner_and_mac(self) -> None:
        runner, unit, _worker = self.tree()
        staff.defer_retire((runner.id, unit.id))
        before = self.staff_file.read_bytes()
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                result = staff.apply_seed(staff.SeedInput(()))
                self.assertEqual(result.pending_retire, tuple(sorted((runner.id, unit.id))))
                self.assertEqual(result, staff.snapshot())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def assert_invalid_operation_descriptor_blocks(self, wrong_file: bool) -> None:
        with staff.operation():
            path = self.root / "wrong-lock" if wrong_file else self.staff_file.parent / "operation.lock"
            with path.open("a") as unlocked, self.lock_notice(self.staff_file.parent / "operation.lock") as listener:
                environment = {**os.environ, "STAFF_OPERATION_FD": str(unlocked.fileno())}
                program = (
                    "from scripts.production.staff_test_support import observe_operation_lock; "
                    "observe_operation_lock(); from scripts.production import staff; "
                    "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='fallback', runtime_id='e49bd66f-2aaa-5521-b99d-225ca97f55f8')\")")
                child = subprocess.Popen([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program], cwd=CHECKOUT,
                    env=environment, pass_fds=(unlocked.fileno(),), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                self.assert_operation_blocks(child, listener)
        output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(child.returncode, 0, output + errors)
        self.assertEqual(len(staff.find(kind="showrunner")), 1)

    def test_operation_rejects_an_inherited_descriptor_for_another_file(self) -> None:
        self.assert_invalid_operation_descriptor_blocks(True)

    def test_operation_rejects_an_unlocked_inherited_descriptor(self) -> None:
        self.assert_invalid_operation_descriptor_blocks(False)

    def test_pending_retirement_waits_for_workers_then_retires_units_before_showrunners(self) -> None:
        runner, unit, worker = self.tree()
        runner = staff.set_doc(runner.id, staff.LiveDoc("natedev", self.root / "sdlc.md"))
        unit = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", self.root / "models"), expected=unit.worktree)
        child = fixture_member("showrunner", name="child", parent=runner.id)
        staff.defer_retire((runner.id, unit.id))
        original = self.staff_file.read_bytes()
        staff.defer_retire((unit.id, runner.id))
        self.assertEqual(self.staff_file.read_bytes(), original)
        self.assertEqual(staff.retire_pending(), tuple(sorted((runner.id, unit.id))))
        self.assertEqual(self.staff_file.read_bytes(), original)
        unit = staff.rename(unit.id, "panes")
        self.assertEqual(staff.snapshot().pending_retire, tuple(sorted((runner.id, unit.id))))
        _ = staff.retire(worker.id)
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree("natedev", self.root / "models")))
        self.assertEqual(staff.by_id(runner.id), replace(runner, doc=staff.WrappedDoc("natedev", self.root / "sdlc.md")))
        self.assertEqual(staff.by_id(child.id), child)
        completed = self.staff_file.read_bytes()
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(self.staff_file.read_bytes(), completed)

    def test_pending_retirement_round_trips_over_mac_transport(self) -> None:
        runner, unit, worker = self.tree()
        runner = staff.set_doc(runner.id, staff.LiveDoc("natedev", self.root / "sdlc.md"))
        unit = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", self.root / "models"), expected=unit.worktree)
        with self.machine("mac"):
            staff.defer_retire((runner.id, unit.id))
            self.assertEqual(staff.snapshot().pending_retire, tuple(sorted((runner.id, unit.id))))
            self.assertEqual(staff.retire_pending(), tuple(sorted((runner.id, unit.id))))
            _ = staff.retire(worker.id)
            self.assertEqual(staff.retire_pending(), ())
        self.assertFalse(self.mac_file.exists())
        self.assertCountEqual(staff.snapshot().rows, (
            replace(runner, doc=staff.WrappedDoc("natedev", self.root / "sdlc.md")),
            replace(unit, worktree=staff.RemovedWorktree("natedev", self.root / "models")),
        ))

    def test_pending_retirement_drops_ids_already_retired(self) -> None:
        runner = fixture_member("showrunner", name="gone", doc=staff.WrappedDoc("natedev", self.root / "gone.md"))
        staff.defer_retire((runner.id,))
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.snapshot().pending_retire, ())
        self.assertEqual(staff.by_id(runner.id), runner)

    def test_pending_retirement_keeps_unfilled_locations_until_their_facts_are_known(self) -> None:
        runner, unit, worker = self.tree()
        _ = staff.retire(worker.id)
        staff.defer_retire((runner.id, unit.id))
        before = self.staff_file.read_bytes()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(staff.retire_pending(), tuple(sorted((runner.id, unit.id))))
        self.assertIn(f"keeping {unit.id}: worktree is unfilled", errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(runner.id), runner)


if __name__ == "__main__":
    _ = unittest.main()
