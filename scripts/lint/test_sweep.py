#!/usr/bin/env python3
"""Tests for sweep.py on a fabricated target directory: which unhashed files
go with a build unit, which are orphans, and which stay."""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest import mock

import sweep

GIB = 1 << 30
SIZE = 64 * 1024
DAY = 86_400
APP = "0123456789abcdef"
DEMO = "1111111111111111"
TOOL = "2222222222222222"
MY_TOOL = "3333333333333333"
STALE = "4444444444444444"


def write(path: Path, size: int = SIZE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Random bytes, so no filesystem stores the file sparse or deduplicated.
    _ = path.write_bytes(os.urandom(size))
    return path


def dep_info(copy: Path, output: str | None = None) -> Path:
    path = copy.with_suffix(".d")
    _ = path.write_text(f"{output or copy}: /src/main.rs\n")
    return path


def unit(tree: Path, directory: str, name: str, digest: str, used: float, size: int = SIZE) -> Path:
    """A build unit's fingerprint dir, last used at used, and its own file."""
    fingerprint = tree / ".fingerprint" / f"{name}-{digest}"
    fingerprint.mkdir(parents=True)
    record = fingerprint / f"bin-{name}.json"
    _ = record.write_text("{}")
    os.utime(record, (used, used))
    os.utime(fingerprint, (used, used))
    return write(tree / directory / f"{name}-{digest}", size)


class Target:
    """target/debug with live copies, orphaned copies and files nothing claims."""

    def __init__(self, base: Path) -> None:
        now = time.time()
        self.root: Path = base / "target"
        self.tree: Path = self.root / "debug"
        tree = self.tree
        examples = tree / "examples"
        # Live: hard-linked on Linux, as cargo does there.
        self.app: Path = tree / "app"
        os.link(unit(tree, "deps", "app", APP, now - 3 * DAY), self.app)
        self.app_dep_info: Path = dep_info(self.app)
        self.demo: Path = examples / "demo"
        os.link(unit(tree, "examples", "demo", DEMO, now), self.demo)
        self.demo_dep_info: Path = dep_info(self.demo)
        # Live: copied, as cargo does on macOS.
        self.tool: Path = tree / "tool"
        _ = shutil.copy2(unit(tree, "deps", "tool", TOOL, now), self.tool)
        self.tool_dep_info: Path = dep_info(self.tool)
        # Live: a hyphenated bin name, whose unit file carries the crate name.
        self.my_tool: Path = tree / "my-tool"
        os.link(unit(tree, "deps", "my_tool", MY_TOOL, now), self.my_tool)
        # Orphans: a copy with no unit of its name, a copy of a unit that was
        # rebuilt at another size, and a dep-info whose copy is gone.
        self.old: Path = write(examples / "old")
        stale_unit = unit(tree, "deps", "stale", STALE, now, SIZE // 2)
        self.stale: Path = write(tree / "stale")
        self.gone_dep_info: Path = dep_info(examples / "gone")
        self.orphans: set[Path] = {
            self.old,
            dep_info(self.old),
            self.stale,
            dep_info(self.stale),
            self.gone_dep_info,
        }
        self.stale_unit: Path = stale_unit
        # Nothing claims these: no dep-info, a dep-info naming another
        # directory, and a second link from outside any unit.
        linked = write(self.root / "tmp" / "linked")
        self.linked: Path = tree / "linked"
        os.link(linked, self.linked)
        self.unclaimed: set[Path] = {
            write(tree / "notes.txt"),
            write(examples / "stray"),
            write(examples / "elsewhere"),
            dep_info(examples / "elsewhere", "/other/place/elsewhere"),
            self.linked,
            dep_info(self.linked),
        }


def scanned(target: Target) -> tuple[sweep.Scan, dict[str, sweep.Group]]:
    scan = sweep.Scan()
    owners = sweep.group_roots(sweep.build_trees(str(target.root)), scan)
    sweep.walk(str(target.root), None, owners, scan)
    return scan, owners


def run_sweep(target: Target, budget: int, dry_run: bool) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output):
        status = sweep.sweep([str(target.root)], sweep.build_trees(str(target.root)), budget, "test", dry_run)
    return status, output.getvalue()


class SweepCase(unittest.TestCase):
    def target(self) -> Target:
        return Target(Path(self.enterContext(tempfile.TemporaryDirectory())).resolve())


class DirectoryBlocksTests(SweepCase):
    def test_shared_seen_counts_a_hard_link_in_the_first_directory_only(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        first = base / "first"
        second = base / "second"
        source = write(first / "output")
        second.mkdir()
        os.link(source, second / "output")
        blocks = source.stat().st_blocks * 512

        self.assertEqual(sweep.directory_blocks(str(first)), blocks)
        self.assertEqual(sweep.directory_blocks(str(second)), blocks)
        seen: set[sweep.InodeKey] = set()
        self.assertEqual(sweep.directory_blocks(str(first), seen), blocks)
        self.assertEqual(sweep.directory_blocks(str(second), seen), 0)


class GroupTests(SweepCase):
    def test_hard_linked_copy_and_dep_info_join_their_unit(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        group = owners[str(target.tree / "deps" / f"app-{APP}")]
        self.assertIs(owners[str(target.app)], group)
        self.assertIs(owners[str(target.app_dep_info)], group)
        self.assertIs(owners[str(target.demo)], owners[str(target.tree / "examples" / f"demo-{DEMO}")])
        self.assertIs(owners[str(target.demo_dep_info)], owners[str(target.demo)])

    def test_copy_that_is_not_a_link_joins_the_unit_of_its_name_and_size(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        self.assertIs(owners[str(target.tool)], owners[str(target.tree / "deps" / f"tool-{TOOL}")])
        self.assertIs(owners[str(target.tool_dep_info)], owners[str(target.tool)])

    def test_hyphenated_copy_joins_its_crate_file(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        self.assertIs(owners[str(target.my_tool)], owners[str(target.tree / "deps" / f"my_tool-{MY_TOOL}")])

    def test_copies_whose_unit_is_gone_are_orphans(self) -> None:
        target = self.target()
        scan, owners = scanned(target)
        self.assertEqual({Path(path) for path in scan.orphans.entries}, target.orphans)
        self.assertEqual(owners[str(target.stale_unit)].kind, "unit")

    def test_files_nothing_claims_stay(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        claimed = sorted(str(path) for path in target.unclaimed if str(path) in owners)
        self.assertEqual(claimed, [])


class LastUseTests(SweepCase):
    def test_listing_a_fingerprint_dir_is_not_a_use(self) -> None:
        target = self.target()
        fingerprint = target.tree / ".fingerprint" / f"app-{APP}"
        compiled = fingerprint.stat().st_mtime
        # A listing refreshes the directory's atime and nothing else.
        os.utime(fingerprint, (time.time(), compiled))
        _, owners = scanned(target)
        self.assertEqual(owners[str(fingerprint)].last_used, compiled)


class DepInfoTests(SweepCase):
    def test_names_the_copy_in_its_directory(self) -> None:
        target = self.target()
        directory = str(target.tree / "examples")
        self.assertEqual(sweep.dep_info_output(str(target.demo_dep_info), directory), "demo")
        self.assertEqual(sweep.dep_info_output(str(target.gone_dep_info), directory), "gone")

    def test_escaped_space(self) -> None:
        target = self.target()
        _ = dep_info(target.tree / "a b", str(target.tree / "a b").replace(" ", "\\ "))
        self.assertEqual(sweep.dep_info_output(str(target.tree / "a b.d"), str(target.tree)), "a b")

    def test_another_directory_stem_or_no_target_is_not_cargos(self) -> None:
        target = self.target()
        examples = str(target.tree / "examples")
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "elsewhere.d"), examples))
        _ = dep_info(target.tree / "examples" / "renamed", str(target.tree / "examples" / "other"))
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "renamed.d"), examples))
        _ = (target.tree / "examples" / "empty.d").write_text("no target here\n")
        self.assertIsNone(sweep.dep_info_output(str(target.tree / "examples" / "empty.d"), examples))


