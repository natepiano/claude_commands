#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for index.py: incremental reads of the JSON lines, rebuilds, views, and `buildlog query`."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.buildlog import index
from scripts.buildlog.cgroup_test_support import CPU, GIB, ZRAM_READING, slice_reading
from scripts.buildlog import parse
from scripts.buildlog import store
from scripts.buildlog import report
from scripts.buildlog import record as recorder
from scripts.buildlog import step_outputs
from scripts.lib import test_hang_guard
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lint import sweep
from scripts.test_times.gate_record_test_support import gate_run
from scripts.buildlog.record import TARGET_DIR_UNSET
from scripts.buildlog.store import UNKNOWN_IDENTITY

CLI = Path(__file__).with_name("cli.py")
TESTDATA = Path(__file__).with_name("testdata")
STAMP = "2026-10-02T12:00:00.000Z"
FIXTURE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS fixture_index ON steps (status)"
SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
Record = dict[str, object]
_TEST_LOG = tempfile.TemporaryDirectory(prefix="buildlog-tests-")
_TEST_ROOT = Path(_TEST_LOG.name) / "buildlog"


COORDINATION_VARIABLES = (
    "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR",
    "MAC_TEST_STATE_DIR", "NOTIFIER_SESSIONS_DIR",
)


def use_test_log() -> None:
    os.environ["BUILDLOG_DIR"] = str(_TEST_ROOT)
    os.environ["BUILDLOG_TRANSCRIPTS"] = str(Path(_TEST_LOG.name) / "transcripts")


use_test_log()


def setUpModule() -> None:
    unittest.enterModuleContext(mock.patch.dict(
        os.environ, {name: str(_TEST_ROOT / name) for name in COORDINATION_VARIABLES},
    ))


