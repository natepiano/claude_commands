"""Disk snapshots from a fake tree; no test walks the host's disks."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from typing import override
from unittest import mock

from scripts.buildlog import cli, disk, filesystem, report
from scripts.buildlog import store
from scripts.lint import sweep
from scripts.buildlog import test_index
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib.cargo_output import CargoOutputRoots
from scripts.lib.user_zone import user_zone
from scripts.lint.memory_test_support import config_environment
from scripts.mac_test import linux_runner


def setUpModule() -> None:
    forbid_real_sender()


class DiskTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = self.base()
        test_index.point_root_at(self, root / "log")
        self.enterContext(mock.patch.dict(os.environ, config_environment(root)))
        self.enterContext(mock.patch.dict(os.environ, {
            "LINT_SWEEP_STATE_DIR": str(root / "sweep"), "USER_ZONE": "America/New_York",
        }))
        _ = (root / "buildlog.conf").write_text(
            "# whole: 1..\nDISK_WALK_MINUTES=60\n# whole: 0..23\nDISK_WALK_HOUR=3\n")

    def base(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_linux_capacity_checks_the_external_output_device(self) -> None:
        base = self.base()
        outputs = CargoOutputRoots(base, base / "target", base / "external")
        outputs.target_directory.mkdir()
        outputs.build_directory.mkdir()
        with mock.patch.object(linux_runner, "read_cargo_output_roots", return_value=outputs), \
             mock.patch.object(shutil, "disk_usage", side_effect=[(100, 10, 90), (100, 10, 90), (100, 99, 1)]):
            self.assertEqual(linux_runner.output_free_bytes(base), 1)

    def run_linux_probe(self) -> subprocess.CompletedProcess[str]:
        base = self.base()
        checkout = base / "checkout"
        checkout.mkdir()
        _ = (checkout / "Cargo.toml").write_text('[workspace]\n')
        installed = base / ".claude"
        installed.mkdir()
        (installed / "scripts").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
        _ = (installed / ".build-target-script-commit").write_text("fixture\n")
        support = base / "support"
        support.mkdir()
        capacities = {str(base): 97, str(checkout): 91}
        _ = (support / "capacity.json").write_text(json.dumps(capacities))
        tools = base / ".cargo" / "bin"
        tools.mkdir(parents=True)
        stub = (
            f"#!{sys.executable}\n"
            + "import json, os, sys\nfrom pathlib import Path\n"
            + "tool = Path(sys.argv[0]).name\n"
            + "support = Path(os.environ['PROBE_SUPPORT'])\n"
            + "if tool in ('cargo', 'rustc'):\n"
            + "    raise AssertionError(sys.argv)\n"
            + "elif tool == 'df':\n"
            + "    print('Filesystem Blocks Used Available Use% Mounted on')\n"
            + "    capacities = json.loads((support / 'capacity.json').read_text())\n"
            + "    for path in sys.argv[2:]:\n"
            + "        if not Path(path).is_dir():\n            raise SystemExit(1)\n"
            + "        print('fixture 999999999 0', capacities[path] * 1024 ** 2, '1%', path)\n"
            + "else:\n    print(tool + ' fixture')\n"
        )
        for tool in ("cargo", "rustc", "cargo-nextest", "cargo-mend", "df", "hostname"):
            path = tools / tool
            _ = path.write_text(stub)
            path.chmod(0o755)
        environment = {
            **os.environ, "HOME": str(base), "PYTHONPATH": str(support),
            "PROBE_SUPPORT": str(support),
        }
        return subprocess.run(["bash", "-c", linux_runner.probe_script("checkout")],
                              env=environment, capture_output=True, text=True, check=False)

    def test_linux_probe_reports_checkout_capacity_without_cargo_or_rustc(self) -> None:
        result = self.run_linux_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("free_gib=91.000000\n", result.stdout)
        self.assertIn("nextest=cargo-nextest fixture\n", result.stdout)
        self.assertIn("mend=cargo-mend fixture\n", result.stdout)
        self.assertIn("rc=0\n", result.stdout)

    def test_linux_capacity_retries_parent_when_output_is_removed_during_measurement(self) -> None:
        base = self.base()
        output = base / "cargo"
        output.mkdir()
        roots = CargoOutputRoots(base, base, output)
        measured: list[Path] = []

        def usage(path: Path) -> tuple[int, int, int]:
            measured.append(path)
            if path == output:
                output.rmdir()
                raise FileNotFoundError(str(path))
            return (100, 10, 90)

        with mock.patch.object(linux_runner, "read_cargo_output_roots", return_value=roots), \
             mock.patch.object(shutil, "disk_usage", side_effect=usage):
            self.assertEqual(linux_runner.output_free_bytes(base), 90)
        self.assertEqual(measured, [base, base, output, base])
        self.assertFalse(output.exists())

    def test_capacity_rows_count_each_device_once(self) -> None:
        base = self.base()
        paths = [base / name for name in ("source", "build", "alias")]
        for path in paths:
            path.mkdir()
        readings = {
            str(paths[0]): filesystem.FilesystemUsage(20, 80, 100, "1:1", "root"),
            str(paths[1]): filesystem.FilesystemUsage(60, 140, 200, "2:1", "build"),
            str(paths[2]): filesystem.FilesystemUsage(60, 140, 200, "2:1", "build"),
        }
        with mock.patch.object(filesystem, "filesystem_usage", side_effect=readings.__getitem__):
            rows = disk.capacity_rows(paths)
        self.assertEqual([(row["device"], row["free"]) for row in rows], [("1:1", 80), ("2:1", 140)])

    def test_snapshot_reports_saved_output_device_outside_the_checkout(self) -> None:
        base = self.base()
        checkout = base / "checkout"
        output = base / "other-drive" / "cargo"
        checkout.mkdir()
        output.mkdir(parents=True)
        cache = sweep.cargo_output_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(json.dumps([{
            "workspace_root": str(checkout), "target_directory": str(checkout / "target"),
            "build_directory": str(output),
        }]))
        original_stat = os.stat

        def device_stat(path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
                        *, dir_fd: int | None = None, follow_symlinks: bool = True) -> os.stat_result:
            info = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if str(path) == str(output):
                fields = list(info)
                fields[2] = 987654
                return os.stat_result(fields)
            return info

        def usage(path: str) -> filesystem.FilesystemUsage:
            if path == str(output):
                return filesystem.FilesystemUsage(60, 140, 200, "2:1", "cargo-device")
            return filesystem.FilesystemUsage(20, 80, 100, "1:1", "root-device")

        with mock.patch.dict(os.environ, {"SCCACHE_DIR": ""}), \
             mock.patch.object(disk, "FOLDERS", [("checkout", str(checkout))]), \
             mock.patch.object(disk, "read_floor", return_value=None), \
             mock.patch.object(os, "stat", side_effect=device_stat), \
             mock.patch.object(filesystem, "filesystem_usage", side_effect=usage):
            result = disk.snapshot()
        with mock.patch.object(disk, "read_snapshot", return_value=result):
            text = "\n".join(report.disk_section())
        self.assertIn(str(output), text)
        self.assertIn("2:1, cargo-device", text)
        self.assertEqual([row["device"] for row in result.get("filesystems", [])], ["1:1", "2:1"])

    def test_filesystem_total_preserves_reserved_blocks_and_top_layer_identity(self) -> None:
        from scripts.buildlog import cgroup_capture

        base = self.base()
        devices = base / "sys/dev/block"
        devices.mkdir(parents=True)
        dm = base / "sys/devices/virtual/block/dm-0"
        dm.mkdir(parents=True)
        (devices / "254:0").symlink_to(dm, target_is_directory=True)
        stats = os.statvfs_result((4096, 4096, 1000, 400, 300, 0, 0, 0, 0, 255))
        original_stat = os.stat

        def target_stat(path: object, **kwargs: object) -> os.stat_result:
            if path == str(base):
                return os.stat_result((0, 0, os.makedev(254, 0), 0, 0, 0, 0, 0, 0, 0))
            return original_stat(path, **kwargs)  # pyright: ignore[reportArgumentType]

        with mock.patch.object(os, "statvfs", return_value=stats), \
             mock.patch.object(os, "stat", side_effect=target_stat), \
             mock.patch.object(cgroup_capture, "SYS_DEV_BLOCK", devices):
            usage = filesystem.filesystem_usage(str(base))
        self.assertEqual((usage.total, usage.used, usage.free), (1000 * 4096, 600 * 4096, 300 * 4096))
        self.assertNotEqual(usage.total, usage.used + usage.free)
        self.assertEqual(usage.device, "254:0")
        self.assertEqual(usage.device_name, "dm-0")

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
        usage = filesystem.FilesystemUsage(used=100_000, free=20_000)

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
                lambda: filesystem.FilesystemUsage(used=30_000, free=40_000),
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

        def usage() -> filesystem.FilesystemUsage:
            events.append("usage")
            return filesystem.FilesystemUsage(used=500, free=600)

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
        old: disk.DiskSnapshot = {
            "measured_at": "2026-10-03T07:00:00Z", "host": "test", "rows": [],
            "used": 1, "free": 2, "floor": None,
        }
        full: disk.DiskSnapshot = {
            **old, "filesystems": [], "outside_build_caches": [],
            "outside_build_cache_totals": [], "previous_measured_at": None,
        }

        def walk(path: str, _seen: set[tuple[int, int]]) -> int:
            events.append(path)
            return 100

        def usage(path: str) -> filesystem.FilesystemUsage:
            self.assertEqual(path, "/")
            events.append("usage")
            return filesystem.FilesystemUsage(used=500, free=600)

        for label, previous, hour in (("none", None, 15), ("old shape", old, 15), ("old full", full, 3)):
            clock = mock.Mock(return_value=datetime(2026, 10, 4, hour, tzinfo=user_zone()))
            with (
                self.subTest(previous=label),
                mock.patch.object(disk, "FOLDERS", [("first", "first"), ("second", "second")]),
                mock.patch.object(disk, "read_floor", return_value=None),
                mock.patch.object(disk, "read_snapshot", return_value=previous),
                mock.patch.object(disk, "datetime", wraps=datetime, now=clock),
                mock.patch.object(sweep, "directory_blocks", side_effect=walk),
                mock.patch.object(filesystem, "filesystem_usage", side_effect=usage),
            ):
                events.clear()
                result = disk.snapshot()

            self.assertEqual(events, ["first", "second", "usage"])
            self.assertEqual(result["used"], 500)
            self.assertEqual(result["free"], 600)
            self.assertEqual(result["capacity_measured_at"], result["measured_at"])

    def test_malformed_floor_read_reports_its_config_key(self) -> None:
        config = self.base() / "lint.conf"
        _ = config.write_text("sweep_free_floor_gib.testhost=lots\n")
        with (
            mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}),
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
            mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}),
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
        root = self.base()
        test_index.point_root_at(self, root / "log")
        self.enterContext(mock.patch.dict(os.environ, config_environment(root)))
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
            measured = disk.measure([("rust", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None)

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

    def test_outside_directories_exclude_cargo_tag_target(self) -> None:
        root = self.base() / "rust"
        target = root / "project" / "target"
        fingerprint = target / "debug" / ".fingerprint" / "app-0123456789abcdef"
        fingerprint.mkdir(parents=True)
        _ = (fingerprint / "bin-app.json").write_text("{}")
        _ = (target / sweep.CACHE_TAG).write_text(
            f"Signature: 8a477f597d28d172789f06886806bc55\n{sweep.CARGO_TAG_LINE}\n"
        )
        _ = (target / "debug" / "app").write_bytes(os.urandom(4096))

        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            measured = disk.measure([("rust", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None)

        self.assertEqual(measured["outside_build_caches"], [])
        self.assertEqual(measured["outside_build_cache_totals"], [
            {"label": "rust", "bytes": 0, "growth_bytes": 0},
        ])

    def test_outside_directories_count_another_tools_cache_tag(self) -> None:
        root = self.base() / "rust"
        target = root / "project" / "cache"
        fingerprint = target / "debug" / ".fingerprint" / "app-0123456789abcdef"
        fingerprint.mkdir(parents=True)
        _ = (fingerprint / "bin-app.json").write_text("{}")
        _ = (target / sweep.CACHE_TAG).write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n"
            + "# This file is a cache directory tag created by another tool.\n"
        )
        _ = (target / "debug" / "app").write_bytes(os.urandom(4096))

        with mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1):
            measured = disk.measure([("rust", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None)

        by_path = {row["path"]: row for row in measured["outside_build_caches"]}
        self.assertIn(str(target), by_path)
        self.assertGreater(by_path[str(target)]["bytes"], 0)

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
            first = disk.measure(folders, lambda: filesystem.FilesystemUsage(0, 0), None)
            disk.write_snapshot(first)
            next_file = deep / "next"
            _ = next_file.write_bytes(os.urandom(8192))
            fresh = root / "new"
            fresh.mkdir()
            _ = (fresh / "new-file").write_bytes(os.urandom(4096))
            second = disk.measure(
                folders, lambda: filesystem.FilesystemUsage(0, 0), None,
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
            first = disk.measure(folders, lambda: filesystem.FilesystemUsage(0, 0), None)
            disk.write_snapshot(first)
            file.unlink()
            removed.rmdir()
            second = disk.measure(
                folders, lambda: filesystem.FilesystemUsage(0, 0), None,
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

        result = disk.measure([("tmp", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None)

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

        result = disk.measure([("rust", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None)

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
                [("tmp", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None,
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
            [("tmp", str(root))], lambda: filesystem.FilesystemUsage(0, 0), None,
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
        began = datetime(2026, 10, 4, 3, tzinfo=user_zone())
        clock = mock.Mock(return_value=began)
        with (
            mock.patch.object(disk, "FOLDERS", [("tmp", str(root))]),
            mock.patch.object(disk, "MIN_OUTSIDE_DIRECTORY_BYTES", 1),
            mock.patch.object(disk, "read_floor", return_value=None),
            mock.patch.object(filesystem, "filesystem_usage", return_value=filesystem.FilesystemUsage(0, 0)),
            mock.patch.object(disk, "datetime", wraps=datetime, now=clock),
            mock.patch.object(time, "time", return_value=began.timestamp()) as timestamp,
        ):
            first = disk.snapshot()
            gone: disk.FilesystemCapacity = {
                "path": str(root / "gone"), "device": "9:9", "device_name": "old device", "used": 7, "free": 8,
            }
            first.setdefault("filesystems", []).append(gone)
            first["output_unavailable"] = ["saved mapping warning"]
            disk.write_snapshot(first)
            _ = (folder / "second").write_bytes(os.urandom(4096))
            original = (store.root() / store.DISK_NAME).read_bytes()

            def capacity(path: str) -> filesystem.FilesystemUsage:
                if path == gone["path"]:
                    raise FileNotFoundError(path)
                return filesystem.FilesystemUsage(50, 60)

            for elapsed, request_age in ((timedelta(minutes=10), None), (timedelta(hours=2), None),
                                         (timedelta(hours=2), timedelta(minutes=-1))):
                request_path = store.root() / store.DISK_WALK_REQUEST_NAME
                if request_age is not None:
                    store.write_disk_walk_request({"requested_at": store.utc_iso((began + request_age).timestamp()),
                                                   "alert_at": store.utc_iso(began.timestamp()), "unnamed_bytes": 123})
                with (
                    self.subTest(age=elapsed, request_age=request_age),
                    mock.patch.object(disk, "mapped_output_paths", side_effect=AssertionError("unexpected walk")),
                    mock.patch.object(sweep, "target_dirs", side_effect=AssertionError("unexpected walk")),
                    mock.patch.object(disk, "read_cargo_output_roots", side_effect=AssertionError("unexpected walk")),
                    mock.patch.object(sweep, "directory_blocks", side_effect=AssertionError("unexpected walk")),
                    mock.patch.object(disk, "outside_directory_bytes", side_effect=AssertionError("unexpected walk")),
                    mock.patch.object(disk, "read_floor", return_value=42),
                    mock.patch.object(filesystem, "filesystem_usage", side_effect=capacity),
                ):
                    clock.return_value = began + elapsed
                    refreshed = disk.snapshot()
                for key in ("rows", "outside_build_caches", "outside_build_cache_totals", "measured_at",
                            "previous_measured_at", "output_unavailable"):
                    self.assertEqual(dict(refreshed)[key], dict(first)[key])
                self.assertEqual((refreshed["used"], refreshed["free"], refreshed["floor"]), (50, 60, 42))
                self.assertEqual(refreshed["capacity_measured_at"], store.utc_iso((began + elapsed).timestamp()))
                self.assertEqual(refreshed.get("filesystems", [])[-1], gone)
                for row in refreshed.get("filesystems", [])[:-1]:
                    self.assertEqual((row["used"], row["free"]), (50, 60))
                self.assertEqual((store.root() / store.DISK_NAME).read_bytes(), original)
                disk.write_snapshot(refreshed)
                original = (store.root() / store.DISK_NAME).read_bytes()
                if request_age is not None:
                    self.assertTrue(request_path.is_file())
                    request_path.unlink()
            store.write_disk_walk_request({"requested_at": store.utc_iso((began + timedelta(minutes=1)).timestamp()),
                                           "alert_at": store.utc_iso(began.timestamp()), "unnamed_bytes": 123})
            request_bytes = (store.root() / store.DISK_WALK_REQUEST_NAME).read_bytes()
            clock.return_value = began + timedelta(hours=2)
            timestamp.return_value = (began + timedelta(hours=2)).timestamp()
            requested = disk.snapshot()
            self.assertEqual(requested["previous_measured_at"], first["measured_at"])
            self.assertEqual(requested["measured_at"], store.utc_iso((began + timedelta(hours=2)).timestamp()))
            self.assertGreater(requested["outside_build_caches"][0]["growth_bytes"], 0)
            self.assertEqual((store.root() / store.DISK_WALK_REQUEST_NAME).read_bytes(), request_bytes)
            (store.root() / store.DISK_WALK_REQUEST_NAME).unlink()
            clock.return_value = began + timedelta(days=1)
            timestamp.return_value = (began + timedelta(days=1)).timestamp()
            second = disk.snapshot()

        self.assertEqual(second["previous_measured_at"], first["measured_at"])
        self.assertGreater(second["outside_build_caches"][0]["growth_bytes"], 0)


if __name__ == "__main__":
    _ = unittest.main()