class SweepTests(SweepCase):
    def test_orphans_go_within_budget_and_nothing_else_does(self) -> None:
        target = self.target()
        status, output = run_sweep(target, 1 << 40, dry_run=False)
        self.assertEqual(status, 0)
        self.assertIn("within the 1024.0 GiB budget (test)", output)
        self.assertIn(f"removed {len(target.orphans)} orphaned files (", output)
        self.assertNotIn("build units", output)
        self.assertEqual([path for path in target.orphans if path.exists()], [])
        kept = [target.app, target.demo, target.tool, target.my_tool, target.stale_unit, *target.unclaimed]
        self.assertEqual([path for path in kept if not path.exists()], [])

    def test_dry_run_reports_orphans_apart_from_evictions(self) -> None:
        target = self.target()
        scan, _ = scanned(target)
        total = sum(scan.blocks.values())
        orphaned = sweep.freed_by(scan.orphans, scan, dict(scan.links))
        status, output = run_sweep(target, total - orphaned - 1, dry_run=True)
        self.assertEqual(status, 0)
        lines = output.splitlines()
        self.assertTrue(lines[1].startswith(f"lint sweep: would remove {len(target.orphans)} orphaned files ("))
        self.assertTrue(lines[2].startswith("lint sweep: would remove 1 build units and 0 incremental dirs ("))
        self.assertEqual([path for path in target.orphans if not path.exists()], [])
        self.assertTrue(target.app.exists())

    def test_evicting_a_unit_removes_its_copies_and_frees_their_blocks(self) -> None:
        target = self.target()
        scan, _ = scanned(target)
        total = sum(scan.blocks.values())
        orphaned = sweep.freed_by(scan.orphans, scan, dict(scan.links))
        # Removing app's unit, the least recently used, frees its file only
        # because its hard-linked copy in debug/ goes with it.
        status, output = run_sweep(target, total - orphaned - SIZE, dry_run=False)
        self.assertEqual(status, 0)
        self.assertIn("removed 1 build units and 0 incremental dirs", output)
        gone = [
            target.app,
            target.app_dep_info,
            target.tree / "deps" / f"app-{APP}",
            target.tree / ".fingerprint" / f"app-{APP}",
        ]
        self.assertEqual([path for path in gone if path.exists()], [])
        self.assertTrue(target.demo.exists())
        self.assertTrue(target.tool.exists())


