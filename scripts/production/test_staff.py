"""Exercise the staff contract with isolated state and an actual fake SSH process."""
from __future__ import annotations

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
from contextlib import redirect_stderr
from dataclasses import FrozenInstanceError, replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Barrier
from typing import cast
from unittest import mock

from scripts.production import staff
from scripts.production.staff_test_support import CHECKOUT, PROCESS_TIMEOUT, STAFF_SCRIPT, StaffTestCase


MISSING_ID = staff.StaffId("s00000000000")

KILLED_WRITER = """import os, sys
sys.platform = 'linux'
from scripts.production import staff
os.replace = lambda *args, **kwargs: os._exit(73)
staff.ensure('showrunner', name='interrupted')
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
            _ = ready.wait(timeout=PROCESS_TIMEOUT)
            row = staff.ensure(kind, name=name, parent=parent)
            result.send((True, str(row.id)))
        except Exception as error:
            result.send((False, repr(error)))


class StaffTests(StaffTestCase):
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
            _ = ready.wait(timeout=PROCESS_TIMEOUT)
            for process in processes:
                process.join(timeout=PROCESS_TIMEOUT)
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
                process.join(timeout=PROCESS_TIMEOUT)
                process.close()
            for result in results:
                result.close()

    def test_worker_names_preserve_role_and_positive_number(self) -> None:
        for role in ("impl", "test", "fix", "review", "designer", "reviewer"):
            for number in (1, 12):
                with self.subTest(role=role, number=number):
                    name = staff.WorkerName.parse(f"{role}{number}")
                    self.assertEqual(name.role.value, role)
                    self.assertEqual(name.number, number)
        for invalid in ("impl", "impl0", "bob1", "impl-1", "impl1x", " impl1", "impl1\n"):
            with self.subTest(invalid=invalid), self.assertRaises(staff.StaffRefused):
                _ = staff.WorkerName.parse(invalid)

    def test_row_round_trip_is_frozen_and_stores_only_membership(self) -> None:
        _, _, worker = self.tree()
        self.assertEqual(set(worker.to_json()), {"id", "kind", "name", "parent"})
        self.assertEqual(staff.StaffRow.from_json(worker.to_json()), worker)
        with self.assertRaises(FrozenInstanceError):
            setattr(worker, "name", "test1")

    def test_write_verbs_are_rerun_safe_and_cli_reports_unchanged(self) -> None:
        _ = self.assert_rerun("ensure", "showrunner", "--name", "sdlc")
        showrunner = staff.by_path("sdlc")
        assert showrunner is not None
        _ = self.assert_rerun("ensure", "unit", "--name", "models", "--parent", showrunner.id)
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.assert_rerun("ensure", "worker", "--name", "impl1", "--parent", unit.id)
        self.assertEqual(len(staff.find(kind="worker", parent=unit.id)), 1)
        _ = self.assert_rerun("rename", unit.id, "--name=registry")
        other = staff.ensure("showrunner", name="natedev")
        _ = self.assert_rerun("move", unit.id, f"--parent={other.id}")
        _ = self.assert_rerun("move", other.id, f"--parent={showrunner.id}")
        _ = self.assert_rerun("move", other.id, "--parent=none")
        _ = self.assert_rerun("retire", unit.id)
        before = self.staff_file.read_bytes()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertIsNone(staff.retire(unit.id))
        self.assertIn("unchanged", errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_api_noops_return_the_existing_row_and_preserve_bytes(self) -> None:
        showrunner, unit, worker = self.tree()
        for row in (showrunner, unit, worker):
            before = self.staff_file.read_bytes()
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(staff.ensure(row.kind, name=row.name, parent=row.parent), row)
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
        other = staff.ensure("unit", name="other", parent=showrunner.id)
        second = staff.ensure("worker", name="impl1", parent=other.id)
        self.assertNotEqual(first.id, second.id)
        self.assert_refused(lambda: staff.rename(other.id, unit.name))
        another_showrunner = staff.ensure("showrunner", name="natedev")
        conflict = staff.ensure("unit", name="models", parent=another_showrunner.id)
        self.assert_refused(lambda: staff.move(conflict.id, showrunner.id))
        self.assert_refused(lambda: staff.rename(another_showrunner.id, showrunner.name))
        # Promotion needs both kinds to coexist briefly under the same short name.
        promoted = staff.ensure("showrunner", name=unit.name)
        self.assertNotEqual(promoted.id, unit.id)

    def test_invalid_parent_graph_and_worker_mutations_preserve_the_file(self) -> None:
        showrunner, unit, worker = self.tree()
        child = staff.ensure("showrunner", name="child", parent=showrunner.id)
        rejected: tuple[Callable[[], object], ...] = (
            lambda: staff.ensure("worker", name="test1", parent=showrunner.id),
            lambda: staff.ensure("worker", name="test1"),
            lambda: staff.ensure("worker", name="impl0", parent=unit.id),
            lambda: staff.ensure("unit", name="bad", parent=unit.id),
            lambda: staff.ensure("unit", name="bad", parent=MISSING_ID),
            lambda: staff.ensure("unit", name="bad"),
            lambda: staff.ensure("showrunner", name="bad", parent=unit.id),
            lambda: staff.move(showrunner.id, child.id),
            lambda: staff.move(showrunner.id, showrunner.id),
            lambda: staff.move(unit.id, worker.id),
            lambda: staff.move(unit.id, MISSING_ID),
            lambda: staff.rename(worker.id, "test1"),
            lambda: staff.move(worker.id, unit.id),
            lambda: staff.retire(showrunner.id),
        )
        for index, action in enumerate(rejected):
            with self.subTest(case=index):
                self.assert_refused(action)

    def test_short_names_cannot_alias_staff_ids(self) -> None:
        showrunner, unit, worker = self.tree()
        actions: tuple[Callable[[], object], ...] = (
            lambda: staff.ensure("showrunner", name=unit.id),
            lambda: staff.ensure("unit", name=worker.id, parent=showrunner.id),
            lambda: staff.rename(showrunner.id, worker.id),
            lambda: staff.rename(unit.id, showrunner.id),
        )
        for action in actions:
            with self.subTest(action=action):
                self.assert_refused(action)

    def test_promoted_showrunner_and_original_unit_have_distinct_paths(self) -> None:
        showrunner, unit, worker = self.tree()
        promoted = staff.ensure("showrunner", name=unit.name, parent=showrunner.id)
        promoted_unit = staff.ensure("unit", name="registry", parent=promoted.id)
        promoted_worker = staff.ensure("worker", name="impl1", parent=promoted_unit.id)
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
        other_unit = staff.ensure("unit", name="other", parent=showrunner.id)
        other_worker = staff.ensure("worker", name="test1", parent=other_unit.id)
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
        self.assertEqual(staff.retire(unit.id), unit)
        self.assertIsNone(staff.by_id(unit.id))
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
        other = staff.ensure("showrunner", name="natedev")
        moved = staff.move(unit.id, other.id)
        self.assertEqual(moved, replace(renamed, parent=other.id))
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertIsNone(staff.by_path("tools/registry/impl1"))
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)
        attached = staff.move(other.id, showrunner.id)
        self.assertEqual(attached, replace(other, parent=showrunner.id))
        self.assertIsNone(staff.by_path("tools/natedev/registry/impl1"))
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)
        self.assertEqual(staff.move(other.id, None), other)
        self.assertEqual(staff.by_path("natedev/registry/impl1"), worker)

    def test_retire_showrunner_detaches_child_showrunners(self) -> None:
        parent = staff.ensure("showrunner", name="parent")
        child = staff.ensure("showrunner", name="child", parent=parent.id)
        unit = staff.ensure("unit", name="models", parent=child.id)
        self.assertEqual(staff.retire(parent.id), parent)
        self.assertIsNone(staff.by_id(parent.id))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertEqual(staff.by_path("child/models"), unit)

    def test_retired_ids_are_not_reused_and_all_ids_fit_herdr(self) -> None:
        showrunner, unit, worker = self.tree()
        self.assertEqual(staff.retire(worker.id), worker)
        replacement = staff.ensure("worker", name=worker.name, parent=unit.id)
        self.assertNotEqual(replacement.id, worker.id)
        rows = [showrunner, unit, worker, replacement]
        for role in ("test", "fix", "review", "designer", "reviewer"):
            rows.append(staff.ensure("worker", name=f"{role}1", parent=unit.id))
        self.assertEqual(len({row.id for row in rows}), len(rows))
        for row in rows:
            self.assertRegex(row.id, r"^s[0-9a-f]{11}$")
            self.assertRegex(row.id, r"^[a-z][a-z0-9_-]{0,31}$")

    def test_file_uses_canonical_sorted_rows_and_no_cached_path(self) -> None:
        _ = self.tree()
        rows = staff.find()
        expected = {"schema": 1, "rows": [row.to_json() for row in sorted(rows, key=lambda row: row.id)]}
        self.assertEqual(self.staff_file.read_text(), json.dumps(expected, indent=2, sort_keys=True) + "\n")
        original = self.staff_file.read_bytes()
        elsewhere = self.root / "elsewhere" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(elsewhere)}):
            row = staff.ensure("showrunner", name="separate")
            self.assertEqual(staff.find(), [row])
        self.assertEqual(self.staff_file.read_bytes(), original)
        self.assertCountEqual(staff.find(), rows)

    def test_missing_file_is_unreadable_for_reads_but_bootstraps_on_write(self) -> None:
        for read in (lambda: staff.by_id(MISSING_ID), lambda: staff.by_path("sdlc"), staff.find):
            with self.subTest(read=read), self.assertRaises(staff.StaffUnreadable) as raised:
                _ = read()
            self.assertIsInstance(raised.exception, OSError)
        self.assertFalse(self.staff_file.exists())
        created = staff.ensure("showrunner", name="sdlc")
        self.assertEqual(staff.by_id(created.id), created)

    def test_malformed_or_unsupported_records_cannot_be_read_or_overwritten(self) -> None:
        showrunner, unit, worker = self.tree()
        valid = showrunner.to_json()
        malformed: list[object] = [
            [], {"schema": 2, "rows": []}, {"schema": 1, "rows": {}},
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
            lambda: staff.ensure("showrunner", name="new"),
            lambda: staff.rename(showrunner.id, "new"),
            lambda: staff.move(showrunner.id, None), lambda: staff.retire(showrunner.id),
        )
        for index, document in enumerate([*malformed, "unparseable"]):
            before = self.write_document(document)
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
        for action in (lambda: staff.rename(MISSING_ID, "new"), lambda: staff.move(MISSING_ID, None)):
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
                _ = staff.ensure("showrunner", name="interrupted")
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
                _ = staff.ensure("showrunner", name="recovered")
            after = self.staff_file.read_bytes()
            self.assertNotEqual(after, before)
            created = staff.by_path("recovered")
            self.assertIsNotNone(created)
            self.assertEqual(synced_versions, [after])
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(staff.ensure("showrunner", name="recovered"), created)
            self.assertIn("unchanged", errors.getvalue())
            self.assertEqual(synced_versions, [after, after])
        self.assertEqual(self.staff_file.read_bytes(), after)
        self.assertEqual(list(self.staff_file.parent.glob(".staff.json.*")), [])

    def test_next_write_removes_temp_left_by_a_killed_writer(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        killed = subprocess.run(
            [sys.executable, "-c", KILLED_WRITER], cwd=CHECKOUT,
            capture_output=True, text=True, check=False, timeout=PROCESS_TIMEOUT,
        )
        self.assertEqual(killed.returncode, 73, killed.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)
        orphans = list(self.staff_file.parent.glob(".staff.json.*"))
        self.assertTrue(orphans, "writer did not leave a temp file before os.replace")
        unrelated = self.staff_file.parent / "keep.tmp"
        _ = unrelated.write_text("unrelated")
        created = staff.ensure("showrunner", name="recovered")
        self.assertTrue(all(not path.exists() for path in orphans))
        self.assertEqual(unrelated.read_text(), "unrelated")
        self.assertEqual(staff.by_id(created.id), created)

    def test_cli_show_and_get_sort_paths_and_apply_filters(self) -> None:
        showrunner, unit, worker = self.tree()
        other = staff.ensure("showrunner", name="aaa")
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

    def test_cli_maps_refused_unreadable_and_missing_mutations(self) -> None:
        _, unit, worker = self.tree()
        before = self.staff_file.read_bytes()
        refused = self.cli("rename", worker.id, "--name=test1", "--json", code=2)
        self.assertTrue(refused.stderr.strip())
        self.assertEqual(self.staff_file.read_bytes(), before)
        missing = self.cli("move", MISSING_ID, f"--parent={unit.parent}", "--json", code=1)
        self.assertTrue(missing.stderr.strip())
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = self.staff_file.write_text("{truncated")
        unreadable = self.cli("show", "--json", code=2)
        self.assertTrue(unreadable.stderr.strip())
        self.assertEqual(self.staff_file.read_text(), "{truncated")

    def test_move_cli_requires_the_explicit_parent_option(self) -> None:
        showrunner, unit, _ = self.tree()
        other = staff.ensure("showrunner", name="other")
        before = self.staff_file.read_bytes()
        _ = self.cli("move", unit.id, other.id, "--json", code=2)
        _ = self.cli("move", showrunner.id, "--json", code=2)
        self.assertEqual(self.staff_file.read_bytes(), before)
        attached = self.cli("move", other.id, f"--parent={showrunner.id}", "--json")
        self.assertEqual(cast(object, json.loads(attached.stdout)), replace(other, parent=showrunner.id).to_json())
        detached = self.cli("move", other.id, "--parent=none", "--json")
        self.assertEqual(cast(object, json.loads(detached.stdout)), other.to_json())

    def test_mac_reads_and_writes_use_owner_via_fake_ssh_without_a_local_copy(self) -> None:
        showrunner, unit, worker = self.tree()
        owner_show = self.cli("show", "--json")
        owner_display = self.cli("show").stdout
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
            self.assertEqual(self.cli("show").stdout, owner_display)
            created = staff.ensure("worker", name="test1", parent=unit.id)
            moved_name = staff.rename(unit.id, "registry")
            self.assertEqual(moved_name, replace(unit, name="registry"))
            self.assertEqual(staff.move(showrunner.id, None), showrunner)
            self.assertEqual(staff.retire(created.id), created)
            self.assertIsNone(staff.by_id(created.id))
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(staff.by_path("sdlc/registry/impl1"), worker)
        commands = self.ssh_commands()
        self.assertEqual(commands[:len(reads) + 1],
                         [["show", "--snapshot", "--json"]] * len(reads) + [["show", "--json"]])
        self.assertEqual([command[0] for command in commands[len(reads) + 1:]], ["ensure", "rename", "move", "retire", "show"])

    def test_mac_preserves_names_that_begin_with_a_dash(self) -> None:
        expected = staff.ensure("showrunner", name="-scratch")
        local_file = self.mac_file
        with self.machine("mac"):
            self.assertEqual(staff.ensure("showrunner", name="-scratch"), expected)
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
                    "STAFF_FILE": str(local_file), "STAFF_TEST_SSH_FAIL": "1",
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
                lambda: staff.ensure("showrunner", name="blocked"),
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
        attached = staff.ensure("showrunner", name="style", parent=showrunner.id)
        lines = {line.split()[0]: line for line in self.cli("show").stdout.splitlines()}
        for row, path in ((unit, "sdlc/models"), (worker, "sdlc/models/impl1"), (attached, "style")):
            with self.subTest(path=path):
                self.assertEqual(self.cli("get", path).stdout.rstrip("\n"), lines[row.id])
                self.assertEqual(lines[row.id].split()[2], path)

    def test_mac_write_waits_for_owner_operation_lock(self) -> None:
        _, unit, _ = self.tree()
        destination = staff.ensure("showrunner", name="other")
        lock = self.staff_file.parent / "operation.lock"
        with self.lock_notice(lock) as listener:
            with staff.operation():
                with self.machine("mac"):
                    pending = self.start_script(STAFF_SCRIPT, "move", unit.id, f"--parent={destination.id}", "--json")
                    self.assert_operation_blocks(pending, listener)
                self.assertEqual(staff.by_id(unit.id), unit)
            output, errors = pending.communicate(timeout=PROCESS_TIMEOUT)
        self.assertEqual(pending.returncode, 0, errors)
        self.assertEqual(cast(object, json.loads(output)), replace(unit, parent=destination.id).to_json())
        self.assertEqual(staff.by_id(unit.id), replace(unit, parent=destination.id))
        self.assertFalse(self.mac_file.exists())

    def test_operation_preserves_body_errors_and_releases_the_lock(self) -> None:
        failure = OSError("source update failed")
        with self.assertRaises(OSError) as raised:
            with staff.operation():
                raise failure
        self.assertIs(raised.exception, failure)
        with staff.operation():
            row = staff.ensure("showrunner", name="after-failure")
        self.assertEqual(staff.by_id(row.id), row)

    def test_operation_lock_errors_have_the_staff_unreadable_type(self) -> None:
        with mock.patch.object(fcntl, "flock", side_effect=OSError("lock failed")):
            with self.assertRaisesRegex(staff.StaffUnreadable, "staff operation: lock failed"):
                with staff.operation():
                    self.fail("entered an operation without acquiring its lock")

    def test_absent_records_have_the_same_typed_signal_on_owner_and_mac(self) -> None:
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                with self.assertRaises(staff.StaffAbsent):
                    _ = staff.snapshot()
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"]])
        self.assertFalse(self.staff_file.exists())
        self.assertFalse(self.mac_file.exists())

    def test_operation_joins_inherited_descriptor_and_keeps_the_outer_lock(self) -> None:
        with staff.operation():
            with staff.operation():
                _ = staff.ensure("showrunner", name="outer")
            child = subprocess.run([sys.executable, "-c",
                "from scripts.production import staff; " +
                "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='child')\")"],
                cwd=CHECKOUT, pass_fds=staff.operation_fds(), capture_output=True,
                text=True, timeout=PROCESS_TIMEOUT, check=False)
            self.assertEqual(child.returncode, 0, child.stderr)
            with self.lock_notice(self.staff_file.parent / "operation.lock") as listener:
                seed = self.start_script(STAFF_SCRIPT, "ensure", "showrunner", "--name=after")
                self.assert_operation_blocks(seed, listener)
        output, errors = seed.communicate(timeout=PROCESS_TIMEOUT)
        self.assertEqual(seed.returncode, 0, output + errors)
        self.assertEqual({row.name for row in staff.snapshot().rows}, {"outer", "child", "after"})

    def test_operation_joins_a_valid_inherited_lock_after_its_owner_exits(self) -> None:
        path = self.staff_file.parent / "operation.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as inherited:
            exited = subprocess.Popen([sys.executable, "-c",
                "import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX)", str(inherited.fileno())],
                pass_fds=(inherited.fileno(),))
            self.assertEqual(exited.wait(timeout=PROCESS_TIMEOUT), 0)
            environment = {**os.environ, "STAFF_OPERATION_FD": str(inherited.fileno())}
            child = subprocess.run([sys.executable, "-c",
                "from scripts.production import staff; " +
                "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='orphan')\")"],
                cwd=CHECKOUT, env=environment, pass_fds=(inherited.fileno(),), capture_output=True,
                text=True, timeout=3, check=False)
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
                    "exec(\"with staff.operation():\\n staff.ensure('showrunner', name='fallback')\")")
                child = subprocess.Popen([sys.executable, "-c", program], cwd=CHECKOUT,
                    env=environment, pass_fds=(unlocked.fileno(),), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                self.assert_operation_blocks(child, listener)
        output, errors = child.communicate(timeout=PROCESS_TIMEOUT)
        self.assertEqual(child.returncode, 0, output + errors)
        self.assertEqual(len(staff.find(kind="showrunner")), 1)

    def test_operation_rejects_an_inherited_descriptor_for_another_file(self) -> None:
        self.assert_invalid_operation_descriptor_blocks(True)

    def test_operation_rejects_an_unlocked_inherited_descriptor(self) -> None:
        self.assert_invalid_operation_descriptor_blocks(False)

    def test_pending_retirement_waits_for_workers_then_retires_units_before_showrunners(self) -> None:
        runner, unit, worker = self.tree()
        child = staff.ensure("showrunner", name="child", parent=runner.id)
        staff.defer_retire((runner.id, unit.id))
        original = self.staff_file.read_bytes()
        staff.defer_retire((unit.id, runner.id))
        self.assertEqual(self.staff_file.read_bytes(), original)
        self.assertEqual(staff.retire_pending(), tuple(sorted((runner.id, unit.id))))
        self.assertEqual(self.staff_file.read_bytes(), original)
        _ = staff.rename(unit.id, "panes")
        self.assertEqual(staff.snapshot().pending_retire, tuple(sorted((runner.id, unit.id))))
        _ = staff.retire(worker.id)
        self.assertEqual(staff.retire_pending(), ())
        self.assertIsNone(staff.by_id(unit.id))
        self.assertIsNone(staff.by_id(runner.id))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        completed = self.staff_file.read_bytes()
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(self.staff_file.read_bytes(), completed)

    def test_pending_retirement_round_trips_over_mac_transport(self) -> None:
        runner, unit, worker = self.tree()
        with self.machine("mac"):
            staff.defer_retire((runner.id, unit.id))
            self.assertEqual(staff.snapshot().pending_retire, tuple(sorted((runner.id, unit.id))))
            self.assertEqual(staff.retire_pending(), tuple(sorted((runner.id, unit.id))))
            _ = staff.retire(worker.id)
            self.assertEqual(staff.retire_pending(), ())
        self.assertFalse(self.mac_file.exists())
        self.assertEqual(staff.snapshot().rows, ())

    def test_pending_retirement_drops_ids_already_retired(self) -> None:
        runner = staff.ensure("showrunner", name="gone")
        staff.defer_retire((runner.id,))
        _ = staff.retire(runner.id)
        self.assertEqual(staff.retire_pending(), ())
        self.assertEqual(staff.snapshot().pending_retire, ())


if __name__ == "__main__":
    _ = unittest.main()
