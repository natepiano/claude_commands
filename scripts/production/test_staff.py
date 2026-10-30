"""Exercise the staff contract with isolated state and an actual fake SSH process."""
from __future__ import annotations

import io
import json
import multiprocessing
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stderr
from dataclasses import FrozenInstanceError, replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import staff


CHECKOUT = Path(__file__).resolve().parents[2]
STAFF_SCRIPT = Path(__file__).with_name("staff.py")
MISSING_ID = staff.StaffId("s00000000000")
PROCESS_TIMEOUT = 30

# subprocesses must exercise owner behavior even when this suite runs on the Mac.
OWNER_CLI = """import os, runpy, sys
sys.platform = os.environ.get('STAFF_TEST_PLATFORM', 'linux')
script = sys.argv.pop(1)
sys.argv[0] = script
runpy.run_path(script, run_name='__main__')
"""

KILLED_WRITER = """import os, sys
sys.platform = 'linux'
from scripts.production import staff
os.replace = lambda *args, **kwargs: os._exit(73)
staff.ensure('showrunner', name='interrupted')
"""

# Execute the real CLI against a different temp file: this is the owner, not a
# canned response, so remote reads, writes, and error mapping must all agree.
FAKE_SSH = """import json, os, runpy, shlex, sys
from pathlib import Path
arguments = sys.argv[1:]
with open(os.environ['STAFF_TEST_SSH_LOG'], 'a') as log:
    log.write(json.dumps(arguments) + '\\n')
if os.environ.get('STAFF_TEST_SSH_FAIL'):
    reason = os.environ.get('STAFF_TEST_SSH_REASON', 'ssh: connection refused')
    if reason:
        print(reason, file=sys.stderr)
    raise SystemExit(int(os.environ.get('STAFF_TEST_SSH_EXIT', '255')))
if len(arguments) < 6 or arguments[:5] != ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'natedev']:
    raise SystemExit('unexpected SSH target or options')
command = arguments[5:]
if len(command) == 1:
    command = shlex.split(command[0])
if command[:2] != ['~/.claude/scripts/lib/py', '~/.claude/scripts/production/staff.py']:
    raise SystemExit('unexpected remote Python or staff script')
if '--json' not in command[2:]:
    raise SystemExit('remote staff call requires --json')
os.environ['STAFF_FILE'] = os.environ['STAFF_TEST_OWNER_FILE']
sys.platform = 'linux'
sys.path.insert(0, os.environ['STAFF_TEST_CHECKOUT'])
sys.argv = [os.environ['STAFF_TEST_SCRIPT'], *command[2:]]
runpy.run_path(sys.argv[0], run_name='__main__')
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


class StaffTests(unittest.TestCase):
    root: Path = Path()
    staff_file: Path = Path()
    ssh_log: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staff_file = self.root / "owner" / "staff.json"
        self.ssh_log = self.root / "ssh.jsonl"
        binaries = self.root / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n{FAKE_SSH}", encoding="utf-8")
        ssh.chmod(0o755)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "STAFF_FILE": str(self.staff_file),
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "STAFF_TEST_SSH_LOG": str(self.ssh_log),
            "STAFF_TEST_OWNER_FILE": str(self.staff_file),
            "STAFF_TEST_CHECKOUT": str(CHECKOUT),
            "STAFF_TEST_SCRIPT": str(STAFF_SCRIPT),
            "STAFF_TEST_SSH_FAIL": "",
            "STAFF_TEST_SSH_EXIT": "255",
            "STAFF_TEST_SSH_REASON": "ssh: connection refused",
            "STAFF_TEST_PLATFORM": "linux",
            "PYTHONDONTWRITEBYTECODE": "1",
        })))
        _ = self.enterContext(mock.patch("scripts.production.staff.remote.other_machine", return_value="mac"))

    def tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        showrunner = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="models", parent=showrunner.id)
        worker = staff.ensure("worker", name="impl1", parent=unit.id)
        return showrunner, unit, worker

    def cli(self, *arguments: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [sys.executable, "-c", OWNER_CLI, str(STAFF_SCRIPT), *arguments],
            cwd=CHECKOUT, capture_output=True, text=True, check=False,
            timeout=PROCESS_TIMEOUT,
        )
        self.assertEqual(completed.returncode, code, completed.stderr)
        return completed

    def write_document(self, document: object) -> bytes:
        self.staff_file.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        _ = self.staff_file.write_bytes(content)
        return content

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

    def ssh_commands(self) -> list[list[str]]:
        commands: list[list[str]] = []
        for line in self.ssh_log.read_text().splitlines():
            arguments = cast(list[str], json.loads(line))
            self.assertEqual(arguments[:5], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "natedev"])
            command = shlex.split(arguments[5]) if len(arguments) == 6 else arguments[5:]
            self.assertEqual(command[:2], ["~/.claude/scripts/lib/py", "~/.claude/scripts/production/staff.py"])
            self.assertIn("--json", command[2:])
            commands.append(command[2:])
        return commands

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
        local_file = self.root / "mac" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(local_file), "STAFF_TEST_PLATFORM": "darwin"}), \
                mock.patch("scripts.production.staff.remote.other_machine", return_value="natedev"):
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
        self.assertEqual(commands[:len(reads) + 1], [["show", "--json"]] * (len(reads) + 1))
        self.assertEqual([command[0] for command in commands[len(reads) + 1:]], ["ensure", "rename", "move", "retire", "show"])

    def test_mac_preserves_names_that_begin_with_a_dash(self) -> None:
        expected = staff.ensure("showrunner", name="-scratch")
        local_file = self.root / "mac" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(local_file)}), \
                mock.patch("scripts.production.staff.remote.other_machine", return_value="natedev"):
            self.assertEqual(staff.ensure("showrunner", name="-scratch"), expected)
            renamed = staff.rename(expected.id, "-renamed")
        self.assertEqual(renamed, staff.rename(expected.id, "-renamed"))
        self.assertEqual(staff.by_path("-renamed"), renamed)
        self.assertFalse(local_file.parent.exists())
        self.assertIn("--name=-scratch", self.ssh_commands()[0])
        self.assertIn("--name=-renamed", self.ssh_commands()[1])

    def test_owner_selection_uses_the_shared_remote_module(self) -> None:
        showrunner, _, _ = self.tree()
        local_file = self.root / "mac" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(local_file)}), \
                mock.patch("scripts.shutdown.remote.other_machine", return_value="natedev"):
            self.assertEqual(staff.by_id(showrunner.id), showrunner)
        self.assertFalse(local_file.parent.exists())
        self.assertEqual(self.ssh_commands(), [["show", "--json"]])

    def test_remote_errors_keep_application_reasons_and_identify_ssh_failures(self) -> None:
        _ = self.tree()
        before = self.staff_file.read_bytes()
        local_file = self.root / "mac" / "staff.json"
        cases = (
            (2, "usage: staff.py: invalid option", "usage: staff.py: invalid option"),
            (1, "remote interpreter failed", "remote interpreter failed"),
            (17, "remote command interrupted", "remote command interrupted"),
            (255, "ssh: connection refused", "natedev not reached"),
            (2, "", "natedev not reached"),
        )
        with mock.patch("scripts.production.staff.remote.other_machine", return_value="natedev"):
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
        local_file = self.root / "mac" / "staff.json"
        with mock.patch.dict(os.environ, {"STAFF_FILE": str(local_file), "STAFF_TEST_SSH_FAIL": "1"}), \
                mock.patch("scripts.production.staff.remote.other_machine", return_value="natedev"):
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


if __name__ == "__main__":
    _ = unittest.main()