class TargetDirTests(SweepCase):
    def test_sweeps_the_named_directory_without_a_workspace(self) -> None:
        target = self.target()
        output = io.StringIO()
        environ = {sweep.BUDGET_ENV: "1024", sweep.CONFIG_ENV: "/nonexistent/lint.conf"}
        with mock.patch.dict(os.environ, environ), redirect_stdout(output):
            status = sweep.main([sweep.TARGET_DIR_FLAG, str(target.root), "--dry-run"])
        self.assertEqual(status, 0)
        self.assertIn(f"lint sweep: {target.root} is ", output.getvalue())
        self.assertEqual([path for path in target.orphans if not path.exists()], [])

    def test_flag_without_a_directory_is_an_error(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(sweep.main([sweep.TARGET_DIR_FLAG]), 2)


def cargo_target(base: Path, name: str, digest: str, used: float) -> tuple[Path, Path]:
    """A target directory with one unit last used at used: (its build tree, the unit's file)."""
    root = base / name / "target"
    tree = root / "debug"
    output = unit(tree, "deps", name, digest, used)
    _ = (root / sweep.RUSTC_INFO).write_text("{}")
    return tree, output


class FloorTests(SweepCase):
    def base(self) -> Path:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            sweep.FLOOR_STATE_ENV: str(base / "state"),
            sweep.BUILDLOG_DIR_ENV: str(base / "buildlog"),
        }))
        _ = self.enterContext(mock.patch.object(sweep, "CI_TARGETS", (str(base / "ci-1"), str(base / "ci-2"))))
        _ = self.enterContext(mock.patch.object(
            sweep, "send_floor_alert", side_effect=AssertionError("unexpected floor alert send")))
        return base

    def test_base_blocks_unexpected_alert_send(self) -> None:
        _ = self.base()
        self.assertIsInstance(sweep.send_floor_alert, mock.Mock)
        with self.assertRaisesRegex(AssertionError, "unexpected floor alert send"):
            _ = sweep.send_floor_alert("test", sweep.FloorAlertChannels.NATEDEV)

    def hold(self, base: Path, floor: int, free: list[int]) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(sweep, "free_bytes", side_effect=free), redirect_stdout(output):
            status = sweep.hold_floor(floor, False, [str(base)], str(base / "floor.lock"))
        return status, output.getvalue()

    def prior(self, free: int, caches: int, age: int = 60, alert_at: float | None = None,
              push_at: float | None = None) -> None:
        sweep.write_floor_record(
            sweep.floor_record_path(),
            sweep.FloorRecord(time.time() - age, free, caches,
                              sweep.DeliveredAlert(alert_at) if alert_at is not None else sweep.NoDeliveredAlert(),
                              sweep.DeliveredAlert(push_at) if push_at is not None else sweep.NoDeliveredAlert()),
        )

    def test_cache_growth_explains_free_space_fall(self) -> None:
        base = self.base()
        _, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        caches = sweep.directory_blocks(str(base / "repo" / "target"))
        self.prior(10 * GIB, caches - SIZE)
        with (mock.patch.object(sweep, "UNEXPLAINED_FALL_BYTES", 1),
              mock.patch.object(sweep, "send_floor_alert") as send):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE // 2, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(output_file.exists())
        send.assert_not_called()

    def test_unexplained_fall_alerts_with_disk_directories_and_child(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(30 * GIB, sweep.directory_blocks(str(base / "repo" / "target")))
        snapshot = {
            "measured_at": "2026-10-04T19:53:00-07:00",
            "previous_measured_at": "2026-10-04T19:43:00-07:00",
            "outside_build_cache_totals": [{"label": "/tmp", "bytes": 27 * GIB, "growth_bytes": 15 * GIB}],
            "outside_build_caches": [
                {"path": "/tmp/traces", "bytes": 20 * GIB, "growth_bytes": 12 * GIB,
                 "largest_child_path": "/tmp/traces/frame", "largest_child_growth_bytes": 10 * GIB},
                {"path": "/tmp/other", "bytes": 7 * GIB, "growth_bytes": 3 * GIB,
                 "largest_child_path": None, "largest_child_growth_bytes": None},
            ],
        }
        path = base / "buildlog" / "disk.json"
        path.parent.mkdir()
        _ = path.write_text(json.dumps(snapshot))
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])
        self.assertEqual(status, 0)
        message = cast(str, send.call_args.args[0])
        self.assertIn("/tmp/traces", message)
        self.assertIn("/tmp/traces/frame", message)
        self.assertIn("2026-10-04 19:53 PDT", message)
        self.assertIn("came after buildlog-disk's last measurement at 2026-10-04 19:53 PDT", message)
        self.assertNotIn("outside measured folders", message)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV)
        self.assertIn("stale", message)
        self.assertFalse((base / "repo" / "target" / "debug" / "deps" / f"repo-{APP}").exists())

    def test_busy_and_ci_growth_count_as_build_caches(self) -> None:
        base = self.base()
        idle_tree, _ = cargo_target(base, "idle", APP, time.time() - DAY)
        busy_tree, _ = cargo_target(base, "busy", DEMO, time.time())
        ci = base / "ci-1"
        _ = write(ci / "existing")
        baseline = sum(sweep.directory_blocks(str(path)) for path in (idle_tree.parent, busy_tree.parent, ci))
        self.prior(20 * GIB, baseline)
        _ = write(busy_tree / "extra", 2 * SIZE)
        _ = write(ci / "extra", 2 * SIZE)
        lock = os.open(busy_tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (mock.patch.object(sweep, "UNEXPLAINED_FALL_BYTES", SIZE),
              mock.patch.object(sweep, "send_floor_alert") as send):
            status, _ = self.hold(base, 20 * GIB, [20 * GIB - 3 * SIZE, 20 * GIB])
        self.assertEqual(status, 0)
        send.assert_not_called()
        record = sweep.read_floor_record(sweep.floor_record_path())
        self.assertIsInstance(record, sweep.FloorRecord)
        assert isinstance(record, sweep.FloorRecord)
        self.assertEqual(record.build_cache_bytes, sum(sweep.directory_blocks(str(path)) for path in (idle_tree.parent, busy_tree.parent, ci)))

    def test_old_record_does_not_trigger_growth_alert(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(30 * GIB, 0, age=31 * 60)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        send.assert_not_called()

    def test_large_removal_alerts_without_prior_record(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(sweep, "send_floor_alert", return_value=True) as send):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        self.assertIn("No earlier floor sweep", cast(str, send.call_args.args[0]))
        self.assertIn("measurement unavailable (missing)", cast(str, send.call_args.args[0]))
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV)

    def test_exhausted_cache_under_floor_alerts_phone_and_natedev(self) -> None:
        base = self.base()
        _, output_file = cargo_target(base, "idle", APP, time.time() - DAY)
        busy_tree, _ = cargo_target(base, "busy", DEMO, time.time())
        _ = write(base / "ci-1" / "output")
        held_bytes = sweep.directory_blocks(str(busy_tree.parent))
        ci_bytes = sweep.directory_blocks(str(base / "ci-1"))
        lock = os.open(busy_tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertFalse(output_file.exists())
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)
        message = cast(str, send.call_args.args[0])
        self.assertIn("has no build cache left that it may remove", message)
        self.assertIn("free space is 9.0 GiB, under the 10.0 GiB floor", message)
        self.assertIn("free 1.0 GiB outside build caches", message)
        self.assertIn(f"Not swept: 1 target dirs a build holds ({sweep.gib(held_bytes)})", message)
        self.assertIn(f"CI's targets ({sweep.gib(ci_bytes)})", message)
        self.assertNotIn("Could not remove", message)
        record = sweep.read_floor_record(sweep.floor_record_path())
        assert isinstance(record, sweep.FloorRecord)
        self.assertIsInstance(record.push_history, sweep.DeliveredAlert)
        self.assertIsInstance(record.alert_history, sweep.NoDeliveredAlert)

    def test_exhausted_cache_alerts_even_when_nothing_was_removed(self) -> None:
        base = self.base()
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)
        self.assertIn("removed 0.0 GiB", cast(str, send.call_args.args[0]))
        self.assertIn("Not swept: CI's targets (0.0 GiB).", cast(str, send.call_args.args[0]))

    def test_under_floor_with_removable_units_left_sends_no_push(self) -> None:
        base = self.base()
        _, first = cargo_target(base, "first", APP, time.time() - 2 * DAY)
        _, second = cargo_target(base, "second", DEMO, time.time() - DAY)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB - 1])[0], 0)
        self.assertTrue(first.exists() or second.exists())
        send.assert_not_called()

    def test_natedev_alert_hour_does_not_hold_back_push(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, alert_at=time.time() - 10 * 60)
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)

    def test_recent_push_holds_next_push_for_its_own_hour(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, push_at=time.time() - 10 * 60)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        send.assert_not_called()

    def test_push_older_than_hour_is_due_again(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, push_at=time.time() - 61 * 60)
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)

    def test_failed_push_does_not_start_its_hour(self) -> None:
        base = self.base()
        with mock.patch.object(sweep, "send_floor_alert", side_effect=[False, True]) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
            first = sweep.read_floor_record(sweep.floor_record_path())
            assert isinstance(first, sweep.FloorRecord)
            self.assertIsInstance(first.push_history, sweep.NoDeliveredAlert)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertEqual(send.call_count, 2)
        latest = sweep.read_floor_record(sweep.floor_record_path())
        assert isinstance(latest, sweep.FloorRecord)
        self.assertIsInstance(latest.push_history, sweep.DeliveredAlert)

    def test_delivered_push_stamp_uses_time_after_send(self) -> None:
        base = self.base()
        start = time.time()
        clock = [start]

        def deliver(_message: str, _channels: sweep.FloorAlertChannels) -> bool:
            clock[0] = start + 80
            return True

        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "send_floor_alert", side_effect=deliver)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        record = sweep.read_floor_record(sweep.floor_record_path())
        assert isinstance(record, sweep.FloorRecord)
        self.assertEqual(record.measured_at, start)
        self.assertEqual(record.push_history, sweep.DeliveredAlert(start + 80))

    def test_recent_push_allows_natedev_removal_alert(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(10 * GIB, 0, push_at=time.time() - 10 * 60)
        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(sweep, "send_floor_alert", return_value=True) as send):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV)
        self.assertIn("removed more than 32 GiB", cast(str, send.call_args.args[0]))

    def test_failed_removal_stays_in_record_and_send_failure_keeps_status(self) -> None:
        base = self.base()
        tree, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        before = sweep.directory_blocks(str(tree.parent))
        with (mock.patch.object(sweep, "remove", return_value=1),
              mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(sweep, "send_floor_alert", return_value=False) as send):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])
        self.assertEqual(status, 1)
        self.assertTrue(output_file.exists())
        record = sweep.read_floor_record(sweep.floor_record_path())
        self.assertIsInstance(record, sweep.FloorRecord)
        assert isinstance(record, sweep.FloorRecord)
        self.assertEqual(record.build_cache_bytes, before)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)
        message = cast(str, send.call_args.args[0])
        self.assertIn("Not swept: CI's targets (0.0 GiB).\nCould not remove 1 path this sweep; the next sweep tries again.", message)

    def test_both_sends_failing_retry_and_one_delivery_stamps_hour(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(sweep, "send_floor_alert", side_effect=[False, True]) as send):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
            _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
            _, _ = cargo_target(base, "third", TOOL, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        self.assertEqual(send.call_count, 2)
        record = sweep.read_floor_record(sweep.floor_record_path())
        self.assertIsInstance(record, sweep.FloorRecord)
        assert isinstance(record, sweep.FloorRecord)
        self.assertIsInstance(record.alert_history, sweep.DeliveredAlert)

    def test_alert_delivery_failure_never_changes_exit_status(self) -> None:
        send_floor_alert = sweep.send_floor_alert
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(subprocess, "run", side_effect=OSError("offline")),
              mock.patch.object(sweep, "send_floor_alert", wraps=send_floor_alert),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        self.assertIn("message alert failed", errors.getvalue())
        self.assertNotIn("phone alert failed", errors.getvalue())

    def test_one_channel_delivering_counts_and_both_results_print(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        results = [
            subprocess.CompletedProcess([], 1, "", "relay unavailable"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with (mock.patch.object(subprocess, "run", side_effect=results) as run,
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["input"], "disk notice")
        phone_command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(phone_command[-4:], ["--priority", "0", "natedev: disk under its floor", "disk notice"])
        self.assertIn("message alert queued", output.getvalue())
        self.assertIn("phone alert delivered", output.getvalue())

    def test_natedev_only_channel_never_calls_phone(self) -> None:
        result = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(subprocess, "run", return_value=result) as run:
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV))
        self.assertEqual(run.call_count, 1)
        message_command = cast(list[str], run.call_args.args[0])
        self.assertIn("message/send.py", message_command[1])

    def test_queued_message_stamps_record_and_suppresses_next_alert(self) -> None:
        send_floor_alert = sweep.send_floor_alert
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        results = [subprocess.CompletedProcess([], 1, "", "")]
        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(subprocess, "run", side_effect=results) as run,
              mock.patch.object(sweep, "send_floor_alert", wraps=send_floor_alert)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
            record = sweep.read_floor_record(sweep.floor_record_path())
            self.assertIsInstance(record, sweep.FloorRecord)
            assert isinstance(record, sweep.FloorRecord)
            self.assertIsInstance(record.alert_history, sweep.DeliveredAlert)
            _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        self.assertEqual(run.call_count, 1)

    def test_delivered_stamp_uses_time_after_send(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        start = time.time()
        clock = [start]

        def deliver(_message: str, _channels: object) -> bool:
            clock[0] = start + 80
            return True

        with (mock.patch.object(sweep, "LARGE_REMOVAL_BYTES", 1),
              mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "send_floor_alert", side_effect=deliver)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        record = sweep.read_floor_record(sweep.floor_record_path())
        assert isinstance(record, sweep.FloorRecord)
        self.assertEqual(record.measured_at, start)
        self.assertEqual(record.alert_history, sweep.DeliveredAlert(start + 80))

    def test_missing_and_corrupt_floor_records_have_named_state(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())
        path.parent.mkdir()
        _ = path.write_text("broken json")
        self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())

    def test_old_floor_record_has_no_push_and_push_stamp_round_trips(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        path.parent.mkdir()
        _ = path.write_text(json.dumps({"measured_at": 100.0, "free_bytes": 5, "build_cache_bytes": 8,
                                        "last_alert_at": 90.0}))
        old = sweep.read_floor_record(path)
        self.assertEqual(old, sweep.FloorRecord(100.0, 5, 8, sweep.DeliveredAlert(90.0),
                                                sweep.NoDeliveredAlert()))
        record = sweep.FloorRecord(100.0, 5, 8, sweep.DeliveredAlert(90.0), sweep.DeliveredAlert(95.0))
        sweep.write_floor_record(path, record)
        self.assertEqual(json.loads(path.read_text())["last_push_at"], 95.0)
        self.assertEqual(sweep.read_floor_record(path), record)

    def test_invalid_push_stamp_makes_floor_record_unavailable(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        path.parent.mkdir()
        for value in ("yesterday", float("nan"), float("inf")):
            _ = path.write_text(json.dumps({"measured_at": 100.0, "free_bytes": 5, "build_cache_bytes": 8,
                                            "last_alert_at": None, "last_push_at": value}))
            self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())

    def test_boolean_floor_fields_make_record_unavailable(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        path.parent.mkdir()
        record = {"measured_at": 100.0, "free_bytes": 5, "build_cache_bytes": 8,
                  "last_alert_at": 90.0, "last_push_at": 95.0}
        for field in record:
            for value in (True, False):
                with self.subTest(field=field, value=value):
                    _ = path.write_text(json.dumps({**record, field: value}))
                    self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())

    def test_disk_measurement_names_missing_unreadable_and_old_shape(self) -> None:
        base = self.base()
        self.assertEqual(sweep.read_disk_measurement(), sweep.UnavailableDiskMeasurement("missing"))
        path = base / "buildlog" / "disk.json"
        path.parent.mkdir()
        _ = path.write_text("not json")
        self.assertEqual(sweep.read_disk_measurement(), sweep.UnavailableDiskMeasurement("unreadable"))
        _ = path.write_text(json.dumps({"measured_at": "2026-10-04T19:53:00-07:00", "rows": []}))
        self.assertEqual(sweep.read_disk_measurement(), sweep.UnavailableDiskMeasurement("old shape"))
        _ = path.write_text(json.dumps({"measured_at": "2026-10-04T19:53:00-07:00",
                                        "previous_measured_at": "2026-10-04T19:43:00-07:00",
                                        "outside_build_caches": []}))
        self.assertEqual(sweep.read_disk_measurement(), sweep.UnavailableDiskMeasurement("old shape"))

    def test_no_prior_disk_measurement_names_largest_directories(self) -> None:
        base = self.base()
        path = base / "buildlog" / "disk.json"
        path.parent.mkdir()
        _ = path.write_text(json.dumps({
            "measured_at": "2026-10-04T19:53:00-07:00",
            "previous_measured_at": None,
            "outside_build_cache_totals": [{"label": "/tmp", "bytes": 10 * GIB, "growth_bytes": 10 * GIB}],
            "outside_build_caches": [
                {"path": f"/tmp/{name}", "bytes": size * GIB, "growth_bytes": size * GIB,
                 "largest_child_path": None, "largest_child_growth_bytes": None}
                for name, size in (("one", 1), ("four", 4), ("two", 2), ("three", 3))
            ],
        }))
        lines = sweep.disk_measurement_lines(sweep.read_disk_measurement(), 0, 0, time.time())
        text = "\n".join(lines)
        self.assertIn("No earlier buildlog-disk measurement", text)
        self.assertIn("/tmp/four", text)
        self.assertIn("/tmp/three", text)
        self.assertIn("/tmp/two", text)
        self.assertNotIn("/tmp/one", text)

    def test_folder_totals_include_deleted_directories_and_small_files(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(30 * GIB, sweep.directory_blocks(str(base / "repo" / "target")))
        path = base / "buildlog" / "disk.json"
        path.parent.mkdir()
        _ = path.write_text(json.dumps({
            "measured_at": "2026-10-04T19:53:00-07:00",
            "previous_measured_at": "2026-10-04T19:43:00-07:00",
            "outside_build_cache_totals": [{"label": "/tmp", "bytes": 2 * GIB, "growth_bytes": -2 * GIB}],
            "outside_build_caches": [
                {"path": "/tmp/new", "bytes": 2 * GIB, "growth_bytes": 2 * GIB,
                 "largest_child_path": None, "largest_child_growth_bytes": None},
            ],
        }))
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])[0], 0)
        message = cast(str, send.call_args.args[0])
        self.assertIn("Measured folders shrank 2.0 GiB", message)
        self.assertIn("/tmp/new: grew 2.0 GiB", message)
        self.assertIn("came after buildlog-disk's last measurement", message)

    def test_snapshot_growth_above_fall_covers_all_without_negative_remainder(self) -> None:
        now = time.time()
        measurement = sweep.AvailableDiskMeasurement(
            datetime.fromtimestamp(now - 60, sweep.PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 660, sweep.PACIFIC)),
            [], 30 * GIB,
        )
        previous = sweep.FloorRecord(now - 120, 30 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert())
        message = sweep.floor_alert_text(now, 35 * GIB, previous, 20 * GIB, 0,
                                         ["removed more than 32 GiB in one sweep"], measurement)
        self.assertIn("cover all of it", message)
        self.assertNotIn("-10.0 GiB", message)
        self.assertEqual(message.count(sweep.pacific_time(measurement.measured_at)), 1)
        assert isinstance(measurement.comparison, sweep.EarlierDiskMeasurement)
        self.assertEqual(message.count(sweep.pacific_time(measurement.comparison.measured_at)), 1)

    def test_measurement_within_fall_places_remainder_after_its_time(self) -> None:
        now = time.time()
        measured = datetime.fromtimestamp(now - 30, sweep.PACIFIC)
        measurement = sweep.AvailableDiskMeasurement(
            measured,
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, sweep.PACIFIC)),
            [], 2 * GIB,
        )
        previous = sweep.FloorRecord(now - 120, 30 * GIB, 0, sweep.NoDeliveredAlert(),
                                     sweep.NoDeliveredAlert())
        message = sweep.floor_alert_text(now, 0, previous, 10 * GIB, 0, [], measurement)
        self.assertIn(f"8.0 GiB came after {sweep.pacific_time(measured)} or outside measured folders", message)
        self.assertNotIn("remains outside measured folders", message)

    def test_no_prior_floor_sweep_omits_explained_fall(self) -> None:
        now = time.time()
        measurement = sweep.AvailableDiskMeasurement(
            datetime.fromtimestamp(now, sweep.PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, sweep.PACIFIC)),
            [], 30 * GIB,
        )
        message = sweep.floor_alert_text(now, 35 * GIB, sweep.NoFloorRecord(), 0, 0,
                                         ["removed more than 32 GiB in one sweep"], measurement)
        self.assertIn("No earlier floor sweep", message)
        self.assertNotIn("fall beyond cache growth", message)

    def test_free_space_rise_uses_rising_wording(self) -> None:
        now = time.time()
        previous = sweep.FloorRecord(now - 60, 20 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert())
        message = sweep.floor_alert_text(now, 35 * GIB, previous, -2 * GIB, -1 * GIB,
                                         ["removed more than 32 GiB in one sweep"],
                                         sweep.UnavailableDiskMeasurement("missing"))
        self.assertIn("free space rose 2.0 GiB", message)
        self.assertIn("build caches shrank 1.0 GiB", message)
        self.assertNotIn("-2.0 GiB", message)

    def test_long_paths_fit_phone_and_keep_each_last_component(self) -> None:
        now = time.time()
        directories = [
            sweep.OutsideCacheDirectory(
                f"/tmp/{'a' * 290}/directory{index}", 20 * GIB, 10 * GIB,
                sweep.LargestGrowingChild(f"/tmp/{'b' * 290}/child{index}", 8 * GIB),
            ) for index in range(3)
        ]
        measurement = sweep.AvailableDiskMeasurement(
            datetime.fromtimestamp(now, sweep.PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, sweep.PACIFIC)),
            directories, 30 * GIB,
        )
        previous = sweep.FloorRecord(now - 60, 30 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert())
        message = sweep.floor_alert_text(now, 35 * GIB, previous, 20 * GIB, 0,
                                         ["removed more than 32 GiB in one sweep"], measurement)
        self.assertLessEqual(len(message), 1024)
        for index in range(3):
            self.assertIn(f"directory{index}", message)
            self.assertIn(f"child{index}", message)

    def test_host_key_then_default_key(self) -> None:
        values = {"sweep_free_floor_gib.natedev": "500", "sweep_free_floor_gib": "50", "sweep_free_floor_gib.mac": "x"}
        self.assertEqual(sweep.floor_bytes(values, "natedev"), (500 * GIB, "sweep_free_floor_gib.natedev"))
        self.assertEqual(sweep.floor_bytes(values, "other"), (50 * GIB, "sweep_free_floor_gib"))
        self.assertEqual(sweep.floor_bytes(values, "mac"), (None, "sweep_free_floor_gib.mac"))
        self.assertEqual(sweep.floor_bytes({}, "natedev"), (None, None))

    def test_finds_cargo_targets_and_nothing_inside_them(self) -> None:
        base = self.base()
        _ = cargo_target(base, "repo", APP, time.time())
        _ = cargo_target(base / "repo" / "target", "nested", DEMO, time.time())
        _ = cargo_target(base / ".git", "hidden", TOOL, time.time())
        _ = write(base / "uv" / "CACHEDIR.TAG")
        self.assertEqual(sweep.target_dirs([str(base)]), [str(base / "repo" / "target")])

    def test_below_the_floor_the_least_recently_used_unit_of_any_target_goes(self) -> None:
        base = self.base()
        now = time.time()
        _, idle = cargo_target(base, "idle", APP, now - 5 * DAY)
        _, busy = cargo_target(base, "busy", DEMO, now)
        status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(idle.exists())
        self.assertTrue(busy.exists())
        self.assertIn("removed 1 build units", output)

    def test_a_target_a_build_holds_is_left_alone(self) -> None:
        base = self.base()
        tree, held = cargo_target(base, "held", APP, time.time() - 5 * DAY)
        lock = os.open(tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _, other = cargo_target(base, "other", DEMO, time.time())
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
        self.assertTrue(held.exists())
        self.assertFalse(other.exists())
        self.assertIn("1 left alone while a build holds them", output)

    def test_at_the_floor_or_while_another_sweep_holds_it_nothing_goes(self) -> None:
        base = self.base()
        _, output_file = cargo_target(base, "repo", APP, time.time() - 5 * DAY)
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB]), (0, ""))
        guard = sweep.try_lock(str(base / "floor.lock"))
        assert guard is not None
        self.addCleanup(os.close, guard)
        status, output = self.hold(base, 10 * GIB, [GIB])
        self.assertEqual(status, 0)
        self.assertIn("another sweep is holding it", output)
        self.assertTrue(output_file.exists())

    def test_floor_only_skips_the_workspace_and_holds_the_configured_floor(self) -> None:
        config = self.base() / "lint.conf"
        _ = config.write_text("sweep_free_floor_gib.testhost=7\n")
        with (
            mock.patch.dict(os.environ, {sweep.CONFIG_ENV: str(config)}),
            mock.patch.object(socket, "gethostname", return_value="testhost"),
            mock.patch.object(sweep, "sweep_workspace") as workspace,
            mock.patch.object(sweep, "hold_floor", return_value=0) as floor,
        ):
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 0)
        workspace.assert_not_called()
        floor.assert_called_once_with(7 * GIB, False)

    def test_malformed_floor_is_an_error(self) -> None:
        config = self.base() / "lint.conf"
        _ = config.write_text("sweep_free_floor_gib=lots\n")
        with mock.patch.dict(os.environ, {sweep.CONFIG_ENV: str(config)}), redirect_stderr(io.StringIO()):
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 2)


