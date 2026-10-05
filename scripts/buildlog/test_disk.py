"""Disk snapshots from a fake tree; no test walks the host's disks."""

from __future__ import annotations

import io
import os
import socket
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr
from pathlib import Path
from typing import override
from unittest import mock

import cli
import disk
import store
import sweep
import test_index


class DiskTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        test_index.point_root_at(self, self.base() / "log")

    def base(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_measure_assigns_hard_links_to_the_first_row(self) -> None:
        base = self.base()
        first = base / "first"
        second = base / "second"
        first.mkdir()
        second.mkdir()
        source = first / "source"
        _ = source.write_bytes(os.urandom(8192))
        os.link(source, first / "inside-link")
        os.link(source, second / "cross-link")
        other = second / "other"
        _ = other.write_bytes(os.urandom(4096))
        source_bytes = source.stat().st_blocks * 512
        other_bytes = other.stat().st_blocks * 512
        usage = disk.FilesystemUsage(used=100_000, free=20_000)

        result = disk.measure([("first", str(first)), ("second", str(second))], lambda: usage, 10_000)

        self.assertEqual(result["rows"], [
            {"label": "first", "bytes": source_bytes},
            {"label": "second", "bytes": other_bytes},
        ])
        self.assertEqual(result["outside_build_cache_totals"], [
            {"label": "first", "bytes": source_bytes, "growth_bytes": source_bytes},
            {"label": "second", "bytes": other_bytes, "growth_bytes": other_bytes},
        ])
        self.assertEqual(result["used"], usage.used)
        self.assertEqual(result["free"], usage.free)
        self.assertEqual(result["floor"], 10_000)
        self.assertEqual(result["host"], store.host_name())
        self.assertTrue(result["measured_at"].endswith("Z"))

    def test_unreadable_folder_is_skipped(self) -> None:
        base = self.base()
        unreadable = base / "unreadable"
        readable = base / "readable"
        unreadable.mkdir()
        readable.mkdir()
        file = readable / "output"
        _ = file.write_bytes(os.urandom(4096))
        blocks = file.stat().st_blocks * 512
        scandir = os.scandir

        def list_directory(path: str) -> Iterator[os.DirEntry[str]]:
            if path == str(unreadable):
                raise PermissionError(path)
            return scandir(path)

        with mock.patch.object(os, "scandir", side_effect=list_directory):
            result = disk.measure(
                [("unreadable", str(unreadable)), ("readable", str(readable))],
                lambda: disk.FilesystemUsage(used=30_000, free=40_000),
                None,
            )
        self.assertEqual(result["rows"], [
            {"label": "unreadable", "bytes": 0},
            {"label": "readable", "bytes": blocks},
        ])

    def test_measure_reads_usage_once_after_all_rows_and_then_stamps_time(self) -> None:
        events: list[str] = []

        def walk(path: str, _seen: set[tuple[int, int]]) -> int:
            events.append(path)
            return 100

        def usage() -> disk.FilesystemUsage:
            events.append("usage")
            return disk.FilesystemUsage(used=500, free=600)

        def now() -> float:
            events.append("timestamp")
            return 1_000.0

        with (
            mock.patch.object(sweep, "directory_blocks", side_effect=walk),
            mock.patch.object(time, "time", side_effect=now),
        ):
            result = disk.measure([("first", "first"), ("second", "second")], usage, None)

        self.assertEqual(events, ["first", "second", "usage", "timestamp"])
        self.assertEqual(result["used"], 500)
        self.assertEqual(result["free"], 600)
        self.assertEqual(result["measured_at"], store.utc_iso(1_000.0))

    def test_snapshot_reads_filesystem_usage_once_after_the_walk(self) -> None:
        events: list[str] = []

        def walk(path: str, _seen: set[tuple[int, int]]) -> int:
            events.append(path)
            return 100

        def usage(path: str) -> disk.FilesystemUsage:
            self.assertEqual(path, "/")
            events.append("usage")
            return disk.FilesystemUsage(used=500, free=600)

        with (
            mock.patch.object(disk, "FOLDERS", [("first", "first"), ("second", "second")]),
            mock.patch.object(disk, "read_floor", return_value=None),
            mock.patch.object(sweep, "directory_blocks", side_effect=walk),
            mock.patch.object(disk, "filesystem_usage", side_effect=usage),
        ):
            result = disk.snapshot()

        self.assertEqual(events, ["first", "second", "usage"])
        self.assertEqual(result["used"], 500)
        self.assertEqual(result["free"], 600)

    def test_malformed_floor_read_reports_its_config_key(self) -> None:
        config = self.base() / "lint.conf"
        _ = config.write_text("sweep_free_floor_gib.testhost=lots\n")
        with (
            mock.patch.dict(os.environ, {sweep.CONFIG_ENV: str(config)}),
            mock.patch.object(socket, "gethostname", return_value="testhost"),
        ):
            with self.assertRaises(disk.InvalidFloor) as caught:
                _ = disk.read_floor()
        self.assertEqual(
            str(caught.exception),
            f"sweep_free_floor_gib.testhost in {config} must be a non-negative number of GiB",
        )

    def test_disk_command_keeps_last_snapshot_on_malformed_floor(self) -> None:
        base = self.base()
        test_index.point_root_at(self, base / "log")
        config = base / "lint.conf"
        _ = config.write_text("sweep_free_floor_gib.testhost=lots\n")
        previous = store.root() / store.DISK_NAME
        previous.parent.mkdir(parents=True)
        _ = previous.write_text("last good snapshot\n")
        errors = io.StringIO()

        with (
            mock.patch.dict(os.environ, {sweep.CONFIG_ENV: str(config)}),
            mock.patch.object(socket, "gethostname", return_value="testhost"),
            mock.patch.object(disk, "measure") as measure,
            redirect_stderr(errors),
        ):
            status = cli.main(["disk"])

        self.assertEqual(status, 1)
        self.assertEqual(
            errors.getvalue(),
            f"buildlog disk: sweep_free_floor_gib.testhost in {config} must be a non-negative number of GiB\n",
        )
        self.assertEqual(previous.read_text(), "last good snapshot\n")
        measure.assert_not_called()

    def test_snapshot_round_trip_in_suite_log(self) -> None:
        test_index.point_root_at(self, self.base() / "log")
        expected: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T12:00:00.000Z",
            "host": "natedev",
            "rows": [{"label": "~/rust", "bytes": 4096}],
            "used": 12_000,
            "free": 8_000,
            "floor": None,
        }
        self.assertIsNone(disk.read_snapshot())
        disk.write_snapshot(expected)
        self.assertEqual(disk.read_snapshot(), expected)
        self.assertFalse((store.root() / "disk.json.tmp").exists())
        _ = (store.root() / store.DISK_NAME).write_text("invalid json")
        self.assertIsNone(disk.read_snapshot())

    def test_outside_directories_exclude_targets_and_count_nested_hard_links_once(self) -> None:
        root = self.base() / "rust"
        traces = root / "project" / "traces"
        deep = traces / "deep"
        target = root / "project" / "target"
        deep.mkdir(parents=True)
        target.mkdir()
        _ = (target / sweep.RUSTC_INFO).write_text("target")
        _ = (target / "cache").write_bytes(os.urandom(4096))
        source = traces / "trace"
        _ = source.write_bytes(os.urandom(4096))
        os.link(source, deep / "trace-link")
        blocks = source.stat().st_blocks * 512

        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            measured = disk.measure([("rust", str(root))], lambda: disk.FilesystemUsage(0, 0), None)

        by_path = {row["path"]: row for row in measured["outside_build_caches"]}
        self.assertEqual(set(by_path), {str(root / "project"), str(traces)})
        self.assertEqual(by_path[str(root / "project")]["bytes"], blocks)
        self.assertEqual(by_path[str(traces)]["bytes"], blocks)
        self.assertEqual(by_path[str(root / "project")]["largest_child_path"], str(traces))
        self.assertEqual(by_path[str(root / "project")]["largest_child_growth_bytes"], blocks)
        self.assertIsNone(measured["previous_measured_at"])
        target_bytes = (target / "cache").stat().st_blocks * 512
        marker_bytes = (target / sweep.RUSTC_INFO).stat().st_blocks * 512
        self.assertEqual(measured["rows"][0]["bytes"], blocks + target_bytes + marker_bytes)

    def test_outside_directories_compare_previous_snapshot_and_count_new_directory(self) -> None:
        root = self.base() / "tmp"
        old = root / "old"
        child = old / "child"
        deep = child / "deep"
        deep.mkdir(parents=True)
        first_file = deep / "first"
        _ = first_file.write_bytes(os.urandom(4096))
        folders = [("tmp", str(root))]

        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            first = disk.measure(folders, lambda: disk.FilesystemUsage(0, 0), None)
            disk.write_snapshot(first)
            next_file = deep / "next"
            _ = next_file.write_bytes(os.urandom(8192))
            fresh = root / "new"
            fresh.mkdir()
            _ = (fresh / "new-file").write_bytes(os.urandom(4096))
            second = disk.measure(
                folders, lambda: disk.FilesystemUsage(0, 0), None,
                disk.previous_outside_measurement(disk.read_snapshot()),
            )

        growth = next_file.stat().st_blocks * 512
        by_path = {row["path"]: row for row in second["outside_build_caches"]}
        self.assertEqual(second["previous_measured_at"], first["measured_at"])
        self.assertEqual(by_path[str(old)]["growth_bytes"], growth)
        self.assertEqual(by_path[str(child)]["growth_bytes"], growth)
        self.assertEqual(by_path[str(old)]["largest_child_growth_bytes"], growth)
        self.assertEqual(by_path[str(child)]["largest_child_path"], str(deep))
        self.assertEqual(by_path[str(child)]["largest_child_growth_bytes"], growth)
        self.assertNotIn(str(deep), by_path)
        self.assertEqual(by_path[str(fresh)]["growth_bytes"], by_path[str(fresh)]["bytes"])

    def test_deleted_directory_reduces_folder_growth_without_a_listed_row(self) -> None:
        root = self.base() / "tmp"
        removed = root / "removed"
        removed.mkdir(parents=True)
        file = removed / "data"
        _ = file.write_bytes(os.urandom(4096))
        file_bytes = file.stat().st_blocks * 512
        folders = [("tmp", str(root))]

        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            first = disk.measure(folders, lambda: disk.FilesystemUsage(0, 0), None)
            disk.write_snapshot(first)
            file.unlink()
            removed.rmdir()
            second = disk.measure(
                folders, lambda: disk.FilesystemUsage(0, 0), None,
                disk.previous_outside_measurement(disk.read_snapshot()),
            )

        self.assertEqual(second["outside_build_caches"], [])
        self.assertEqual(first["outside_build_cache_totals"], [
            {"label": "tmp", "bytes": file_bytes, "growth_bytes": file_bytes},
        ])
        self.assertEqual(second["outside_build_cache_totals"], [
            {"label": "tmp", "bytes": 0, "growth_bytes": -file_bytes},
        ])

    def test_folder_total_counts_loose_files_and_directories_below_listing_threshold(self) -> None:
        root = self.base() / "tmp"
        small = root / "small"
        small.mkdir(parents=True)
        loose = root / "loose"
        nested = small / "nested"
        _ = loose.write_bytes(os.urandom(4096))
        _ = nested.write_bytes(os.urandom(4096))
        expected = (loose.stat().st_blocks + nested.stat().st_blocks) * 512

        result = disk.measure([("tmp", str(root))], lambda: disk.FilesystemUsage(0, 0), None)

        self.assertEqual(result["outside_build_caches"], [])
        self.assertEqual(result["outside_build_cache_totals"], [
            {"label": "tmp", "bytes": expected, "growth_bytes": expected},
        ])

    def test_folder_total_excludes_targets_and_counts_hard_links_once(self) -> None:
        root = self.base() / "rust"
        traces = root / "traces"
        traces.mkdir(parents=True)
        target = root / "project" / "target"
        target.mkdir(parents=True)
        _ = (target / sweep.RUSTC_INFO).write_text("target")
        _ = (target / "cache").write_bytes(os.urandom(4096))
        loose = root / "loose"
        _ = loose.write_bytes(os.urandom(4096))
        os.link(loose, traces / "linked")
        own = traces / "own"
        _ = own.write_bytes(os.urandom(4096))
        expected = (loose.stat().st_blocks + own.stat().st_blocks) * 512

        result = disk.measure([("rust", str(root))], lambda: disk.FilesystemUsage(0, 0), None)

        self.assertEqual(result["outside_build_cache_totals"], [
            {"label": "rust", "bytes": expected, "growth_bytes": expected},
        ])

    def test_old_snapshot_has_no_comparable_directory_measurement(self) -> None:
        root = self.base() / "tmp"
        folder = root / "traces"
        folder.mkdir(parents=True)
        _ = (folder / "file").write_bytes(os.urandom(4096))
        old: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T12:00:00Z", "host": "test", "rows": [],
            "used": 0, "free": 0, "floor": None,
        }
        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            result = disk.measure(
                [("tmp", str(root))], lambda: disk.FilesystemUsage(0, 0), None,
                disk.previous_outside_measurement(old),
            )
        self.assertIsNone(result["previous_measured_at"])
        self.assertEqual(result["outside_build_caches"][0]["growth_bytes"], result["outside_build_caches"][0]["bytes"])

    def test_previous_directory_rows_without_folder_totals_are_not_comparable(self) -> None:
        root = self.base() / "tmp"
        root.mkdir()
        file = root / "loose"
        _ = file.write_bytes(os.urandom(4096))
        blocks = file.stat().st_blocks * 512
        old: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T12:00:00Z", "host": "test", "rows": [],
            "used": 0, "free": 0, "floor": None, "outside_build_caches": [],
        }

        result = disk.measure(
            [("tmp", str(root))], lambda: disk.FilesystemUsage(0, 0), None,
            disk.previous_outside_measurement(old),
        )

        self.assertIsNone(result["previous_measured_at"])
        self.assertEqual(result["outside_build_cache_totals"], [
            {"label": "tmp", "bytes": blocks, "growth_bytes": blocks},
        ])

    def test_snapshot_reads_the_file_it_will_replace(self) -> None:
        root = self.base() / "tmp"
        folder = root / "traces"
        folder.mkdir(parents=True)
        _ = (folder / "first").write_bytes(os.urandom(4096))
        with (
            mock.patch.object(disk, "FOLDERS", [("tmp", str(root))]),
            mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1),
            mock.patch.object(disk, "read_floor", return_value=None),
            mock.patch.object(disk, "filesystem_usage", return_value=disk.FilesystemUsage(0, 0)),
        ):
            first = disk.snapshot()
            disk.write_snapshot(first)
            _ = (folder / "second").write_bytes(os.urandom(4096))
            second = disk.snapshot()

        self.assertEqual(second["previous_measured_at"], first["measured_at"])
        self.assertGreater(second["outside_build_caches"][0]["growth_bytes"], 0)


if __name__ == "__main__":
    _ = unittest.main()