def local_day(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone().date().isoformat()


def step(record_id: str, **fields: object) -> Record:
    record: Record = {
        "kind": "step",
        "v": 1,
        "id": record_id,
        "host": "natedev",
        "started_at": STAMP,
        "ended_at": STAMP,
        "duration_s": 10.0,
        "step": "clippy",
        "argv": ["cargo", "clippy", "--workspace"],
        "cwd": "/r/feature",
        "repo_path": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "sha": "abc1234",
        "caller": "agent",
        "status": 0,
        **parse.no_facts(),
        "log": None,
    }
    record.update(fields)
    return record


def call(record_id: str, **fields: object) -> Record:
    record: Record = {
        "kind": "call",
        "v": 1,
        "id": record_id,
        "host": "natedev",
        "started_at": STAMP,
        "ended_at": STAMP,
        "tool": "verify.sh",
        "command": "test hana",
        "verb": "test",
        "package": "hana",
        "outcome": "ran",
        "status": 0,
        "cached": True,
        "wait_s": 0,
        "wall_s": 0,
        "build_s": None,
        "saved_s": 0,
        "repo_path": "/r/hana",
        "worktree": "/r/feature",
        "branch": "feature",
        "backfilled": False,
    }
    record.update(fields)
    return record


def sample(at: str, **fields: object) -> Record:
    record: Record = {
        "kind": "sample",
        "v": 1,
        "host": "natedev",
        "at": at,
        "boot_id": "boot-a",
        "mem_used_bytes": 4 * 2**30,
        "swap_used_bytes": 2 * 2**30,
        "stall_some_us": 1_000_000,
        "stall_full_us": 200_000,
    }
    record.update(fields)
    return record


def ci_job(job_id: int, name: str, conclusion: str, times: tuple[str, str, str]) -> Record:
    created, started, completed = times
    return {
        "job_id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "started_at": started,
        "completed_at": completed,
        "runner": "hana-linux-1",
        "labels": ["hana-linux"],
        "steps": [
            {
                "number": 1,
                "name": "Set up job",
                "status": "completed",
                "conclusion": conclusion,
                "started_at": started,
                "completed_at": completed,
            }
        ],
    }


def ci_run(run_id: int, attempt: int, jobs: list[Record]) -> Record:
    return {
        "kind": "ci_run",
        "v": 1,
        "repo": "natepiano/hana",
        "run_id": run_id,
        "attempt": attempt,
        "workflow": "CI",
        "event": "push",
        "branch": "main",
        "sha": "0" * 40,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-02T12:00:00Z",
        "started_at": "2026-10-02T12:00:00Z",
        "updated_at": "2026-10-02T12:10:00Z",
        "url": f"https://github.com/natepiano/hana/actions/runs/{run_id}",
        "jobs": jobs,
    }


def point_root_at(test: unittest.TestCase, root: Path) -> None:
    """Override the suite's temporary log root for this test only."""
    previous = os.environ.get("BUILDLOG_DIR")
    os.environ["BUILDLOG_DIR"] = str(root)
    _ = cast(object, test.enterContext(mock.patch.dict(os.environ, {name: str(root / name) for name in COORDINATION_VARIABLES})))

    def restore() -> None:
        if previous is None:
            _ = os.environ.pop("BUILDLOG_DIR", None)
        else:
            os.environ["BUILDLOG_DIR"] = previous

    test.addCleanup(restore)


def rows(sql: str, *parameters: object) -> list[tuple[object, ...]]:
    with closing(index.read_only()) as connection:
        return cast(list[tuple[object, ...]], connection.execute(sql, parameters).fetchall())


def table_rows(table: str, where: str = "", *parameters: object) -> list[Record]:
    with closing(index.read_only()) as connection:
        connection.row_factory = sqlite3.Row
        records = cast(list[sqlite3.Row],
                       connection.execute(f"SELECT * FROM {table} {where} ORDER BY 1", parameters).fetchall())
        return [{name: cast(object, row[name]) for name in row.keys()
                 if not (table == "gate_modules" and name in {"config_reads", "folder_scans", "content_reads"} and row[name] is None)}
                for row in records]


class LogIsolationTests(unittest.TestCase):
    def test_import_preserves_the_callers_coordination_environment(self) -> None:
        environment = dict(os.environ)
        for name in COORDINATION_VARIABLES:
            _ = environment.pop(name, None)
        result = subprocess.run(
            [str(CLI.parents[1] / "lib/py"), "-c",
             "import os; from scripts.buildlog import test_index; print({key: os.environ[key] "
             + "for key in test_index.COORDINATION_VARIABLES if key in os.environ})"],
            cwd=Path(__file__).resolve().parents[2], env=environment, capture_output=True, text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")

    def test_each_module_uses_a_temporary_log(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "BUILDLOG_DIR"}
        real_root = Path.home() / ".local/state/buildlog"

        bootstrap = '''import importlib, json, os, sys, tempfile, traceback, unittest, unittest.mock
from scripts.buildlog import store

for module in json.loads(sys.argv[1]):
    importlib.import_module(module)

results = []
for module in sys.argv[2:]:
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        pid = os.fork()
        if pid == 0:
            os.dup2(output.fileno(), 1)
            os.dup2(errors.fileno(), 2)
            try:
                importlib.import_module(module)
                print(store.root(), flush=True)
            except BaseException:
                traceback.print_exc()
                raise SystemExit(1)
            raise SystemExit(0)
        _, status = os.waitpid(pid, 0)
        output.seek(0)
        errors.seek(0)
        results.append((os.waitstatus_to_exitcode(status), output.read().decode(), errors.read().decode()))
print(json.dumps(results))
'''
        modules = sorted(Path(__file__).parent.glob("test_*.py"))
        production_modules = sorted(
            "scripts.buildlog." + module.stem
            for module in Path(__file__).parent.glob("*.py")
            if not module.name.startswith("test_")
            and not module.name.endswith("_test_support.py")
            and module.name != "__init__.py"
        )
        parent = subprocess.run(
            [str(CLI.parents[1] / "lib/py"), "-c", bootstrap, json.dumps(production_modules),
             *("scripts.buildlog." + module.stem for module in modules)],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, check=True,
        )
        results = cast(list[tuple[int, str, str]], json.loads(parent.stdout))
        for module, (status, output, errors) in zip(modules, results, strict=True):
            with self.subTest(module=module.name):
                result = subprocess.CompletedProcess([module.name], status, output, errors)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(Path(result.stdout.strip()), real_root, module.name)


def encode(record: Record) -> bytes:
    return (json.dumps(record, separators=(",", ":")) + "\n").encode()


def configure_cleanup(test: unittest.TestCase, root: Path) -> Path:
    config = root.parent / "config"
    _ = write_config_fixture(config / "buildlog.conf")
    test.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
    return config


class IndexTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    host_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        self.host_file = self.root / "natedev" / "2026-10.jsonl"
        self.host_file.parent.mkdir(parents=True)

    def test_vcpu_schema_rebuild_keeps_old_records_and_reads_new_counts(self) -> None:
        old: Record = {**gate_run(), "kind": "gate_run", "v": 1, "id": "old", "host": "natedev"}
        new: Record = {**old, "id": "new", "vcpus": 16}
        self.write(self.host_file, old, new)
        columns, key, description = index.TABLES["gate_runs"]
        old_columns = [column for column in columns if column[0] != "vcpus"]
        with mock.patch.dict(index.TABLES, {"gate_runs": (old_columns, key, description)}), \
                mock.patch.object(index, "SCHEMA_VERSION", index.SCHEMA_VERSION - 1), \
                mock.patch.object(index, "GATE_RUN_COLUMNS", old_columns):
            self.assertEqual(index.update(), 2)
            old_path = index.index_path()
            self.assertEqual(self.rows("SELECT id FROM gate_runs ORDER BY id"), [("new",), ("old",)])
        self.assertEqual(index.update(), 2)
        self.assertNotEqual(index.index_path(), old_path)
        self.assertEqual(self.rows("SELECT id, vcpus FROM gate_runs ORDER BY id"), [("new", 16), ("old", None)])
        with closing(sqlite3.connect(old_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM gate_runs").fetchone(), (2,))

    def test_gate_config_reads_store_sorted_empty_and_missing_records(self) -> None:
        template = gate_run()["modules"][0]
        run: Record = {**gate_run(), "kind": "gate_run", "v": 1, "id": "config-reads", "host": "mac",
                       "modules": [{**template, "module": "test_empty.py", "config_reads": [], "folder_scans": [], "content_reads": []},
                                   {**template, "module": "test_missing.py"},
                                   {**template, "module": "test_read.py", "config_reads": ["z", "a", "z"],
                                    "folder_scans": ["scripts/", "docs", "scripts/"],
                                    "content_reads": ["scripts/z.py", "scripts/a.py", "scripts/z.py"]}]}
        self.write(self.host_file, run)
        _ = index.update()
        self.assertIn("config_reads", [row[1] for row in self.rows("PRAGMA table_info(gate_modules)")])
        self.assertEqual(self.rows("SELECT module, config_reads FROM gate_modules ORDER BY module"),
                         [("test_empty.py", "[]"), ("test_missing.py", None), ("test_read.py", '["a", "z"]')])
        self.assertEqual(self.rows("SELECT module, folder_scans FROM gate_modules ORDER BY module"),
                         [("test_empty.py", "[]"), ("test_missing.py", None),
                          ("test_read.py", '["docs", "scripts/"]')])
        self.assertEqual(self.rows("SELECT module, content_reads FROM gate_modules ORDER BY module"),
                         [("test_empty.py", "[]"), ("test_missing.py", None),
                          ("test_read.py", '["scripts/a.py", "scripts/z.py"]')])

    def test_update_enables_the_configured_spill_threshold(self) -> None:
        spill_pages = conf.read("buildlog").whole("INDEX_SPILL_PAGES")
        with closing(index.open_for_update(index.index_path())) as writer:
            self.assertEqual(writer.execute("PRAGMA cache_spill").fetchone(), (spill_pages,))

    def test_update_uses_wal_for_new_and_existing_indexes(self) -> None:
        with closing(index.open_for_update(index.index_path())) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone(), ("wal",))
        with closing(sqlite3.connect(index.index_path())) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode = DELETE").fetchone(), ("delete",))
        with closing(index.open_for_update(index.index_path())) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone(), ("wal",))

    def test_read_only_returns_committed_rows_during_spill_and_after_writer_closes(self) -> None:
        wal = Path(str(index.index_path()) + "-wal")
        with closing(index.open_for_update(index.index_path())) as writer:
            writer.execute("INSERT INTO meta (key, value) VALUES ('retained', 'committed')").close()
            writer.execute("PRAGMA cache_size = 10").close()
            writer.execute("PRAGMA cache_spill = 10").close()
            writer.execute("PRAGMA cache_spill = ON").close()
            before = wal.stat().st_size if wal.exists() else 0
            writer.execute("BEGIN IMMEDIATE").close()
            try:
                writer.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                                   ((f"pending-{number}", "x" * 4096) for number in range(100))).close()
                with self.subTest("WAL grows before commit"):
                    self.assertGreater(wal.stat().st_size if wal.exists() else 0, before)
                with mock.patch.object(index, "BUSY_TIMEOUT_MS", 100), closing(index.read_only()) as reader:
                    self.assertEqual(reader.execute("SELECT COUNT(*) FROM meta").fetchone(), (1,))
            finally:
                writer.execute("ROLLBACK").close()
        self.assertFalse(wal.exists())
        with closing(index.read_only()) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM meta").fetchone(), (1,))

    def test_complete_update_time_rolls_back_with_a_failed_update(self) -> None:
        self.write(self.host_file, step("retained"))
        with mock.patch.object(time, "time", return_value=100.0):
            _ = index.update()
        self.write(self.host_file, step("retained"), step("new"))
        with mock.patch.object(time, "time", side_effect=[150.0, RuntimeError("clock failed")]):
            with self.assertRaisesRegex(RuntimeError, "clock failed"):
                _ = index.update()
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key='updated_at'"), [("100.0",)])
        self.assertEqual(self.ids(), ["retained"])
        with mock.patch.object(time, "time", return_value=200.0):
            _ = index.update()
        self.assertEqual(self.rows("SELECT value FROM meta WHERE key='updated_at'"), [("200.0",)])
        self.assertEqual(self.ids(), ["new", "retained"])

    def test_reader_keeps_complete_rows_during_an_uncommitted_refresh(self) -> None:
        self.write(self.host_file, step("retained"))
        _ = index.update()
        with closing(index.open_for_update(index.index_path())) as writer:
            writer.execute("BEGIN IMMEDIATE").close()
            writer.execute("DELETE FROM steps").close()
            with closing(index.read_only()) as reader:
                reader.execute("PRAGMA busy_timeout=0").close()
                self.assertEqual(reader.execute("SELECT id FROM steps").fetchall(), [("retained",)])
            writer.execute("ROLLBACK").close()

    def test_report_uses_the_newest_complete_compatible_index_until_the_current_one_finishes(self) -> None:
        self.write(self.host_file, step("retained"))
        with mock.patch.object(time, "time", return_value=100.0):
            _ = index.update()
        original = index.index_path()
        alternate = {"fixture_rows": ([("src", "TEXT", "source")], "", "fixture")}
        with mock.patch.dict(index.TABLES, alternate):
            self.assertEqual(index.complete_gate_index_in(self.root), original)
            with mock.patch.object(time, "time", return_value=200.0):
                _ = index.update()
            newest = index.index_path()
        with mock.patch.object(index, "COST_INDEXES", (*index.COST_INDEXES, FIXTURE_INDEX_SQL)):
            self.assertEqual(index.complete_gate_index_in(self.root), newest)
            index.open_for_update(index.index_path()).close()
            self.assertEqual(index.complete_gate_index_in(self.root), newest)
        self.assertEqual(index.complete_gate_index_in(self.root), original)

    def test_report_ignores_an_alternate_index_with_an_invalid_completion_time(self) -> None:
        self.write(self.host_file, step("retained"))
        _ = index.update()
        valid = index.index_path()
        with mock.patch.object(index, "COST_INDEXES", (*index.COST_INDEXES, FIXTURE_INDEX_SQL)):
            _ = index.update()
            with closing(sqlite3.connect(index.index_path())) as writer, writer:
                writer.execute("UPDATE meta SET value='invalid' WHERE key='updated_at'").close()
        alternate = {"fixture_rows": ([("src", "TEXT", "source")], "", "fixture")}
        with mock.patch.dict(index.TABLES, alternate):
            self.assertEqual(index.complete_gate_index_in(self.root), valid)

    def test_schema_files_keep_independent_rows_and_offsets(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        original_path = index.index_path()
        with closing(sqlite3.connect(original_path)) as connection, connection:
            _ = connection.execute("UPDATE steps SET duration_s=123 WHERE id='retained'")
        with closing(sqlite3.connect(original_path)) as connection:
            original_rows = [line for line in connection.iterdump() if "updated_at" not in line]
        alternate = {"fixture_rows": ([("src", "TEXT", "source"), ("value", "TEXT", "fixture value")], "", "fixture")}
        with mock.patch.dict(index.TABLES, alternate):
            alternate_path = index.index_path()
            self.assertNotEqual(alternate_path, original_path)
            self.assertEqual(index.update(), 1)
            self.assertEqual(self.rows("SELECT duration_s FROM steps"), [(10.0,)])
            self.assertEqual(index.update(), 0)
        self.assertEqual(index.index_path(), original_path)
        self.assertEqual(index.update(), 0)
        with closing(sqlite3.connect(original_path)) as connection:
            self.assertEqual([line for line in connection.iterdump() if "updated_at" not in line], original_rows)
        self.assertEqual(self.rows("SELECT duration_s FROM steps"), [(123,)])
        self.assertTrue(alternate_path.is_file())

    def test_schema_path_changes_with_columns_and_indexes(self) -> None:
        original_path = index.index_path()
        columns, key, description = index.TABLES["steps"]
        without_group = [column for column in columns if column[0] != "build_history_group"]
        without_history = tuple(statement for statement in index.COST_INDEXES
                                if "steps_build_group_history" not in statement)
        with mock.patch.dict(index.TABLES, {"steps": (without_group, key, description)}):
            self.assertNotEqual(index.index_path(), original_path)
        with mock.patch.object(index, "COST_INDEXES", without_history):
            self.assertNotEqual(index.index_path(), original_path)

    def test_cleanup_reads_the_unused_schema_age_from_config_at_use(self) -> None:
        config = configure_cleanup(self, self.root)
        _ = write_config_fixture(config / "buildlog.conf", "INDEX_UNUSED_DAYS=30")
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        active_path = index.index_path()
        alternate = {"fixture_rows": ([("src", "TEXT", "source")], "", "fixture")}
        with mock.patch.dict(index.TABLES, alternate):
            self.assertEqual(index.update(), 1)
            old_path = index.index_path()
        with mock.patch.object(index, "COST_INDEXES", (*index.COST_INDEXES, FIXTURE_INDEX_SQL)):
            self.assertEqual(index.update(), 1)
            recent_path = index.index_path()
        now = time.time()
        old = now - 2 * 86400
        recent = now - 23 * 3600
        sidecars = [old_path.with_name(old_path.name + suffix) for suffix in SQLITE_SIDECARS]
        for path in sidecars:
            _ = path.write_bytes(b"old schema sidecar")
        os.utime(old_path, (old, old))
        os.utime(recent_path, (old, recent))
        self.assertEqual(index.update(), 0)
        self.assertTrue(old_path.is_file())
        _ = write_config_fixture(config / "buildlog.conf", "INDEX_UNUSED_DAYS=1")
        os.utime(active_path, (old, old))
        self.assertEqual(index.update(), 0)
        self.assertFalse(old_path.exists())
        for path in sidecars:
            self.assertFalse(path.exists(), path.name)
        self.assertTrue(recent_path.is_file())
        os.utime(recent_path, (recent, old))
        self.assertEqual(index.update(), 0)
        self.assertTrue(recent_path.is_file())
        self.assertEqual(self.ids(), ["retained"])
        _ = write_config_fixture(config / "buildlog.conf", "INDEX_UNUSED_DAYS=0")
        self.assertEqual(index.update(), 0)
        self.assertTrue(active_path.is_file())

    def test_update_removes_a_fresh_legacy_index_and_its_sidecars(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        legacy = self.root / "index.sqlite"
        paths = [legacy, *(legacy.with_name(legacy.name + suffix) for suffix in SQLITE_SIDECARS)]
        for path in paths:
            _ = path.write_bytes(b"legacy index")
        self.assertEqual(index.update(), 0)
        for path in paths:
            self.assertFalse(path.exists(), path.name)
        self.assertEqual(self.ids(), ["retained"])

    def test_update_finishes_interrupted_index_cleanup_and_removes_orphan_sidecars(self) -> None:
        config = configure_cleanup(self, self.root)
        _ = write_config_fixture(config / "buildlog.conf", "INDEX_UNUSED_DAYS=1")
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        old = time.time() - 2 * 86400
        unlink = Path.unlink
        for name in ("index.sqlite", "index-" + "a" * 64 + ".sqlite"):
            with self.subTest(name=name):
                obsolete = self.root / name
                paths = [obsolete, *(obsolete.with_name(name + suffix) for suffix in SQLITE_SIDECARS)]
                for path in paths:
                    _ = path.write_bytes(b"obsolete index")
                    os.utime(path, (old, old))
                removals = 0

                def interrupt_cleanup(path: Path, missing_ok: bool = False) -> None:
                    nonlocal removals
                    if path in paths and path.exists():
                        if removals == 1:
                            raise PermissionError("cleanup interrupted")
                        removals += 1
                    unlink(path, missing_ok=missing_ok)

                with mock.patch.object(Path, "unlink", interrupt_cleanup):
                    with self.assertRaisesRegex(PermissionError, "cleanup interrupted"):
                        _ = index.update()
                self.assertEqual(index.update(), 0)
                for path in paths:
                    self.assertFalse(path.exists(), path.name)
        orphan = self.root / ("index-" + "b" * 64 + ".sqlite-wal")
        _ = orphan.write_bytes(b"orphan sidecar")
        os.utime(orphan, (old, old))
        self.assertEqual(index.update(), 0)
        self.assertFalse(orphan.exists())
        self.assertEqual(self.ids(), ["retained"])

    def test_reindex_removes_current_index_sidecars_before_rebuilding(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        current = index.index_path()
        sidecars = [current.with_name(current.name + suffix) for suffix in SQLITE_SIDECARS]
        for path in sidecars:
            _ = path.write_bytes(b"stale sidecar")
        open_for_update = index.open_for_update

        def open_after_cleanup(path: Path) -> sqlite3.Connection:
            for sidecar in sidecars:
                self.assertFalse(sidecar.exists(), sidecar.name)
            return open_for_update(path)

        with mock.patch.object(index, "open_for_update", side_effect=open_after_cleanup):
            self.assertEqual(index.reindex(), 1)
        self.assertEqual(self.ids(), ["retained"])
        self.assertEqual(index.update(), 0)

    def test_update_reads_the_current_index_touch_interval_from_config_at_use(self) -> None:
        config = configure_cleanup(self, self.root)
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        current = index.index_path()
        now = time.time()
        for minutes, age, touches in ((120, 61, False), (60, 59, False), (60, 61, True)):
            with self.subTest(minutes=minutes, age=age):
                _ = write_config_fixture(config / "buildlog.conf", f"INDEX_TOUCH_MINUTES={minutes}")
                stamp = now - age * 60
                os.utime(current, (stamp, stamp))
                self.assertEqual(index.update(), 0)
                if touches:
                    self.assertGreater(current.stat().st_mtime, stamp)
                else:
                    self.assertEqual(current.stat().st_mtime, stamp)
                self.assertEqual(self.ids(), ["retained"])

    def test_missing_tables_rebuild_records_before_the_saved_offsets(self) -> None:
        self.write(self.host_file, step("retained"), call("retained-call"))
        self.assertEqual(index.update(), 2)
        original = self.host_file.read_bytes()
        for table in (next(iter(index.TABLES)), "files"):
            with self.subTest(table=table):
                with closing(sqlite3.connect(index.index_path())) as connection:
                    _ = connection.execute(f"DROP TABLE {table}")
                self.assertEqual(index.update(), 2)
                self.assertEqual(self.ids(), ["retained"])
                self.assertEqual(self.rows("SELECT id FROM calls"), [("retained-call",)])
                self.assertEqual(index.update(), 0)
                self.assertEqual(self.host_file.read_bytes(), original)

    def test_update_preserves_an_old_index_during_other_schema_cleanup(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, step("retained"))
        self.assertEqual(index.update(), 1)
        retained_path = index.index_path()
        old = time.time() - 40 * 86400
        os.utime(retained_path, (old, old))
        self.assertEqual(index.update(), 0)
        self.assertGreater(retained_path.stat().st_mtime, old)
        with mock.patch.object(index, "COST_INDEXES", (*index.COST_INDEXES, FIXTURE_INDEX_SQL)):
            self.assertEqual(index.update(), 1)
        self.assertTrue(retained_path.is_file())

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def rows(self, sql: str, *parameters: object) -> list[tuple[object, ...]]:
        return rows(sql, *parameters)

    def ids(self) -> list[object]:
        return [row[0] for row in self.rows("SELECT id FROM steps ORDER BY id")]

    def test_local_success_preserves_earlier_ssh_loss_and_original_queue_time(self) -> None:
        recorder.ssh_attempt({
            "call_id": "fallback", "target": "aws", "repo_identity": "github.com/team/hana",
            "queued_at": 100.0, "departed_at": 140.0, "slot": 3, "outcome": "lost",
            "completion": "missing", "cleanup": "unconfirmed", "unresolved_count": None,
            "evidence_at": 145.0,
        })
        self.write(self.host_file, call("fallback", target="natedev", status=0),
                   step("local-pass", call_id="fallback", target="natedev", caller="verify", status=0))

        _ = index.update()

        self.assertEqual(self.rows("SELECT target, status FROM calls WHERE id = 'fallback'"), [("natedev", 0)])
        query = ("SELECT call_id, target, repo_identity, queued_at, departed_at, slot, outcome, "
                 + "completion, cleanup, unresolved_count, evidence_at FROM ssh_attempts")
        expected = [("fallback", "aws", "github.com/team/hana", 100.0, 140.0, 3, "lost",
                     "missing", "unconfirmed", None, 145.0)]
        self.assertEqual(self.rows(query), expected)
        self.assertEqual(index.update(), 0)
        _ = index.reindex()
        self.assertEqual(self.rows(query), expected)

    def test_confirmed_completion_does_not_imply_confirmed_cleanup(self) -> None:
        for outcome, count in (("passed", None), ("failed", 2)):
            recorder.ssh_attempt({
                "call_id": outcome, "target": "aws", "repo_identity": "github.com/team/hana",
                "queued_at": 100.0, "departed_at": 140.0, "slot": 2, "outcome": outcome,
                "completion": "confirmed", "cleanup": "unconfirmed", "unresolved_count": count,
                "evidence_at": 151.0,
            })

        _ = index.update()

        self.assertEqual(self.rows(
            "SELECT call_id, completion, cleanup, unresolved_count, typeof(unresolved_count), evidence_at "
            + "FROM ssh_attempts ORDER BY call_id"),
            [("failed", "confirmed", "unconfirmed", 2, "integer", 151.0),
             ("passed", "confirmed", "unconfirmed", None, "null", 151.0)])

    def test_each_ssh_attempt_keeps_its_target_slot_and_evidence(self) -> None:
        for target, slot, outcome in (("mac", 1, "declined"), ("aws", 4, "passed")):
            recorder.ssh_attempt({
                "call_id": "one-call", "target": target, "repo_identity": "github.com/team/hana",
                "queued_at": 100.0, "departed_at": 140.0 + slot, "slot": slot, "outcome": outcome,
                "completion": "confirmed" if outcome == "passed" else "missing",
                "cleanup": "confirmed", "unresolved_count": 0, "evidence_at": 150.0 + slot,
            })

        _ = index.update()

        self.assertEqual(self.rows(
            "SELECT target, queued_at, departed_at, slot, outcome, completion, cleanup, unresolved_count, evidence_at "
            + "FROM ssh_attempts WHERE call_id = 'one-call' ORDER BY departed_at"),
            [("mac", 100.0, 141.0, 1, "declined", "missing", "confirmed", 0, 151.0),
             ("aws", 100.0, 144.0, 4, "passed", "confirmed", "confirmed", 0, 154.0)])

    def test_legacy_calls_do_not_invent_ssh_attempt_evidence(self) -> None:
        self.write(self.host_file, call("legacy", mac="passed_full", target="mac"))
        _ = index.update()
        self.assertEqual(self.rows("SELECT id, target, mac FROM calls"), [("legacy", "mac", "passed_full")])
        self.assertEqual(self.rows("SELECT count(*) FROM ssh_attempts"), [(0,)])

    def test_mac_nextest_rows_use_the_call_repository_from_local_steps_or_ssh_attempts(self) -> None:
        config = configure_cleanup(self, self.root)
        _ = write_config_fixture(config / "test_times.conf", "NEXTEST_SLOW_TEST_S=1.0")
        identity = "https://github.com/example/hana"
        facts = parse.log_facts("nextest", "        PASS [ 2.000s] hana tests::slow\n")
        mac_file = self.root / "Mac" / "2026-10.jsonl"
        mirrors = [step(f"mac-{name}", host="Nates-MacBook-Pro-2023", caller="verify-mac",
                        step="nextest", call_id=f"{name}-call", repo_path=None,
                        repo_identity=UNKNOWN_IDENTITY, **facts)
                   for name in ("local", "ssh", "unrelated")]
        self.write(mac_file, *mirrors)
        self.write(self.host_file,
                   call("local-call"), call("ssh-call"), call("unrelated-call"),
                   step("local-evidence", call_id="local-call", repo_identity=identity))
        recorder.ssh_attempt({
            "call_id": "ssh-call", "target": "mac", "repo_identity": identity,
            "queued_at": 100.0, "departed_at": 140.0, "slot": 1, "outcome": "passed",
            "completion": "confirmed", "cleanup": "confirmed", "unresolved_count": 0,
            "evidence_at": 150.0,
        })
        original = mac_file.read_bytes()
        _ = index.update()
        expected = [("mac-local", identity), ("mac-ssh", identity), ("mac-unrelated", UNKNOWN_IDENTITY)]
        step_query = ("SELECT id, repo_identity FROM steps WHERE host = 'Nates-MacBook-Pro-2023'"
                      + " ORDER BY id")
        test_query = ("SELECT t.step_id, s.repo_identity FROM tests t JOIN steps s ON s.id = t.step_id"
                      + " WHERE t.host = 'Nates-MacBook-Pro-2023' ORDER BY t.step_id")
        for rebuild in (False, True):
            with self.subTest(rebuild=rebuild):
                if rebuild:
                    _ = index.reindex()
                self.assertEqual(self.rows(step_query), expected)
                self.assertEqual(self.rows(test_query), expected)
                self.assertEqual(index.update(), 0)
        self.assertEqual(mac_file.read_bytes(), original)

    def test_derived_repository_identity_matches_rebuild_in_either_evidence_order(self) -> None:
        _ = configure_cleanup(self, self.root)
        local_identity = "https://example.org/local"
        ssh_identity = "https://example.org/ssh"
        local = step("evidence", call_id="served", repo_identity=local_identity)
        ssh: Record = {"kind": "ssh_attempt", "id": "attempt", "call_id": "served",
                       "repo_identity": ssh_identity, "at": STAMP}
        for first, second, initial in ((ssh, local, ssh_identity), (local, ssh, local_identity)):
            with self.subTest(first=first["kind"]):
                mirror = step("mirror", call_id="served", host="Nates-MacBook-Pro-2023",
                              caller="verify-mac", repo_path=None, repo_identity=UNKNOWN_IDENTITY)
                self.write(self.host_file, call("served"), mirror, first)
                _ = index.update()
                self.assertEqual(self.rows("SELECT repo_identity FROM steps WHERE id = 'mirror'"),
                                 [(initial,)])
                store.append_line(self.host_file, second)
                _ = index.update()
                query = "SELECT repo_identity, identity_derived FROM steps WHERE id = 'mirror'"
                incremental = self.rows(query)
                self.assertEqual(incremental, [(local_identity, 1)])
                _ = index.reindex()
                self.assertEqual(self.rows(query), incremental)

    def test_call_identity_changes_preserve_recorder_written_step_identity(self) -> None:
        _ = configure_cleanup(self, self.root)
        identity = "https://example.org/recorded"
        self.write(self.host_file, call("served", host="other-gate"),
                   step("recorded", call_id="served", repo_identity=identity),
                   step("unknown", call_id="served", host="other-builder", caller="verify-linux",
                        repo_path=None, repo_identity=UNKNOWN_IDENTITY))
        _ = index.update()
        store.append_line(self.host_file, step("later", call_id="served", host="other-builder",
                                              started_at="2026-10-02T12:01:00.000Z", repo_identity="https://example.org/later"))
        _ = index.update()
        query = "SELECT repo_identity, identity_derived FROM steps WHERE id = 'recorded'"
        expected = [(identity, 0)]
        self.assertEqual(self.rows(query), expected)
        self.assertEqual(self.rows("SELECT repo_identity FROM steps WHERE id = 'unknown'"),
                         [("https://example.org/later",)])
        _ = index.reindex()
        self.assertEqual(self.rows(query), expected)

    def test_identity_resolution_skips_samples_and_steps_of_untouched_calls(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, call("served"), call("untouched"),
                   step("evidence", call_id="served", repo_identity="https://example.org/known"),
                   step("mirror", call_id="served", repo_identity=UNKNOWN_IDENTITY),
                   step("other", call_id="untouched", repo_identity=UNKNOWN_IDENTITY))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as database, database:
            _ = database.execute("UPDATE steps SET repo_identity = ?, identity_derived = 1 WHERE id IN ('mirror', 'other')",
                                 ("https://example.org/unchanged",))
        for entry in (sample(STAMP), {"kind": "memory_snapshot", "id": "snapshot", "host": "natedev", "at": STAMP}):
            store.append_line(self.host_file, entry)
            _ = index.update()
            self.assertEqual(self.rows("SELECT id, repo_identity FROM steps WHERE identity_derived = 1 ORDER BY id"),
                             [("mirror", "https://example.org/unchanged"), ("other", "https://example.org/unchanged")])
        store.append_line(self.host_file, call("served"))
        _ = index.update()
        self.assertEqual(self.rows("SELECT id, repo_identity FROM steps WHERE identity_derived = 1 ORDER BY id"),
                         [("mirror", "https://example.org/known"), ("other", "https://example.org/unchanged")])

    def test_update_restores_a_missing_call_index_before_reading_new_steps(self) -> None:
        _ = configure_cleanup(self, self.root)
        self.write(self.host_file, call("served"), step("evidence", call_id="served", repo_identity="https://example.org/known"))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as database, database:
            _ = database.execute("DROP INDEX steps_call")
        store.append_line(self.host_file, step("mirror", call_id="served", repo_identity=UNKNOWN_IDENTITY,
                                              tests=[{"binary": "hana", "test": "slow", "status": "passed", "duration_s": 2.0}]))
        _ = index.update()
        self.assertEqual(self.rows("SELECT repo_identity FROM steps WHERE id = 'mirror'"), [("https://example.org/known",)])
        join = ("SELECT t.test FROM steps s JOIN tests t ON t.step_id = s.id"
                + " WHERE s.repo_identity = ? AND s.host = ? AND s.started_at >= ?")
        parameters = ("https://example.org/known", "natedev", STAMP)
        self.assertEqual(self.rows(join, *parameters), [("slow",)])
        plan = self.rows("EXPLAIN QUERY PLAN " + join, *parameters)
        self.assertTrue(any("SEARCH t USING" in str(row[3]) and "step_id=?" in str(row[3]) for row in plan), plan)

    def test_nextest_recording_reads_the_threshold_at_use_and_keeps_only_durations_above_it(self) -> None:
        config = configure_cleanup(self, self.root)
        lines = [
            "        PASS [ 0.749s] hana tests::below",
            "        PASS [ 0.750s] hana tests::boundary",
            "        PASS [ 0.751s] hana tests::above_boundary",
            "        PASS [ 1.250s] hana tests::higher_boundary",
            "        PASS [ 1.251s] hana tests::above_higher_boundary",
            "        FAIL [ 0.010s] hana tests::failed",
            "  TRY 2 PASS [ 0.020s] hana tests::retried",
            "        SLOW [> 0.030s] hana tests::reported_slow",
            "        PASS [ 0.040s] hana tests::reported_slow",
        ]
        for threshold, expected in (
            (0.75, {"tests::above_boundary", "tests::higher_boundary", "tests::above_higher_boundary",
                    "tests::failed", "tests::retried", "tests::reported_slow"}),
            (1.25, {"tests::above_higher_boundary", "tests::failed", "tests::retried", "tests::reported_slow"}),
        ):
            with self.subTest(threshold=threshold):
                _ = write_config_fixture(config / "test_times.conf", f"NEXTEST_SLOW_TEST_S={threshold}")
                tests = parse.test_results(lines)[0]
                self.assertEqual({test["test"] for test in tests}, expected)
                self.write(self.host_file, step("threshold", step="nextest", tests=tests))
                _ = index.update()
                self.assertEqual({row[0] for row in self.rows("SELECT test FROM tests")}, expected)

    def cargo_build_rows(self, *commands: list[str]) -> list[tuple[object, ...]]:
        self.write(self.host_file, *(step(f"build-{number:03}", argv=argv) for number, argv in enumerate(commands)))
        self.assertEqual(index.update(), len(commands))
        return self.rows("SELECT build_base, build_items FROM steps ORDER BY id")

    def test_hana_filter_and_runtime_arguments_do_not_change_the_cargo_build(self) -> None:
        first, second = self.cargo_build_rows(
            ["cargo", "nextest", "run", "--workspace", "--bin", "hana", "-E", "package(hana)",
             "--test-threads", "2", "--no-fail-fast", "--retries", "2", "-j", "8", "--message-format", "json"],
            ["cargo", "test", "--all", "--bin=hana", "-E", "package(other)",
             "--test-threads=4", "--retries=3", "-j16", "--message-format=short", "--", "--features", "ignored"],
        )
        self.assertIsInstance(first[0], str)
        self.assertEqual(first, second)
        self.assertNotEqual(json.loads(cast(str, first[1])), [])

    def test_packages_features_profiles_and_target_are_normalized_together(self) -> None:
        first, second, third = self.cargo_build_rows(
            ["cargo", "nextest", "run", "-p", "hana", "-p", "bridge", "--exclude", "z", "--exclude", "a",
             "--features", "hana/dynamic_linking,trace", "-F", "serde, ui", "--release", "--target", "x86_64-unknown-linux-gnu", "--bin", "hana"],
            ["env", "RUSTC_WRAPPER=", "/nix/store/tool/bin/cargo", "+nightly", "test", "--package=bridge", "--package", "hana",
             "--exclude=a", "--exclude=z", "-F", "ui", "--features=trace,serde", "--features", "hana/dynamic_linking",
             "--profile", "release", "--target=x86_64-unknown-linux-gnu", "--bin=hana"],
            ["cargo", "nextest", "run", "--package", "bridge", "-p", "hana", "--exclude", "a", "--exclude", "z",
             "--features", "ui serde trace hana/dynamic_linking", "--cargo-profile=release",
             "--target", "x86_64-unknown-linux-gnu", "--bin", "hana"],
        )
        self.assertIsInstance(first[0], str)
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_each_build_input_changes_base_but_selectors_do_not(self) -> None:
        commands = [
            ["cargo", "nextest", "run", "--workspace", "--lib"],
            ["cargo", "nextest", "run", "--workspace", "--tests"],
            ["cargo", "check", "--workspace", "--lib"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--release"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--profile", "ci"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--target", "aarch64-unknown-linux-gnu"],
            ["cargo", "nextest", "run", "-p", "hana", "--lib"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--exclude", "hana"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--features", "hana/dynamic_linking"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--all-features"],
            ["cargo", "nextest", "run", "--workspace", "--lib", "--no-default-features"],
        ]
        rows = self.cargo_build_rows(*commands)
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertNotEqual(rows[0][1], rows[1][1])
        for command, row in zip(commands[2:], rows[2:], strict=True):
            with self.subTest(argv=command):
                self.assertIsInstance(row[0], str)
                self.assertNotEqual(row[0], rows[0][0])
        self.assertEqual(len({row[0] for row in rows[2:]}), len(rows) - 2)

    def test_repeated_targets_form_a_sorted_set_in_the_cargo_build_key(self) -> None:
        prefix = ["cargo", "nextest", "run", "--workspace", "--bin", "hana"]
        first, reordered, last_only, different_pair, default = self.cargo_build_rows(
            [*prefix, "--target", "aarch64-unknown-linux-gnu", "--target", "x86_64-unknown-linux-gnu"],
            [*prefix, "--target=x86_64-unknown-linux-gnu", "--target=aarch64-unknown-linux-gnu",
             "--target", "x86_64-unknown-linux-gnu"],
            [*prefix, "--target", "x86_64-unknown-linux-gnu"],
            [*prefix, "--target", "wasm32-unknown-unknown", "--target", "x86_64-unknown-linux-gnu"],
            prefix,
        )
        self.assertIsInstance(first[0], str)
        self.assertEqual(first, reordered)
        self.assertEqual(len({first[0], last_only[0], different_pair[0], default[0]}), 4)
        self.assertEqual(json.loads(cast(str, first[0]))["target"],
                         ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"])
        self.assertEqual(json.loads(cast(str, default[0]))["target"], [])
        for row in (last_only, different_pair, default):
            self.assertEqual(row[1], first[1])

    def test_hana_lib_and_multiple_test_items_survive_filter_and_feature_options(self) -> None:
        first, second, third = self.cargo_build_rows(
            ["cargo", "nextest", "run", "--lib", "--test", "a", "--test", "b", "-E", "package(hana)", "--features", "hana/dynamic_linking"],
            ["cargo", "test", "--test=b", "--lib", "--test=a", "--features=hana/dynamic_linking"],
            ["cargo", "nextest", "run", "--workspace", "--tests"],
        )
        self.assertEqual(first, second)
        items = cast(list[str], json.loads(cast(str, first[1])))
        self.assertEqual(items, ["--lib", "--test a", "--test b"])
        self.assertNotEqual(first[0], third[0])
        self.assertNotEqual(first[1], third[1])

    def test_all_cargo_target_selectors_are_items_and_inline_values_are_equivalent(self) -> None:
        first, second, default = self.cargo_build_rows(
            ["cargo", "test", "--lib", "--bin", "hana", "--bins", "--test", "a", "--tests", "--example", "demo",
             "--examples", "--bench", "speed", "--benches", "--all-targets", "--doc"],
            ["cargo", "test", "--doc", "--all-targets", "--benches", "--bench=speed", "--examples", "--example=demo",
             "--tests", "--test=a", "--bins", "--bin=hana", "--lib"],
            ["cargo", "test"],
        )
        self.assertEqual(first, second)
        self.assertEqual(first[0], default[0])
        items = cast(list[str], json.loads(cast(str, first[1])))
        self.assertEqual(items, [
            "--all-targets", "--bench speed", "--benches", "--bin hana", "--bins", "--doc", "--example demo",
            "--examples", "--lib", "--test a", "--tests",
        ])
        self.assertEqual(json.loads(cast(str, default[1])), [])

    def test_non_cargo_steps_have_no_build_identity(self) -> None:
        for base, items in self.cargo_build_rows(
            ["python3", "scripts/lint/sweep.py", "--workspace", "--tests"],
            ["taplo", "fmt"],
        ):
            self.assertIsNone(base)
            self.assertIsNone(items)

    def test_step_build_columns_preserve_names_and_choose_the_recorded_target_folder(self) -> None:
        self.write(
            self.host_file,
            step("target", target_dir="/shared/target", repo_identity="https://example.com/hana",
                 compiled_crate_names=["hana", "dependency", "hana"], crates_compiled=3),
            step("worktree", target_dir=None, compiled_crate_names=[], crates_compiled=0),
            step("cwd", worktree=None, cwd="/r/subdirectory"),
            step("unset", target_dir=TARGET_DIR_UNSET, repo_identity="git-root:fixture-root"),
            step("unknown", target_dir=UNKNOWN_IDENTITY, repo_identity=UNKNOWN_IDENTITY, worktree=None, cwd="/r/unknown"),
            step("null", target_dir=None, repo_identity=None),
        )
        original = self.host_file.read_bytes()
        self.assertEqual(index.update(), 6)
        rows = self.rows("SELECT id, compiled_crate_names, target_dir, repo_identity, target_folder FROM steps ORDER BY id")
        decoded = [(identifier, json.loads(cast(str, names)) if names is not None else None, target_dir, repository, folder)
                   for identifier, names, target_dir, repository, folder in rows]
        self.assertEqual(decoded, [
            ("cwd", None, UNKNOWN_IDENTITY, UNKNOWN_IDENTITY, None),
            ("null", None, UNKNOWN_IDENTITY, UNKNOWN_IDENTITY, None),
            ("target", ["hana", "dependency", "hana"], "/shared/target", "https://example.com/hana", "/shared/target"),
            ("unknown", None, UNKNOWN_IDENTITY, UNKNOWN_IDENTITY, None),
            ("unset", None, TARGET_DIR_UNSET, "git-root:fixture-root", None),
            ("worktree", [], UNKNOWN_IDENTITY, UNKNOWN_IDENTITY, None),
        ])
        columns = [(column[1], column[2], column[3]) for column in self.rows("PRAGMA table_info(steps)")
                   if column[1] in ("target_dir", "repo_identity")]
        self.assertCountEqual(columns, [("target_dir", "TEXT", 1), ("repo_identity", "TEXT", 1)])
        self.assertEqual(self.host_file.read_bytes(), original)

    def test_output_roots_survive_indexing_and_legacy_steps_stay_readable(self) -> None:
        self.write(self.host_file,
                   step("split", target_dir="/artifacts", target_directory="/artifacts",
                        build_directory="/intermediates"),
                   step("same", target_directory="/output", build_directory="/output"),
                   step("legacy", target_dir="/legacy-output"),
                   step("override", target_dir="/override-output",
                        argv=["cargo", "build", "--target-dir", "/override-output"]),
                   step("target-only", target_directory="/artifacts"),
                   step("build-only", build_directory="/intermediates"),
                   step("unknown"))
        self.assertEqual(index.update(), 7)
        self.assertEqual(self.rows(
            "SELECT id, target_directory, build_directory FROM steps ORDER BY id"), [
                ("build-only", UNKNOWN_IDENTITY, UNKNOWN_IDENTITY),
                ("legacy", UNKNOWN_IDENTITY, UNKNOWN_IDENTITY),
                ("override", UNKNOWN_IDENTITY, UNKNOWN_IDENTITY),
                ("same", "/output", "/output"),
                ("split", "/artifacts", "/intermediates"),
                ("target-only", UNKNOWN_IDENTITY, UNKNOWN_IDENTITY),
                ("unknown", UNKNOWN_IDENTITY, UNKNOWN_IDENTITY),
            ])

    def test_steps_without_output_pairs_do_not_add_recorded_cargo_mappings(self) -> None:
        config = configure_cleanup(self, self.root)
        _ = write_config_fixture(config / "lint.conf")
        self.enterContext(mock.patch.dict(os.environ, {"LINT_SWEEP_STATE_DIR": str(self.root / "sweep-state")}))
        override = self.root / "override-output"
        plain = self.root / "plain-output"
        started = (sweep.TARGET_OVERRIDE_RECORDER_CUT + timedelta(seconds=1)).isoformat()
        self.write(self.host_file,
                   step("override", target_dir=str(override), started_at=started,
                        argv=["cargo", "build", "--target-dir", str(override)]),
                   step("plain", target_dir=str(plain), started_at=started,
                        argv=["cargo", "build"]))
        self.assertEqual(index.update(), 2)
        outputs = step_outputs.recorded_output_roots(index.index_path(), "natedev")
        assert isinstance(outputs, step_outputs.RecordedStepOutputs)
        self.assertEqual(len(outputs.rows), 2)
        with mock.patch.object(store, "host_name", return_value="natedev"):
            mappings = sweep.discover_cargo_outputs([], lint_settings=conf.read("lint"))[1]
        pairs = {(item.target_directory, item.build_directory) for item in mappings.workspaces}
        for output in outputs.rows:
            with self.subTest(target_folder=output.target_folder):
                assert isinstance(output.target_folder, step_outputs.AbsoluteRecordedRoot)
                self.assertNotIn((output.target_folder.path, output.target_folder.path), pairs)

    def test_schema_bump_rebuilds_inferred_output_pairs_from_unchanged_log(self) -> None:
        self.write(self.host_file, step("inferred", target_dir="/legacy-output"))
        original = self.host_file.read_bytes()
        with mock.patch.object(index, "SCHEMA_VERSION", 34):
            self.assertEqual(index.update(), 1)
            old_index = index.index_path()
        with closing(sqlite3.connect(old_index)) as connection, connection:
            connection.execute(
                "UPDATE steps SET target_directory = target_dir, build_directory = target_dir"
            ).close()
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("SELECT target_directory, build_directory FROM steps"),
                         [(UNKNOWN_IDENTITY, UNKNOWN_IDENTITY)])
        self.assertEqual(self.host_file.read_bytes(), original)

    def test_test_phase_charge_bytes_rank_steps_and_preserve_null_for_older_rows(self) -> None:
        self.write(self.host_file,
                   step("large", test_phase_charge={"bytes": 8192}),
                   step("small", test_phase_charge={"bytes": 1024}),
                   step("no-charge", test_phase_charge=None),
                   step("legacy"))
        original = self.host_file.read_bytes()
        self.assertEqual(index.update(), 4)
        query = "SELECT id, test_phase_charge_bytes FROM steps ORDER BY test_phase_charge_bytes DESC, id"
        expected = [("large", 8192), ("small", 1024), ("legacy", None), ("no-charge", None)]
        self.assertEqual(self.rows(query), expected)
        self.assertEqual(index.reindex(), 4)
        self.assertEqual(self.rows(query), expected)
        self.assertEqual(self.host_file.read_bytes(), original)

    def test_charge_column_rejects_boolean_and_text_bytes(self) -> None:
        self.write(self.host_file,
                   step("boolean", test_phase_charge={"bytes": True}),
                   step("text", test_phase_charge={"bytes": "8192"}))
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows("SELECT id, test_phase_charge_bytes FROM steps ORDER BY id"),
                         [("boolean", None), ("text", None)])

    def test_schema_twenty_three_rebuilds_history_build_keys_without_rewriting_logs(self) -> None:
        self.write(self.host_file, step("legacy", seat="impl", argv=["cargo", "nextest", "run", "--workspace", "--bin", "hana"]))
        original = self.host_file.read_bytes()
        with closing(sqlite3.connect(index.index_path())) as connection:
            connection.execute("CREATE TABLE steps (id TEXT)").close()
            connection.execute("PRAGMA user_version = 23").close()
            connection.commit()
        self.assertEqual(index.update(), 1)
        self.assertGreater(index.SCHEMA_VERSION, 23)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        row = self.rows("SELECT worker, compiled_crate_names, target_dir, repo_identity, target_folder, build_base, build_items FROM steps")[0]
        self.assertEqual(row[:5], ("impl", None, UNKNOWN_IDENTITY, UNKNOWN_IDENTITY, None))
        self.assertIsInstance(row[5], str)
        self.assertNotEqual(json.loads(cast(str, row[6])), [])
        self.assertEqual(self.host_file.read_bytes(), original)

    def test_unknown_and_none_folders_never_use_the_worktree_after_schema_rebuild(self) -> None:
        self.write(self.host_file, step("none", target_dir="none"), step("empty", target_dir=""),
                   step("unknown", target_dir="unknown"), step("unset", target_dir="unset"),
                   step("absolute", target_dir="/shared/actual-target"))
        with closing(sqlite3.connect(index.index_path())) as connection:
            connection.execute("CREATE TABLE steps (id TEXT)").close()
            connection.execute(f"PRAGMA user_version = {index.SCHEMA_VERSION - 1}").close()
            connection.commit()
        self.assertEqual(index.update(), 5)
        self.assertEqual(self.rows("SELECT id, target_dir, target_folder FROM steps ORDER BY id"), [
            ("absolute", "/shared/actual-target", "/shared/actual-target"),
            ("empty", "unknown", None), ("none", "none", None),
            ("unknown", "unknown", None), ("unset", "unset", None),
        ])

    def test_report_history_keeps_legacy_groups_separate_from_real_folders(self) -> None:
        cases: tuple[tuple[str, Record, Record, bool], ...] = (
            ("legacy worktree", {"target_dir": "unset"}, {}, True),
            ("legacy cwd", {"worktree": None}, {"worktree": None}, True),
            ("other legacy checkout", {"worktree": "/old"}, {"worktree": "/new"}, False),
            ("known shared folder", {"target_dir": "/shared/output", "worktree": "/old"},
             {"target_dir": "/shared/output", "worktree": "/new"}, True),
            ("different real folders", {"target_dir": "/old/output"}, {"target_dir": "/new/output"}, False),
            ("legacy to known", {}, {"target_dir": "/shared/output"}, False),
            ("known to legacy", {"target_dir": "/shared/output"}, {}, False),
            ("explicit none", {"target_dir": "none"}, {"target_dir": "none"}, False),
            ("unresolved to unresolved", {"target_dir": "unresolved"}, {"target_dir": "unresolved"}, False),
            ("unresolved to legacy", {"target_dir": "unresolved"}, {}, False),
            ("legacy to unresolved", {}, {"target_dir": "unresolved"}, False),
        )
        for name, previous, current, expected in cases:
            with self.subTest(history=name):
                self.write(self.host_file,
                           step("previous", started_at="2026-09-01T00:00:00Z", finished_s=1.0, **previous),
                           step("current", finished_s=1.0, **current))
                _ = index.reindex()
                self.assertEqual(self.rows("SELECT " + report.warm_step_clause() + " FROM steps WHERE id = 'current'"),
                                 [(int(expected),)])

    def test_history_query_limits_reads_to_its_folder_or_checkout_group(self) -> None:
        size = 800
        self.write(self.host_file, *[
            step(str(number), worktree=f"/checkout/{number}",
                 target_dir=f"/output/{number}" if number % 2 else "unknown",
                 started_at=f"2026-09-01T00:{number // 60:02d}:{number % 60:02d}Z", finished_s=1.0)
            for number in range(size)
        ])
        _ = index.reindex()
        with closing(index.read_only()) as connection:
            sql = "SELECT " + report.warm_step_clause() + " FROM steps"
            plan = str(connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            self.assertIn("steps_build_group_history", plan)
            self.assertIn("build_history_group=? AND build_base=? AND started_at_us<?", plan)
            batches = 0

            def count_instructions() -> int:
                nonlocal batches
                batches += 1
                return 0

            connection.set_progress_handler(count_instructions, 1000)
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows, [(0,)] * size)
            self.assertLessEqual(batches * 1000, size * 150)

    def test_warm_history_lookup_has_the_folder_base_time_index(self) -> None:
        self.write(self.host_file, step("history"))
        self.assertEqual(index.update(), 1)
        indexed_columns = [
            [column[2] for column in self.rows(f'PRAGMA index_info("{name}")')]
            for name, in self.rows("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'steps'")
        ]
        self.assertIn(["host", "build_history_group", "build_base", "started_at_us"], indexed_columns)
        base, started_at_us = self.rows("SELECT build_base, started_at_us FROM steps")[0]
        plan = str(self.rows(
            "EXPLAIN QUERY PLAN SELECT id FROM steps WHERE host = ? AND build_history_group = ?"
            + " AND build_base = ? AND started_at_us < ? AND finished_s IS NOT NULL",
            "natedev", "legacy:/r/feature", base, started_at_us,
        ))
        self.assertIn("steps_build_group_history", plan)
        self.assertIn("started_at_us<?", plan)

    def test_indexed_start_instants_preserve_microseconds_and_normalize_offsets(self) -> None:
        self.write(
            self.host_file,
            step("whole", started_at="1970-01-01T00:00:00Z"),
            step("fractional-zero", started_at="1970-01-01T00:00:00.000000Z"),
            step("fraction", started_at="1970-01-01T00:00:00.123456Z"),
            step("offset", started_at="1970-01-01T01:00:00.123456+01:00"),
            step("next-microsecond", started_at="1970-01-01T00:00:00.123457Z"),
            step("before-epoch", started_at="1969-12-31T23:59:59.999999Z"),
        )
        original = self.host_file.read_bytes()
        self.assertEqual(index.update(), 6)
        self.assertEqual(self.rows("SELECT id, started_at_us FROM steps ORDER BY id"), [
            ("before-epoch", -1), ("fraction", 123456), ("fractional-zero", 0),
            ("next-microsecond", 123457), ("offset", 123456), ("whole", 0),
        ])
        self.assertEqual(self.host_file.read_bytes(), original)

    def test_first_index_read_maps_worker_fields_and_leaves_source_unchanged(self) -> None:
        partial = encode(step("pending", worker="next"))[:20]
        self.write(self.host_file, step("old-step", seat="impl", delegate_session="run-1"),
                   call("old-call", seat="test"))
        with self.host_file.open("ab") as output:
            _ = output.write(partial)
        original = self.host_file.read_bytes()
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows("SELECT worker FROM steps"), [("impl",)])
        self.assertEqual(self.rows("SELECT worker FROM calls"), [("test",)])
        rewritten = self.host_file.read_bytes()
        self.assertEqual(self.rows("SELECT unit_session FROM steps"), [("run-1",)])
        self.assertEqual(rewritten, original)
        self.assertTrue(rewritten.endswith(partial))
        modified = self.host_file.stat().st_mtime_ns
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.host_file.stat().st_mtime_ns, modified)

    def test_reads_only_new_complete_lines(self) -> None:
        first, second, third = (encode(step(f"s{number}")) for number in range(3))
        _ = self.host_file.write_bytes(first + second + third[:20])
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.ids(), ["s0", "s1"])
        self.assertEqual(index.update(), 0)
        with self.host_file.open("ab") as handle:
            _ = handle.write(third[20:])
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s0", "s1", "s2"])

    def test_file_replaced_by_rename_continues(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        self.assertEqual(index.update(), 2)
        partial = self.host_file.with_name("incoming.tmp")
        self.write(partial, step("s0"), step("s1"), step("s2"))
        _ = partial.replace(self.host_file)
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s0", "s1", "s2"])

    def test_shrunk_file_is_read_again(self) -> None:
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        self.write(self.host_file, step("s0", **nextest), step("s1"), step("s2"))
        _ = index.update()
        self.assertEqual(len(self.rows("SELECT * FROM tests")), 3)
        self.write(self.host_file, step("s3"))
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.ids(), ["s3"])
        self.assertEqual(self.rows("SELECT * FROM tests"), [])

    def test_rewritten_file_is_read_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        self.write(self.host_file, step("s5", status=1), step("s6", status=1), step("s7", status=1))
        self.assertEqual(index.update(), 3)
        self.assertEqual(self.ids(), ["s5", "s6", "s7"])

    def test_changed_last_record_is_read_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        self.write(self.host_file, step("s0"), step("s9", log="natedev/logs/2026-10/s9.log.gz"))
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.ids(), ["s0", "s9"])

    def test_deleted_file_rows_go(self) -> None:
        mac_file = self.root / "mac" / "2026-10.jsonl"
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        self.write(self.host_file, step("s0"))
        self.write(mac_file, step("m0", host="mac", **nextest), call("c0", host="mac"))
        self.assertEqual(index.update(), 3)
        mac_file.unlink()
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.ids(), ["s0"])
        self.assertEqual(self.rows("SELECT * FROM tests"), [])
        self.assertEqual(self.rows("SELECT * FROM calls"), [])
        self.assertEqual(self.rows("SELECT path FROM files"), [("natedev/2026-10.jsonl",)])

    def test_schema_version_mismatch_rebuilds(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("CREATE TABLE leftover (x)")
            _ = connection.execute(f"PRAGMA user_version = {index.SCHEMA_VERSION + 1}")
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name = 'leftover'"), [])
        self.assertEqual(self.ids(), ["s0", "s1"])

    def test_legacy_schema_recovers_gate_history_before_the_saved_offset(self) -> None:
        run: Record = {**gate_run(), "kind": "gate_run", "v": 1, "id": "recovered-gate", "host": "natedev"}
        before = step("before-gate", duration_s=12.5)
        after = call("after-gate", wall_s=6.0)
        source = self.host_file.relative_to(self.root).as_posix()
        for legacy_version in (29, 30):
            with self.subTest(legacy_version=legacy_version):
                index.index_path().unlink(missing_ok=True)
                self.write(self.host_file, before, run, after)
                original = self.host_file.read_bytes()
                with closing(sqlite3.connect(index.index_path())) as connection:
                    index.create_schema(connection)
                    for table in ("gate_tests", "gate_modules", "gate_suites", "gate_runs"):
                        _ = connection.execute(f"DROP TABLE {table}")
                    index.add_step(connection, before, source)
                    index.add_call(connection, after, source)
                    with self.host_file.open("rb") as handle:
                        stored_marks = index.marks(handle, len(original))
                    _ = connection.execute(
                        "INSERT INTO files (path, offset, marks) VALUES (?, ?, ?)",
                        (source, len(original), stored_marks),
                    )
                    _ = connection.execute(f"PRAGMA user_version = {legacy_version}")
                    connection.commit()

                self.assertEqual(index.update(), 3)
                self.assertEqual(self.rows(
                    "SELECT id, selection, status, modules_run, tests_run FROM gate_runs"),
                    [("recovered-gate", "affected", 1, 1, None)])
                self.assertEqual(self.rows(
                    "SELECT run_id, suite, wall_s, status FROM gate_suites"),
                    [("recovered-gate", "scripts", 4.0, 1)])
                self.assertEqual(self.rows(
                    "SELECT run_id, suite, module, attempt, tests_run, tests_s FROM gate_modules"),
                    [("recovered-gate", "scripts", "test_example.py", 1, 2, 1.5)])
                self.assertEqual(self.rows(
                    "SELECT run_id, suite, module, attempt, test, duration_s, status FROM gate_tests ORDER BY test"),
                    [("recovered-gate", "scripts", "test_example.py", 1, "Example.test_fail", 0.5, "fail"),
                     ("recovered-gate", "scripts", "test_example.py", 1, "Example.test_pass", 1.0, "pass")])
                self.assertEqual(self.rows("SELECT id, duration_s FROM steps"), [("before-gate", 12.5)])
                self.assertEqual(self.rows("SELECT id, wall_s FROM calls"), [("after-gate", 6.0)])
                self.assertEqual(self.host_file.read_bytes(), original)
                self.assertEqual(index.update(), 0)
                self.assertEqual(self.host_file.read_bytes(), original)

    def test_schema_five_rebuilds_with_memory_admission_columns(self) -> None:
        self.write(self.host_file, step("s0", mem_wait_s=12))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 5")
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT mem_wait_s FROM steps"), [(12,)])

    def test_memory_stall_columns_and_samples_ingest(self) -> None:
        self.assertGreater(index.SCHEMA_VERSION, 4)
        sample_file = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(
            self.host_file,
            step("s0", peak_mem_bytes=123456, mem_stall_some_s=1.25, mem_stall_full_s=0.25, mem_wait_s=7),
            step("s1", peak_mem_bytes=123456),
        )
        self.write(sample_file, sample(STAMP, builds_anon_bytes=2**30, ci_anon_bytes=2 * 2**30), sample("2026-10-02T12:01:00.000Z", stall_some_us=1_500_000))
        self.assertEqual(index.update(), 4)
        self.assertEqual(
            self.rows("SELECT id, mem_stall_some_s, mem_stall_full_s FROM steps ORDER BY id"),
            [("s0", 1.25, 0.25), ("s1", None, None)],
        )
        self.assertEqual(
            self.rows(
                "SELECT host, at, boot_id, mem_used_bytes, swap_used_bytes, stall_some_us, stall_full_us"
                + " FROM samples ORDER BY at"
            ),
            [
                ("natedev", STAMP, "boot-a", 4 * 2**30, 2 * 2**30, 1_000_000, 200_000),
                ("natedev", "2026-10-02T12:01:00.000Z", "boot-a", 4 * 2**30, 2 * 2**30, 1_500_000, 200_000),
            ],
        )
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.rows("SELECT mem_wait_s FROM steps ORDER BY id"), [(7,), (None,)])
        self.assertEqual(self.rows("SELECT builds_anon_bytes, ci_anon_bytes FROM samples ORDER BY at"), [(2**30, 2 * 2**30), (None, None)])

    def test_raw_sample_resources_and_scope_counters_survive_schema_rebuild(self) -> None:
        slices = {"builds": slice_reading(12 * GIB), "ci": slice_reading(3 * GIB, identity=99)}
        filesystem = {"state": "measured", "total": 100 * GIB, "used": 60 * GIB, "free": 30 * GIB,
                      "device": "254:0", "device_name": "dm-0"}
        pools = {"builds": {"state": "measured", "free_pool_tokens": 17}, "ci": {"state": "unavailable"}}
        scope = {"cpu": CPU, "io": {"state": "unavailable"}, "identity": 42}
        path = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(path, sample(STAMP), sample("2026-10-02T12:01:00Z", slices=slices,
                                             filesystem=filesystem, jobserver=pools, zram=ZRAM_READING))
        self.write(self.host_file, step("resources", scope_resources=scope))
        original = path.read_bytes()
        self.assertEqual(index.update(), 3)
        self.assertEqual(self.rows("SELECT slices, filesystem, jobserver, zram FROM samples ORDER BY at")[0],
                         (None, None, None, None))
        encoded = self.rows("SELECT slices, filesystem, jobserver, zram FROM samples ORDER BY at")[1]
        self.assertEqual([json.loads(cast(str, value)) for value in encoded], [slices, filesystem, pools, ZRAM_READING])
        self.assertEqual({row[1]: row[2] for row in self.rows("PRAGMA table_info(samples)")}["zram"], "TEXT")
        self.assertEqual(json.loads(cast(str, self.rows("SELECT scope_resources FROM steps")[0][0])), scope)
        for legacy_version in (25, 32):
            with self.subTest(legacy_version=legacy_version):
                with closing(sqlite3.connect(index.index_path())) as connection:
                    _ = connection.execute("ALTER TABLE samples DROP COLUMN zram")
                    _ = connection.execute(f"PRAGMA user_version = {legacy_version}")
                    connection.commit()
                self.assertEqual(index.update(), 3)
                self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
                self.assertEqual(path.read_bytes(), original)
                rows = self.rows("SELECT slices, filesystem, jobserver, zram FROM samples ORDER BY at")
                self.assertEqual(rows[0], (None, None, None, None))
                self.assertEqual([json.loads(cast(str, value)) for value in rows[1]],
                                 [slices, filesystem, pools, ZRAM_READING])

    def test_cpu_io_sample_columns_preserve_old_nulls_and_new_counters(self) -> None:
        path = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(path, sample(STAMP), sample("2026-10-02T12:01:00.000Z",
                   cpu_stall_some_us=12_000_000, cpu_stall_full_us=0,
                   io_stall_some_us=4_000_000, io_stall_full_us=1_000_000,
                   cpu_idle_ticks=400, cpu_iowait_ticks=50, cpu_total_ticks=621))
        original = path.read_bytes()
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows(
            "SELECT cpu_stall_some_us, cpu_stall_full_us, io_stall_some_us, io_stall_full_us,"
            + " cpu_idle_ticks, cpu_iowait_ticks, cpu_total_ticks FROM samples ORDER BY at"),
            [(None, None, None, None, None, None, None), (12_000_000, 0, 4_000_000, 1_000_000, 400, 50, 621)])
        column_types = {row[1]: row[2] for row in self.rows("PRAGMA table_info(samples)")}
        for name in ("cpu_stall_some_us", "cpu_stall_full_us", "io_stall_some_us", "io_stall_full_us",
                     "cpu_idle_ticks", "cpu_iowait_ticks", "cpu_total_ticks"):
            self.assertEqual(column_types[name], "INTEGER")
        self.assertEqual(path.read_bytes(), original)

    def test_schema_twenty_two_rebuilds_cpu_io_counters_from_json_lines(self) -> None:
        path = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(path, sample(STAMP, cpu_stall_some_us=123, cpu_stall_full_us=12,
                               io_stall_some_us=456, io_stall_full_us=45,
                               cpu_idle_ticks=789, cpu_iowait_ticks=7, cpu_total_ticks=1000))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 22")
            _ = connection.execute("DELETE FROM samples")
            connection.commit()
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows(
            "SELECT cpu_stall_some_us, cpu_stall_full_us, io_stall_some_us, io_stall_full_us,"
            + " cpu_idle_ticks, cpu_iowait_ticks, cpu_total_ticks FROM samples"),
            [(123, 12, 456, 45, 789, 7, 1000)])

    def test_schema_command_lists_samples_and_stall_columns(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TABLE samples", result.stdout)
        self.assertIn("mem_stall_some_s", result.stdout)
        self.assertIn("mem_stall_full_s", result.stdout)
        self.assertIn("TABLE mac_idle", result.stdout)
        self.assertIn("one row per cause per call", result.stdout)
        self.assertIn("retry_marker", result.stdout)

    def test_sample_power_profile_indexes_without_backfilling_old_lines(self) -> None:
        path = self.root / "natedev" / "samples-2026-10.jsonl"
        since = "2026-10-02T12:00:30.000Z"
        records = (sample(STAMP), sample("2026-10-02T12:01:00.000Z",
                                         power_profile="performance", power_profile_since=since))
        self.write(path, *records)
        original = path.read_bytes()
        self.assertEqual(index.update(), 2)
        self.assertEqual(
            self.rows("SELECT power_profile, power_profile_since FROM samples ORDER BY at"),
            [(None, None), ("performance", since)],
        )
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([column for column in index.SAMPLE_COLUMNS
                          if column[0] in ("power_profile", "power_profile_since")], [
            ("power_profile", "TEXT", "power-profiles-daemon profile; NULL when not recorded"),
            ("power_profile_since", "TEXT", "UTC ISO mtime of the daemon's state file: when the profile was set"),
        ])

    def test_schema_fourteen_rebuilds_sample_profiles_from_json_lines(self) -> None:
        path = self.root / "natedev" / "samples-2026-10.jsonl"
        self.write(path, sample(STAMP, power_profile="power-saver", power_profile_since=STAMP))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 14")
            _ = connection.execute("UPDATE samples SET power_profile = NULL, power_profile_since = NULL")
            connection.commit()
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT power_profile, power_profile_since FROM samples"), [("power-saver", STAMP)])

    def test_reindex_reads_everything_again(self) -> None:
        self.write(self.host_file, step("s0"), step("s1"), call("c0"))
        self.assertEqual(index.update(), 3)
        self.assertEqual(index.reindex(), 3)
        self.assertEqual(self.ids(), ["s0", "s1"])

    def test_read_only_rejects_writes(self) -> None:
        self.write(self.host_file, step("s0"))
        _ = index.update()
        with closing(index.read_only()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                _ = connection.execute("DELETE FROM steps")
            with self.assertRaises(sqlite3.OperationalError):
                _ = connection.execute("CREATE TABLE extra (x)")
        self.assertEqual(self.ids(), ["s0"])

    def test_views(self) -> None:
        nextest = parse.log_facts("nextest", (TESTDATA / "nextest.txt").read_text())
        log = "natedev/logs/2026-10/n0.log.gz"
        self.write(
            self.host_file,
            step("c0", duration_s=10.0, peak_mem_bytes=2 * 1073741824),
            step("c1", duration_s=20.0, status=101, errors=1, log=log),
            step("n0", step="nextest", status=100, **nextest),
            call("v0", outcome="reused", saved_s=30),
            call("v1", outcome="reused", saved_s=12),
            call("v2", outcome="ran", wall_s=50),
        )
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(
                1,
                1,
                [
                    ci_job(11, "Clippy", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")),
                    ci_job(12, "Benchmark", "skipped", skipped),
                ],
            ),
            ci_run(
                1,
                2,
                [ci_job(21, "Clippy", "failure", ("2026-10-02T12:20:00Z", "2026-10-02T12:20:10Z", "2026-10-02T12:21:10Z"))],
            ),
        )
        _ = index.update()
        day = local_day(STAMP)

        clippy = self.rows(
            "SELECT day, repo, runs, failed, total_s, avg_s, max_s, max_peak_gib FROM step_days WHERE step = 'clippy'"
        )
        self.assertEqual(clippy, [(day, "hana", 2, 1, 30.0, 15.0, 20.0, 2.0)])

        failures = self.rows("SELECT id, worktree_name, log FROM failures ORDER BY id")
        self.assertEqual([row[0] for row in failures], ["c1", "n0"])
        self.assertEqual(failures[0][1], "feature")
        failed_log = failures[0][2]
        self.assertEqual(failed_log, f"{self.root}/{log}")
        self.assertTrue(Path(str(failed_log)).is_absolute())
        self.assertIsNone(failures[1][2])

        flaky = self.rows("SELECT test, runs, flaky, retried, passed, failed FROM flaky_tests ORDER BY test")
        self.assertEqual(flaky, [("tests::fails", 1, 0, 1, 0, 1), ("tests::flaky", 1, 1, 1, 1, 0)])
        self.assertEqual(self.rows("SELECT test, max_s FROM slow_tests"), [("tests::slow", 1.5)])

        outcomes = self.rows("SELECT day, repo, verb, outcome, calls, wall_s, saved_s FROM call_outcomes ORDER BY outcome")
        self.assertEqual(outcomes, [(day, "hana", "test", "ran", 1, 50, 0), (day, "hana", "test", "reused", 2, 0, 42)])

        jobs = self.rows(
            "SELECT name, jobs, failed, avg_s, max_s, avg_queue_s, max_queue_s FROM ci_job_days ORDER BY name"
        )
        # A skipped job never ran: no time of its own, and none dragged into an average.
        self.assertEqual(jobs, [("Benchmark", 1, 0, None, None, None, None), ("Clippy", 2, 1, 90.0, 120.0, 20.0, 30.0)])
        runs = self.rows("SELECT run_id, attempt, jobs, duration_s FROM ci_runs ORDER BY attempt")
        self.assertEqual(runs, [(1, 1, 2, 600.0), (1, 2, 1, 600.0)])
        self.assertEqual(len(self.rows("SELECT * FROM ci_steps")), 3)

    def test_verify_mac_no_test_status_stays_raw_without_counting_as_failure(self) -> None:
        mac_file = self.root / "Mac" / "2026-10.jsonl"
        self.write(
            mac_file,
            step("no-test", host="Mac", caller="verify-mac", step="nextest", status=4),
            step("test-failure", host="Mac", caller="verify-mac", step="nextest", status=42),
            step("test-pass", host="Mac", caller="verify-mac", step="nextest", status=0),
        )
        self.assertEqual(index.update(), 3)

        self.assertGreater(index.SCHEMA_VERSION, 10)
        self.assertEqual(
            self.rows("SELECT id, status FROM steps ORDER BY id"),
            [("no-test", 4), ("test-failure", 42), ("test-pass", 0)],
        )
        self.assertEqual(
            self.rows("SELECT runs, failed FROM step_days WHERE caller = 'verify-mac'"),
            [(3, 1)],
        )
        self.assertEqual(
            self.rows("SELECT id, status FROM failures ORDER BY id"),
            [("test-failure", 42)],
        )

    def test_verify_mac_status_four_without_step_counts_as_failure(self) -> None:
        self.write(self.host_file, step("missing-step", caller="verify-mac", step=None, status=4))
        self.assertEqual(index.update(), 1)

        self.assertEqual(
            self.rows("SELECT step, runs, failed FROM step_days WHERE caller = 'verify-mac'"),
            [(None, 1, 1)],
        )
        self.assertEqual(
            self.rows("SELECT id, status FROM failures"),
            [("missing-step", 4)],
        )

    def test_ci_jobs_distinguish_rerun_from_carried_over_and_exclude_carried_time(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-03T12:20:00Z", original[1], original[2])
        rerun = ("2026-10-03T12:20:00Z", "2026-10-03T12:20:10Z", "2026-10-03T12:21:10Z")
        fresh = ("2026-10-03T12:30:00Z", "2026-10-03T12:30:05Z", "2026-10-03T12:31:05Z")
        second_attempt = ci_run(1, 2, [ci_job(21, "Clippy", "success", carried), ci_job(22, "Test", "failure", rerun)])
        second_attempt.update({"created_at": "2026-10-03T12:20:00Z", "started_at": "2026-10-03T12:20:00Z",
                               "updated_at": "2026-10-03T12:32:00Z"})
        other_run = ci_run(2, 1, [ci_job(31, "Clippy", "success", fresh)])
        other_run.update({"created_at": "2026-10-03T12:30:00Z", "started_at": "2026-10-03T12:30:00Z",
                          "updated_at": "2026-10-03T12:32:00Z"})
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            second_attempt,
            ci_run(1, 1, [ci_job(11, "Clippy", "success", original)]),
            other_run,
        )
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT job_id, run_state, duration_s, queued_s FROM ci_jobs ORDER BY job_id"),
            [(11, "ran", 120.0, 30.0), (21, "carried_over", None, None), (22, "ran", 60.0, 10.0), (31, "ran", 60.0, 5.0)],
        )
        self.assertEqual(
            self.rows(
                "SELECT day, name, jobs, failed, avg_s, max_s, avg_queue_s, max_queue_s"
                + " FROM ci_job_days ORDER BY day, name"
            ),
            [
                ("2026-10-02", "Clippy", 1, 0, 120.0, 120.0, 30.0, 30.0),
                ("2026-10-03", "Clippy", 1, 0, 60.0, 60.0, 5.0, 5.0),
                ("2026-10-03", "Test", 1, 1, 60.0, 60.0, 10.0, 10.0),
            ],
        )

    def test_ci_job_days_uses_run_attempt_day_across_local_midnight(self) -> None:
        before_midnight = datetime(2026, 10, 2, 23, 59).astimezone().isoformat()
        after_midnight = datetime(2026, 10, 3, 0, 1).astimezone().isoformat()
        started = datetime(2026, 10, 3, 0, 2).astimezone().isoformat()
        completed = datetime(2026, 10, 3, 0, 4).astimezone().isoformat()
        run = ci_run(3, 1, [ci_job(31, "Midnight", "success", (after_midnight, started, completed))])
        run.update({"created_at": before_midnight, "started_at": before_midnight, "updated_at": completed})
        self.write(self.root / "ci" / "2026-10.jsonl", run)
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT day, name, jobs, avg_queue_s FROM ci_job_days"),
            [("2026-10-02", "Midnight", 1, 60.0)],
        )
        self.assertEqual(self.rows("SELECT queued_s, run_state FROM ci_jobs"), [(60.0, "ran")])

    def test_call_token_wait_defaults_to_zero_for_old_records(self) -> None:
        self.write(self.host_file, call("old"), call("new", token_wait_s=13))
        _ = index.update()
        self.assertEqual(
            self.rows("SELECT id, token_wait_s FROM calls ORDER BY id"),
            [("new", 13), ("old", 0)],
        )

    def test_offload_call_fields_reach_index_and_old_records_read_as_null(self) -> None:
        self.write(
            self.host_file,
            call("old"),
            call(
                "offloaded",
                mac="declined",
                mac_reason="no_tests",
                local_reason="needs Linux filesystem semantics",
                mac_s=12.5,
            ),
        )

        _ = index.update()

        self.assertEqual(
            self.rows(
                "SELECT id, mac, mac_reason, local_reason, mac_s FROM calls ORDER BY id"
            ),
            [
                (
                    "offloaded",
                    "declined",
                    "no_tests",
                    "needs Linux filesystem semantics",
                    12.5,
                ),
                ("old", None, None, None, None),
            ],
        )
        self.assertGreater(index.SCHEMA_VERSION, 23)

    def test_copy_call_fields_reach_index_and_old_records_read_as_null(self) -> None:
        self.write(
            self.host_file,
            call("old"),
            call("none", copy="", copy_s=None, copy_count=None, copy_natedev_s=None),
            call("won", copy="won", copy_s=41.5, copy_count=2, copy_natedev_s=95.25),
        )
        _ = index.update()
        self.assertEqual(
            self.rows("SELECT id, copy, copy_s, copy_count, copy_natedev_s FROM calls ORDER BY id"),
            [("none", "", None, None, None), ("old", None, None, None, None), ("won", "won", 41.5, 2, 95.25)],
        )

    def test_schema_sixteen_rebuilds_copy_fields_from_json_lines(self) -> None:
        self.write(self.host_file, call("copied", copy="gave_way", copy_s=12.0, copy_count=1, copy_natedev_s=30.0))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 16")
            _ = connection.execute("UPDATE calls SET copy = NULL, copy_s = NULL")
            connection.commit()
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT copy, copy_s, copy_count FROM calls"), [("gave_way", 12.0, 1)])

    def test_natedev_nextest_stopped_by_a_copy_win_is_not_a_failure(self) -> None:
        self.write(
            self.host_file,
            call("copy-won", copy="won", mac="failed", mac_reason="", mac_s=40.0, status=1, outcome="failed"),
            call("natedev-won", copy="natedev_won", status=1, outcome="failed"),
            step("stopped", step="nextest", caller="verify", call_id="copy-won", status=143),
            step("copy-won-clippy", caller="verify", call_id="copy-won", status=101),
            step("finished-first", step="nextest", caller="verify", call_id="natedev-won", status=100),
            step("mac-copy", host="Mac", step="nextest", caller="verify-mac", call_id="copy-won", status=100),
        )
        _ = index.update()
        self.assertEqual(
            self.rows("SELECT id, status FROM failures ORDER BY id"),
            [("copy-won-clippy", 101), ("finished-first", 100), ("mac-copy", 100)],
        )
        self.assertEqual(
            self.rows("SELECT host, step, runs, failed FROM step_days ORDER BY host, step"),
            [("Mac", "nextest", 1, 1), ("natedev", "clippy", 1, 1), ("natedev", "nextest", 2, 1)],
        )
        self.assertEqual(self.rows("SELECT status FROM steps WHERE id = 'stopped'"), [(143,)])

    def test_schema_thirteen_rebuilds_call_fields_from_json_lines(self) -> None:
        self.write(self.host_file, call("recorded", target="mac", hold_wait_s=0.4))
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 13")
            _ = connection.execute("UPDATE calls SET target = 'natedev', hold_wait_s = 99")
            connection.commit()
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT target, hold_wait_s FROM calls"), [("mac", 0.4)])

    def test_old_calls_have_null_target_and_hold_wait_but_new_empty_target_is_preserved(self) -> None:
        self.write(self.host_file, call("old"), call("empty", target="", hold_wait_s=0))
        _ = index.update()
        self.assertEqual(
            self.rows("SELECT id, target, hold_wait_s FROM calls ORDER BY id"),
            [("empty", "", 0.0), ("old", None, None)],
        )

    def test_steps_call_index_is_built_and_used(self) -> None:
        self.write(self.host_file, step("step", call_id="call"))
        _ = index.update()
        self.assertEqual(self.rows("PRAGMA index_info(steps_call)"), [(0, 28, "call_id")])
        self.assertIn("steps_call", str(self.rows("EXPLAIN QUERY PLAN SELECT id FROM steps WHERE call_id = ?", "call")))

    def test_queue_bump_reaches_query_rebuild_and_schema(self) -> None:
        event: Record = {"kind": "queue_bump", "v": 1, "id": "move-1", "host": "natedev", "at": STAMP,
                         "actor": "user", "session": "session-id", "step": "nextest", "folder": "/tmp/tree", "old_position": 3}
        self.write(self.host_file, event)
        _ = index.update()
        self.assertEqual(self.rows("SELECT actor, session, step, folder, old_position FROM queue_bumps"),
                         [("user", "session-id", "nextest", "/tmp/tree", 3)])
        _ = index.update()
        self.assertEqual(self.rows("SELECT count(*) FROM queue_bumps"), [(1,)])
        schema = subprocess.run([sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False)
        self.assertIn("TABLE queue_bumps", schema.stdout)
        with closing(sqlite3.connect(index.index_path_in(self.root))) as connection:
            _ = connection.execute("PRAGMA user_version = 1")
        _ = index.update()
        self.assertEqual(self.rows("SELECT old_position FROM queue_bumps"), [(3,)])

    def test_mac_holder_and_hold_event_schema_keep_source_and_identity_indexes(self) -> None:
        self.write(self.host_file, call("busy", mac="declined", mac_reason="busy", mac_holder="hana nextest"), call("old"))
        _ = index.update()
        self.assertEqual(self.rows("SELECT id, mac_holder FROM calls ORDER BY id"), [("busy", "hana nextest"), ("old", None)])
        self.assertEqual([row[2] for row in self.rows("PRAGMA index_info(hold_events_key_at)")],
                         ["host", "target", "hold_key", "at", "event"])
        self.assertEqual([row[2] for row in self.rows("PRAGMA index_info(hold_events_target_at)")], ["target", "at"])
        self.assertEqual([row[2] for row in self.rows("PRAGMA index_info(hold_events_src)")], ["src"])
        result = subprocess.run([sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TABLE hold_events", result.stdout)
        self.assertIn("one row per began or ended event", result.stdout)
        self.assertIn("no row proves a hold is in force now", result.stdout)

    def test_schema_describes_target_and_hold_wait(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"target\s+TEXT\s+where the call was when it ended: mac, natedev, or empty")
        self.assertRegex(
            result.stdout,
            r"hold_wait_s\s+REAL\s+seconds waited on a held target, each declined Mac retry included; memory waits are not counted",
        )

    def test_hold_build_rows_reach_index_rebuild_and_schema(self) -> None:
        path = self.root / "natedev" / "hold-builds-test.jsonl"
        self.write(path, {
            "kind": "hold_build", "id": "build", "host": "natedev", "at": STAMP,
            "hold_key": "holder", "hold_since": STAMP, "boot_id": "boot", "pid": 123,
            "proc_start": "456", "class": "no_session", "session": "", "command": "cargo test", "cwd": "/repo",
        })
        self.assertEqual(index.update(), 1)
        self.assertEqual(index.update(), 0)
        self.assertEqual(self.rows("SELECT hold_key, pid, proc_start, class, session, command, action FROM hold_builds"),
                         [("holder", 123, "456", "no_session", "", "cargo test", "audit")])
        with path.open("ab") as output:
            _ = output.write(encode({
                "kind": "hold_build", "id": "stopped", "host": "natedev", "at": STAMP,
                "hold_key": "holder", "hold_since": STAMP, "pid": 456, "proc_start": "789",
                "action": "stopped", "command": "python3 -m unittest",
            }))
        self.assertEqual(index.update(), 1)
        self.assertEqual(self.rows("SELECT action FROM hold_builds ORDER BY id"), [("audit",), ("stopped",)])
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("ALTER TABLE hold_builds DROP COLUMN action")
            _ = connection.execute("PRAGMA user_version = 20")
            connection.commit()
        self.assertEqual(index.update(), 2)
        self.assertEqual(self.rows("SELECT action FROM hold_builds ORDER BY id"), [("audit",), ("stopped",)])
        self.assertEqual(self.rows("PRAGMA user_version"), [(index.SCHEMA_VERSION,)])
        self.assertEqual(index.reindex(), 2)
        self.assertEqual(self.rows("SELECT DISTINCT src FROM hold_builds"), [("natedev/hold-builds-test.jsonl",)])
        result = subprocess.run([sys.executable, str(CLI), "schema"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TABLE hold_builds", result.stdout)
        self.assertIn("one row per build, hold and action: audit or stopped", result.stdout)
        self.assertIn("audit or stopped; old records read as audit", result.stdout)
        path.unlink()
        _ = index.update()
        self.assertEqual(self.rows("SELECT count(*) FROM hold_builds"), [(0,)])

    def test_ci_skipped_and_zero_queue_jobs_have_distinct_states(self) -> None:
        same_stamp = "2026-10-02T12:00:00Z"
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Zero", "success", (same_stamp, same_stamp, "2026-10-02T12:01:00Z")), ci_job(12, "Skipped", "skipped", skipped)]),
        )
        _ = index.update()

        self.assertEqual(
            self.rows("SELECT job_id, run_state, duration_s, queued_s FROM ci_jobs ORDER BY job_id"),
            [(11, "ran", 60.0, 0.0), (12, "skipped", None, None)],
        )

    def test_tree_and_port_lint_columns(self) -> None:
        self.write(
            self.host_file,
            step("s0", tree_key="k" * 64, tree_changed=False),
            step("s1", tree_key=None, tree_changed=True),
            step("s2"),
            call("p0", tool="port-lint", caller="cargo-port", outcome="reused", reuses="s0", saved_s=42),
            call("p1", tool="port-lint", caller="cargo-port", outcome="deferred", status=75, reason="port-lint: deferred"),
        )
        _ = index.update()
        steps = self.rows("SELECT id, tree_key, tree_changed FROM steps ORDER BY id")
        self.assertEqual(steps, [("s0", "k" * 64, 0), ("s1", None, 1), ("s2", None, None)])
        calls = self.rows("SELECT id, tool, caller, outcome, status, reuses, reason FROM calls ORDER BY id")
        self.assertEqual(
            calls,
            [
                ("p0", "port-lint", "cargo-port", "reused", 0, "s0", None),
                ("p1", "port-lint", "cargo-port", "deferred", 75, None, "port-lint: deferred"),
            ],
        )
        plan = self.rows(
            "EXPLAIN QUERY PLAN SELECT id FROM steps WHERE worktree = ? AND step = ? AND tree_key = ?",
            "/r/feature",
            "clippy",
            "k" * 64,
        )
        self.assertIn("steps_tree", str(plan))

    def test_cli_query_json(self) -> None:
        self.write(self.host_file, step("s0", status=0), step("s1", status=101))
        result = subprocess.run(
            [sys.executable, str(CLI), "query", "--json", "SELECT id, status, step FROM steps ORDER BY id"],
            capture_output=True,
            text=True,
            check=False,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = cast(list[dict[str, object]], json.loads(result.stdout))
        self.assertEqual(
            rows, [{"id": "s0", "status": 0, "step": "clippy"}, {"id": "s1", "status": 101, "step": "clippy"}]
        )
        self.assertIn("2 rows in", result.stderr)

        failed = subprocess.run(
            [sys.executable, str(CLI), "query", "DELETE FROM steps"],
            capture_output=True,
            text=True,
            check=False,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(failed.returncode, 1)
        self.assertIn("readonly", failed.stderr)


if __name__ == "__main__":
    _ = unittest.main()