CONFIG = """\
# sweep_budget_gib=1 in a comment counts for nothing
[operations]
sweep=on

[sweep]
sweep_budget_gib = 24   # every repo without its own key
sweep_budget_gib.hana=96
sweep_budget_gib.hana=1
sweep_budget_gib.broken=lots
sweep_budget_gib.unset=
"""
# No signing, hooks or identity from the user's own git config.
GIT_ENVIRONMENT = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path) -> None:
    _ = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENVIRONMENT, capture_output=True, check=True)


class BudgetTests(unittest.TestCase):
    def config(self) -> str:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "lint.conf"
        _ = path.write_text(CONFIG)
        return str(path)

    def test_config_reads_as_lint_config_sh_does(self) -> None:
        values = sweep.config_values(self.config())
        self.assertEqual(values["sweep"], "on")
        self.assertEqual(values["sweep_budget_gib"], "24")
        self.assertEqual(values["sweep_budget_gib.hana"], "96")
        self.assertEqual(values["sweep_budget_gib.unset"], "")
        self.assertEqual(sweep.config_values("/nonexistent/lint.conf"), {})

    def test_repo_key_then_default_key_then_built_in(self) -> None:
        config = self.config()
        self.assertEqual(sweep.budget_bytes({}, config, "hana"), (96 * GIB, f"sweep_budget_gib.hana in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "nateroids"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "unset"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, None), (24 * GIB, f"sweep_budget_gib in {config}"))
        missing = "/nonexistent/lint.conf"
        self.assertEqual(
            sweep.budget_bytes({}, missing, "hana"), (24 * GIB, f"the default, no sweep_budget_gib in {missing}")
        )

    def test_environment_overrides_the_config(self) -> None:
        config = self.config()
        environ = {sweep.BUDGET_ENV: "8.5"}
        self.assertEqual(
            sweep.budget_bytes(environ, config, "hana"), (int(8.5 * GIB), "LINT_SWEEP_BUDGET_GIB in the environment")
        )
        self.assertEqual(sweep.budget_bytes({sweep.BUDGET_ENV: ""}, config, "hana")[0], 96 * GIB)

    def test_malformed_value_names_its_source(self) -> None:
        config = self.config()
        self.assertEqual(sweep.budget_bytes({}, config, "broken"), (None, f"sweep_budget_gib.broken in {config}"))
        for raw in ("-1", "inf", "nan", "96GiB"):
            self.assertEqual(sweep.budget_bytes({sweep.BUDGET_ENV: raw}, config, "hana")[0], None, raw)


class RepoNameTests(unittest.TestCase):
    def test_every_worktree_and_subdirectory_names_the_repo(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        repo = base / "hana"
        (repo / "crates").mkdir(parents=True)
        _ = (repo / "README").write_text("readme\n")
        git("init", "-q", "-b", "main", cwd=repo)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "first", cwd=repo)
        git("worktree", "add", "-q", "-b", "trunk", str(base / "tool-based-ui-trunk"), cwd=repo)
        self.assertEqual(sweep.repo_name(str(repo)), "hana")
        self.assertEqual(sweep.repo_name(str(repo / "crates")), "hana")
        self.assertEqual(sweep.repo_name(str(base / "tool-based-ui-trunk")), "hana")
        self.assertIsNone(sweep.repo_name(str(base)))


if __name__ == "__main__":
    _ = unittest.main()
