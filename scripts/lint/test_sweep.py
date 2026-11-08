#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Tests for sweep.py on a fabricated target directory: which unhashed files
go with a build unit, which are orphans, and which stay."""

from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Collection, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Literal, cast, override
from unittest import mock
from zoneinfo import ZoneInfo


from scripts.lint import sweep, tmp_sweep
from scripts.lib import cargo_target_config
from scripts.lib import file_lock
from scripts.message import session_send
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.lib import conf
from scripts.lib.cargo_output import CargoOutputRoots
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.mac_test import floor_claims
from scripts.lint.test_memory_admit import HISTORY_APPEND_STOPS, stop_history_append
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.buildlog.step_outputs_test_support import create_step_index, record_root_attempt
from scripts.buildlog import index as build_index
from scripts.lib import test_hang_guard
if TYPE_CHECKING:
    from scripts.copies import copies
    from scripts.production.cli_fixture import ImmutableGitSeed
    from scripts.buildlog import record, store
    from scripts.lib import scratch
    from scripts.message import process_table, sessions
    from scripts.notify import review
    from scripts.notify.push_texts import catalog, render, render_entry
    from scripts.notify.user_action import ActionRequired
else:
    from scripts.copies import copies
    from scripts.production.cli_fixture import ImmutableGitSeed
    from scripts.buildlog import record
    from scripts.lib import scratch
    from scripts.buildlog import store
    from scripts.message import process_table
    from scripts.message import sessions
    from scripts.notify import review
    from scripts.notify.push_texts import catalog, render, render_entry
    from scripts.notify.user_action import ActionRequired

GIB = 1 << 30
SIZE = 64 * 1024
DAY = 86_400
APP = "0123456789abcdef"
DEMO = "1111111111111111"
TOOL = "2222222222222222"
MY_TOOL = "3333333333333333"
STALE = "4444444444444444"
fixture_target_dirs = sweep.target_dirs
fixture_send_floor_alert = sweep.send_floor_alert
fixture_send_phone_push = sweep.send_phone_push
fixture_send_holder_message = sweep.send_holder_message
DELIVERED_NATEDEV_ALERT = sweep.FloorChannelOutcome.DELIVERED
FAILED_NATEDEV_ALERT = sweep.FloorChannelOutcome.FAILED


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


PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class AlertSendAttempt:
    recipient: str
    command: list[str]
    key: str
    text: str
    accepted: bool
    saved_record: sweep.FloorRecord | sweep.NoFloorRecord


class CrossingDelivery(list[AlertSendAttempt]):
    base: Path

    def __init__(self, base: Path) -> None:
        super().__init__()
        self.base = base

    @property
    def attempts(self) -> list[AlertSendAttempt]:
        return self


def phone_text(attempts: Sequence[AlertSendAttempt]) -> str:
    return next(attempt.text for attempt in attempts if attempt.recipient == "user")


def phone_calls(send: mock.Mock) -> list[str]:
    """Select the natedev messages for a free-space crossing."""
    return [cast(str, call.args[0]) for call in send.call_args_list
            if cast(str, call.args[0]).startswith("Free disk space fell")]


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    forbid_real_sender()
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(fake_cargo_output_roots())
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        **config_environment(root), "USER_ZONE": "America/Los_Angeles",
    }))


@contextmanager
def fake_sender_process(sender: Path) -> Generator[mock.Mock]:
    """Run both alert channels through the fixture sender."""
    real_run = subprocess.run

    def run(command: list[str], *, input: str | None, text: bool,
            capture_output: bool, timeout: float) -> subprocess.CompletedProcess[str]:
        if Path(command[1]).name != "send.py":
            raise AssertionError(f"unexpected sender command: {command}")
        return real_run([command[0], str(sender), *command[2:]], input=input, text=text,
                        capture_output=capture_output, timeout=timeout)

    with mock.patch.object(subprocess, "run", side_effect=run) as process:
        yield process


def fresh_import_modules(base: Path, directory: str, module: str) -> list[str]:
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, **config_environment(base / "config"),
                   "STAFF_FILE": "/nonexistent/staff.json",
                   "BUILD_HOLD_DIR": str(base / "holders"),
                   "BUILD_HOLD_RELEASE_DIR": str(base / "release"),
                   "BUILDLOG_DIR": str(base / "buildlog"),
                   tmp_sweep.STATE_ENV: str(base / "state"),
                   "NOTIFIER_SESSIONS_DIR": str(base / "sessions"),
                   "PYTHONPATH": os.pathsep.join((str(root / "scripts" / directory), str(root)))}
    for name in ("PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY", "STAFF_OPERATION_FD"):
        _ = environment.pop(name, None)
    command = f"import json, sys, {module}; print(json.dumps(sorted(sys.modules)))"
    result = subprocess.run([sys.executable, "-c", command], cwd=root, env=environment,
                            capture_output=True, text=True, check=False,
                            timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
    assert result.returncode == 0, result.stderr
    return cast(list[str], json.loads(result.stdout))


class ImportTests(unittest.TestCase):
    def test_imports_leave_coordination_modules_unloaded(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for directory, module in (("mac_test", "offload"), ("lint", "sweep")):
            with self.subTest(module=module):
                loaded = fresh_import_modules(base, directory, module)
                coordination = [
                    name for name in loaded
                    if name in {"build_hold", "scripts.build_hold.build_hold",
                                "memory_admit", "scripts.lint.memory_admit"}
                    or name.startswith("scripts.build_queue")
                ]
                self.assertEqual(coordination, [])


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
        status = sweep.sweep([str(target.root)], sweep.build_trees(str(target.root)), budget, "test", dry_run, recent_minutes=60)
    return status, output.getvalue()


def reported_floor_targets(text: str) -> set[str]:
    return {line.split(" from ", 1)[1].split(", last used ", 1)[0]
            for line in text.splitlines() if "the floor would take" in line}


class SweepCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **config_environment(state / "config"),
            tmp_sweep.STATE_ENV: str(state / "sweep"),
            "BUILDLOG_DIR": str(state / "buildlog"),
        })))

    def target(self) -> Target:
        return Target(Path(self.enterContext(tempfile.TemporaryDirectory())).resolve())

    def build_log_rows(self) -> list[dict[str, object]]:
        root = Path(os.environ["BUILDLOG_DIR"])
        rows: list[dict[str, object]] = []
        for path in root.glob("*/????-??.jsonl"):
            for line in path.read_text().splitlines():
                row = cast(dict[str, object], json.loads(line))
                rows.append(row)
        return rows

    def trim_rows(self) -> list[dict[str, object]]:
        return [row for row in self.build_log_rows() if row.get("kind") == "sweep_trim"]

    def trim_events(self) -> list[sweep.SweepRemoval]:
        return [sweep.SweepRemoval(datetime.fromisoformat(cast(str, row["at"])).timestamp(),
                                   cast(str, row["folder"]), cast(sweep.RebuildCause, row["cause"]))
                for row in self.trim_rows()]


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
    def test_group_scan_reads_the_dep_info_limit_once_for_multiple_trees(self) -> None:
        targets = [self.target(), self.target()]
        trees = [str(target.tree) for target in targets]
        scan = sweep.Scan()
        with mock.patch("scripts.lint.sweep.conf.ConfigRead.whole", side_effect=conf.read("lint").whole) as whole:
            owners = sweep.group_roots(trees, scan)
        self.assertTrue(all(str(target.app_dep_info) in owners for target in targets))
        self.assertGreater(len(scan.groups), 2)
        self.assertEqual(whole.call_args_list, [mock.call("sweep_dep_info_head_bytes")])

    def test_linker_temp_and_copied_up_output_belong_to_their_unit(self) -> None:
        target = self.target()
        temporary = write(target.tree / "deps" / f".app-{APP}.12345")
        scan, owners = scanned(target)
        group = owners[str(target.app)]
        self.assertIs(owners.get(str(temporary)), group)
        self.assertIs(owners[str(target.app_dep_info)], group)
        self.assertEqual(sweep.remove([group]).failures, 0)
        self.assertFalse(temporary.exists())
        self.assertFalse(target.app.exists())
        self.assertTrue(target.tool.exists())
        self.assertGreater(sum(scan.blocks.values()), 0)

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
        self.assertEqual({Path(path) for group in scan.orphans for path in group.entries}, target.orphans)
        self.assertEqual(owners[str(target.stale_unit)].kind, "unit")

    def test_files_nothing_claims_stay(self) -> None:
        target = self.target()
        _, owners = scanned(target)
        claimed = sorted(str(path) for path in target.unclaimed if str(path) in owners)
        self.assertEqual(claimed, [])


class LastUseTests(SweepCase):
    def test_stamp_newer_handles_missing_older_and_newer_stamps(self) -> None:
        root = self.target().root
        moment = time.time()
        self.assertFalse(sweep.stamp_newer(str(root), moment))
        stamp = write(root / sweep.USE_STAMP)
        os.utime(stamp, (moment - 1, moment - 1))
        self.assertFalse(sweep.stamp_newer(str(root), moment))
        os.utime(stamp, (moment + 1, moment + 1))
        self.assertTrue(sweep.stamp_newer(str(root), moment))

    def test_listing_a_fingerprint_dir_is_not_a_use(self) -> None:
        target = self.target()
        fingerprint = target.tree / ".fingerprint" / f"app-{APP}"
        compiled = fingerprint.stat().st_mtime
        # A listing refreshes the directory's atime and nothing else.
        os.utime(fingerprint, (time.time(), compiled))
        _, owners = scanned(target)
        self.assertEqual(owners[str(fingerprint)].last_used, compiled)

    def test_fingerprint_directory_mtime_keeps_both_ages_recent(self) -> None:
        target = self.target()
        fingerprint = target.tree / ".fingerprint" / f"app-{APP}"
        modified = time.time() - DAY
        os.utime(fingerprint, (time.time(), modified))
        _, owners = scanned(target)
        group = owners[str(fingerprint)]
        self.assertEqual(group.last_used, modified)
        self.assertEqual(group.compiled, modified)

    def test_incremental_age_keeps_the_directory_mtime(self) -> None:
        target = self.target()
        incremental = target.tree / "incremental" / "app-session"
        child = write(incremental / "output")
        earlier = time.time() - 2 * DAY
        os.utime(child, (earlier, earlier))
        compiled = time.time() - DAY
        os.utime(incremental, (compiled, compiled))
        scan, owners = scanned(target)
        self.assertIn(owners[str(incremental)], scan.groups)
        self.assertEqual(owners[str(incremental)].last_used, compiled)

    def test_grouping_and_walk_share_directory_observations(self) -> None:
        target = self.target()
        trees = sweep.build_trees(str(target.root))
        listed: list[str] = []
        scandir = os.scandir
        def observe(path: str) -> object:
            listed.append(path)
            return scandir(path)
        with mock.patch.object(os, "scandir", side_effect=observe):
            scan = sweep.scan_roots(sweep.DiscoveredTargetRoots([str(target.root)], [str(target.root)]), trees)
        fingerprint = str(target.tree / ".fingerprint" / f"app-{APP}")
        self.assertEqual(listed.count(fingerprint), 1)
        self.assertIn(str(target.app), {path for group in scan.groups for path in group.observed})


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
    def test_budget_requires_a_recent_window_before_it_can_remove_output(self) -> None:
        target = self.target()
        roots, trees = [str(target.root)], [str(target.tree)]
        without_window = cast(Callable[[list[str], list[str], int, str, bool], int], sweep.sweep)
        workspace_without_window = cast(Callable[[str | None, str, bool], int], sweep.sweep_workspace)
        before = {path: path.read_bytes() for path in target.root.rglob("*") if path.is_file()}
        with mock.patch("scripts.lint.sweep.conf.ConfigRead.number", side_effect=AssertionError("No implicit configuration read expected")):
            with self.assertRaisesRegex(TypeError, "recent_minutes"):
                _ = without_window(roots, trees, 0, "test", False)
            with self.assertRaisesRegex(TypeError, "recent_minutes"):
                _ = workspace_without_window(str(target.root), "/nonexistent/lint.conf", False)
        self.assertEqual({path: path.read_bytes() for path in target.root.rglob("*") if path.is_file()}, before)

    def test_budget_uses_compile_time_within_a_day_of_last_use(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        now = time.time()
        tree, old_compile = cargo_target(base, "repo", APP, now - 12 * 3600)
        record = tree / ".fingerprint" / f"repo-{APP}" / "bin-repo.json"
        os.utime(record, (now - 2 * 3600, now - 12 * 3600))
        older_use = unit(tree, "deps", "older-use", DEMO, now - 4 * 3600)
        roots, trees = [str(tree.parent)], [str(tree)]
        total = sum(sweep.scan_roots(roots, trees).blocks.values())

        with redirect_stdout(io.StringIO()):
            status = sweep.sweep(roots, trees, total - 1, "test", False, recent_minutes=60)

        self.assertEqual(status, 0)
        self.assertFalse(old_compile.exists())
        self.assertTrue(older_use.exists())

    def test_budget_dry_run_writes_no_removal_record(self) -> None:
        target = self.target()
        self.assertEqual(run_sweep(target, 0, dry_run=True)[0], 0)
        self.assertEqual(self.trim_rows(), [])
        self.assertFalse(sweep.sweep_removals_path().exists())
        self.assertTrue(target.app.exists())
        self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        self.assertEqual(len(self.trim_rows()), 1)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_orphan_cleanup_writes_no_rebuild_removal_record(self) -> None:
        target = self.target()
        self.assertEqual(run_sweep(target, 1 << 40, dry_run=False)[0], 0)
        self.assertEqual(self.trim_rows(), [])
        self.assertFalse(sweep.sweep_removals_path().exists())
        self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        self.assertEqual(len(self.trim_rows()), 1)

    def test_removal_record_write_failure_does_not_fail_budget_sweep(self) -> None:
        target = self.target()
        errors = io.StringIO()
        with (mock.patch.object(record, "sweep_trim", side_effect=OSError("injected build-log append failure")),
              redirect_stderr(errors)):
            self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        self.assertFalse(target.app.exists())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("could not record removal", errors.getvalue())

    def test_budget_take_records_one_trim_with_confirmed_bytes_and_cause(self) -> None:
        target = self.target()
        confirmed: list[sweep.ConfirmedDeletion] = []
        remove = sweep.remove

        def measured(groups: list[sweep.Group], *, lint_settings: conf.ConfigRead) -> sweep.ConfirmedDeletion:
            account = remove(groups, lint_settings=lint_settings)
            if any(group.kind != "orphan" for group in groups):
                confirmed.append(account)
            return account

        with mock.patch.object(sweep, "remove", side_effect=measured):
            self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        row, = self.trim_rows()
        self.assertEqual((row["folder"], row["extent"], row["cause"]),
                         (str(target.root), "whole", "budget take"))
        self.assertEqual(row["bytes_freed"], sum(account.bytes for account in confirmed))
        self.assertGreater(cast(int, row["bytes_freed"]), 0)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_budget_keeps_recent_output_when_older_output_meets_the_budget(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        now = time.time()
        tree, recent = cargo_target(base, "repo", APP, now - 12 * 3600)
        record = tree / ".fingerprint" / f"repo-{APP}" / "bin-repo.json"
        os.utime(record, (now - 10 * 60, now - 12 * 3600))
        older = unit(tree, "deps", "older", DEMO, now - 2 * 3600)
        roots, trees = [str(tree.parent)], [str(tree)]
        total = sum(sweep.scan_roots(roots, trees).blocks.values())

        with redirect_stdout(io.StringIO()):
            status = sweep.sweep(roots, trees, total - 1, "test", False, recent_minutes=60)

        self.assertEqual(status, 0)
        self.assertTrue(recent.exists())
        self.assertFalse(older.exists())

    def test_budget_takes_recent_output_when_older_output_cannot_meet_the_budget(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        now = time.time()
        tree, older = cargo_target(base, "repo", APP, now - 2 * 3600)
        recent = unit(tree, "deps", "recent", DEMO, now - 10 * 60)

        with redirect_stdout(io.StringIO()):
            status = sweep.sweep([str(tree.parent)], [str(tree)], 0, "test", False, recent_minutes=60)

        self.assertEqual(status, 0)
        self.assertFalse(older.exists())
        self.assertFalse(recent.exists())

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
        remaining = dict(scan.links)
        orphaned = sum(sweep.freed_by(group, scan, remaining).bytes for group in scan.orphans)
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
        remaining = dict(scan.links)
        orphaned = sum(sweep.freed_by(group, scan, remaining).bytes for group in scan.orphans)
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


class RebuildTests(SweepCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(store, "host_name", side_effect=lambda: record.local_step_identity().hosts[0]))

    def test_rebuild_report_fails_when_an_enumerated_month_disappears(self) -> None:
        path = Path(os.environ["BUILDLOG_DIR"]) / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text("{}\n")
        locked_rows = store.locked_rows

        def remove_before_read(month: Path, start: int = 0) -> Generator[dict[str, object]]:
            if month == path:
                month.unlink()
            yield from locked_rows(month, start)

        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(store, "locked_rows", side_effect=remove_before_read),
              mock.patch.object(sweep, "read_sweep_journal", return_value=[]),
              redirect_stdout(output), redirect_stderr(errors)):
            status = sweep.main(["rebuilds", "--days", "7"])
        self.assertEqual(status, 2)
        self.assertIn("cannot read rebuild evidence", errors.getvalue())
        self.assertEqual(output.getvalue(), "")

    def test_trim_lookup_returns_false_for_an_absent_month_without_an_existence_check(self) -> None:
        path = Path(os.environ["BUILDLOG_DIR"]) / "natedev" / "2026-10.jsonl"
        take = sweep.TrimTake("/fixture/target", "floor take", 0)
        with (mock.patch.object(store, "host_file", return_value=path),
              mock.patch.object(Path, "exists", side_effect=AssertionError("trim lookup checked existence"))):
            self.assertFalse(sweep.trim_row_recorded(take))

    def test_legacy_removals_skip_invalid_rows_and_partial_tail(self) -> None:
        path = sweep.sweep_removals_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": 10, "path": "/fixture/target", "cause": "floor take"}
        complete = json.dumps(row).encode() + b"\n"
        invalid = [b"\xff\n", b"not json\n", b"[]\n", b"{}\n",
                   b'{"at": 10, "path": "relative", "cause": "floor take"}\n']
        _ = path.write_bytes(complete + b"".join(invalid) + json.dumps(row).encode())
        self.assertEqual(sweep.read_sweep_removals(), [sweep.SweepRemoval(10, "/fixture/target", "floor take")])

    def test_legacy_removal_file_still_explains_rebuild_without_trim_rows(self) -> None:
        folder = "/tmp/gate-check.legacy"
        self.step(folder, 2 * DAY)
        path = sweep.sweep_removals_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"at": self.now - DAY, "path": folder + "/target",
                                        "cause": "floor take"}) + "\n")
        path.with_suffix(".lock").touch()
        self.step(folder, DAY // 2, 50, 3600)
        self.assert_hours(self.report(), "gate and merge copies",
                          {"floor take": 1, "budget take": 0, "tmp sweep": 0, "none": 0})
        self.assertEqual(self.trim_rows(), [])

    def test_unreadable_removal_records_leave_journal_reporting_available(self) -> None:
        folder = "/tmp/gate-check.hana"
        self.step(folder, 2 * DAY)
        self.removal(DAY, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used yesterday")
        self.step(folder, DAY // 2, 50, 3600)
        for failure in (PermissionError("denied"), OSError("read failed")):
            with self.subTest(failure=failure):
                errors = io.StringIO()
                unreadable = mock.Mock(spec=Path)
                lock = sweep.sweep_removals_path().with_suffix(".lock")
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.touch()
                unreadable.configure_mock(**{"open.side_effect": failure, "with_suffix.return_value": lock})
                with (mock.patch.object(sweep, "sweep_removals_path", return_value=unreadable),
                      redirect_stderr(errors)):
                    output = self.report()
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("could not read removals", errors.getvalue())
                self.assert_hours(output, "gate and merge copies",
                                  {"floor take": 1, "budget take": 0, "tmp sweep": 0, "none": 0})

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.now: float = datetime(2026, 10, 29, 12, tzinfo=UTC).timestamp()
        self.records: list[dict[str, object]] = []
        self.journal: list[str] = []

    def step(self, folder: str, ago: float, crates: int = 0, seconds: float = 60,
             **fields: object) -> None:
        started = self.now - ago
        self.records.append({
            "kind": "step", "id": str(len(self.records)), "host": "natedev",
            "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
            "ended_at": datetime.fromtimestamp(started + seconds, UTC).isoformat(),
            "cwd": folder, "worktree": folder, "step": "check",
            "crates_compiled": crates, "finished_s": seconds, "duration_s": seconds,
            **fields,
        })

    def removal(self, ago: float, message: str, stream: str = "sweep") -> None:
        self.journal.append(json.dumps({
            "__REALTIME_TIMESTAMP": str(int((self.now - ago) * 1_000_000)),
            "MESSAGE": message, "_STREAM_ID": stream,
        }))

    def report(self, days: int = 7) -> str:
        root = Path(os.environ["BUILDLOG_DIR"])
        path = root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in self.records))
        before = {item.relative_to(root): (item.read_bytes(), item.stat().st_mtime_ns)
                  for item in root.rglob("*") if item.is_file()}
        state = Path(os.environ[tmp_sweep.STATE_ENV])
        state_before = {item.relative_to(state): (item.read_bytes(), item.stat().st_mtime_ns)
                        for item in state.rglob("*") if item.is_file()}
        output = io.StringIO()
        with (mock.patch.object(time, "time", return_value=self.now),
              mock.patch.object(sweep, "read_sweep_journal",
                                return_value=sweep.parse_sweep_journal(self.journal)),
              redirect_stdout(output)):
            status = sweep.main(["rebuilds", "--days", str(days)])
        self.assertEqual(status, 0)
        after = {item.relative_to(root): (item.read_bytes(), item.stat().st_mtime_ns)
                 for item in root.rglob("*") if item.is_file()}
        self.assertEqual(after, before, "The rebuild report must not write a log or an index")
        state_after = {item.relative_to(state): (item.read_bytes(), item.stat().st_mtime_ns)
                       for item in state.rglob("*") if item.is_file()}
        self.assertEqual(state_after, state_before, "The rebuild report must not write sweep state")
        return output.getvalue()

    def test_scratch_script_phase_names_stay_in_scratch_script_group(self) -> None:
        self.assertEqual(sweep.clone_kind("/tmp/claude/scratch/phase-demo-18-abc123"), "scratch.py folders")
        self.assertEqual(sweep.clone_kind("/tmp/claude/scratch/phase-lfo-unit-3r-abc123"), "scratch.py folders")
        self.assertEqual(sweep.clone_kind("/tmp/claude/scratch/gate-check.hana"), "gate and merge copies")

    def test_rust_worktree_under_a_scratch_home_stays_a_rust_worktree(self) -> None:
        home = "/tmp/claude/scratch/gate-home-abc123"
        self.enterContext(mock.patch.dict(os.environ, {"HOME": home}))
        self.assertEqual(sweep.clone_kind(f"{home}/rust/hana"), "~/rust worktrees")
        self.assertEqual(sweep.clone_kind("/tmp/claude/scratch/phase-demo-18-abc123"), "scratch.py folders")

    def test_manifest_path_builds_use_workspace_for_previous_step_and_tmp_removal(self) -> None:
        workspace = "/tmp/gate-check.hana"
        self.step(workspace, 2 * DAY)
        self.removal(DAY, f"tmp sweep: deleted {workspace} (1.0 GiB), last changed 2026-10-27 01:00")
        self.step("/home/natepiano", DAY // 2, 50, 3600, worktree=workspace)
        self.assert_hours(self.report(), "gate and merge copies",
                          {"floor take": 0, "budget take": 0, "tmp sweep": 1, "none": 0})

    def test_manifest_path_builds_match_removed_workspace_ancestor(self) -> None:
        workspace = "/tmp/gate-check.hana/member"
        self.step("/home/natepiano", 2 * DAY, worktree=workspace)
        self.removal(DAY, "tmp sweep: deleted /tmp/gate-check.hana (1.0 GiB), last changed 2026-10-27 01:00")
        self.step(workspace, DAY // 2, 50, 3600)
        self.assert_hours(self.report(), "gate and merge copies",
                          {"floor take": 0, "budget take": 0, "tmp sweep": 1, "none": 0})

    def test_background_budget_removal_explains_rebuild_without_journal_output(self) -> None:
        target = self.target()
        folder = str(target.root.parent)
        self.step(folder, 2 * DAY)
        with mock.patch.object(time, "time", return_value=self.now - DAY):
            self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        self.records.extend(self.trim_rows())
        self.step(folder, DAY // 2, 50, 3600)
        self.assert_hours(self.report(), "Total",
                          {"floor take": 0, "budget take": 1, "tmp sweep": 0, "none": 0})
        events = self.trim_events()
        self.assertEqual(events, [sweep.SweepRemoval(self.now - DAY, str(target.root), "budget take")])
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_orphan_journal_line_does_not_explain_a_cold_rebuild(self) -> None:
        folder = "/tmp/gate-check.hana"
        self.step(folder, 2 * DAY)
        self.removal(DAY, f"lint sweep: {folder}/target is 1.0 GiB, within the 2.0 GiB budget (test)")
        self.removal(DAY, "lint sweep: removed 5 orphaned files (1.0 GiB), copied-up output whose build unit is gone; left 0.0 GiB")
        self.step(folder, DAY // 2, 50, 3600)
        self.assert_hours(self.report(), "gate and merge copies",
                          {"floor take": 0, "budget take": 0, "tmp sweep": 0, "none": 1})

    def test_removal_sources_merge_once_per_path_cause_and_second(self) -> None:
        first = sweep.SweepRemoval(self.now, "/tmp/repo/target", "floor take")
        duplicate = sweep.SweepRemoval(self.now + 0.5, first.path, first.cause)
        budget = sweep.SweepRemoval(self.now, first.path, "budget take")
        later = sweep.SweepRemoval(self.now + 1, first.path, first.cause)
        self.assertEqual(sweep.merge_sweep_removals([first], [duplicate, budget, later]),
                         [first, budget, later])

    def assert_hours(self, output: str, kind: str, expected: dict[str, float]) -> None:
        rows = [[cell.strip() for cell in line.split("|")] for line in output.splitlines() if "|" in line]
        header = next(row for row in rows if row[0] == "Clone kind")
        values = next(row for row in rows if row[0] == kind)
        actual = dict(zip(header[1:], (float(value) for value in values[1:]), strict=True))
        self.assertEqual(actual, {**expected, "total": sum(expected.values())})

    def test_rebuilds_names_each_cause_and_clone_kind(self) -> None:
        cases = [
            ("/tmp/gate-check.hana", "gate and merge copies", "floor take", 1),
            ("/tmp/merge-check.hana", "gate and merge copies", "floor take", 2),
            ("/tmp/claude/unit/run/phases/3/clone", "phase clones", "budget take", 3),
            ("/tmp/tmp.ABCDEF", "mktemp folders", "tmp sweep", 4),
            ("/tmp/claude-1000/scratchpad/hana", "scratchpad clones", "none", 5),
            ("/tmp/claude/scratch/until-2026-10-30-hana-abcdef", "scratch.py folders", "floor take", 6),
            ("/tmp/unclassified-hana", "other /tmp", "none", 7),
            (str(Path.home() / "rust" / "hana"), "~/rust worktrees", "none", 8),
        ]
        for folder, _, cause, hours in cases:
            self.step(folder, 2 * DAY)
            self.step(folder, DAY // 2, 50, hours * 3600)
            if cause == "floor take":
                self.removal(DAY, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used 2026-10-27 01:00;")
            elif cause == "budget take":
                self.removal(DAY, f"lint sweep: {folder}/target is 2.0 GiB, over the 1.0 GiB budget (test)")
                self.removal(DAY, "lint sweep: removed 10 build units and 0 incremental dirs (1.0 GiB), last used 2026-10-27 01:00; left 1.0 GiB")
            elif cause == "tmp sweep":
                self.removal(DAY, f"tmp sweep: deleted {folder} (1.0 GiB), last changed 2026-10-27 01:00")

        output = self.report()

        expected: dict[str, dict[str, float]] = {}
        for _, kind, cause, hours in cases:
            totals = expected.setdefault(kind, {"floor take": 0, "budget take": 0, "tmp sweep": 0, "none": 0})
            totals[cause] += hours
        for kind, hours in expected.items():
            self.assert_hours(output, kind, hours)
        self.assert_hours(output, "Total", {"floor take": 9, "budget take": 3, "tmp sweep": 4, "none": 20})

    def test_rebuilds_only_counts_cold_steps_and_uses_compile_time(self) -> None:
        folder = "/tmp/unclassified-hana"
        self.step(folder, 3 * DAY, 49, 20 * 3600)
        self.step(folder, 2 * DAY, 50, 3600, duration_s=20 * 3600)
        self.step(folder, DAY, 51, 7200, finished_s=None)
        self.step(folder, 9 * DAY, 50, 30 * 3600)

        output = self.report()

        self.assert_hours(output, "other /tmp", {"floor take": 0, "budget take": 0, "tmp sweep": 0, "none": 3})

    def test_rebuilds_does_not_assign_a_take_outside_the_previous_step_interval(self) -> None:
        folder = "/tmp/unclassified-hana"
        self.step(folder, 3 * DAY)
        self.removal(2 * DAY, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used 2026-10-26 01:00;")
        self.step(folder, DAY)
        self.removal(DAY // 2, f"lint sweep: the floor took 1.0 GiB from {folder}-other/target, last used 2026-10-26 01:00;")
        self.step(folder, 3600, 50, 3600)
        self.removal(1800, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used 2026-10-26 01:00;")

        output = self.report()

        self.assert_hours(output, "other /tmp", {"floor take": 0, "budget take": 0, "tmp sweep": 0, "none": 1})

    def test_rebuilds_without_a_previous_step_has_no_assigned_cause(self) -> None:
        folder = "/tmp/unclassified-hana"
        self.removal(DAY, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used 2026-10-26 01:00;")
        self.step(folder, 3600, 50, 3600)

        output = self.report()

        self.assert_hours(output, "other /tmp", {"floor take": 0, "budget take": 0, "tmp sweep": 0, "none": 1})


class TargetDirTests(SweepCase):
    def test_sweeps_the_named_directory_without_a_workspace(self) -> None:
        target = self.target()
        output = io.StringIO()
        config = target.root.parent / "lint.conf"
        _ = write_config_fixture(config)
        environ = {sweep.BUDGET_ENV: "1024", "CONF_DIR": str(config.parent)}
        # The machine's real disk floor is not this test's subject; its scan reads every real root.
        with (mock.patch.dict(os.environ, environ), redirect_stdout(output),
              mock.patch.object(sweep, "hold_floor", return_value=0)):
            status = sweep.main([sweep.TARGET_DIR_FLAG, str(target.root), "--dry-run"])
        self.assertEqual(status, 0)
        self.assertIn(f"lint sweep: {target.root} is ", output.getvalue())
        self.assertEqual([path for path in target.orphans if not path.exists()], [])

    @unittest.skipUnless(os.path.isdir("/proc/self"), "the process scan reads /proc")
    def test_a_target_a_process_works_in_is_skipped(self) -> None:
        target = self.target()
        worker = subprocess.Popen(["sleep", "60"], cwd=target.root)
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        output = io.StringIO()
        config = target.root.parent / "lint.conf"
        _ = write_config_fixture(config)
        environ = {**config_environment(config.parent), sweep.BUDGET_ENV: "0"}
        # The machine's real disk floor is not this test's subject; below it, main would page.
        with (mock.patch.dict(os.environ, environ), redirect_stdout(output),
              mock.patch.object(sweep, "hold_floor", return_value=0)):
            status = sweep.main([sweep.TARGET_DIR_FLAG, str(target.root)])
        self.assertEqual(status, 0)
        self.assertIn(f"lint sweep: pid {worker.pid} works in {target.root}; {target.root} skipped", output.getvalue())
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


def cargo_tag_target(base: Path, name: str, digest: str, used: float) -> tuple[Path, Path]:
    """A target with cargo's cache tag and a build tree, but no rustc-info file."""
    root = base / name / "target"
    tree = root / "debug"
    output = unit(tree, "deps", name, digest, used)
    _ = (root / sweep.CACHE_TAG).write_text(
        f"{scratch.CACHE_TAG_SIGNATURE}\n{scratch.CARGO_TAG_LINE}\n"
    )
    return tree, output


def fake_process(proc: Path, pid: int, *, exe: str = "/usr/bin/true", cwd: str = "/", maps: str = "",
                 argv: tuple[str, ...] = ("true",), environ: tuple[str, ...] = ()) -> None:
    """A /proc/<pid> entry as the scan reads it."""
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "exe").symlink_to(exe)
    (entry / "cwd").symlink_to(cwd)
    _ = (entry / "maps").write_text(maps)
    _ = (entry / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
    _ = (entry / "environ").write_bytes(b"".join(item.encode() + b"\0" for item in environ))


def idle_processes(proc: Path, count: int) -> None:
    """Readable processes outside all targets, so every batch must check every pid."""
    outside = proc.parent / "outside"
    for pid in range(1, count + 1):
        fake_process(proc, pid, exe=str(outside / "bin"), cwd=str(outside), maps="".join(
            f"7f00-7f10 r-xp 00000000 00:1f 42 {outside}/lib{number}.so\n"
            for number in range(3)
        ))


def run_from(binary: Path, cwd: Path | None = None) -> subprocess.Popen[bytes]:
    """A sleeping process whose exe is a copy of sleep at binary, as a test binary runs from deps/."""
    sleep = shutil.which("sleep")
    assert sleep is not None
    _ = shutil.copy2(sleep, binary)
    # argv[0] stays "sleep": a multi-call coreutils picks its program by that name.
    return subprocess.Popen(["sleep", "60"], executable=binary, cwd=cwd)


class ProcessUseTests(SweepCase):
    def test_one_target_recheck_finds_exe_cwd_maps_and_nextest_without_claiming_nested_use(self) -> None:
        nested = self.hana / "proof" / "target"
        nested.mkdir(parents=True)
        owners = sweep.TargetOwnerIndex([str(self.hana), str(nested), str(self.other)])
        for how in ("exe", "cwd", "maps", "nextest", "nested", "sibling"):
            with self.subTest(how=how):
                if self.proc.exists():
                    shutil.rmtree(self.proc)
                path = str((nested if how == "nested" else self.hana) / "debug" / "app")
                fake_process(self.proc, 10,
                             exe=path if how == "exe" else "/usr/bin/true",
                             cwd=path if how in ("cwd", "nested") else (
                                 str(self.workspace) if how == "nextest" else str(self.hana) + "-old"),
                             maps=f"7f00-7f10 r-xp 00000000 00:1f 42 {path}\n" if how == "maps" else "",
                             argv=("cargo", "nextest", "run") if how == "nextest" else ("true",))
                uses = sweep.process_use(str(self.hana), owners, str(self.proc))
                self.assertEqual(set(uses), set() if how in ("nested", "sibling") else {str(self.hana)})
                if uses:
                    self.assertEqual(uses[str(self.hana)].pid, 10)
                if how == "nested":
                    self.assertEqual(set(sweep.process_use(str(nested), owners, str(self.proc))), {str(nested)})

    def test_one_target_recheck_parses_only_maps_that_name_its_prefix(self) -> None:
        idle_processes(self.proc, 4)
        path = str(self.hana / "debug" / "mapped library.so")
        wanted = f"7f00-7f10 r-xp 00000000 00:1f 42 {path}\n"
        fake_process(self.proc, 20, maps=wanted)
        original = sweep.mapped_files
        parsed: list[bytes] = []

        def parse(maps: bytes) -> list[str]:
            self.assertIn(os.fsencode(str(self.hana) + os.sep), maps,
                          "an unrelated process's maps must not be decoded")
            parsed.append(maps)
            return list(original(maps))

        with mock.patch.object(sweep, "mapped_files", side_effect=parse):
            uses = sweep.process_use(str(self.hana), sweep.TargetOwnerIndex([str(self.hana)]), str(self.proc))
        self.assertEqual(parsed, [wanted.encode()])
        self.assertEqual(uses, {str(self.hana): sweep.TargetUse(20, f"maps {path}")})

    def test_kernel_process_paths_need_no_resolution_or_outside_parent_walk(self) -> None:
        idle_processes(self.proc, 50)
        fake_process(self.proc, 51, cwd=str(self.hana) + "-other/debug")
        owners = sweep.TargetOwnerIndex([str(self.hana), str(self.other)])
        with (mock.patch.object(os.path, "realpath", wraps=os.path.realpath) as resolve,
              mock.patch.object(os.path, "dirname", wraps=os.path.dirname) as parent):
            uses = sweep.process_uses(owners, str(self.proc))
        self.assertEqual(uses, {})
        self.assertEqual(resolve.call_count, 0)
        self.assertEqual(parent.call_count, 0)

    def test_repeated_process_paths_share_one_owner_lookup_across_batch_rechecks(self) -> None:
        path = str(self.hana / "debug" / "deps" / "shared")
        for pid in range(1, 51):
            fake_process(self.proc, pid, exe=path, maps=f"7f00-7f10 r-xp 00000000 00:1f 42 {path}\n")
        first_pid = int(os.listdir(self.proc)[0])
        owners = sweep.TargetOwnerIndex([str(self.hana), str(self.other)])
        with (mock.patch.object(os.path, "realpath", wraps=os.path.realpath) as resolve,
              mock.patch.object(os.path, "dirname", wraps=os.path.dirname) as parent):
            for _ in range(40):
                uses = sweep.process_uses(owners, str(self.proc))
                self.assertEqual({root: use.pid for root, use in uses.items()}, {str(self.hana): first_pid})
        print(f"shared process path P=50, batches=40: realpath {resolve.call_count}, parents {parent.call_count}")
        self.assertEqual(resolve.call_count, 0)
        self.assertEqual(parent.call_count, 3)
        # The memo holds ownership, not a snapshot of which processes use a target.
        fake_process(self.proc, 51, cwd=str(self.other))
        self.assertEqual({root: use.pid for root, use in sweep.process_uses(owners, str(self.proc)).items()},
                         {str(self.hana): first_pid, str(self.other): 51})

    def test_process_path_resolution_grows_only_by_the_added_targets(self) -> None:
        idle_processes(self.proc, 50)
        targets = [str(self.hana), str(self.hana / "proof" / "target")]
        targets.extend(str(self.base / f"target-{number}") for number in range(78))
        for target in targets:
            Path(target).mkdir(parents=True, exist_ok=True)
        counts: list[int] = []
        parent_counts: list[int] = []
        realpath = os.path.realpath
        dirname = os.path.dirname
        for size in (40, 80):
            with (mock.patch.object(os.path, "realpath", wraps=realpath) as resolve,
                  mock.patch.object(os.path, "dirname", wraps=dirname) as parent):
                uses = sweep.process_uses(targets[:size], str(self.proc))
            self.assertEqual(uses, {})
            counts.append(resolve.call_count)
            parent_counts.append(parent.call_count)
        print(f"process realpath calls P=50: T=40 {counts[0]}, T=80 {counts[1]}")
        print(f"process parent walks P=50: T=40 {parent_counts[0]}, T=80 {parent_counts[1]}")
        self.assertLessEqual(counts[1], counts[0] + 40)
        self.assertLessEqual(counts[0], 40 + 50 * 5)

    def test_symlinked_target_owns_real_process_paths_and_outer_paths_stay_outer(self) -> None:
        nested = self.hana / "proof" / "target"
        nested.mkdir(parents=True)
        alias = self.base / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        fake_process(self.proc, 10, exe=str(nested / "binary"))
        fake_process(self.proc, 11, cwd=str(self.hana / "debug"))
        uses = sweep.process_uses([str(self.hana), str(alias)], str(self.proc))
        self.assertEqual({root: use.pid for root, use in uses.items()},
                         {str(alias): 10, str(self.hana): 11})

    def test_nested_process_paths_keep_only_the_deepest_target_busy(self) -> None:
        nested = self.hana / "proof" / "target"
        nested.mkdir(parents=True)
        for how in ("cwd", "exe", "maps"):
            with self.subTest(how=how):
                if self.proc.exists():
                    shutil.rmtree(self.proc)
                path = str(nested / "debug" / "deps" / "app")
                fake_process(self.proc, 10, cwd=str(nested) if how == "cwd" else "/",
                             exe=path if how == "exe" else "/usr/bin/true",
                             maps=f"7f00-7f10 r-xp 00000000 00:1f 42 {path}\n" if how == "maps" else "")
                uses = sweep.process_uses([str(self.hana), str(nested)], str(self.proc))
                self.assertEqual(set(uses), {str(nested)})

    base: Path = Path()
    proc: Path = Path()
    workspace: Path = Path()
    hana: Path = Path()
    other: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.proc = self.base / "proc"
        self.workspace = self.base / "hana"
        self.hana = self.workspace / "target"
        self.other = self.base / "other" / "target"
        for directory in (self.hana / "debug" / "deps", self.other, self.workspace / "crates" / "app"):
            directory.mkdir(parents=True)

    def uses(self) -> dict[str, str]:
        return {root: use.how for root, use in sweep.process_uses([str(self.hana), str(self.other)],
                                                                   str(self.proc)).items()}

    def test_an_exe_a_working_directory_or_a_mapped_file_under_a_target_is_a_use(self) -> None:
        library = self.hana / "debug" / "deps" / "libbevy_dylib-1234.so"
        fake_process(self.proc, 10, exe=str(self.hana / "debug" / "deps" / "hana-f4b6"))
        fake_process(self.proc, 11, cwd=str(self.other))
        self.assertEqual(self.uses(), {str(self.hana): f"runs {self.hana}/debug/deps/hana-f4b6",
                                       str(self.other): f"works in {self.other}"})
        for entry in self.proc.iterdir():
            shutil.rmtree(entry)
        fake_process(self.proc, 12, maps=f"7f00-7f10 r-xp 00000000 00:1f 42    {library}\n")
        self.assertEqual(self.uses(), {str(self.hana): f"maps {library}"})

    def test_a_stale_process_with_only_deleted_files_under_a_target_and_cwd_outside_is_not_a_use(self) -> None:
        deps = self.hana / "debug" / "deps"
        fake_process(self.proc, 10, exe=f"{deps}/cargo_berth-1234 (deleted)", cwd="/tmp/fixture (deleted)",
                     maps=f"5600-5610 r-xp 00000000 00:1f 42    {deps}/cargo_berth-1234 (deleted)\n"
                     + f"7f00-7f10 r-xp 00000000 00:1f 43    {deps}/libgit-5678.so (deleted)\n")
        self.assertEqual(self.uses(), {})

    def test_a_deleted_exe_counts_with_a_live_mapping_or_a_working_directory_inside(self) -> None:
        deps = self.hana / "debug" / "deps"
        library = deps / "libbevy_dylib-1234.so"
        fake_process(self.proc, 10, exe=f"{deps}/hana-f4b6 (deleted)",
                     maps=f"5600-5610 r-xp 00000000 00:1f 42    {deps}/hana-f4b6 (deleted)\n"
                     + f"7f00-7f10 r-xp 00000000 00:1f 43    {library}\n")
        fake_process(self.proc, 11, exe=f"{self.other}/debug/app (deleted)", cwd=f"{self.other}/debug")
        self.assertEqual(self.uses(), {str(self.hana): f"maps {library}",
                                       str(self.other): f"works in {self.other}/debug"})

    def test_a_sibling_whose_name_starts_with_the_target_is_not_a_use(self) -> None:
        fake_process(self.proc, 10, exe=f"{self.hana}-old/debug/hana", cwd=f"{self.hana}2",
                     maps=f"7f00-7f10 r-xp 00000000 00:1f 42    {self.hana}.bak/lib.so\n")
        self.assertEqual(self.uses(), {})

    def test_a_nextest_run_uses_its_workspace_target_between_test_binaries(self) -> None:
        fake_process(self.proc, 10, exe="/nix/store/x-cargo-nextest/bin/.cargo-nextest-wrapped",
                     cwd=str(self.workspace / "crates" / "app"),
                     argv=("/nix/store/x-cargo-nextest/bin/cargo-nextest", "nextest", "run"))
        self.assertEqual(self.uses(), {str(self.hana): f"runs cargo-nextest in {self.workspace / 'crates' / 'app'}"})

    def test_a_nextest_run_uses_the_target_its_flag_or_environment_names(self) -> None:
        for argv, environ in ((("cargo", "nextest", "run", "--target-dir", str(self.other)), ()),
                              (("cargo", "nextest", "run", f"--target-dir={self.other}"), ()),
                              (("cargo-nextest", "nextest", "run"), (f"CARGO_TARGET_DIR={self.other}",))):
            with self.subTest(argv=argv, environ=environ):
                fake_process(self.proc, 10, exe="/usr/bin/cargo", cwd=str(self.workspace), argv=argv, environ=environ)
                self.assertEqual(self.uses(), {str(self.other): f"runs cargo-nextest in {self.workspace}"})
                shutil.rmtree(self.proc)

    def test_nextest_shared_reader_keeps_legacy_override_and_invalid_config_behavior(self) -> None:
        config = self.workspace / ".cargo"
        config.mkdir()
        legacy = config / "config"
        _ = legacy.write_text('[build]\ntarget-dir = ' + json.dumps(str(self.other)) + '\n')
        _ = (config / "config.toml").write_text('[build]\ntarget-dir = ' + json.dumps(str(self.hana)) + '\n')
        cases = (
            (["cargo", "nextest", "run"], self.other),
            (["cargo", "nextest", "run", "--config", 'build.target-dir=' + json.dumps(str(self.hana))], self.hana),
            (["cargo", "nextest", "run", "--config", 'build.target-dir=' + json.dumps(str(self.hana)),
              "--target-dir", str(self.other)], self.other),
        )
        for argv, folder in cases:
            with self.subTest(argv=argv):
                fake_process(self.proc, 10, exe="/usr/bin/cargo", cwd=str(self.workspace), argv=tuple(argv))
                with mock.patch.object(sweep, "cargo_folder_settings", wraps=cargo_target_config.cargo_folder_settings) as reader:
                    self.assertEqual(self.uses(), {str(folder): f"runs cargo-nextest in {self.workspace}"})
                    self.assertEqual(reader.call_count, 1)
                shutil.rmtree(self.proc)
        _ = legacy.write_text('[build\n')
        fake_process(self.proc, 10, exe="/usr/bin/cargo", cwd=str(self.workspace), argv=("cargo", "nextest", "run"))
        with mock.patch.object(sweep, "cargo_folder_settings", wraps=cargo_target_config.cargo_folder_settings) as reader:
            self.assertEqual(self.uses(), {str(self.hana): f"runs cargo-nextest in {self.workspace}"})
            self.assertEqual(reader.call_count, 1)

    def test_without_proc_nothing_is_in_use(self) -> None:
        self.assertEqual(self.uses(), {})


class ReclamationTests(SweepCase):
    def test_directory_swapped_for_symlink_is_not_followed_or_credited(self) -> None:
        for nested in (False, True):
            with self.subTest(nested=nested):
                base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
                root = base / "target"
                victim = root / "unit" if nested else root
                original_output = write(victim / "original")
                outside = write(base / "outside" / "keep")
                saved = base / "saved-unit"
                parent = victim.parent.stat()
                original_lstat = os.lstat
                swapped = False
                def swap_after_check(path: str, *, dir_fd: int | None = None) -> os.stat_result:
                    nonlocal swapped
                    before = original_lstat(path, dir_fd=dir_fd)
                    matches = path == str(victim)
                    if dir_fd is not None and path == victim.name:
                        opened_parent = os.fstat(dir_fd)
                        matches = (opened_parent.st_dev, opened_parent.st_ino) == (parent.st_dev, parent.st_ino)
                    if matches and not swapped:
                        swapped = True
                        _ = victim.rename(saved)
                        victim.symlink_to(outside.parent, target_is_directory=True)
                    return before
                with mock.patch.object(os, "lstat", side_effect=swap_after_check), redirect_stderr(io.StringIO()):
                    account = sweep.remove_entry(str(root))
                self.assertTrue(swapped, "The replacement must occur between the check and directory open")
                self.assertTrue(outside.exists(), "Deletion must not follow the replacement symlink")
                self.assertTrue((saved / original_output.name).exists())
                self.assertEqual(account.bytes, 0)
                self.assertGreater(account.failures, 0)

    def exercise(self, caller: str, case: str) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        roots = [base / name / "target" for name in ("oldest", "next", "newest")]
        trees = [root / "debug" for root in roots]
        outputs = [write(tree / "output") for tree in trees]
        if case == "earlier link":
            outputs[1].unlink()
            os.link(outputs[0], outputs[1])
        now = time.time()
        groups = [sweep.Group("unit", str(tree), [str(output)], last_used=now - (3 - index) * DAY)
                  for index, (tree, output) in enumerate(zip(trees, outputs))]
        if case == "partial removal":
            extra = write(trees[0] / "extra")
            groups[0].entries.append(str(extra))
        scan = sweep.Scan(groups=groups)
        owners = {entry: group for group in groups for entry in group.entries}
        for root in roots:
            sweep.walk(str(root), None, owners, scan)
        blocks = outputs[0].stat().st_blocks * 512
        total = sum(scan.blocks.values())
        if case in ("outside link", "dry run"):
            os.link(outputs[0], base / "outside-target")
        original_unlink = os.unlink
        first_parent = outputs[0].parent.stat()
        def unlink(path: str, *, dir_fd: int | None = None) -> None:
            matches = path == str(outputs[0])
            if dir_fd is not None and path == outputs[0].name:
                matches = os.path.samestat(first_parent, os.fstat(dir_fd))
            if case in ("failed removal", "partial removal") and matches:
                raise PermissionError("fixture unlink refused")
            original_unlink(path, dir_fd=dir_fd)
        def order(group: sweep.Group) -> tuple[float, ...]:
            return (group.last_used,)
        with (mock.patch.object(os, "unlink", side_effect=unlink) as unlinks,
              mock.patch.object(sweep, "build_hold_targets", return_value={}),
              mock.patch.object(sweep, "process_uses", return_value={}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            if caller == "budget":
                left, failures, taken = sweep.shrink(scan, total, total - blocks // 2,
                                                   case == "dry run", order)
            else:
                left, failures, taken = sweep.shrink_floor(
                    scan, total, total - blocks // 2, case == "dry run",
                    {str(tree): str(root) for tree, root in zip(trees, roots)},
                    {str(root): [str(tree)] for root, tree in zip(roots, trees)}, {}, order, now,
                    sweep.TargetOwnerIndex([str(root) for root in roots]))
        self.assertEqual(left, total - blocks)
        self.assertEqual(failures, int(case in ("failed removal", "partial removal")))
        if case == "dry run":
            unlinks.assert_not_called()
            self.assertEqual([group for group, _ in taken], groups[:1])
            self.assertTrue(all(output.exists() for output in outputs))
            self.assertFalse(sweep.sweep_removals_path().exists())
        elif case == "partial removal":
            self.assertTrue(outputs[0].exists())
            self.assertFalse((trees[0] / "extra").exists())
            self.assertTrue(outputs[1].exists(), "The successful unlink covered the shortfall")
            self.assertTrue(outputs[2].exists())
        else:
            self.assertEqual(outputs[0].exists(), case == "failed removal")
            self.assertFalse(outputs[1].exists(), "The next candidate must cover the real shortfall")
            self.assertTrue(outputs[2].exists(), "Stop once confirmed deletions cover the shortfall")
            if case == "outside link":
                self.assertTrue((base / "outside-target").exists())
                self.assertEqual([size for _, size in taken], [0, blocks])

    def test_link_added_outside_target_after_scan_does_not_cover_shortfall(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                self.exercise(caller, "outside link")

    def test_earlier_unlink_in_same_pass_allows_last_link_to_free_blocks(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                self.exercise(caller, "earlier link")

    def test_failed_unlink_does_not_cover_shortfall(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                self.exercise(caller, "failed removal")

    def test_dry_run_uses_scan_estimate_without_unlinking_or_recording(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                self.exercise(caller, "dry run")

    def test_partial_removal_credits_only_successful_unlinks(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                self.exercise(caller, "partial removal")


def cut_stamp(seconds: float) -> str:
    return store.utc_iso(sweep.TARGET_OVERRIDE_RECORDER_CUT.timestamp() + seconds)


class FloorTests(SweepCase):
    def root_row(self, workspace: str | Path, target: str | Path, build: str | Path, *,
                 cwd: str | Path = "", step: str = "build", call_id: str = "attempt", host: str = "",
                 argv: Sequence[str] = (), target_folder: str | Path = "", started_at: str | None = None) -> None:
        record_root_attempt(build_index.index_path(), str(workspace), str(target), str(build),
                            cwd=str(cwd), step=step, call_id=call_id, host=host or store.host_name(),
                            argv=argv, target_folder=str(target_folder), started_at=started_at)

    def override_outputs(self, started_at: str | None) -> tuple[Path, Path, sweep.CargoOutputIndex]:
        base = self.base()
        tree, payload = cargo_target(base, "workspace", APP, time.time() - DAY)
        target = tree.parent
        self.root_row(tree.parent.parent, target, target, argv=["cargo", "build", "--target-dir", str(target)],
                      target_folder=target, started_at=started_at)
        _, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        return tree, payload, outputs

    def test_matching_override_outputs_supply_evidence_only_after_the_recorder_cut(self) -> None:
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                tree, _, outputs = self.override_outputs(cut_stamp(offset))
                target = tree.parent
                expected = (CargoOutputRoots(tree.parent.parent, target, target),) if offset > 0 else ()
                self.assertEqual(outputs.workspaces, expected)
                self.assertEqual(outputs.no_recorded_evidence,
                                 () if offset > 0 else (sweep.NoRecordedEvidence(str(target)),))

    def test_mismatched_target_folders_supply_no_evidence_on_either_side_of_the_cut(self) -> None:
        for offset in (-1, 1):
            with self.subTest(offset=offset):
                base = self.base()
                tree, _ = cargo_target(base, "workspace", APP, time.time() - DAY)
                actual, _ = cargo_target(base, "actual", DEMO, time.time() - DAY)
                self.root_row(tree.parent.parent, tree.parent, tree.parent,
                              argv=["cargo", "build", "--target-dir", str(actual.parent)],
                              target_folder=actual.parent, started_at=cut_stamp(offset))
                _, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
                self.assertEqual(outputs.workspaces, ())
                self.assertCountEqual(outputs.no_recorded_evidence,
                                      (sweep.NoRecordedEvidence(str(tree.parent)),
                                       sweep.NoRecordedEvidence(str(actual.parent))))

    def test_override_outputs_with_an_unknown_start_remain_protected(self) -> None:
        for stamp in (None, "", "not-a-time"):
            with self.subTest(started_at=stamp):
                tree, _, outputs = self.override_outputs(stamp)
                self.assertEqual(outputs.workspaces, ())
                self.assertEqual(outputs.no_recorded_evidence, (sweep.NoRecordedEvidence(str(tree.parent)),))

    def test_override_started_before_the_cut_stays_protected_when_published_after_it(self) -> None:
        tree, _, _ = self.override_outputs(cut_stamp(-1))
        with closing(sqlite3.connect(build_index.index_path())) as connection, connection:
            _ = connection.execute("UPDATE steps SET ended_at = ?", (cut_stamp(3600),))
        _, outputs = sweep.discover_cargo_outputs([str(tree.parent.parent.parent)], lint_settings=conf.read("lint"))
        self.assertEqual(outputs.workspaces, ())
        self.assertEqual(outputs.no_recorded_evidence, (sweep.NoRecordedEvidence(str(tree.parent)),))

    def test_recorded_outputs_use_the_cwd_when_the_worktree_is_unknown(self) -> None:
        base = self.base()
        workspace, member = base / "workspace", base / "workspace" / "member"
        fallback = base / "fallback"
        target, build, other = base / "target", base / "build", base / "other-target"
        for path in (target, build, other):
            path.mkdir()
        self.root_row(workspace, target, build, cwd=member)
        self.root_row("unknown", other, other, cwd=fallback)
        _, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        self.assertCountEqual(outputs.workspaces, (
            CargoOutputRoots(workspace, target, build), CargoOutputRoots(fallback, other, other),
        ))

    def test_recorded_step_kinds_supply_floor_targets_without_running_cargo(self) -> None:
        base = self.base()
        expected: set[str] = set()
        for number, step in enumerate(("build", "check", "clippy", "doc", "mend", "nextest", "test", "t")):
            tree, _ = cargo_target(base, step, f"{number:016x}", time.time() - DAY)
            self.root_row(tree.parent.parent, tree.parent, tree.parent, step=step, call_id="")
            expected.add(str(tree.parent))
        bin_dir = base / "bin"
        bin_dir.mkdir()
        cargo_calls = base / "cargo-calls"
        cargo = bin_dir / "cargo"
        _ = cargo.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SWEEP_CARGO_CALLS\"\nexit 99\n")
        cargo.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                                          "SWEEP_CARGO_CALLS": str(cargo_calls)}):
            status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(reported_floor_targets(text), expected)
        self.assertFalse(cargo_calls.exists(), cargo_calls.read_text() if cargo_calls.exists() else "")
        self.assertEqual(self.trim_rows(), [])

    def test_repeated_recorded_attempts_keep_both_output_pairs(self) -> None:
        base = self.base()
        workspace = base / "workspace"
        pairs: list[tuple[Path, Path]] = []
        for number in range(2):
            target_tree, _ = cargo_target(base, f"artifacts-{number}", APP, time.time() - DAY)
            build_tree, _ = cargo_target(base, f"build-{number}", DEMO, time.time() - DAY)
            pairs.append((target_tree.parent, build_tree.parent))
            self.root_row(workspace, *pairs[-1], call_id="same-call")
        roots, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        self.assertEqual(set(roots), {str(path) for pair in pairs for path in pair})
        for pair in pairs:
            self.assertEqual(set(outputs.roots_for(str(pair[0]))), set(map(str, pair)))
        status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(reported_floor_targets(text), set(roots))

    def test_unrecorded_and_unknown_root_targets_stay_protected(self) -> None:
        base = self.base()
        tree, payload = cargo_target(base, "unrecorded", APP, time.time() - DAY)
        unknown_tree, unknown_payload = cargo_target(base, "unknown", DEMO, time.time() - DAY)
        relative_tree, relative_payload = cargo_target(base, "relative", TOOL, time.time() - DAY)
        self.root_row(unknown_tree.parent.parent, "unknown", "unknown")
        self.root_row(relative_tree.parent.parent, "target", "target", cwd=relative_tree.parent.parent)
        status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertNotIn("the floor would take", text)
        self.assertIn(str(tree.parent), text)
        self.assertIn("no recorded", text.lower())
        self.assertTrue(all(path.exists() for path in (payload, unknown_payload, relative_payload)))
        _, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        self.assertEqual(set(outputs.no_recorded_evidence),
                         {sweep.NoRecordedEvidence(str(root))
                          for root in (tree.parent, unknown_tree.parent, relative_tree.parent)})

    def protected_report(self, base: Path, holders: Sequence[sweep.FloorHolder] = ()) -> str:
        roots, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        measured = sweep.measure_protected_folders(
            frozenset(item.path for item in outputs.no_recorded_evidence), frozenset(roots))
        return sweep.floor_out_of_reach_text(time.time(), 0, 0, 10 * GIB, holders, frozenset(), 0,
                                             sweep.UnavailableDiskMeasurement("missing"), 0, protected=measured)

    def test_overridden_step_outputs_stay_protected_without_authorizing_the_default_root(self) -> None:
        base = self.base()
        protected: set[str] = set()
        payloads: list[Path] = []
        for name in ("target-flag", "config-flag", "recorded-folder"):
            tree, payload = cargo_target(base, name, APP, time.time() - DAY)
            actual, actual_payload = cargo_target(base, name + "-actual", DEMO, time.time() - DAY)
            argv = (["cargo", "build", "--target-dir", str(actual.parent)] if name == "target-flag" else
                    ["cargo", "build", "--config", f'build.target-dir="{actual.parent}"'] if name == "config-flag" else
                    ["cargo", "build"])
            self.root_row(tree.parent.parent, tree.parent, tree.parent, argv=argv,
                          target_folder=actual.parent if name == "recorded-folder" else "")
            protected.update((str(tree.parent), str(actual.parent)))
            payloads.extend((payload, actual_payload))
        status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(reported_floor_targets(text), set())
        for root in protected:
            self.assertIn(f"protected {root}: no recorded evidence", text)
        self.assertTrue(all(path.exists() for path in payloads))

    def test_unrecorded_nested_targets_outside_search_roots_stay_protected(self) -> None:
        for source in ("recorded", "cache"):
            with self.subTest(source=source):
                base = self.base()
                outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
                outer, outer_file = cargo_target(outside, "outer", APP, time.time() - DAY)
                nested, nested_file = cargo_target(outer.parent, "nested", DEMO, time.time() - DAY)
                mapping = CargoOutputRoots(base / "workspace", outer.parent, outer.parent)
                if source == "recorded":
                    self.root_row(mapping.workspace_root, mapping.target_directory, mapping.build_directory)
                else:
                    sweep.save_cargo_output_roots(sweep.CargoOutputIndex((mapping,)))
                roots, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
                self.assertIn(str(nested.parent), roots)
                self.assertEqual(outputs.no_recorded_evidence, (sweep.NoRecordedEvidence(str(nested.parent)),))
                status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
                self.assertEqual(status, 0)
                self.assertEqual(reported_floor_targets(text), {str(outer.parent)})
                self.assertIn(f"protected {nested.parent}: no recorded evidence", text)
                self.assertTrue(outer_file.exists())
                self.assertTrue(nested_file.exists())

    def test_protected_bytes_remain_in_the_saved_floor_measurement(self) -> None:
        base = self.base()
        tree, payload = cargo_target(base, "protected", APP, time.time() - DAY)
        size = sweep.directory_blocks(str(tree.parent))
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT):
            status, text = self.hold(base, 10 * GIB, [0], recorded=False)
        self.assertEqual(status, 0)
        self.assertIn("1 left alone", text)
        saved = self.saved_record()
        self.assertEqual(saved.build_cache_bytes, size)
        self.assertTrue(payload.exists())
        self.assertEqual(self.trim_rows(), [])

    def test_protected_report_limits_paths_and_measures_each_folder_once_per_pass(self) -> None:
        base = self.base()
        protected: list[Path] = []
        for number in range(4):
            tree = cargo_target(base / ("x" * 80), f"protected-{number}", APP, time.time() - DAY)[0]
            _ = write(tree / "extra", (number + 1) * SIZE)
            root = tree.parent.with_name(f"target-{number}")
            _ = tree.parent.rename(root)
            protected.append(root)
        _ = write_config_fixture(conf.path("lint"), "sweep_measurement_paths=2\nsweep_message_path_widths=48\n"
                                 + "sweep_phone_floor_gib=300\n")
        with (mock.patch.object(sweep, "directory_blocks", wraps=sweep.directory_blocks) as measure,
              mock.patch.object(sweep, "disk_floor_channels", return_value=sweep.FloorAlertChannels.NATEDEV),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            status, text = self.hold(base, 10 * GIB, [0], recorded=False)
        self.assertEqual(status, 0)
        self.assertIn("4 left alone", text)
        for root in protected:
            self.assertEqual(sum(call.args[0] == str(root) for call in measure.call_args_list), 1)
        self.assertGreater(send.call_count, 0)
        reports = [cast(str, call.args[0]) for call in send.call_args_list
                   if "no recorded evidence" in cast(str, call.args[0])]
        self.assertTrue(reports)
        for message in reports:
            paths = [line.split(": holds ", 1)[0] for line in message.splitlines() if ": holds " in line]
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(len(path) <= 48 and "…" in path for path in paths))
            self.assertTrue(paths[0].endswith("/target-3"))
            self.assertTrue(paths[1].endswith("/target-2"))

    def test_a_real_private_floor_pass_saves_every_workspace_pair(self) -> None:
        base = self.base()
        pairs: list[CargoOutputRoots] = []
        workspace = base / "workspace"
        for number in range(3):
            tree = cargo_target(base, f"pair-{number}", APP, time.time() - DAY)[0]
            pair = CargoOutputRoots(workspace, tree.parent, tree.parent)
            pairs.append(pair)
            if number:
                self.root_row(workspace, pair.target_directory, pair.build_directory)
        sweep.save_cargo_output_roots(sweep.CargoOutputIndex((pairs[0],)))
        status, text = self.hold(base, 10 * GIB, [11 * GIB], recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(text, "")
        saved = sweep.saved_cargo_output_roots()
        self.assertEqual(set((*saved.workspaces, *saved.history)), set(pairs))
        self.assertEqual(saved.workspaces, (pairs[1],))
        self.assertEqual(set(saved.history), {pairs[0], pairs[2]})

    def test_a_target_removed_during_shrink_does_not_fail_the_floor_pass(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "idle", APP, time.time() - DAY)
        shrink = sweep.shrink_floor

        def device(path: str) -> int:
            """Report one filesystem wherever TMPDIR is. A missing path still raises."""
            _ = os.stat(path)
            return base.stat().st_dev

        def remove_target(
            scan: sweep.Scan, total: int, budget: int, dry_run: bool,
            tree_targets: dict[str, str], trees: dict[str, list[str]],
            busy: dict[str, sweep.TargetUse | sweep.LockHeld | sweep.BuildHoldTarget | sweep.NoRecordedEvidence],
            removal_order: sweep.RemovalOrder, planned_at: float, owners: sweep.TargetOwnerIndex, *,
            lint_settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
            policy: sweep.MacFloor | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
        ) -> tuple[int, int, list[tuple[sweep.FloorTake, int]]]:
            result = shrink(scan, total, budget, dry_run, tree_targets, trees, busy, removal_order,
                            planned_at, owners, lint_settings=lint_settings, policy=policy)
            shutil.rmtree(tree.parent)
            return result

        errors = io.StringIO()
        with (mock.patch.object(sweep, "output_device", side_effect=device),
              mock.patch.object(sweep, "shrink_floor", side_effect=remove_target),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT),
              redirect_stderr(errors)):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)
        self.assertEqual(status, 0, errors.getvalue())
        self.assertNotIn("Errno", errors.getvalue())
        saved = self.saved_record()
        self.assertEqual(saved.free_bytes, 11 * GIB)
        self.assertEqual(saved.build_cache_bytes, 0)

    def test_the_removal_alert_excludes_a_target_removed_by_another_process(self) -> None:
        base = self.base()
        taken_tree, taken_output = cargo_target(base, "taken", APP, time.time() - 2 * DAY)
        other_tree, other_output = cargo_target(base, "other", DEMO, time.time() - DAY)
        _ = write(taken_output, GIB // 8)
        _ = write(other_output, GIB // 8)
        taken_bytes = sweep.directory_blocks(str(taken_tree.parent))
        other_bytes = sweep.directory_blocks(str(other_tree.parent))
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        shrink = sweep.shrink_floor

        def device(path: str) -> int:
            """Report one filesystem wherever TMPDIR is. A missing path still raises."""
            _ = os.stat(path)
            return base.stat().st_dev

        def remove_other_target(
            scan: sweep.Scan, total: int, budget: int, dry_run: bool,
            tree_targets: dict[str, str], trees: dict[str, list[str]],
            busy: dict[str, sweep.TargetUse | sweep.LockHeld | sweep.BuildHoldTarget | sweep.NoRecordedEvidence],
            removal_order: sweep.RemovalOrder, planned_at: float, owners: sweep.TargetOwnerIndex, *,
            lint_settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
            policy: sweep.MacFloor | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
        ) -> tuple[int, int, list[tuple[sweep.FloorTake, int]]]:
            result = shrink(scan, total, budget, dry_run, tree_targets, trees, busy, removal_order,
                            planned_at, owners, lint_settings=lint_settings, policy=policy)
            shutil.rmtree(other_tree.parent)
            return result

        with (mock.patch.object(sweep, "output_device", side_effect=device),
              mock.patch.object(sweep, "shrink_floor", side_effect=remove_other_target),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - GIB // 16, 11 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(taken_output.exists())
        message = cast(str, send.call_args.args[0])
        self.assertIn(f"removed {sweep.gib(taken_bytes)} of build caches", message)
        self.assertNotIn(f"removed {sweep.gib(taken_bytes + other_bytes)} of build caches", message)
        self.assertIn("removed more than", message)
        saved = self.saved_record()
        self.assertEqual(saved.free_bytes, 11 * GIB)
        self.assertEqual(saved.build_cache_bytes, sweep.directory_blocks(str(taken_tree.parent)))

    def test_a_dry_floor_pass_reports_an_unreadable_recorded_root_index(self) -> None:
        base = self.base()
        tree, payload = cargo_target(base, "protected", APP, time.time() - DAY)
        _ = build_index.index_path().write_text("broken database")
        errors = io.StringIO()
        with redirect_stderr(errors):
            status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertIn("recorded output roots unavailable", errors.getvalue())
        self.assertIn(f"protected {tree.parent}: no recorded evidence", text)
        self.assertEqual(reported_floor_targets(text), set())
        self.assertTrue(payload.exists())

    def test_protected_sizes_exclude_recorded_children_and_count_shared_files_once(self) -> None:
        base = self.base()
        outer, own = cargo_target(base, "outer", APP, time.time() - DAY)
        nested, nested_file = cargo_target(outer.parent, "nested", DEMO, time.time() - DAY)
        sibling, sibling_file = cargo_target(base, "sibling", TOOL, time.time() - DAY)
        self.root_row(nested.parent.parent, nested.parent, nested.parent)
        os.link(own, outer / "same-file")
        os.link(own, sibling / "shared-file")
        roots, outputs = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        measured = dict(sweep.measure_protected_folders(
            frozenset(item.path for item in outputs.no_recorded_evidence), frozenset(roots)))
        exclusive = sum(path.stat().st_blocks * 512 for path in
                        (own, outer / ".fingerprint" / f"outer-{APP}" / "bin-outer.json", outer.parent / sweep.RUSTC_INFO,
                         sibling_file, sibling / ".fingerprint" / f"sibling-{TOOL}" / "bin-sibling.json",
                         sibling.parent / sweep.RUSTC_INFO))
        self.assertEqual(set(measured), {str(outer.parent), str(sibling.parent)})
        self.assertEqual(sum(measured.values()), exclusive)
        self.assertTrue(nested_file.exists())

    def test_low_space_alert_lists_protected_folders_by_size(self) -> None:
        base = self.base()
        small_tree, small = cargo_target(base, "small", APP, time.time() - DAY)
        large_tree, large = cargo_target(base, "large", DEMO, time.time() - DAY)
        _ = write(large_tree / "unclaimed", 64 * 1024 * 1024)
        message = self.protected_report(base)
        lines = [line for line in message.splitlines() if ": holds " in line]
        self.assertEqual(lines, [f"{large_tree.parent}: holds 0.1 GiB.",
                                 f"{small_tree.parent}: holds 0.0 GiB."])
        self.assertIn("no recorded evidence", message)
        self.assertTrue(small.exists())
        self.assertTrue(large.exists())

    def test_protected_folder_is_not_a_floor_holder_in_a_low_space_pass(self) -> None:
        base = self.base()
        protected_tree, protected_file = cargo_target(base, "protected", APP, time.time() - DAY)
        busy_tree, busy_file = cargo_target(base, "busy", DEMO, time.time() - DAY)
        protected, busy = str(protected_tree.parent), str(busy_tree.parent)
        self.root_row(busy_tree.parent.parent, busy_tree.parent, busy_tree.parent)
        use = sweep.TargetUse(62001, "maps build output")
        with (mock.patch.object(sweep, "process_uses", return_value={protected: use, busy: use}),
              mock.patch.object(sweep, "scan_floor_roots", wraps=sweep.scan_floor_roots) as scan,
              mock.patch.object(sweep, "shrink_floor", wraps=sweep.shrink_floor) as shrink):
            status, printed = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(list(cast(sweep.DiscoveredTargetRoots, scan.call_args.args[0])), [])
        in_use = cast(dict[str, sweep.TargetUse | sweep.LockHeld | sweep.NoRecordedEvidence], shrink.call_args.args[6])
        self.assertEqual(in_use, {busy: use, protected: sweep.NoRecordedEvidence(protected)})
        self.assertIn(f"protected {protected}: no recorded evidence", printed)
        self.assertIn("0 target dirs (0.0 GiB), 2 left alone", printed)
        self.assertNotIn(f"{protected} left alone", printed)
        self.assertNotIn("the floor would take", printed)
        holders = sweep.floor_holders(in_use, {})
        self.assertEqual([holder.path for holder in holders], [busy])
        message = self.protected_report(base, holders)
        in_use_lines, protected_lines = message.split("These folders have no recorded evidence.", 1)
        self.assertIn(f"{protected}: holds 0.0 GiB.", protected_lines)
        self.assertIn("These target dirs are in use", in_use_lines)
        self.assertNotIn(protected, in_use_lines)
        holder_messages = [sweep.holder_message_text(holder, 0, 10 * GIB) for holder in holders]
        self.assertEqual(len(holder_messages), 1)
        self.assertTrue(all(protected not in text for text in holder_messages))
        self.assertTrue(protected_file.exists())
        self.assertTrue(busy_file.exists())

    def test_unavailable_index_uses_saved_roots_after_the_checkout_is_gone(self) -> None:
        base = self.base()
        tree, payload = cargo_target(base, "cached", APP, time.time() - DAY)
        old_tree, old_payload = cargo_target(base, "historical", DEMO, time.time() - 2 * DAY)
        workspace = base / "gone-checkout"
        workspace.mkdir()
        mapping = CargoOutputRoots(workspace, tree.parent, tree.parent)
        historical = CargoOutputRoots(workspace, old_tree.parent, old_tree.parent)
        sweep.save_cargo_output_roots(sweep.CargoOutputIndex((mapping,), history=(historical,)))
        workspace.rmdir()
        index = build_index.index_path()
        index.parent.mkdir(parents=True, exist_ok=True)
        _ = index.write_text("not a SQLite database")
        status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(reported_floor_targets(text), {str(tree.parent), str(old_tree.parent)})
        self.assertTrue(payload.exists())
        self.assertTrue(old_payload.exists())

    def test_recorded_roots_outside_the_search_root_keep_ci_pair_exclusion(self) -> None:
        base = self.base()
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        target_tree, target_file = cargo_target(outside, "artifacts", APP, time.time() - DAY)
        build_tree, build_file = cargo_target(outside, "intermediates", DEMO, time.time() - DAY)
        idle_tree, idle_file = cargo_target(outside, "idle", TOOL, time.time() - DAY)
        self.root_row(base / "workspace", target_tree.parent, build_tree.parent)
        self.root_row(base / "other", idle_tree.parent, idle_tree.parent)
        _ = write_config_fixture(conf.path("lint"), f"sweep_ci_targets={build_tree.parent}\n")
        status, text = self.hold(base, 10 * GIB, [0], dry_run=True, recorded=False)
        self.assertEqual(status, 0)
        self.assertEqual(reported_floor_targets(text), {str(idle_tree.parent)})
        self.assertTrue(all(path.exists() for path in (target_file, build_file, idle_file)))

    def test_output_owners_keep_mapping_order_and_exclude_neighbor_paths(self) -> None:
        base = self.base()
        outer = CargoOutputRoots(base / "outer", base / "artifacts", base / "build")
        neighbor = CargoOutputRoots(base / "neighbor", base / "artifacts-old", base / "build-old")
        inner = CargoOutputRoots(base / "inner", base / "build" / "nested", base / "build" / "nested")
        index = sweep.CargoOutputIndex((outer, neighbor, inner))
        self.assertEqual(index.mappings_for(inner.build_directory / "debug"), (outer, inner))
        self.assertEqual(index.mappings_for(neighbor.target_directory / "release"), (neighbor,))
        self.assertEqual(index.mappings_for(base / "unrelated"), ())

    def test_linked_output_roots_follow_transitive_mappings_and_current_directories(self) -> None:
        base = self.base()
        roots = [base / name for name in ("target", "build", "shared", "other")]
        for root in roots:
            root.mkdir()
        target, build, shared, other = roots
        index = sweep.CargoOutputIndex((
            CargoOutputRoots(base / "one", target, build),
            CargoOutputRoots(base / "two", shared, other),
            CargoOutputRoots(base / "bridge", build, shared),
        ))
        self.assertEqual(index.roots_for(str(target)), tuple(sorted(map(str, roots))))
        shared.rmdir()
        self.assertEqual(index.roots_for(str(other)), tuple(sorted(map(str, (target, build, other)))))
        shared.mkdir()
        self.assertEqual(index.roots_for(str(build)), tuple(sorted(map(str, roots))))

    def test_recorded_pairs_precede_saved_pairs_and_dry_discovery_keeps_the_cache(self) -> None:
        base = self.base()
        workspace = base / "workspace"
        pairs = tuple(CargoOutputRoots(workspace, base / (name + "-target"), base / (name + "-build"))
                      for name in ("recorded-one", "recorded-two", "cached", "historical"))
        first, second, cached, historical = pairs
        for pair in pairs:
            pair.target_directory.mkdir()
            pair.build_directory.mkdir()
        self.root_row(workspace, first.target_directory, first.build_directory)
        self.root_row(workspace, second.target_directory, second.build_directory)
        sweep.save_cargo_output_roots(sweep.CargoOutputIndex((cached,), history=(historical,)))
        before = sweep.cargo_output_cache_path().read_bytes()
        roots, index = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        self.assertEqual(index.workspaces, pairs)
        self.assertEqual(set(roots), {str(root) for pair in pairs
                                     for root in (pair.target_directory, pair.build_directory)})
        for pair in (first, second):
            self.assertEqual(set(index.roots_for(str(pair.target_directory))),
                             {str(pair.target_directory), str(pair.build_directory)})
        self.assertEqual(sweep.cargo_output_cache_path().read_bytes(), before)

    def test_many_workspaces_keep_their_own_output_pairs(self) -> None:
        base = self.base()
        pairs: list[CargoOutputRoots] = []
        for number in range(32):
            workspace = base / f"workspace-{number:02}"
            target, build = workspace / "target", workspace / "intermediates"
            target.mkdir(parents=True)
            build.mkdir()
            self.root_row(workspace, target, build)
            pairs.append(CargoOutputRoots(workspace, target, build))
        roots, index = sweep.discover_cargo_outputs([str(base)], lint_settings=conf.read("lint"))
        self.assertEqual(index.workspaces, tuple(pairs))
        self.assertEqual(set(roots), {str(root) for pair in pairs
                                     for root in (pair.target_directory, pair.build_directory)})
        for pair in pairs:
            self.assertEqual(set(index.roots_for(str(pair.target_directory))),
                             {str(pair.target_directory), str(pair.build_directory)})

    def test_floor_fixture_supports_repeated_capacity_reads_during_the_scan(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "repo", APP, time.time() - DAY)
        original_scan = sweep.scan_floor_roots
        readings: list[int] = []

        def scan(roots: list[str], trees: dict[str, list[str]], *,
                 lint_settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> sweep.Scan:
            readings.extend(sweep.free_bytes(str(base)) for _ in range(8))
            result = original_scan(roots, trees, lint_settings=lint_settings)
            readings.extend(sweep.free_bytes(str(base)) for _ in range(8))
            return result

        with mock.patch.object(sweep, "scan_floor_roots", side_effect=scan):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        self.assertTrue(sweep.floor_record_path().exists())
        self.assertEqual(readings, [10 * GIB - 1] * 16)

    def paired_output(self, base: Path) -> tuple[CargoOutputRoots, Path, Path]:
        workspace = base / "workspace"
        target = workspace / "target"
        build = base / "intermediates"
        own = unit(build / "debug", "deps", "app", APP, time.time() - 3 * DAY)
        copy = write(target / "debug" / "app", own.stat().st_size)
        _ = dep_info(copy)
        _ = write(build / sweep.RUSTC_INFO, 0)
        _ = write(workspace / "Cargo.toml", 0)
        mapping = CargoOutputRoots(workspace, target, build)
        self.root_row(workspace, target, build)
        token = sweep.OUTPUT_ROOTS.set(sweep.CargoOutputIndex((mapping,)))
        self.addCleanup(sweep.OUTPUT_ROOTS.reset, token)
        return mapping, own, copy

    def test_external_build_directory_pairs_unit_and_artifact(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        roots = [str(mapping.target_directory), str(mapping.build_directory)]
        trees = {root: sweep.build_trees(root) for root in roots}
        scan = sweep.scan_floor_roots(roots, trees)
        self.assertEqual(len(scan.groups), 1)
        group, = scan.groups
        self.assertIn(str(own), group.entries)
        self.assertIn(str(copy), group.entries)
        self.assertIn(str(copy.with_suffix(".d")), group.entries)
        self.assertEqual(scan.whole_targets, [])
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(own.exists())
        self.assertFalse(copy.exists())
        deleted = cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text()))
        self.assertEqual(set(deleted), set(roots))

    def test_paired_roots_without_fingerprints_keep_zero_time_units(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        fingerprint = own.parent.parent / ".fingerprint" / f"app-{APP}"
        shutil.rmtree(fingerprint)
        os.utime(own, (0, 0))
        second = write(copy.parent / "deps" / f"app-{APP}")
        (copy.parent / ".fingerprint").mkdir()
        for path in (second, copy, copy.with_suffix(".d")):
            os.utime(path, (0, 0))
        roots = [str(mapping.build_directory), str(mapping.target_directory)]
        scan = sweep.scan_floor_roots(roots, {root: sweep.build_trees(root) for root in roots})
        group, = scan.groups
        self.assertTrue({str(own), str(second), str(copy)}.issubset(group.entries))
        self.assertEqual((group.last_used, group.compiled), (0, 0))

    def test_changed_fingerprint_keeps_its_target_after_an_older_target(self) -> None:
        base = self.base()
        now = time.time()
        first, first_output = cargo_target(base, "first", APP, now - 6 * 3600)
        second, second_output = cargo_target(base, "second", TOOL, now - 4 * 3600)
        fingerprint = first / ".fingerprint" / f"first-{APP}"
        os.utime(fingerprint, (now, now - 3600))
        roots = [str(first.parent), str(second.parent)]
        scan = sweep.scan_floor_roots(roots, {root: sweep.build_trees(root) for root in roots})
        ordered = sorted(scan.groups, key=lambda group: sweep.unit_age(group, now))
        self.assertEqual([str(second_output) in group.entries for group in ordered], [True, False])
        self.assertIn(str(first_output), ordered[1].entries)

    def test_selected_unit_frees_both_devices_but_credits_only_the_shortfall_device(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        other_tree, other = cargo_target(base, "other", TOOL, time.time() - DAY)
        roots = [str(mapping.build_directory), str(mapping.target_directory), str(other_tree.parent)]
        group = sweep.Group("unit", str(own.parent.parent), [str(own), str(copy)],
                            [(2, 1), (1, 2)], time.time() - DAY)
        unselected = sweep.Group("unit", str(other_tree), [str(other)], [(1, 3)], time.time() - DAY)
        scan = sweep.Scan(blocks={(2, 1): 20, (1, 2): 40, (1, 3): 100},
                          links={(2, 1): 1, (1, 2): 1, (1, 3): 1}, groups=[group, unselected])
        trees = {root: sweep.build_trees(root) for root in roots}
        tree_targets = {str(own.parent.parent): str(mapping.build_directory), str(other_tree): str(other_tree.parent)}
        token = sweep.FLOOR_FILESYSTEM.set(sweep.FilesystemFloor(2, str(mapping.build_directory), 20,
                                                               (str(mapping.build_directory),), False))
        self.addCleanup(sweep.FLOOR_FILESYSTEM.reset, token)
        def take_pair(selected: sweep.FloorTake, _scan: sweep.Scan, _remaining: dict[sweep.InodeKey, int],
                      _dry_run: bool, *, lint_settings: conf.ConfigRead) -> sweep.ConfirmedDeletion:
            result = sweep.remove([selected], lint_settings=lint_settings)
            self.assertEqual(result.failures, 0)
            return sweep.ConfirmedDeletion(60, 0, result.removed, {1: 40, 2: 20})
        with (mock.patch.object(sweep, "reclaim", side_effect=take_pair),
              mock.patch.object(sweep, "process_use", return_value={}), redirect_stdout(io.StringIO())):
            left, failures, taken = sweep.shrink_floor(scan, 20, 0, False, tree_targets, trees, {},
                lambda item: (item.last_used,), time.time() + 5, sweep.TargetOwnerIndex(roots))
        self.assertEqual((left, failures), (0, 0))
        self.assertEqual(taken, [(group, 20)])
        self.assertFalse(own.exists())
        self.assertFalse(copy.exists())
        self.assertTrue(other.exists())

    def test_held_checkout_protects_both_output_roots(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        since = time.time()
        self.write_build_hold(base, since)
        record = cast(sessions.SessionRecord, cast(object, {
            "sessionId": "holder-session", "pid": 4242, "name": "build-speed",
        }))
        proc = base / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cwd").symlink_to(mapping.workspace_root, target_is_directory=True)
        with (mock.patch.object(sessions, "live_sessions", return_value=[record]),
              mock.patch.object(sweep, "PROC_DIR", str(proc))):
            held = sweep.build_hold_targets([str(mapping.target_directory), str(mapping.build_directory)], {})
        self.assertEqual(set(held), {str(mapping.target_directory), str(mapping.build_directory)})
        self.assertTrue(own.exists())
        self.assertTrue(copy.exists())

    def test_interrupted_pair_keeps_pending_artifact_paths(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        roots = [str(mapping.build_directory), str(mapping.target_directory)]
        trees = {root: sweep.build_trees(root) for root in roots}
        scan = sweep.scan_floor_roots(roots, trees)
        group, = scan.groups
        original = sweep.remove_entry
        stopped = False
        def stop_at_artifact(path: str) -> sweep.ConfirmedDeletion:
            nonlocal stopped
            if Path(path).is_relative_to(mapping.target_directory):
                stopped = True
                raise KeyboardInterrupt("stop after intermediates")
            return original(path)
        with (mock.patch.object(sweep, "remove_entry", side_effect=stop_at_artifact),
              self.assertRaises(KeyboardInterrupt)):
            with sweep.recoverable_trim(str(mapping.build_directory), "floor take", scan, False,
                                        sweep.TargetOwnerIndex(roots)):
                _ = sweep.reclaim(group, scan, dict(scan.links), False)
        self.assertTrue(stopped)
        self.assertFalse(own.exists())
        self.assertTrue(copy.exists())
        intent = sweep.read_trim_intent(sweep.trim_intent_path(str(mapping.build_directory)))
        self.assertIn(str(copy), intent.group_paths)
        with mock.patch.object(sweep, "process_uses", return_value={}):
            sweep.recover_pending_trims()
            sweep.recover_pending_trims()
        self.assertFalse(copy.exists())
        row, = self.trim_rows()
        self.assertEqual(row["cause"], "floor take")
        self.assertFalse(sweep.trim_intent_path(str(mapping.build_directory)).exists())

    def prepare_pair_take(self, mapping: CargoOutputRoots) -> sweep.TrimTake:
        roots = [str(mapping.build_directory), str(mapping.target_directory)]
        scan = sweep.scan_floor_roots(roots, {root: sweep.build_trees(root) for root in roots})
        group, = scan.groups
        take = sweep.TrimTake(str(mapping.build_directory), "floor take", 1, output_roots=tuple(roots))
        sweep.trim_intent_path(take.target).parent.mkdir(parents=True, exist_ok=True)
        take.prepare(group)
        return take

    def test_changed_pair_entries_clear_the_old_intent_and_allow_a_new_take(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        take = self.prepare_pair_take(mapping)
        for path in take.group_paths:
            before = os.lstat(path)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        recovered = sweep.read_trim_intent(sweep.trim_intent_path(take.target))
        _ = recovered.recover()
        saved = sweep.read_trim_intent(sweep.trim_intent_path(take.target))
        self.assertEqual(saved.pending, [])
        self.assertEqual(saved.group_paths, [])
        self.assertTrue(own.exists())
        self.assertTrue(copy.exists())
        self.assertTrue(recovered.publish())
        self.assertFalse(sweep.trim_intent_path(take.target).exists())
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertFalse(own.exists())
        self.assertFalse(copy.exists())
        self.assertEqual(len(self.trim_rows()), 1)

    def test_pair_recovery_keeps_a_changed_descendant_with_an_unchanged_parent(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        take = self.prepare_pair_take(mapping)
        fingerprint = own.parent.parent / ".fingerprint" / f"app-{APP}"
        changed = fingerprint / "bin-app.json"
        parent_version = sweep.output_version(fingerprint.stat())
        _ = changed.write_text('{"new": "build output"}')
        self.assertEqual(sweep.output_version(fingerprint.stat()), parent_version)
        recovered = sweep.read_trim_intent(sweep.trim_intent_path(take.target))
        _ = recovered.recover()
        self.assertTrue(changed.exists())
        self.assertEqual(changed.read_text(), '{"new": "build output"}')
        self.assertFalse(own.exists())
        self.assertFalse(copy.exists())
        self.assertEqual(recovered.pending, [])
        self.assertTrue(recovered.publish())
        self.assertFalse(sweep.trim_intent_path(take.target).exists())

    def test_ci_exclusion_keeps_both_output_roots_and_takes_only_idle_output(self) -> None:
        for dry_run in (True, False):
            for excluded in ("target", "build"):
                with self.subTest(dry_run=dry_run, excluded=excluded):
                    base = self.base()
                    mapping, own, copy = self.paired_output(base)
                    _, idle = cargo_target(base, "idle", TOOL, time.time() - DAY)
                    ci = mapping.target_directory if excluded == "target" else mapping.build_directory
                    _ = write_config_fixture(conf.path("lint"), f"sweep_ci_targets={ci}\n")
                    status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], dry_run, recorded=True)
                    self.assertEqual(status, 0)
                    self.assertTrue(own.exists())
                    self.assertTrue(copy.exists())
                    self.assertEqual(idle.exists(), dry_run)
                    lines = [line for line in text.splitlines() if "the floor " in line and " from " in line]
                    self.assertEqual(len(lines), 1)
                    self.assertIn(str(idle.parents[2]), lines[0])

    def test_filesystem_floors_skips_a_target_removed_after_the_directory_check(self) -> None:
        base = self.base()
        target = base / "target"
        target.mkdir()
        isdir = os.path.isdir

        def remove_after_check(path: str) -> bool:
            result = isdir(path)
            if path == str(target):
                target.rmdir()
            return result

        def device(path: str) -> int:
            return os.stat(path).st_dev

        with (mock.patch.object(os.path, "isdir", side_effect=remove_after_check),
              mock.patch.object(sweep, "output_device", side_effect=device)):
            floors = sweep.filesystem_floors(10 * GIB, [str(target)], lint_settings=conf.read("lint"))
        self.assertTrue(all(str(target) not in floor.targets for floor in floors))

    def test_secondary_floor_uses_configured_percent_and_groups_each_device(self) -> None:
        base = self.base()
        first = base / "first"
        second = base / "second"
        first.mkdir()
        second.mkdir()
        _ = write_config_fixture(conf.path("lint"), "sweep_free_floor_percent=25\n")
        usage = os.statvfs_result((4096, 4096, 1000, 500, 500, 0, 0, 0, 0, 255))
        def device(path: str) -> int:
            return 2 if path == str(second) else 1
        with (mock.patch.object(sweep, "output_device", side_effect=device),
              mock.patch.object(os, "statvfs", return_value=usage)):
            floors = sweep.filesystem_floors(12345, [str(first), str(second)], lint_settings=conf.read("lint"))
        self.assertEqual([(item.device, item.floor_bytes, item.targets) for item in floors],
                         [(1, 12345, (str(first),)), (2, 1000 * 4096 // 4, (str(second),))])
        self.assertEqual([item.root_device for item in floors], [True, False])

    def test_missing_or_bad_floor_percent_refuses_once(self) -> None:
        for value in ("", "bad", "-1", "101"):
            with self.subTest(value=value):
                base = self.base()
                _ = write_config_fixture(conf.path("lint"), f"sweep_free_floor_percent={value}\n")
                errors = io.StringIO()
                with (redirect_stderr(errors),
                      mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
                    self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB], recorded=True)[0], 2)
                send.assert_called()
                cast(mock.Mock, sweep.send_phone_push).assert_not_called()
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("sweep_free_floor_percent", errors.getvalue())

    def test_busy_floor_lock_does_not_stop_another_filesystem(self) -> None:
        base = self.base()
        one = sweep.FilesystemFloor(1, str(base), 100, (), True)
        two = sweep.FilesystemFloor(2, str(base), 200, (), False)
        root_record = sweep.floor_record_path()
        root_record.parent.mkdir(parents=True, exist_ok=True)
        with (mock.patch.object(sweep, "discover_cargo_outputs", return_value=([], sweep.CargoOutputIndex())),
              mock.patch.object(sweep, "filesystem_floors", return_value=[one, two]),
              mock.patch.object(sweep, "free_bytes", return_value=0),
              mock.patch.object(sweep, "alert_floor_lock"),
              mock.patch.object(sweep, "send_floor_alert", return_value=FAILED_NATEDEV_ALERT),
              root_record.with_suffix(".lock").open("a+") as lock,
              redirect_stdout(io.StringIO())):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(sweep.hold_floor(100, False), 0)
        self.assertFalse(root_record.exists())
        self.assertTrue(root_record.with_name(two.record_stem + ".json").exists())

    def test_paired_roots_with_a_shared_inode_report_no_independent_takeable_bytes(self) -> None:
        base = self.base()
        mapping, own, copy = self.paired_output(base)
        copy.unlink()
        os.link(own, copy)
        roots = [str(mapping.target_directory), str(mapping.build_directory)]
        scan = sweep.scan_floor_roots(roots, {root: sweep.build_trees(root) for root in roots})
        rows = sweep.floor_scan_targets(scan, roots, dict.fromkeys(roots, 0.0), {})
        row = next(row for row in rows if row["folder"] == str(mapping.target_directory))
        self.assertEqual(row.get("takeable_bytes"), copy.with_suffix(".d").stat().st_blocks * 512)
        self.assertEqual(row.get("reason"), "links outside the target")

    def test_floor_records_and_alerts_are_isolated_by_device(self) -> None:
        base = self.base()
        paths: list[Path] = []
        for device, root in ((1, True), (2, False)):
            token = sweep.FLOOR_FILESYSTEM.set(sweep.FilesystemFloor(device, str(base), 100, (), root))
            try:
                path = sweep.floor_record_path()
                paths.append(path)
                with sweep.locked_floor_record(path):
                    sweep.write_floor_record(path, sweep.FloorRecord(
                        time.time(), device, 0, sweep.DeliveredAlert(float(device)), sweep.NoDeliveredAlert(),
                        filesystem=str(device)))
            finally:
                sweep.FLOOR_FILESYSTEM.reset(token)
        self.assertNotEqual(paths[0], paths[1])
        for device, path in enumerate(paths, 1):
            saved = sweep.read_floor_record(path)
            self.assertIsInstance(saved, sweep.FloorRecord)
            assert isinstance(saved, sweep.FloorRecord)
            self.assertEqual((saved.free_bytes, saved.alert_history), (device, sweep.DeliveredAlert(float(device))))
            self.assertTrue(path.with_name(path.stem + "-alert.lock").exists())

    def test_cold_rebuild_joins_an_external_removal_to_its_saved_workspace(self) -> None:
        base = self.base()
        mapping, _, _ = self.paired_output(base)
        sweep.save_cargo_output_roots(sweep.CargoOutputIndex((mapping,)))
        folder = str(mapping.workspace_root)
        steps = [sweep.RebuildStep(folder, folder, 1, 2, 0, 0), sweep.RebuildStep(folder, folder, 5, 6, 50, 3)]
        cold = sweep.rebuild_causes(steps, [sweep.SweepRemoval(3, str(mapping.build_directory), "floor take")], 0, 10)
        self.assertEqual([item.cause for item in cold], ["floor take"])

    def test_floor_sources_have_no_fixed_build_drive_path(self) -> None:
        source = Path(sweep.__file__).read_text()
        self.assertNotIn('"/build/', source)
        self.assertNotIn("'/build/", source)

    def residue_target(self, base: Path, name: str, used: float, *, stamp: bool = True) -> tuple[Path, Path]:
        root = base / name / "target"
        payload = write(root / "doc" / "index.html")
        _ = (root / sweep.RUSTC_INFO).write_text("{}")
        if stamp:
            _ = (root / sweep.USE_STAMP).write_text("")
        for path in root.rglob("*"):
            os.utime(path, (used, used))
        return root, payload

    def test_nested_unit_take_has_its_own_intent_and_trim_row(self) -> None:
        base = self.base()
        outer_tree, outer_output = cargo_target(base, "outer", APP, time.time() - DAY)
        nested_tree, nested_output = cargo_target(outer_tree.parent, "proof", DEMO, time.time() - 3 * DAY)
        original_remove = sweep.remove
        checked: list[str] = []

        def inspect_intent(groups: Sequence[sweep.FloorTake], *, lint_settings: conf.ConfigRead) -> sweep.ConfirmedDeletion:
            if any(isinstance(group, sweep.Group) and group.build_tree == str(nested_tree) for group in groups):
                intent = sweep.read_trim_intent(sweep.trim_intent_path(str(nested_tree.parent)))
                self.assertEqual(intent.target, str(nested_tree.parent))
                self.assertTrue(intent.pending)
                checked.append(intent.target)
            return original_remove(groups, lint_settings=lint_settings)

        with mock.patch.object(sweep, "remove", side_effect=inspect_intent):
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertEqual(checked, [str(nested_tree.parent)])
        self.assertFalse(nested_output.exists())
        self.assertTrue(outer_output.exists())
        row, = self.trim_rows()
        self.assertEqual(row["folder"], str(nested_tree.parent))

    def test_interrupted_take_records_its_target_before_the_first_unlink(self) -> None:
        for whole in (False, True):
            with self.subTest(whole=whole):
                base = self.base()
                if whole:
                    root, _ = self.residue_target(base, "docs", time.time() - DAY)
                else:
                    tree, _ = cargo_target(base, "units", APP, time.time() - DAY)
                    root = tree.parent
                original_remove_entry = sweep.remove_entry

                def stop_after_unlink(path: str) -> sweep.ConfirmedDeletion:
                    account = original_remove_entry(path)
                    if account.removed:
                        raise KeyboardInterrupt("stopped after the first unlink")
                    return account

                with mock.patch.object(sweep, "remove_entry", side_effect=stop_after_unlink):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
                self.assertIn(str(root), cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text())))
                sweep.recover_pending_trims()
                self.assertIn(str(root), cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text())))
                self.assertFalse(sweep.trim_intent_path(str(root)).exists())

    def test_held_style_sync_lock_keeps_a_target_without_units(self) -> None:
        base = self.base()
        root, payload = self.residue_target(base, "style", time.time() - DAY)
        style_root = root.with_name(scratch.STYLE_TARGET_NAME)
        _ = root.rename(style_root)
        payload = style_root / payload.relative_to(root)
        lock = write(style_root / scratch.STYLE_LOCK_NAME, 0)
        with lock.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertTrue(payload.exists())
        self.assertEqual(self.trim_rows(), [])

    def test_orphan_only_target_meets_the_shortfall_before_older_units(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                base = self.base()
                _, old_unit = cargo_target(base, "old", APP, time.time() - 3 * DAY)
                root, residue = self.residue_target(base, "newer", time.time() - DAY)
                tree = root / "debug"
                (tree / ".fingerprint").mkdir(parents=True)
                orphan = write(tree / "gone")
                info = dep_info(orphan)
                self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], dry_run, recorded=True)[0], 0)
                self.assertTrue(old_unit.exists())
                self.assertTrue(residue.exists())
                self.assertEqual((orphan.exists(), info.exists()), (dry_run, dry_run))

    def test_failed_whole_payload_delete_keeps_markers_for_the_next_run(self) -> None:
        base = self.base()
        send = self.enterContext(mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT))
        root, payload = self.residue_target(base, "docs", time.time() - DAY)
        _ = (root / sweep.CACHE_TAG).write_text(sweep.CARGO_TAG_LINE + "\n")
        markers = [root / sweep.RUSTC_INFO, root / sweep.CACHE_TAG]
        original_remove_entry = sweep.remove_entry

        def fail_payload(path: str) -> sweep.ConfirmedDeletion:
            if Path(path) == payload:
                return sweep.ConfirmedDeletion(0, 1)
            return original_remove_entry(path)

        with mock.patch.object(sweep, "remove_entry", side_effect=fail_payload):
            _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        send.assert_called()
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertTrue(payload.exists())
        self.assertTrue(all(marker.exists() for marker in markers))
        self.assertIn(str(root), sweep.target_dirs([str(base)]))
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertFalse(payload.exists())
        self.assertFalse(any(marker.exists() for marker in markers))
        self.assertTrue(root.is_dir())

    def test_cargo_output_inside_a_build_unit_is_counted_and_rechecked_with_it(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "builder", APP, time.time() - DAY)
        nested = tree / "build" / ("app-" + APP) / "out" / "target"
        _ = write(nested / sweep.RUSTC_INFO, 2)
        payload = write(nested / "debug" / "large", 4 * SIZE)
        scan = sweep.scan_roots([str(tree.parent)], [str(tree)])
        group, = scan.groups
        key = (payload.stat().st_dev, payload.stat().st_ino)
        self.assertIn(key, group.inodes)
        self.assertEqual(sum(scan.blocks.values()), sweep.directory_blocks(str(tree.parent)))
        self.assertEqual(sweep.target_dirs([str(base)]), [str(tree.parent)])
        self.assertTrue(sweep.group_unchanged(group))
        _ = payload.write_bytes(b"changed build output")
        self.assertFalse(sweep.group_unchanged(group))

    def test_whole_doc_target_precedes_units_and_stops_at_the_shortfall(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                base = self.base()
                now = time.time()
                root, payload = self.residue_target(base, "old", now - 3 * DAY)
                _, newer = cargo_target(base, "newer", APP, now - DAY)
                status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], dry_run, recorded=True)
                self.assertEqual(status, 0)
                lines = [line for line in text.splitlines() if "the floor " in line and " from " in line]
                self.assertEqual(len(lines), 1)
                self.assertIn(f" from {root}, last used ", lines[0])
                self.assertIn("would take" if dry_run else "took", lines[0])
                self.assertEqual(payload.exists(), dry_run)
                self.assertTrue(newer.exists())
                self.assertTrue(root.is_dir())
                if not dry_run:
                    files = [path for path in root.rglob("*") if path.is_file()]
                    self.assertTrue(all(path.name in (*sweep.LOCK_NAMES, ".claude-busy", ".lock")
                                        for path in files), files)
                    row, = self.trim_rows()
                    self.assertEqual((row["folder"], row["cause"]), (str(root), "floor take"))
                    self.assertGreater(cast(int, row["bytes_freed"]), 0)
                    deleted = cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text()))
                    self.assertIn(str(root), deleted)

    def test_whole_target_keeps_every_lock_inode_and_respects_rustdoc(self) -> None:
        for blocked in (True, False):
            with self.subTest(blocked=blocked):
                base = self.base()
                root, payload = self.residue_target(base, "docs", time.time() - DAY)
                locks = [root / ".claude-busy", root / "doc" / ".lock"]
                locks.extend(root / "debug" / name for name in sweep.LOCK_NAMES)
                for lock in locks:
                    _ = write(lock, 0)
                identities = {path: path.stat().st_ino for path in locks}
                with (root / "doc" / ".lock").open("r+") as handle:
                    if blocked:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
                self.assertEqual(status, 0)
                self.assertEqual(payload.exists(), blocked)
                self.assertEqual({path: path.stat().st_ino for path in locks}, identities)
                self.assertEqual(len(self.trim_rows()), int(not blocked))

    def test_busy_whole_target_is_kept_while_an_idle_older_one_meets_the_floor(self) -> None:
        base = self.base()
        now = time.time()
        held, kept = self.residue_target(base, "held", now - 4 * DAY)
        _, removed = self.residue_target(base, "idle", now - 3 * DAY)
        _, newer = cargo_target(base, "newer", APP, now - DAY)
        with scratch.removal_lock(held) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertTrue(kept.exists())
        self.assertFalse(removed.exists())
        self.assertTrue(newer.exists())

    def test_new_unstamped_residue_uses_entry_mtime_in_the_floor_order(self) -> None:
        base = self.base()
        _, fresh = self.residue_target(base, "fresh", time.time(), stamp=False)
        _, old = cargo_target(base, "old", APP, time.time() - DAY)
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())

    def test_nested_target_is_found_and_its_build_tree_is_not_owned_by_the_outer_target(self) -> None:
        base = self.base()
        outer, _ = self.residue_target(base, "outer", time.time() - 3 * DAY)
        tree, output = cargo_target(outer, "proof", APP, time.time() - DAY)
        self.assertEqual(set(sweep.target_dirs([str(base)])), {str(outer), str(tree.parent)})
        self.assertNotIn(str(tree), sweep.build_trees(str(outer)))
        self.assertIn(str(tree), sweep.build_trees(str(tree.parent)))
        scan = sweep.scan_roots([str(outer)], sweep.build_trees(str(outer)))
        self.assertEqual(scan.groups, [])
        self.assertNotIn((output.stat().st_dev, output.stat().st_ino), scan.blocks)

    def test_busy_nested_target_stays_while_outer_residue_is_taken(self) -> None:
        base = self.base()
        outer, residue = self.residue_target(base, "outer", time.time() - 3 * DAY)
        tree, nested = cargo_target(outer, "proof", APP, time.time() - DAY)
        with scratch.removal_lock(tree.parent) as acquired:
            self.assertTrue(acquired)
            status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(residue.exists())
        self.assertTrue(nested.exists())
        self.assertIn(f" from {outer}, last used ", text)
        self.assertEqual([row["folder"] for row in self.trim_rows()], [str(outer)])

    def test_nested_bytes_are_counted_once_before_and_after_the_take(self) -> None:
        base = self.base()
        outer, residue = self.residue_target(base, "outer", time.time() - 3 * DAY)
        tree, nested = cargo_target(outer, "proof", APP, time.time() - DAY)
        before = sweep.directory_blocks(str(outer))
        with scratch.removal_lock(tree.parent) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertFalse(residue.exists())
        self.assertTrue(nested.exists())
        recorded = self.saved_record()
        after = sweep.directory_blocks(str(outer))
        self.assertEqual(recorded.build_cache_bytes, after)
        scan_row, = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
        targets = cast(list[dict[str, object]], scan_row["idle_targets"])
        # Busy nested output is accounted outside the idle scan, never in its outer target.
        self.assertLess(sum(cast(int, target["bytes"]) for target in targets), before)

    def test_idle_nested_targets_have_one_owner_for_each_byte_in_both_measurements(self) -> None:
        base = self.base()
        outer, residue = self.residue_target(base, "outer", time.time() - 3 * DAY)
        tree, nested = cargo_target(outer, "proof", APP, time.time() - DAY)
        before = sweep.directory_blocks(str(outer))
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertFalse(residue.exists())
        self.assertTrue(nested.exists())
        scan_row, = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
        targets = cast(list[dict[str, object]], scan_row["idle_targets"])
        self.assertEqual({target["folder"] for target in targets}, {str(outer), str(tree.parent)})
        self.assertEqual(sum(cast(int, target["bytes"]) for target in targets), before)
        recorded = self.saved_record()
        self.assertEqual(recorded.build_cache_bytes, sweep.directory_blocks(str(outer)))

    def test_whole_take_rechecks_new_build_holds_process_use_stamps_and_output(self) -> None:
        for change in ("build hold", "process", "use stamp", "output"):
            with self.subTest(change=change):
                base = self.base()
                root, payload = self.residue_target(base, "docs", time.time() - DAY)
                original_scan = sweep.scan_floor_roots
                def change_after_scan(roots: list[str], trees: dict[str, list[str]], *, lint_settings: conf.ConfigRead) -> sweep.Scan:
                    result = original_scan(roots, trees, lint_settings=lint_settings)
                    if change == "build hold":
                        holds.return_value = {str(root): sweep.BuildHoldTarget("builder")}
                    elif change == "process":
                        uses.return_value = {str(root): sweep.TargetUse(42, "works here")}
                    elif change == "use stamp":
                        os.utime(root / sweep.USE_STAMP, (time.time() + 5, time.time() + 5))
                    else:
                        _ = payload.write_bytes(b"a build replaced this output")
                    return result
                with (mock.patch.object(sweep, "scan_floor_roots", side_effect=change_after_scan),
                      mock.patch.object(sweep, "build_hold_targets", wraps=sweep.build_hold_targets) as holds,
                      mock.patch.object(sweep, "process_uses", return_value={}) as uses):
                    status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
                self.assertEqual(status, 0)
                self.assertTrue(payload.exists())
                self.assertEqual(self.trim_rows(), [])

    def test_whole_take_rechecks_a_cargo_lock_acquired_after_the_scan(self) -> None:
        base = self.base()
        root, payload = self.residue_target(base, "docs", time.time() - DAY)
        tree = root / "debug"
        lock = write(tree / sweep.LOCK_NAMES[0], 0)
        original_scan = sweep.scan_floor_roots
        with lock.open("r+") as handle:
            def lock_after_scan(roots: list[str], trees: dict[str, list[str]], *, lint_settings: conf.ConfigRead) -> sweep.Scan:
                result = original_scan(roots, trees, lint_settings=lint_settings)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return result
            with mock.patch.object(sweep, "scan_floor_roots", side_effect=lock_after_scan):
                self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)
        self.assertTrue(payload.exists())
        self.assertEqual(self.trim_rows(), [])

    def test_stopped_whole_take_publishes_once_on_the_next_floor_run(self) -> None:
        base = self.base()
        root, payload = self.residue_target(base, "docs", time.time() - DAY)
        with (mock.patch.object(record, "sweep_trim", side_effect=OSError("stop before publication")),
              redirect_stderr(io.StringIO())):
            _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertFalse(payload.exists())
        self.assertEqual(self.trim_rows(), [])
        for _ in range(2):
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB], recorded=True)[0], 0)
            row, = self.trim_rows()
            self.assertEqual((row["folder"], row["cause"]), (str(root), "floor take"))

    def test_orphans_precede_an_older_whole_take(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                base = self.base()
                now = time.time()
                root, residue = self.residue_target(base, "whole", now - 3 * DAY)
                tree, unit_output = cargo_target(base, "newer", APP, now - DAY)
                orphan = write(tree / "gone")
                orphan_info = dep_info(orphan)
                shortfall = sweep.directory_blocks(str(root)) + 1
                status, text = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], dry_run, recorded=True)
                self.assertEqual(status, 0)
                lines = [line for line in text.splitlines() if "the floor " in line and " from " in line]
                self.assertEqual([line.split(" from ", 1)[1].split(", last used ", 1)[0] for line in lines],
                                 [str(tree.parent), str(root)])
                self.assertEqual((residue.exists(), orphan.exists(), orphan_info.exists()), (dry_run,) * 3)
                self.assertTrue(unit_output.exists())

    def test_interrupted_trim_append_is_recovered_as_one_whole_row(self) -> None:
        causes: tuple[Literal["budget take", "floor take"], ...] = ("budget take", "floor take")
        for tail in HISTORY_APPEND_STOPS:
            for cause in causes:
                with self.subTest(tail=tail, cause=cause):
                    base = self.base()
                    tree, output = cargo_target(base / "clone-\u0394", "shared", APP, time.time() - DAY)
                    deleted_at = 1_790_000_000.0
                    account = sweep.remove_entry(str(output))
                    take = sweep.TrimTake(str(tree.parent), cause, 0, at=deleted_at,
                                          bytes_freed=account.bytes, removed=account.removed)
                    take.save()
                    with stop_history_append(store, tail), redirect_stderr(io.StringIO()):
                        self.assertFalse(take.publish())
                    self.assertTrue(sweep.trim_intent_path(str(tree.parent)).exists())
                    for _ in range(2):
                        sweep.recover_pending_trims()
                    log = store.host_file(store.host_name(), deleted_at)
                    store.append_line(log, {"unrelated": True})
                    row, = self.trim_rows()
                    self.assertEqual((row["folder"], row["cause"], row["bytes_freed"], row["at"]),
                                     (str(tree.parent), cause, account.bytes, store.utc_iso(deleted_at)))
                    self.assertTrue(log.read_bytes().endswith(b"\n"))
                    self.assertFalse(sweep.trim_intent_path(str(tree.parent)).exists())

    def test_trim_retry_uses_the_whole_row_boundary_before_an_unfinished_old_tail(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        deleted_at = 1_790_000_000.0
        log = store.host_file(store.host_name(), deleted_at)
        store.append_line(log, {"unrelated": True})
        whole = log.read_bytes()
        with log.open("ab") as handle:
            _ = handle.write(b'{"stopped_old_row":"' + b"x" * 4096)
        account = sweep.remove_entry(str(output))
        take = sweep.TrimTake(str(tree.parent), "floor take", 0, at=deleted_at,
                              bytes_freed=account.bytes, removed=account.removed)
        take.save()
        intent = sweep.trim_intent_path(str(tree.parent))
        unlink = Path.unlink

        def stop_before_forgetting(file: Path, missing_ok: bool = False) -> None:
            if file == intent:
                raise InterruptedError("stop after publishing the trim")
            unlink(file, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", stop_before_forgetting), self.assertRaises(InterruptedError):
            _ = take.publish()
        sweep.recover_pending_trims()
        row, = self.trim_rows()
        self.assertEqual((row["folder"], row["bytes_freed"]), (str(tree.parent), account.bytes))
        self.assertTrue(log.read_bytes().startswith(whole))
        self.assertFalse(intent.exists())

    def pending_trim(self, target: Path, output: Path, at: float) -> sweep.TrimTake:
        observed = output.stat()
        take = sweep.TrimTake(str(target), "floor take", 1, at=at)
        take.pending = [sweep.PendingUnlink(str(output), observed.st_dev, observed.st_ino,
                                          observed.st_blocks * 512, observed.st_nlink, False)]
        take.group_paths = [str(output)]
        take.save()
        return take

    def test_recreated_renamed_and_linked_outputs_count_as_removed_without_reclaim_credit(self) -> None:
        for change in ("recreated", "renamed", "outside hard link"):
            with self.subTest(change=change):
                base = self.base()
                tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
                deleted_at = 1_790_000_000.0
                take = self.pending_trim(tree.parent, output, deleted_at)
                survivor = base / "saved-inode"
                if change == "outside hard link":
                    os.link(output, survivor)
                    # The intent must also retain the link count seen before the unlink.
                    take = self.pending_trim(tree.parent, output, deleted_at)
                    output.unlink()
                else:
                    _ = output.rename(survivor)
                    if change == "recreated":
                        _ = write(output)
                with mock.patch.object(time, "time", return_value=deleted_at + DAY):
                    recovered = take.recover()
                    self.assertIsInstance(recovered, sweep.UnconfirmedReclaim)
                    if not isinstance(recovered, sweep.UnconfirmedReclaim):
                        self.fail("a name change cannot confirm that its inode's blocks were reclaimed")
                    self.assertEqual((recovered.removed, recovered.paths), (1, (str(output),)))
                    self.assertEqual(take.unconfirmed_paths, [str(output)])
                    self.assertEqual(take.removed, 1, "the old name was removed even if Cargo reused it")
                    self.assertEqual(take.bytes_freed, 0, "a missing name does not prove that blocks were freed")
                    self.assertTrue(take.publish())
                row, = self.trim_rows()
                self.assertEqual((row["bytes_freed"], row["at"]), (0, store.utc_iso(deleted_at)))
                self.assertTrue(survivor.exists())
                self.assertEqual(output.exists(), change == "recreated")

    def test_recovery_publishes_saved_deletion_time_before_unlocking_the_target(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        _ = (tree / ".cargo-lock").touch()
        deleted_at = 1_790_000_000.0
        _ = self.pending_trim(tree.parent, output, deleted_at)
        output.unlink()
        unlock = sweep.release
        observed_unlocks: list[list[int]] = []

        def release_after_publication(held: list[int]) -> None:
            if held:
                row, = self.trim_rows()
                self.assertEqual(row["at"], store.utc_iso(deleted_at))
                self.assertFalse(sweep.trim_intent_path(str(tree.parent)).exists())
                observed_unlocks.append(list(held))
            unlock(held)

        with (mock.patch.object(sweep, "release", side_effect=release_after_publication),
              mock.patch.object(time, "time", return_value=deleted_at + DAY)):
            sweep.recover_pending_trims()
        self.assertEqual(len(observed_unlocks), 1)

    def test_unpublishable_intent_time_is_set_aside_before_a_later_take(self) -> None:
        for caller in ("recovery", "take"):
            with self.subTest(caller=caller):
                base = self.base()
                tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
                take = sweep.TrimTake(str(tree.parent), "floor take", 0,
                                      at=1790000000000, removed=1)
                take.save()
                intent = sweep.trim_intent_path(str(tree.parent))
                broken = intent.read_bytes()
                errors = io.StringIO()
                with redirect_stderr(errors):
                    if caller == "recovery":
                        sweep.recover_pending_trims()
                        sweep.recover_pending_trims()
                    self.assertEqual(self.take(base, tree.parent, "floor"), 0)
                damaged, = intent.parent.glob(intent.name + ".damaged-*")
                self.assertEqual(damaged.read_bytes(), broken)
                self.assertEqual(errors.getvalue().count("could not recover"), 1)
                self.assertFalse(intent.exists())
                self.assertFalse(output.exists())
                row, = self.trim_rows()
                self.assertEqual(row["folder"], str(tree.parent))

    def test_damaged_trim_outcome_names_the_preserved_intent(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "shared", APP, time.time() - DAY)
        intent = sweep.trim_intent_path(str(tree.parent))
        intent.parent.mkdir(parents=True)
        broken = b'{}'
        _ = intent.write_bytes(broken)
        with redirect_stderr(io.StringIO()):
            result = sweep.load_trim_for_recovery(intent)
        self.assertIsInstance(result, sweep.DamagedTrimIntentSetAside)
        if not isinstance(result, sweep.DamagedTrimIntentSetAside):
            self.fail("damaged trim recovery must name its preserved intent")
        self.assertEqual(result.path.read_bytes(), broken)
        self.assertTrue(result.path.name.startswith(intent.name + ".damaged-"))
        self.assertFalse(intent.exists())

    def test_bad_trim_intent_is_reported_once_and_does_not_block_a_later_take(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        intent = sweep.trim_intent_path(str(tree.parent))
        intent.parent.mkdir(parents=True)
        broken = b'{"target":'
        _ = intent.write_bytes(broken)
        errors = io.StringIO()
        with redirect_stderr(errors):
            sweep.recover_pending_trims()
            sweep.recover_pending_trims()
            self.assertEqual(self.take(base, tree.parent, "floor"), 0)
        self.assertEqual(errors.getvalue().count("could not recover"), 1)
        self.assertFalse(output.exists())
        row, = self.trim_rows()
        self.assertEqual(row["folder"], str(tree.parent))
        self.assertFalse(intent.exists())
        self.assertTrue(any(path.read_bytes() == broken for path in intent.parent.iterdir() if path.is_file()),
                        "the invalid intent must be kept aside for inspection")

    def take(self, base: Path, target: Path, caller: str) -> int:
        if caller == "floor":
            return self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0]
        with mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "0"}), redirect_stdout(io.StringIO()):
            return sweep.sweep_workspace(str(target), conf.read("lint"), False, recent_minutes=60)

    def running_nextest(self, base: Path, build_dir: str,
                        process_uses: Callable[[Sequence[str], str], dict[str, sweep.TargetUse]],
                        setting: Literal["environment", "config"] = "config") -> None:
        workspace = base / "workspace"
        workspace.mkdir(exist_ok=True)
        _ = (workspace / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
        environment = (f"CARGO_HOME={base / 'cargo-home'}",)
        if setting == "environment":
            environment += (f"CARGO_BUILD_BUILD_DIR={build_dir}",)
        else:
            config = workspace / ".cargo" / "config.toml"
            config.parent.mkdir()
            _ = config.write_text(f"[build]\nbuild-dir = {json.dumps(build_dir)}\n")
        proc = base / "proc"
        fake_process(proc, 42, exe="/usr/bin/cargo-nextest", cwd=str(workspace),
                     argv=("cargo-nextest", "nextest", "run"), environ=environment)
        def private_uses(targets: list[str]) -> dict[str, sweep.TargetUse]:
            return process_uses(targets, str(proc))

        _ = self.enterContext(mock.patch.object(sweep, "process_uses", side_effect=private_uses))

    def test_stopped_trim_is_recovered_once_without_deleting_again(self) -> None:
        append = record.sweep_trim
        reclaim = sweep.reclaim
        for caller in ("budget", "floor"):
            for stop_at in ("before append", "after append", "after delete"):
                with self.subTest(caller=caller, stop_at=stop_at):
                    base = self.base()
                    tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
                    expected = output.stat().st_blocks * 512 + sweep.directory_blocks(str(tree / ".fingerprint"))
                    stopped = False
                    at = float(int(time.time()))

                    def interrupted_append(folder: str, bytes_freed: int, extent: Literal["whole", "partial"],
                                           cause: Literal["budget take", "floor take"], at: float) -> None:
                        nonlocal stopped
                        self.assertFalse(output.exists(), "Inject the stop after the actual deletion")
                        if not stopped:
                            stopped = True
                            if stop_at == "after append":
                                append(folder, bytes_freed, extent, cause, at)
                            raise OSError("injected stop at trim append")
                        append(folder, bytes_freed, extent, cause, at)

                    def interrupted_delete(group: sweep.Group, scan: sweep.Scan,
                                           remaining: dict[sweep.InodeKey, int], dry_run: bool, *,
                                           lint_settings: conf.ConfigRead) -> sweep.ReclamationEstimate | sweep.ConfirmedDeletion:
                        nonlocal stopped
                        _ = reclaim(group, scan, remaining, dry_run, lint_settings=lint_settings)
                        self.assertFalse(output.exists())
                        stopped = True
                        raise KeyboardInterrupt("injected stop after deletion")

                    with (mock.patch.object(record, "sweep_trim", side_effect=interrupted_append),
                          mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                          mock.patch.object(time, "time", return_value=at),
                          redirect_stderr(io.StringIO())):
                        if stop_at == "after delete":
                            with (mock.patch.object(sweep, "reclaim", side_effect=interrupted_delete),
                                  self.assertRaises(KeyboardInterrupt)):
                                _ = self.take(base, tree.parent, caller)
                        else:
                            self.assertEqual(self.take(base, tree.parent, caller), 0)
                    self.assertTrue(stopped)
                    self.assertFalse(output.exists())
                    if stop_at != "after delete":
                        self.assertEqual(len(self.trim_rows()), int(stop_at == "after append"))
                    for _ in range(2):
                        with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                              mock.patch.object(time, "time", return_value=at + 60)):
                            if caller == "floor":
                                self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB, 10 * GIB], recorded=True)[0], 0)
                            else:
                                self.assertEqual(self.take(base, tree.parent, caller), 0)
                        row, = self.trim_rows()
                        self.assertEqual((row["folder"], row["bytes_freed"], row["extent"], row["cause"], row["at"]),
                                         (str(tree.parent), expected, "whole", caller + " take", store.utc_iso(at)))

    def test_zero_byte_partial_removal_still_records_a_trim(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                base = self.base()
                send = self.enterContext(mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT))
                tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
                fingerprint = tree / ".fingerprint" / f"shared-{APP}" / "bin-shared.json"
                clone = base / "copy" / "target"
                clone.mkdir(parents=True)
                os.link(output, clone / "artifact")
                os.link(fingerprint, clone / "fingerprint")
                parent = fingerprint.parent.stat()
                unlink = os.unlink

                def refuse_fingerprint(path: str, *, dir_fd: int | None = None) -> None:
                    if (path == str(fingerprint) or (path == fingerprint.name and dir_fd is not None
                                                    and os.path.samestat(parent, os.fstat(dir_fd)))):
                        raise PermissionError("injected fingerprint unlink failure")
                    unlink(path, dir_fd=dir_fd)

                with (mock.patch.object(os, "unlink", side_effect=refuse_fingerprint),
                      mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                      redirect_stderr(io.StringIO())):
                    status = self.take(base, tree.parent, caller)
                if caller == "floor":
                    send.assert_called()
                    cast(mock.Mock, sweep.send_phone_push).assert_not_called()
                self.assertEqual(status, 1)
                self.assertFalse(output.exists())
                self.assertTrue(fingerprint.exists())
                self.assertTrue((clone / "artifact").exists())
                row, = self.trim_rows()
                self.assertEqual((row["folder"], row["bytes_freed"], row["extent"], row["cause"]),
                                 (str(tree.parent), 0, "partial", caller + " take"))

    def test_nextest_build_folder_from_environment_or_config_is_busy_for_every_sweeper(self) -> None:
        process_uses = sweep.process_uses
        for setting in ("environment", "config"):
            for caller in ("budget", "floor"):
                with self.subTest(setting=setting, caller=caller):
                    base = self.base()
                    tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
                    self.running_nextest(base, str(tree.parent), process_uses, setting)
                    with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                          mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT),
                          mock.patch.object(sweep, "send_holder_message", return_value=sweep.FloorChannelOutcome.FAILED)):
                        self.assertEqual(self.take(base, tree.parent, caller), 0)
                        self.assertTrue(output.exists(), f"{caller} removed a running nextest build folder")
                        self.assertEqual(self.trim_rows(), [])

    def test_floor_keeps_nextest_build_folders_from_cargo_path_templates(self) -> None:
        process_uses = sweep.process_uses
        # Cargo Book, Configuration: build.build-dir defines these roots.
        # https://doc.rust-lang.org/cargo/reference/config.html#buildbuild-dir
        for placeholder in ("cargo-cache-home", "workspace-root", "workspace-path-hash"):
            with self.subTest(placeholder=placeholder):
                base = self.base()
                template = ("{cargo-cache-home}/build/{workspace-path-hash}/shared/target"
                            if placeholder == "workspace-path-hash" else "{" + placeholder + "}/shared/target")
                self.running_nextest(base, template, process_uses)
                if placeholder == "workspace-path-hash":
                    folder = base / "cargo-home" / "build" / sweep.cargo_workspace_path_hash(base / "workspace" / "Cargo.toml")
                else:
                    folder = base / ("cargo-home" if placeholder == "cargo-cache-home" else "workspace")
                output = cargo_target(folder, "shared", APP, time.time() - 2 * DAY)[1]
                idle_tree, idle = cargo_target(base, "unrelated", TOOL, time.time() - DAY)
                with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                      mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT),
                      mock.patch.object(sweep, "send_holder_message", return_value=sweep.FloorChannelOutcome.FAILED)):
                    status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
                self.assertEqual(status, 0, text)
                self.assertTrue(output.exists(), "The floor removed a running templated build folder")
                self.assertFalse(idle.exists(), "A resolved template must leave an unrelated cache reclaimable")
                row, = self.trim_rows()
                self.assertEqual((row["folder"], row["cause"]), (str(idle_tree.parent), "floor take"))

    def test_cargo_workspace_path_hash_matches_cargos_manifest_path_hash(self) -> None:
        # Golden derived from Cargo custom_build_dir + util/hex.rs, Rust Path::hash,
        # and rustc-stable-hash 0.1.2, independently of the Python resolver.
        # https://github.com/rust-lang/cargo/blob/master/src/context/mod.rs#L734-L762
        # https://github.com/rust-lang/rust/blob/master/library/std/src/path.rs#L3584-L3631
        # https://github.com/rust-lang/rustc-stable-hash/tree/v0.1.2/src
        self.assertEqual(sweep.cargo_workspace_path_hash(Path("/workspaces/sample/Cargo.toml")),
                         "f6/cfca1227ebe898")

    def test_unknown_build_folder_template_keeps_the_whole_cargo_cache(self) -> None:
        process_uses = sweep.process_uses
        base = self.base()
        outputs = [cargo_target(base / "cargo-home", name, digest, time.time() - 2 * DAY)[1]
                   for name, digest in (("first", APP), ("second", DEMO))]
        _, outside_cargo_home = cargo_target(base, "unrelated", TOOL, time.time() - DAY)
        self.running_nextest(base, "{cargo-cache-home}/build/{unknown-template}", process_uses)
        with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
            status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertTrue(all(output.exists() for output in outputs), "Unknown expansion must keep every cached target")
        self.assertTrue(outside_cargo_home.exists(), "Unknown expansion must also keep caches outside Cargo home")
        self.assertEqual(self.trim_rows(), [])

    def test_floor_keeps_a_standing_target_only_while_its_checkout_is_busy(self) -> None:
        base = self.base()
        seed = ImmutableGitSeed.create(base, "main", "Sweep tests", "sweep@example.invalid",
                                       {"Cargo.toml": "[workspace]\n", "source.txt": "style source\n"})
        with mock.patch.dict(os.environ, {"HOME": str(base), scratch.HOLDS_ENV: str(base / "holds.json")}):
            with copies.standing("style-unit", seed.checkout, copies.CommittedSource("HEAD")) as checkout:
                target = checkout / "target"
                output = unit(target / "debug", "deps", "style", APP, time.time() - DAY)
                with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                      mock.patch.object(sweep, "lock_holder", return_value=os.getpid()),
                      mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT),
                      mock.patch.object(sweep, "send_holder_message", return_value=sweep.FloorChannelOutcome.FAILED)):
                    status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB - 1], recorded=True)
                self.assertEqual(status, 0, text)
                self.assertTrue(output.exists(), "The floor removed a standing target between Cargo commands")
                self.assertEqual(self.trim_rows(), [])
            self.assertTrue((checkout / scratch.BUSY).is_file())
            with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
                status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertFalse(output.exists(), "An idle standing checkout must permit target reclaim")
        row, = self.trim_rows()
        self.assertEqual((row["folder"], row["cause"]), (str(target), "floor take"))

    def test_floor_finds_and_reclaims_an_idle_retained_standing_target(self) -> None:
        base = self.base()
        seed = ImmutableGitSeed.create(base, "main", "Sweep tests", "sweep@example.invalid",
                                       {"Cargo.toml": "[workspace]\n", "source.txt": "style source\n"})
        with mock.patch.dict(os.environ, {"HOME": str(base), scratch.HOLDS_ENV: str(base / "holds.json")}):
            with copies.standing("style-unit", seed.checkout, copies.CommittedSource("HEAD")) as checkout:
                target = checkout / "target"
            output = unit(target / "debug", "deps", "style", APP, time.time() - DAY)
            # Cargo may omit rustc-info when its caller made the target directory.
            self.assertFalse((target / sweep.RUSTC_INFO).exists())
            self.assertEqual(sweep.target_dirs([str(base)]), [str(target)])
            with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
                status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertFalse(output.exists())
        row, = self.trim_rows()
        self.assertEqual((row["folder"], row["extent"], row["cause"]), (str(target), "whole", "floor take"))

    def test_default_floor_discovers_configured_standing_targets_and_respects_busy_use(self) -> None:
        base = self.base()
        pool = base / "configured-pool"
        config = base / "copies.conf"
        configured = (Path(__file__).resolve().parents[2] / "config/copies.conf").read_text()
        _ = config.write_text("\n".join(f"root={pool}" if line.startswith("root=") else line
                                           for line in configured.splitlines()))
        seed = ImmutableGitSeed.create(base, "main", "Sweep tests", "sweep@example.invalid",
                                       {"Cargo.toml": "[workspace]\n", "source.txt": "standing source\n"})
        discover = sweep.target_dirs

        def configured_targets(roots: Sequence[str], *, lint_settings: conf.ConfigRead,
                               checkouts: list[str] | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
                               known_trees: dict[str, list[str]] | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> list[str]:
            # Scan the private pool only when the default roots include it.
            return discover([str(pool)] if str(pool) in roots else [],
                            lint_settings=lint_settings, checkouts=checkouts, known_trees=known_trees)

        with (mock.patch.dict(os.environ, {"COPIES_CONFIG": str(config), "HOME": str(base),
                                          scratch.HOLDS_ENV: str(base / "holds.json")}),
              mock.patch.object(sweep, "target_dirs", side_effect=configured_targets),
              mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT),
              mock.patch.object(sweep, "send_holder_message", return_value=sweep.FloorChannelOutcome.FAILED),
              redirect_stdout(io.StringIO())):
            with copies.standing("style-unit", seed.checkout, copies.CommittedSource("HEAD")) as checkout:
                target = checkout / "target"
                output = unit(target / "debug", "deps", "style", APP, time.time() - DAY)
                self.root_row(checkout, target, target)
                with mock.patch.object(sweep, "free_bytes", return_value=0):
                    self.assertEqual(sweep.hold_floor(GIB, False, lock=str(base / "floor.lock")), 0)
                self.assertTrue(output.exists(), "A busy configured standing target must survive")
                self.assertEqual(self.trim_rows(), [])
            with self.free_space([0, GIB]):
                self.assertEqual(sweep.hold_floor(GIB, False, lock=str(base / "floor.lock")), 0)
        self.assertFalse(output.exists(), "The default floor must reclaim an idle configured standing target")
        row, = self.trim_rows()
        self.assertEqual(row["folder"], str(target))

    def test_partial_unlink_failure_still_records_successfully_freed_bytes(self) -> None:
        for caller in ("budget", "floor"):
            with self.subTest(caller=caller):
                base = self.base()
                send = self.enterContext(mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT))
                now = time.time()
                tree, refused = cargo_target(base, "shared", APP, now - 3 * DAY)
                kept = unit(tree, "deps", "newer", DEMO, now - DAY)
                fingerprint = tree / ".fingerprint" / f"shared-{APP}" / "bin-shared.json"
                expected = fingerprint.stat().st_blocks * 512
                total = sum(sweep.scan_roots([str(tree.parent)], [str(tree)]).blocks.values())
                parent = refused.parent.stat()
                unlink = os.unlink

                def refuse_payload(path: str, *, dir_fd: int | None = None) -> None:
                    if (path == str(refused) or (path == refused.name and dir_fd is not None
                                                and os.path.samestat(parent, os.fstat(dir_fd)))):
                        raise PermissionError("injected payload unlink failure")
                    unlink(path, dir_fd=dir_fd)

                with (mock.patch.object(os, "unlink", side_effect=refuse_payload),
                      mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    if caller == "budget":
                        status = sweep.sweep([str(tree.parent)], [str(tree)], total - 1, "test", False, recent_minutes=60)
                    else:
                        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
                if caller == "floor":
                    send.assert_called()
                    cast(mock.Mock, sweep.send_phone_push).assert_not_called()
                self.assertEqual(status, 1)
                self.assertTrue(refused.exists())
                self.assertFalse(fingerprint.exists())
                self.assertTrue(kept.exists())
                row, = self.trim_rows()
                self.assertEqual((row["bytes_freed"], row["extent"], row["cause"]),
                                 (expected, "partial", caller + " take"))

    def test_swapped_directory_is_not_followed_or_credited_in_trim_row(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        victim = tree / "incremental" / "session"
        saved_output = write(victim / "object.o")
        outside = write(base / "outside" / "keep")
        saved = base / "saved"
        parent = victim.parent.stat()
        expected = output.stat().st_blocks * 512 + sweep.directory_blocks(str(tree / ".fingerprint"))
        lstat = os.lstat
        swapped = False

        def swap_before_open(path: str, *, dir_fd: int | None = None) -> os.stat_result:
            nonlocal swapped
            observed = lstat(path, dir_fd=dir_fd)
            matches = path == str(victim) or (path == victim.name and dir_fd is not None
                                              and os.path.samestat(parent, os.fstat(dir_fd)))
            if matches and not swapped:
                swapped = True
                _ = victim.rename(saved)
                victim.symlink_to(outside.parent, target_is_directory=True)
            return observed

        # Start the race only inside the deletion, after the scan and validation.
        remove = sweep.remove

        def raced_remove(groups: list[sweep.Group], *, lint_settings: conf.ConfigRead) -> sweep.ConfirmedDeletion:
            with mock.patch.object(os, "lstat", side_effect=swap_before_open):
                return remove(groups, lint_settings=lint_settings)

        with (mock.patch.object(sweep, "remove", side_effect=raced_remove),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            status = sweep.sweep([str(tree.parent)], [str(tree)], 0, "test", False, recent_minutes=60)
        self.assertEqual(status, 1)
        self.assertTrue(swapped)
        self.assertTrue(outside.exists())
        self.assertTrue((saved / saved_output.name).exists())
        row, = self.trim_rows()
        self.assertEqual((row["bytes_freed"], row["extent"]), (expected, "partial"))

    def test_two_clones_share_one_target_and_its_own_last_use(self) -> None:
        base = self.base()
        now = time.time()
        shared_tree, shared = cargo_target(base, "shared", APP, now - 3 * DAY)
        _, older = cargo_target(base, "other", DEMO, now - 2 * DAY)
        for name in ("copy-a", "copy-b"):
            copy = base / "copies" / name
            copy.mkdir(parents=True)
            (copy / "target").symlink_to(shared_tree.parent, target_is_directory=True)
        stamp = shared_tree.parent / sweep.USE_STAMP
        stamp.touch()
        os.utime(stamp, (now - DAY, now - DAY))
        roots = sweep.target_dirs([str(base)])
        self.assertEqual(roots.count(str(shared_tree.parent)), 1)
        self.assertEqual(len(roots), 2)
        with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(shared.exists(), "A recent use in either copy protects the one shared target")
        self.assertFalse(older.exists())

    def test_shared_cargo_lock_has_closed_results_and_skips_the_target(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        lock = tree / sweep.LOCK_NAMES[0]
        with lock.open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            result = sweep.lock_trees([str(tree)])
            self.assertIsInstance(result, sweep.TargetLockBusy)
            with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
                  mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT)):
                status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB - 1], recorded=True)
            self.assertEqual(status, 0)
            self.assertTrue(output.exists())
            self.assertEqual(self.trim_rows(), [])
        result = sweep.lock_trees([str(tree)])
        self.assertIsInstance(result, sweep.TargetLocksAcquired)
        assert isinstance(result, sweep.TargetLocksAcquired)
        try:
            with lock.open("a") as contender, self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            sweep.release(result.held)

    def test_floor_partial_take_records_exact_confirmed_bytes_once(self) -> None:
        base = self.base()
        now = time.time()
        tree, oldest = cargo_target(base, "shared", APP, now - 3 * DAY)
        kept = unit(tree, "deps", "newer", DEMO, now - DAY)
        fingerprint = tree / ".fingerprint" / f"shared-{APP}" / "bin-shared.json"
        expected = sum(path.stat().st_blocks * 512 for path in (oldest, fingerprint))
        with (mock.patch.object(time, "time", return_value=now),
              mock.patch.object(sweep, "held_cargo_lock_files", return_value=set())):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(oldest.exists())
        self.assertTrue(kept.exists())
        row, = self.trim_rows()
        self.assertEqual({key: row[key] for key in ("host", "folder", "at", "bytes_freed", "extent", "cause")},
                         {"host": store.host_name(), "folder": str(tree.parent),
                          "at": store.utc_iso(now), "bytes_freed": expected,
                          "extent": "partial", "cause": "floor take"})
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_floor_whole_take_includes_every_build_unit_and_incremental_dir_in_one_row(self) -> None:
        base = self.base()
        now = time.time()
        tree, first = cargo_target(base, "shared", APP, now - 3 * DAY)
        second = unit(tree, "deps", "second", DEMO, now - 2 * DAY)
        incremental = write(tree / "incremental" / "shared-session" / "object.o")
        expected = sweep.directory_blocks(str(tree / ".fingerprint")) + sum(
            path.stat().st_blocks * 512 for path in (first, second, incremental))
        with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 2 * expected, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(any(path.exists() for path in (first, second, incremental)))
        row, = self.trim_rows()
        self.assertEqual((row["folder"], row["bytes_freed"], row["extent"], row["cause"]),
                         (str(tree.parent), expected, "whole", "floor take"))
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_floor_equal_use_targets_each_write_one_trim_for_interleaved_units(self) -> None:
        base = self.base()
        now = time.time()
        expected: dict[str, int] = {}
        outputs: list[Path] = []
        for name, first_age, second_age in (("first", 5, 3), ("second", 4, 2)):
            tree, first = cargo_target(base, name, APP, now - first_age * DAY)
            second = unit(tree, "deps", "newer", DEMO, now - second_age * DAY)
            outputs.extend((first, second))
            stamp = tree.parent / sweep.USE_STAMP
            stamp.touch()
            os.utime(stamp, (now - DAY, now - DAY))
            expected[str(tree.parent)] = sweep.directory_blocks(str(tree / ".fingerprint")) + sum(
                path.stat().st_blocks * 512 for path in (first, second))
        with mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()):
            status, text = self.hold(base, 10 * GIB, [10 * GIB - 2 * sum(expected.values()), 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertFalse(any(path.exists() for path in outputs))
        rows = self.trim_rows()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({cast(str, row["folder"]): row["bytes_freed"] for row in rows}, expected)
        self.assertEqual([(row["extent"], row["cause"]) for row in rows], [("whole", "floor take")] * 2)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_shared_artifacts_linked_from_a_clone_free_no_payload_bytes(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "shared", APP, time.time() - DAY)
        clone = base / "copies" / "copy-a" / "target"
        clone.mkdir(parents=True)
        linked = clone / "kept-artifact"
        os.link(output, linked)
        fingerprint = tree / ".fingerprint" / f"shared-{APP}" / "bin-shared.json"
        expected = fingerprint.stat().st_blocks * 512
        with (mock.patch.object(sweep, "held_cargo_lock_files", return_value=set()),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB - SIZE], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        self.assertTrue(linked.exists())
        self.assertEqual(linked.stat().st_nlink, 1)
        row, = self.trim_rows()
        self.assertEqual(row["bytes_freed"], expected)
        self.assertLess(cast(int, row["bytes_freed"]), linked.stat().st_blocks * 512)

    def test_floor_claims_an_absent_busy_mark_before_unlinking_output(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "copy", APP, time.time() - 3 * DAY)
        mark = tree.parent / ".claude-busy"
        reclaim = sweep.reclaim
        attempts: list[bool] = []

        def late_holder(group: sweep.Group, scan: sweep.Scan, remaining: dict[sweep.InodeKey, int],
                        dry_run: bool, *, lint_settings: conf.ConfigRead) -> sweep.ReclamationEstimate | sweep.ConfirmedDeletion:
            with mark.open("a") as holder:
                try:
                    fcntl.flock(holder, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    attempts.append(False)
                else:
                    attempts.append(True)
                return reclaim(group, scan, remaining, dry_run, lint_settings=lint_settings)

        self.assertFalse(mark.exists())
        with mock.patch.object(sweep, "reclaim", late_holder):
            status, text = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertTrue(attempts)
        self.assertFalse(any(attempts), "a holder started while the floor sweep was unlinking output")
        self.assertFalse(output.exists())

    def test_floor_keeps_a_shared_busy_copy_or_target_then_reclaims_after_unlock(self) -> None:
        for location in ("copy", "target"):
            with self.subTest(location=location):
                base = self.base()
                tree, output = cargo_target(base, "copy", APP, time.time() - 3 * DAY)
                mark = (tree.parent.parent if location == "copy" else tree.parent) / ".claude-busy"
                with mark.open("a") as holder:
                    fcntl.flock(holder, fcntl.LOCK_SH)
                    with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT):
                        status, text = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB - SIZE], recorded=True)
                    self.assertEqual(status, 0, text)
                    self.assertTrue(output.exists())
                status, text = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
                self.assertEqual(status, 0, text)
                self.assertFalse(output.exists())

    def test_floor_rechecks_busy_mark_after_its_scan(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "copy", APP, time.time() - 3 * DAY)
        mark = tree.parent.parent / ".claude-busy"
        scan = sweep.scan_floor_roots
        with mark.open("a") as holder:
            def start_after_scan(roots: list[str], trees: dict[str, list[str]], *, lint_settings: conf.ConfigRead) -> sweep.Scan:
                result = scan(roots, trees, lint_settings=lint_settings)
                fcntl.flock(holder, fcntl.LOCK_SH)
                return result
            with (mock.patch.object(sweep, "scan_floor_roots", side_effect=start_after_scan),
                  mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT)):
                status, text = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB - SIZE], recorded=True)
            self.assertEqual(status, 0, text)
            self.assertTrue(output.exists())

    def test_outside_link_leaves_floor_exhausted_and_sends_alert(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "old", APP, time.time() - DAY)
        original_scan = sweep.scan_floor_roots
        def link_after_scan(roots: list[str], trees: dict[str, list[str]], *, lint_settings: conf.ConfigRead) -> sweep.Scan:
            scan = original_scan(roots, trees, lint_settings=lint_settings)
            os.link(output, base / "outside-target")
            return scan
        with (mock.patch.object(sweep, "scan_floor_roots", side_effect=link_after_scan),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB - SIZE], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        self.assertTrue((base / "outside-target").exists())
        send.assert_called_once()
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()

    def test_floor_removes_old_hard_links_in_order_and_keeps_newer_output(self) -> None:
        base = self.base()
        now = time.time()
        first_tree, first = cargo_target(base, "first", APP, now - 3 * DAY)
        second_tree, second = cargo_target(base, "second", DEMO, now - 2 * DAY)
        _, newer = cargo_target(base, "newer", TOOL, now - DAY)
        second.unlink()
        os.link(first, second)
        with mock.patch.object(sweep, "remove", wraps=sweep.remove) as removed:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE // 2, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(newer.exists())
        groups = [cast(list[sweep.Group], call.args[0]) for call in removed.call_args_list]
        self.assertEqual([[group.build_tree for group in batch] for batch in groups],
                         [[str(first_tree)], [str(second_tree)]])

    def test_floor_keeps_target_used_after_discovery_before_deletion_planning(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 3 * DAY)
        _, next_output = cargo_target(base, "next", DEMO, now - 2 * DAY)
        clock = [now]
        original = sweep.directory_blocks
        def use_during_size_walk(path: str, seen: set[sweep.InodeKey] | None = None,
                                 excluded: Collection[str] = ()) -> int:
            stamp = old_tree.parent / sweep.USE_STAMP
            if not stamp.exists():
                stamp.touch()
                os.utime(stamp, (now + 10, now + 10))
                clock[0] = now + 20
            return original(path, seen, excluded)
        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "directory_blocks", side_effect=use_during_size_walk)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(old.exists())
        self.assertFalse(next_output.exists())

    def test_floor_records_deletion_before_unlock_and_attributes_rebuild_during_pass(self) -> None:
        base = self.base()
        now = float(int(time.time()))
        first_tree, _ = cargo_target(base, "first", APP, now - 3 * DAY)
        _, _ = cargo_target(base, "second", DEMO, now - 2 * DAY)
        clock = [now]
        original_remove, original_release = sweep.remove, sweep.release
        releases: list[float] = []
        def delete_at_known_time(groups: list[sweep.Group], *, lint_settings: conf.ConfigRead) -> sweep.ConfirmedDeletion:
            clock[0] = now + (10 if groups[0].build_tree == str(first_tree) else 30)
            return original_remove(groups, lint_settings=lint_settings)
        def rebuild_after_release(held: list[int]) -> None:
            try:
                if not releases:
                    self.assertEqual(self.trim_events(),
                                     [sweep.SweepRemoval(now + 10, str(first_tree.parent), "floor take")])
            finally:
                original_release(held)
            releases.append(clock[0])
            if len(releases) == 1:
                clock[0] = now + 20
        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "remove", side_effect=delete_at_known_time),
              mock.patch.object(sweep, "release", side_effect=rebuild_after_release)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE - SIZE // 2, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertEqual(releases, [now + 10, now + 30])
        folder = str(first_tree.parent.parent)
        steps = [sweep.RebuildStep(folder, folder, now + 1, now + 5, 0, 1),
                 sweep.RebuildStep(folder, folder, now + 20, now + 21, 50, 1)]
        cold = sweep.rebuild_causes(steps, self.trim_events(), now, now + 40)
        self.assertEqual([step.cause for step in cold], ["floor take"])

    def test_parallel_target_scan_counts_shared_inodes_once(self) -> None:
        base = self.base()
        first_tree, first = cargo_target(base, "first", APP, time.time() - DAY)
        second_tree, second = cargo_target(base, "second", DEMO, time.time() - DAY)
        second.unlink()
        os.link(first, second)
        roots = [str(first_tree.parent), str(second_tree.parent)]
        trees = {roots[0]: [str(first_tree)], roots[1]: [str(second_tree)]}
        serial = sweep.scan_roots(roots, [str(first_tree), str(second_tree)])
        parallel = sweep.scan_floor_roots(roots, trees)
        self.assertEqual(parallel.blocks, serial.blocks)
        self.assertEqual(parallel.links, serial.links)
        serial_links, parallel_links = dict(serial.links), dict(parallel.links)
        self.assertEqual(sum(sweep.freed_by(group, parallel, parallel_links).bytes for group in parallel.groups),
                         sum(sweep.freed_by(group, serial, serial_links).bytes for group in serial.groups))

    def test_floor_locks_only_one_target_after_candidate_choice_and_stops_at_shortfall(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 3 * DAY)
        _, next_output = cargo_target(base, "next", DEMO, now - 2 * DAY)
        active: list[int] = []
        acquired: list[list[str]] = []
        original_locks, original_release = sweep.lock_trees, sweep.release
        original_scan, original_sizes = sweep.scan_roots, sweep.directory_blocks
        def lock_one(trees: list[str]) -> sweep.TargetLocksAcquired | sweep.TargetLockBusy:
            self.assertEqual(active, [], "Two targets must never hold locks together")
            acquired.append(trees)
            result = original_locks(trees)
            if isinstance(result, sweep.TargetLocksAcquired):
                active.extend(result.held)
            return result
        def unlock(held: list[int]) -> None:
            original_release(held)
            for fd in held:
                active.remove(fd)
        def scan_unlocked(roots: list[str], trees: list[str], *, lint_settings: conf.ConfigRead) -> sweep.Scan:
            self.assertEqual(active, [], "Candidate scan must hold no cargo locks")
            return original_scan(roots, trees, lint_settings=lint_settings)
        def size_unlocked(path: str, seen: set[sweep.InodeKey] | None = None,
                          excluded: Collection[str] = ()) -> int:
            self.assertEqual(active, [], "Size walks must hold no cargo locks")
            return original_sizes(path, seen, excluded)
        with (mock.patch.object(sweep, "lock_trees", side_effect=lock_one),
              mock.patch.object(sweep, "release", side_effect=unlock),
              mock.patch.object(sweep, "scan_roots", side_effect=scan_unlocked),
              mock.patch.object(sweep, "directory_blocks", side_effect=size_unlocked)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertEqual(active, [])
        self.assertEqual(acquired, [[str(old_tree)]])
        self.assertFalse(old.exists())
        self.assertTrue(next_output.exists())

    def test_floor_skips_target_that_becomes_locked_after_choice(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 3 * DAY)
        next_tree, next_output = cargo_target(base, "next", DEMO, now - 2 * DAY)
        original = sweep.lock_trees
        def busy_after_scan(trees: list[str]) -> sweep.TargetLocksAcquired | sweep.TargetLockBusy:
            if trees == [str(old_tree)]:
                return sweep.TargetLockBusy(str(old_tree / sweep.LOCK_NAMES[0]))
            return original(trees)
        with mock.patch.object(sweep, "lock_trees", side_effect=busy_after_scan) as locks:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(old.exists())
        self.assertFalse(next_output.exists())
        self.assertEqual(locks.call_args_list, [mock.call([str(old_tree)]), mock.call([str(next_tree)])])

    def test_floor_keeps_output_changed_after_the_unlocked_scan(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 3 * DAY)
        _, next_output = cargo_target(base, "next", DEMO, now - 2 * DAY)
        original = sweep.lock_trees
        def change_after_scan(trees: list[str]) -> sweep.TargetLocksAcquired | sweep.TargetLockBusy:
            if trees == [str(old_tree)]:
                _ = old.write_bytes(b"new build output")
            return original(trees)
        with mock.patch.object(sweep, "lock_trees", side_effect=change_after_scan):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(old.exists())
        self.assertFalse(next_output.exists())

    def test_floor_records_removals_when_output_is_captured(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "repo", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        events = self.trim_events()
        self.assertEqual([(event.path, event.cause) for event in events],
                         [(str(tree.parent), "floor take")])

    def test_floor_dry_run_above_the_floor_prints_the_free_size(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "repo", APP, time.time() - DAY)
        status, printed = self.hold(base, 10 * GIB, [12 * GIB, 12 * GIB], dry_run=True, recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        self.assertIn("12.0 GiB free, at or above the 10.0 GiB floor; nothing to remove", printed)

    def test_floor_dry_run_writes_no_removal_record(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "repo", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], dry_run=True, recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        self.assertEqual(self.trim_rows(), [])
        self.assertFalse(sweep.sweep_removals_path().exists())
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertEqual(len(self.trim_rows()), 1)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_dry_run_keeps_selection_when_cache_accounting_is_unavailable(self) -> None:
        base = self.base()
        _, first = cargo_target(base, "first", APP, time.time() - 2 * DAY)
        _, second = cargo_target(base, "second", DEMO, time.time() - DAY)
        free = [10 * GIB - 1, 10 * GIB - 1]
        baseline = self.hold(base, 10 * GIB, free, dry_run=True, recorded=True)
        with mock.patch.object(sweep, "directory_blocks", side_effect=OSError("fixture accounting unavailable")):
            result = self.hold(base, 10 * GIB, free, dry_run=True, recorded=True)
        self.assertEqual(result, baseline)
        self.assertIn(f"from {first.parents[2]}", result[1])
        self.assertNotIn(f"from {second.parents[2]}", result[1])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_a_real_pass_does_not_walk_unchanged_idle_targets_again(self) -> None:
        base = self.base()
        targets = [cargo_target(base, name, digest, time.time() - DAY)[0].parent
                   for name, digest in (("first", APP), ("second", DEMO))]
        with mock.patch.object(sweep, "directory_blocks", wraps=sweep.directory_blocks) as walks:
            status, text = self.hold(base, 10 * GIB, [9 * GIB], scanned=11 * GIB, recorded=True)
        self.assertEqual(status, 0)
        self.assertIn("nothing taken", text)
        self.assertEqual([call.args[0] for call in walks.call_args_list if call.args[0] in map(str, targets)], [])
        self.assertEqual(self.trim_rows(), [])

    def test_after_cache_bytes_match_a_full_walk_with_links_across_untouched_targets(self) -> None:
        base = self.base()
        targets: list[Path] = []
        payloads: list[Path] = []
        for number, digest in enumerate((APP, DEMO, TOOL)):
            tree, payload = cargo_target(base, f"repo-{number}", digest, time.time() - (3 - number) * DAY)
            targets.append(tree.parent)
            payloads.append(payload)
        shared = write(targets[0] / "tmp" / "shared")
        for target in targets[1:]:
            (target / "tmp").mkdir()
            os.link(shared, target / "tmp" / "shared")
        with mock.patch.object(sweep, "directory_blocks", wraps=sweep.directory_blocks) as walks:
            status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(payloads[0].exists())
        self.assertTrue(all(path.exists() for path in payloads[1:]))
        self.assertTrue(shared.exists())
        self.assertIn(f"from {targets[0]}", text)
        self.assertEqual([call.args[0] for call in walks.call_args_list if call.args[0] in map(str, targets)],
                         [str(targets[0])])
        seen: set[sweep.InodeKey] = set()
        expected = sum(sweep.directory_blocks(str(target), seen) for target in targets)
        saved = self.saved_record()
        self.assertEqual(saved.build_cache_bytes, expected)

    def test_paired_root_deletion_remeasures_both_output_roots(self) -> None:
        base = self.base()
        mapping, own, artifact = self.paired_output(base)
        artifact.unlink()
        artifact.with_suffix(".d").unlink()
        _ = write(mapping.target_directory / sweep.RUSTC_INFO, 0)
        roots = {str(mapping.target_directory), str(mapping.build_directory)}
        scans: list[sweep.Scan] = []
        original = sweep.scan_floor_roots

        def scan(roots: list[str], trees: dict[str, list[str]], *,
                 lint_settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> sweep.Scan:
            result = original(roots, trees, lint_settings=lint_settings)
            scans.append(result)
            return result

        with (mock.patch.object(sweep, "scan_floor_roots", side_effect=scan),
              mock.patch.object(sweep, "directory_blocks", wraps=sweep.directory_blocks) as walks):
            status, text = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0, text)
        self.assertFalse(own.exists())
        self.assertEqual(scans[-1].changed_targets, roots)
        self.assertCountEqual([call.args[0] for call in walks.call_args_list if call.args[0] in roots], roots)
        seen: set[sweep.InodeKey] = set()
        after = sum(sweep.directory_blocks(root, seen) for root in roots)
        self.assertEqual(self.saved_record().build_cache_bytes, after)

    def test_floor_grouping_resolves_each_target_once(self) -> None:
        base = self.base()
        for number in range(12):
            _ = cargo_target(base, "repo", f"{number:016x}", time.time() - DAY)
        index = sweep.CargoOutputIndex
        roots_for = index.roots_for
        scan_floor_roots = sweep.scan_floor_roots
        target_last_use = sweep.target_last_use
        grouping_queries: list[str] = []
        all_queries: list[str] = []
        observed_queries: list[list[str]] = []
        def resolve(outputs: sweep.CargoOutputIndex, root: str) -> tuple[str, ...]:
            grouping_queries.append(root)
            all_queries.append(root)
            return roots_for(outputs, root)
        def scanned_roots(roots: list[str], trees: dict[str, list[str]], *,
                          lint_settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> sweep.Scan:
            scan = scan_floor_roots(roots, trees, lint_settings=lint_settings)
            grouping_queries.clear()
            return scan
        def last_use(root: str, groups: list[sweep.Group], discovered: Sequence[str] = ()) -> float:
            observed_queries.append(list(grouping_queries))
            return target_last_use(root, groups, discovered)
        with (mock.patch.object(index, "roots_for", resolve),
              mock.patch.object(sweep, "scan_floor_roots", side_effect=scanned_roots),
              mock.patch.object(sweep, "target_last_use", side_effect=last_use)):
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1], dry_run=True, recorded=True)[0], 0)
        root = str(base / "repo" / "target")
        self.assertEqual(observed_queries[0], [root])
        self.assertEqual(all_queries.count(root), 6)

    def base(self) -> Path:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        config = base / "copies.conf"
        configured = (Path(__file__).resolve().parents[2] / "config/copies.conf").read_text()
        _ = config.write_text("\n".join(f"root={base / 'gates'}" if line.startswith("root=") else line
                                           for line in configured.splitlines()) + "\n")
        self.enterContext(mock.patch.dict(os.environ, {
            tmp_sweep.STATE_ENV: str(base),
            "BUILDLOG_DIR": str(base / "buildlog"),
            "BUILD_HOLD_DIR": str(base / "build-hold"),
            "COPIES_CONFIG": str(config),
        }))
        create_step_index(build_index.index_path())
        _ = self.enterContext(mock.patch.object(sweep, "output_device", return_value=base.stat().st_dev))
        _ = self.enterContext(mock.patch.object(sessions, "live_sessions", return_value=[]))
        _ = self.enterContext(mock.patch.object(sweep, "process_uses", return_value={}))
        _ = write_config_fixture(conf.path("lint"),
                                 f"sweep_ci_targets={base / 'ci-1'} {base / 'ci-2'}\n"
                                 + f"sweep_floor_roots={base}\n" + "sweep_phone_floor_gib=0\n")
        _ = self.enterContext(mock.patch.object(
            sweep, "send_floor_alert", side_effect=AssertionError("unexpected floor alert send")))
        _ = self.enterContext(mock.patch.object(
            sweep, "send_phone_push", side_effect=AssertionError("unexpected phone push send")))
        _ = self.enterContext(mock.patch.object(
            sweep, "send_holder_message", side_effect=AssertionError("unexpected session message send")))
        _ = self.enterContext(mock.patch.object(sweep, "holder_sessions", return_value={}))
        return base

    def test_base_blocks_real_phone_sends(self) -> None:
        """Guard the fixture patch that prevents real phone sends."""
        _ = self.base()
        self.assertIsInstance(sweep.send_floor_alert, mock.Mock)
        with self.assertRaisesRegex(AssertionError, "unexpected floor alert send"):
            _ = sweep.send_floor_alert("test")

    @contextmanager
    def free_space(self, free: list[int], scanned: int | None = None) -> Generator[None]:
        """Keep each capacity value stable until the scan or removal changes the fixture state."""
        after_scan = free[0] if scanned is None else scanned
        with (mock.patch.object(sweep, "scan_floor_roots", wraps=sweep.scan_floor_roots) as scan,
              mock.patch.object(sweep, "shrink_floor", wraps=sweep.shrink_floor) as removal):
            def available(*_: str) -> int:
                if removal.called:
                    return free[-1]
                return after_scan if scan.called else free[0]

            with mock.patch.object(sweep, "free_bytes", side_effect=available):
                yield

    def hold(self, base: Path, floor: int, free: list[int], dry_run: bool = False,
             scanned: int | None = None, *, recorded: bool) -> tuple[int, str]:
        """Use the fixture capacity before the scan, after the scan, and after removal."""
        if recorded:
            paired = sweep.OUTPUT_ROOTS.get().workspaces
            rows = [(store.host_name(), str(item.workspace_root), str(item.target_directory), str(item.build_directory))
                    for item in paired]
            mapped = {str(root) for item in paired for root in (item.target_directory, item.build_directory)}
            rows.extend((store.host_name(), str(Path(root).parent), root, root)
                        for root in fixture_target_dirs([str(base)], lint_settings=conf.read("lint")) if root not in mapped)
            with closing(sqlite3.connect(build_index.index_path())) as connection, connection:
                _ = connection.executemany(
                    "INSERT INTO steps(host, worktree, target_directory, build_directory) VALUES (?, ?, ?, ?)", rows)
        output = io.StringIO()
        _ = write_config_fixture(conf.path("lint"),
                                 f"sweep_floor_roots={base}\n")
        with (self.free_space(free, scanned),
              mock.patch.object(tmp_sweep, "state_dir", return_value=base), redirect_stdout(output)):
            status = sweep.hold_floor(floor, dry_run)
        return status, output.getvalue()

    def prior(self, free: int, caches: int, age: int = 60, alert_at: float | None = None,
              push_at: float | None = None) -> None:
        sweep.write_floor_record(
            sweep.floor_record_path(),
            sweep.FloorRecord(time.time() - age, free, caches,
                              sweep.DeliveredAlert(alert_at) if alert_at is not None else sweep.NoDeliveredAlert(),
                              sweep.DeliveredAlert(push_at) if push_at is not None else sweep.NoDeliveredAlert(),
                              sweep.KnownHeldRoots(frozenset())),
        )

    def test_a_changed_held_root_set_does_not_report_its_missing_bytes_as_growth(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "idle", APP, time.time() - DAY)
        held_tree, _ = cargo_target(base, "held", DEMO, time.time() - DAY)
        caches = sweep.directory_blocks(str(tree.parent))
        self.prior(int(10.2 * GIB), caches + 35 * GIB)
        protected = {str(held_tree.parent): sweep.BuildHoldTarget("build-speed")}
        with (mock.patch.object(sweep, "build_hold_targets", return_value=protected),
              mock.patch.object(sweep, "send_floor_alert") as send):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_not_called()
        record = self.saved_record()
        self.assertEqual(record.held_roots, sweep.KnownHeldRoots(frozenset(protected)))
        data = cast(dict[str, object], json.loads(sweep.floor_record_path().read_text()))
        self.assertEqual(data["held_roots"], [str(held_tree.parent)])

    def test_the_same_held_roots_still_report_an_unexplained_free_space_fall(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "idle", APP, time.time() - DAY)
        held_tree, _ = cargo_target(base, "held", DEMO, time.time() - DAY)
        protected = {str(held_tree.parent): sweep.BuildHoldTarget("build-speed")}
        sweep.write_floor_record(sweep.floor_record_path(), sweep.FloorRecord(
            time.time() - 60, 30 * GIB, sweep.directory_blocks(str(tree.parent)),
            sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert(), sweep.KnownHeldRoots(frozenset(protected))))
        with (mock.patch.object(sweep, "build_hold_targets", return_value=protected),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_called_once()
        self.assertIn("free space fell more than 10 GiB beyond build-cache growth", cast(str, send.call_args.args[0]))

    def test_an_old_record_without_held_roots_does_not_fire_a_growth_alert(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "idle", APP, time.time() - DAY)
        path = sweep.floor_record_path()
        path.parent.mkdir(exist_ok=True)
        _ = path.write_text(json.dumps({
            "measured_at": time.time() - 60, "free_bytes": 30 * GIB,
            "build_cache_bytes": sweep.directory_blocks(str(tree.parent)),
            "last_alert_at": None, "last_push_at": None,
        }))
        previous = self.saved_record()
        self.assertIsInstance(previous.held_roots, sweep.UnknownHeldRoots)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_not_called()

    def test_cache_growth_explains_free_space_fall(self) -> None:
        base = self.base()
        _, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        caches = sweep.directory_blocks(str(base / "repo" / "target"))
        self.prior(10 * GIB, caches - SIZE)
        _ = write_config_fixture(conf.path("lint"), "sweep_unexplained_fall_bytes=1\n")
        with mock.patch.object(sweep, "send_floor_alert") as send:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE // 2, 10 * GIB], recorded=True)
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
        _ = path.write_text(json.dumps(snapshot))
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)
        self.assertEqual(status, 0)
        message = cast(str, send.call_args.args[0])
        self.assertIn("/tmp/traces", message)
        self.assertIn("/tmp/traces/frame", message)
        self.assertIn("2026-10-04 19:53 PDT", message)
        self.assertIn("came after buildlog-disk's last measurement at 2026-10-04 19:53 PDT", message)
        self.assertNotIn("outside measured folders", message)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertIn("stale", message)
        self.assertFalse((base / "repo" / "target" / "debug" / "deps" / f"repo-{APP}").exists())

    def test_target_deleted_during_the_scan_is_no_unexplained_fall(self) -> None:
        """natedev 2026-10-25 23:06 AST: 298.1 GiB free at the previous sweep, 293.5 GiB before the
        scan; during the scan another process deleted 117.9 GiB of targets, and free read after it
        was 416.1 GiB. The read from before the scan gave a 122.6 GiB fall beyond cache growth."""
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        caches = sweep.directory_blocks(str(base / "repo" / "target"))
        deleted = int(117.9 * GIB)
        self.prior(int(298.1 * GIB), caches + deleted, age=3 * 60)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            status, _ = self.hold(base, 300 * GIB, [int(293.5 * GIB), int(422.6 * GIB)],
                                  scanned=int(416.1 * GIB), recorded=True)
        self.assertEqual(status, 0)
        send.assert_not_called()

    def test_fall_beyond_a_deletion_during_the_scan_still_alerts(self) -> None:
        """The same deletion, but free read after the scan gained only 95.3 GiB of the 117.9 GiB:
        22.6 GiB fell outside the build caches. The floor is 400 GiB, so free after the scan is still
        under it and the sweep takes cache."""
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        caches = sweep.directory_blocks(str(base / "repo" / "target"))
        self.prior(int(298.1 * GIB), caches + int(117.9 * GIB), age=3 * 60)
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            status, _ = self.hold(base, 400 * GIB, [int(293.5 * GIB), int(400.0 * GIB)],
                                  scanned=int(393.4 * GIB), recorded=True)
        self.assertEqual(status, 0)
        message = cast(str, send.call_args.args[0])
        self.assertIn("free space fell more than 10 GiB beyond build-cache growth", message)
        self.assertIn("rose 95.3 GiB while build caches shrank 117.9 GiB", message)

    def test_deletion_during_the_scan_that_lifts_free_above_the_floor_takes_nothing(self) -> None:
        """natedev 2026-10-25 23:06 AST: 293.5 GiB free before the scan, about 416 GiB after it;
        the shortfall from the first read took 6.5 GiB of cache."""
        base = self.base()
        _, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        status, output = self.hold(base, 300 * GIB, [int(293.5 * GIB), int(416.1 * GIB)],
                                   scanned=int(416.1 * GIB), recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(output_file.exists())
        self.assertIn("416.1 GiB free after the scan, not under the 300.0 GiB floor; nothing taken", output)
        self.assertNotIn("the floor took", output)

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
        _ = write_config_fixture(conf.path("lint"), f"sweep_unexplained_fall_bytes={SIZE}\n")
        with mock.patch.object(sweep, "send_floor_alert") as send:
            status, _ = self.hold(base, 20 * GIB, [20 * GIB - 3 * SIZE, 20 * GIB], recorded=True)
        self.assertEqual(status, 0)
        send.assert_not_called()
        record = self.saved_record()
        self.assertEqual(record.build_cache_bytes, sum(sweep.directory_blocks(str(path)) for path in (idle_tree.parent, busy_tree.parent, ci)))

    def test_old_record_does_not_trigger_growth_alert(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(30 * GIB, 0, age=31 * 60)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        send.assert_not_called()

    def test_large_removal_alerts_without_prior_record(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        self.assertIn("No earlier floor sweep", cast(str, send.call_args.args[0]))
        self.assertIn("measurement unavailable (missing)", cast(str, send.call_args.args[0]))
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()

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
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertFalse(output_file.exists())
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        message = cast(str, send.call_args.args[0])
        self.assertIn("No idle build cache is left to remove", message)
        self.assertIn("natedev still has only 9.0 GiB free. Builds need 10.0 GiB free", message)
        self.assertNotIn("Free ", message)
        lock_path = busy_tree / sweep.LOCK_NAMES[0]
        self.assertIn(f"- {busy_tree.parent} ({sweep.gib(held_bytes)}): pid {os.getpid()} holds the cargo lock {lock_path};"
                      + " no session found", message)
        self.assertIn(f"Not swept: CI's targets ({sweep.gib(ci_bytes)})", message)
        self.assertIn("No phone alert was attempted for this event.", message)
        self.assertNotIn("phone", send.call_args.kwargs)
        self.assertNotIn("Could not remove", message)
        record = self.saved_record()
        self.assertIsInstance(record.push_history, sweep.DeliveredAlert)
        self.assertIsInstance(record.alert_history, sweep.NoDeliveredAlert)

    def test_exhausted_cache_alerts_even_when_nothing_was_removed(self) -> None:
        base = self.base()
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertIn("removed 0.0 GiB", cast(str, send.call_args.args[0]))
        self.assertIn("Not swept: CI's targets (0.0 GiB).", cast(str, send.call_args.args[0]))

    @contextmanager
    def crossing_delivery(self, replies: dict[str, list[int]]) -> Generator[CrossingDelivery]:
        send_floor_alert = fixture_send_floor_alert
        send_holder_message = fixture_send_holder_message
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        delivery = CrossingDelivery(base)
        delivered: set[str] = set()

        def send(command: list[str], *, input: str | None = None, text: bool,
                 capture_output: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertTrue(text)
            self.assertTrue(capture_output)
            self.assertGreater(timeout, 0)
            recipient = command[command.index("--to") + 1]
            self.assertIn(recipient, ("natedev", "user"))
            message = input or ""
            if recipient == "user":
                text_id = command[command.index("--text-id") + 1]
                values = dict(command[position + 1].split("=", 1)
                              for position, argument in enumerate(command) if argument == "--value")
                rendered = render(text_id, **values)
                message = rendered.title + "\n" + rendered.message
            key = command[command.index("--key") + 1] if "--key" in command else ""
            status = replies[recipient].pop(0) if replies.get(recipient) else 0
            accepted = status == 0 and (not key or key not in delivered)
            if status == 0 and key:
                delivered.add(key)
            delivery.append(AlertSendAttempt(recipient, list(command), key, message, accepted,
                                             sweep.read_floor_record(sweep.floor_record_path())))
            return subprocess.CompletedProcess(command, status, "SENT: fixture" if accepted else "SKIPPED: fixture", "")

        with (mock.patch.object(subprocess, "run", side_effect=send),
              mock.patch.object(sweep, "disk_floor_channels", return_value=sweep.FloorAlertChannels.NATEDEV_AND_PHONE),
              mock.patch.object(sweep, "send_floor_alert", wraps=send_floor_alert),
              mock.patch.object(sweep, "send_phone_push", wraps=fixture_send_phone_push),
              mock.patch.object(sweep, "send_holder_message", wraps=send_holder_message),
              mock.patch.object(sweep, "read_disk_measurement", return_value=sweep.UnavailableDiskMeasurement("missing")),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            yield delivery

    def finalize_crossing(self, free_gib: int, *, protected: tuple[tuple[str, int], ...] = (), removed: int = 0) -> None:
        with mock.patch.object(sweep, "free_bytes", return_value=free_gib * GIB):
            sweep.notify_floor_problem(sweep.FloorOutcome(
                tmp_sweep.CompletedSweepAssessment(0), after=free_gib * GIB, protected=protected, removed=removed),
                lint_settings=conf.read("lint"))

    def test_crossing_sends_both_channels_once_and_rearms_only_above_the_line(self) -> None:
        with self.crossing_delivery({}) as delivery:
            for free, expected in ((200, 0), (140, 2), (130, 2), (150, 2), (140, 2), (151, 2), (140, 4)):
                with self.subTest(free=free, expected=expected):
                    self.finalize_crossing(free)
                    self.assertEqual(len(delivery.attempts), expected)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user"] * 2)
            for recipient in ("natedev", "user"):
                keys = [attempt.key for attempt in delivery.attempts if attempt.recipient == recipient]
                self.assertNotEqual(keys[0], keys[1])

    def test_crossing_retries_only_the_channel_without_a_receipt(self) -> None:
        for failed in ("user", "natedev"):
            with self.subTest(failed=failed), self.crossing_delivery({failed: [3, 0]}) as delivery:
                self.finalize_crossing(140)
                self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user"])
                self.finalize_crossing(130)
                self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user", failed])
                self.finalize_crossing(120)
                self.assertEqual(len(delivery.attempts), 3)
                self.assertEqual(sum(attempt.accepted for attempt in delivery.attempts), 2)

    def test_saved_crossing_receipts_suppress_sends_after_a_restart(self) -> None:
        run = subprocess.run
        with self.crossing_delivery({}) as attempts:
            self.finalize_crossing(140)
            self.assertEqual([attempt.recipient for attempt in attempts], ["natedev", "user"])
            script = """from unittest import mock
import sweep
import conf
with mock.patch.object(sweep, "free_bytes", return_value=130 * sweep.GIB), mock.patch.object(sweep, "send_floor_alert") as send:
    sweep.notify_floor_problem(sweep.tmp_sweep.CompletedSweepAssessment(0), lint_settings=conf.read("lint"))
    send.assert_not_called()
"""
            result = run([sys.executable, "-c", script], cwd=Path(sweep.__file__).parent,
                         text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.finalize_crossing(130)
            self.assertEqual(len(attempts), 2)

    def test_crossing_keys_prevent_duplicate_delivery_after_an_interrupted_receipt_save(self) -> None:
        for interrupted_channel in ("natedev", "user"):
            with self.subTest(channel=interrupted_channel), self.crossing_delivery({}) as delivery:
                save = sweep.write_floor_record
                interrupted = False

                def save_receipt(path: Path, record: sweep.FloorRecord) -> None:
                    nonlocal interrupted
                    if not interrupted and any(attempt.recipient == interrupted_channel for attempt in delivery.attempts):
                        interrupted = True
                        raise RuntimeError("fixture stops before the receipt save")
                    save(path, record)

                with mock.patch.object(sweep, "write_floor_record", side_effect=save_receipt):
                    with self.assertRaisesRegex(RuntimeError, "before the receipt save"):
                        self.finalize_crossing(140)
                self.assertTrue(interrupted)
                self.finalize_crossing(130)
                self.finalize_crossing(120)
                self.assertEqual([attempt.recipient for attempt in delivery.attempts if attempt.accepted], ["natedev", "user"])
                for recipient in ("natedev", "user"):
                    selected = [attempt for attempt in delivery.attempts if attempt.recipient == recipient]
                    self.assertTrue(selected)
                    keys = [attempt.key for attempt in selected]
                    commands = [attempt.command for attempt in selected]
                    self.assertEqual(len(set(keys)), 1)
                    for command in commands:
                        self.assertGreaterEqual(float(command[command.index("--repeat-minutes") + 1]), 60)

                for attempt in delivery.attempts:
                    saved = attempt.saved_record
                    self.assertIsInstance(saved, sweep.FloorRecord)
                    assert isinstance(saved, sweep.FloorRecord)
                    self.assertIsInstance(saved.phone_crossing, sweep.OpenPhoneCrossing)
                    assert isinstance(saved.phone_crossing, sweep.OpenPhoneCrossing)
                    self.assertIn(saved.phone_crossing.identity, attempt.key)

    def test_unsaved_crossing_identity_suppresses_phone_but_still_attempts_natedev(self) -> None:
        with self.crossing_delivery({}) as delivery:
            with mock.patch.object(sweep, "write_floor_record", side_effect=OSError(errno.ENOSPC, "fixture full")):
                self.finalize_crossing(140)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev"])

    def test_old_open_crossing_sends_its_first_phone_push_without_another_natedev_alert(self) -> None:
        with self.crossing_delivery({}) as delivery:
            path = sweep.floor_record_path()
            _ = path.write_text(json.dumps({
                "measured_at": time.time() - 60, "free_bytes": 140 * GIB,
                "build_cache_bytes": 0, "last_alert_at": None, "last_push_at": None,
                "phone_crossing": "under_pushed", "failure": "none",
            }))
            self.finalize_crossing(140)
            self.finalize_crossing(130)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["user"])

    def test_failure_alerts_never_send_a_phone_push_for_any_scope(self) -> None:
        with self.crossing_delivery({}) as delivery:
            scopes = (sweep.FloorFailureScope.FILESYSTEM,
                      sweep.WorkspaceFailureScope(("/private/workspace",)),
                      sweep.TemporarySweepFailureScope())
            with mock.patch.object(sweep, "free_bytes", return_value=200 * GIB):
                for scope in scopes:
                    sweep.notify_floor_problem(tmp_sweep.CompletedSweepAssessment(1),
                                               detail="private failure", scope=scope, lint_settings=conf.read("lint"))
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev"] * 3)
            self.assertTrue(all("private failure" in attempt.text for attempt in delivery.attempts))

    def test_failed_low_space_assessment_defers_phone_until_a_successful_assessment(self) -> None:
        with self.crossing_delivery({}) as delivery:
            with mock.patch.object(sweep, "free_bytes", return_value=140 * GIB):
                for status in (1, 1):
                    sweep.notify_floor_problem(tmp_sweep.CompletedSweepAssessment(status),
                                               detail="private failed pass", lint_settings=conf.read("lint"))
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev"])
            self.assertIn("private failed pass", delivery.attempts[0].text)
            self.assertEqual(self.saved_receipts(), (sweep.CrossingReceipt.ACCEPTED, sweep.CrossingReceipt.PENDING))
            self.finalize_crossing(140)
            self.finalize_crossing(130)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user"])
            self.assertNotIn("private failed pass", delivery.attempts[1].text)

    def test_disk_floor_push_body_has_one_recommendation_without_folder_lines(self) -> None:
        kind = sweep.FolderChoiceKind
        for state, choices, expected in (
            ("trash", sweep.AvailableFolderChoices((
                sweep.FolderChoice("~/.local/share/Trash", 26 * GIB, kind.TRASH),
                sweep.FolderChoice("/private/second", 12 * GIB, kind.OTHER),
            )), "Delete ~/.local/share/Trash (recommended): this frees 26.0 GiB, which covers the 10.0 GiB shortfall."),
            ("protected", sweep.AvailableFolderChoices((
                sweep.FolderChoice("/private/protected", 27 * GIB, kind.PROTECTED),
            )), "Review /private/protected first (recommended): deleting it frees 27.0 GiB, which covers the 10.0 GiB shortfall."),
            ("scratch", sweep.AvailableFolderChoices((
                sweep.FolderChoice("/private/scratch", 26 * GIB, kind.SCRATCH),
            )), "Review /private/scratch first (recommended): deleting it frees 26.0 GiB, which covers the 10.0 GiB shortfall."),
            ("other", sweep.AvailableFolderChoices((
                sweep.FolderChoice("/private/other", 26 * GIB, kind.OTHER),
            )), "Review /private/other first (recommended): deleting it frees 26.0 GiB, which covers the 10.0 GiB shortfall."),
            ("insufficient", sweep.InsufficientFolderChoices((
                sweep.FolderChoice("/private/largest", 6 * GIB, kind.OTHER),
                sweep.FolderChoice("/private/second", 2 * GIB, kind.OTHER),
            )), "The largest folder is /private/largest at 6.0 GiB. Deleting it frees only part of the 10.0 GiB shortfall; the natedev session lists every folder."),
            ("unavailable", sweep.UNAVAILABLE_FOLDER_CHOICES,
             "Folder sizes are unknown. Measure (recommended): natedev lists folder sizes, then you choose. Delete nothing: free space stays low."),
        ):
            with self.subTest(state=state):
                push = sweep.disk_floor_push(140 * GIB, 150 * GIB, choices)
                self.assertEqual(push.title, "natedev disk is low; builds can fail")
                self.assertEqual(push.body, "natedev has 140.0 GiB free, below the 150.0 GiB alert line, so builds can fail. " + expected)
                assert isinstance(push.action, ActionRequired)
                self.assertEqual(push.action.text,
                    "In the natedev Claude session, reply measure or delete nothing." if state == "unavailable"
                    else "In the natedev Claude session, say which folders to delete.")

    def test_crossing_session_keeps_trash_and_scratch_effects(self) -> None:
        for folder, effect, recommendation in (
            ("/private/Trash", "Deleting it removes discarded files.",
             "Delete /private/Trash (recommended): this frees 26.0 GiB, which covers the 10.0 GiB shortfall."),
            ("/private/scratch/work", "Deleting it removes temporary work; check for work you still need.",
             "Review /private/scratch/work first (recommended): deleting it frees 26.0 GiB, which covers the 10.0 GiB shortfall."),
        ):
            with self.subTest(folder=folder), self.crossing_delivery({}) as delivery:
                measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [
                    sweep.OutsideCacheDirectory(folder, 26 * GIB, 0, sweep.NoLargestGrowingChild()),
                ], 0)
                with mock.patch.object(sweep, "read_disk_measurement", return_value=measurement):
                    self.finalize_crossing(140)
                message = delivery.attempts[0].text
                lines = [
                    "Free disk space fell below the 150.0 GiB alert line. 140.0 GiB remains.",
                    "The cleanup run freed nothing.",
                    f"{folder}: holds 26.0 GiB. {effect}",
                    recommendation,
                    "Delete nothing: builds can fail when the disk is full.",
                    "natedev checks your choice before it deletes anything.",
                ]
                self.assertEqual(message.splitlines(), lines)
                self.assertIn(recommendation, phone_text(delivery.attempts))

    def test_unavailable_crossing_measurement_names_no_folder(self) -> None:
        stale = sweep.AvailableDiskMeasurement(
            datetime.fromtimestamp(time.time() - 600, UTC), sweep.NoEarlierDiskMeasurement(), [
                sweep.OutsideCacheDirectory("/private/stale-choice", 200 * GIB, 0, sweep.NoLargestGrowingChild()),
            ], 0)
        measurements = [sweep.UnavailableDiskMeasurement(reason) for reason in ("missing", "unreadable", "old shape")]
        for measurement in [*measurements, stale]:
            with self.subTest(measurement=measurement), self.crossing_delivery({}) as delivery:
                _ = write_config_fixture(conf.path("lint"), "sweep_snapshot_max_age_seconds=60\n")
                with mock.patch.object(sweep, "read_disk_measurement", return_value=measurement):
                    self.finalize_crossing(140)
                text = phone_text(delivery.attempts)
                self.assertIn("In the natedev Claude session, reply measure or delete nothing.", text)
                self.assertIn("Folder sizes are unknown. Measure (recommended): natedev lists folder sizes, then you choose. Delete nothing: free space stays low.", text)
                self.assertNotIn("/", text)
                message = delivery.attempts[0].text
                self.assertNotIn("/private/stale-choice", message)
                self.assertNotIn("If the disk fills, builds fail.", message)
                lines = [
                    "Free disk space fell below the 150.0 GiB alert line. 140.0 GiB remains.",
                    "The cleanup run freed nothing.",
                    "Folder sizes are unknown.",
                    "",
                    "Measure first (recommended): natedev lists folder sizes, then you choose what to delete.",
                    "",
                    "Delete nothing: the disk stays low and builds can fail.",
                    "natedev checks your choice before it deletes anything.",
                ]
                self.assertEqual(message.splitlines(), lines)

    def test_crossing_recommends_one_measured_choice_that_frees_the_shortfall(self) -> None:
        with self.crossing_delivery({}) as delivery:
            measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [
                sweep.OutsideCacheDirectory("/tmp/claude/large/target", 180 * GIB, 0, sweep.NoLargestGrowingChild()),
                sweep.OutsideCacheDirectory("/tmp/claude/small/target", 20 * GIB, 0, sweep.NoLargestGrowingChild()),
            ], 0)
            with (mock.patch.object(sweep, "read_disk_measurement", return_value=measurement),
                  mock.patch.object(sweep, "folder_choices", wraps=sweep.folder_choices) as choices):
                self.finalize_crossing(140, removed=2 * GIB)
            choices.assert_called_once()
            recommendation = ("Review /tmp/claude/large/target first (recommended): deleting it frees 180.0 GiB, "
                              "which covers the 10.0 GiB shortfall.")
            text = phone_text(delivery.attempts)
            self.assertIn(recommendation, text)
            self.assertNotIn("/tmp/claude/small/target", text)
            self.assertIn("In the natedev Claude session, say which folders to delete.", text)
            message = delivery.attempts[0].text
            lines = [
                "Free disk space fell below the 150.0 GiB alert line. 140.0 GiB remains.",
                "Cleanup removed 2.0 GiB.",
                "/tmp/claude/large/target: holds 180.0 GiB. Deleting it removes its contents; their use is unknown.",
                "/tmp/claude/small/target: holds 20.0 GiB. Deleting it removes its contents; their use is unknown.",
                recommendation,
                "Delete nothing: builds can fail when the disk is full.",
                "natedev checks your choice before it deletes anything.",
            ]
            self.assertEqual(message.splitlines(), lines)

    def test_crossing_names_the_largest_choice_when_no_choice_frees_enough(self) -> None:
        with self.crossing_delivery({}) as delivery:
            measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [
                sweep.OutsideCacheDirectory("/tmp/claude/largest", 60 * GIB, 0, sweep.NoLargestGrowingChild()),
                sweep.OutsideCacheDirectory("/tmp/claude/small/target", 20 * GIB, 0, sweep.NoLargestGrowingChild()),
                sweep.OutsideCacheDirectory("/tmp/claude/fifth", 5 * GIB, 0, sweep.NoLargestGrowingChild()),
                sweep.OutsideCacheDirectory("/tmp/claude/third", 10 * GIB, 0, sweep.NoLargestGrowingChild()),
                sweep.OutsideCacheDirectory("/tmp/claude/fourth", 8 * GIB, 0, sweep.NoLargestGrowingChild()),
            ], 0)
            with mock.patch.object(sweep, "read_disk_measurement", return_value=measurement):
                self.finalize_crossing(0)
            recommendation = ("The largest folder is /tmp/claude/largest at 60.0 GiB. "
                              "Deleting it frees only part of the 150.0 GiB shortfall; the natedev session lists every folder.")
            text = phone_text(delivery.attempts)
            self.assertIn(recommendation, text)
            self.assertIn("In the natedev Claude session, say which folders to delete.", text)
            self.assertNotIn("/tmp/claude/small/target", text)
            message = delivery.attempts[0].text
            session_fact = ("The largest folder is /tmp/claude/largest at 60.0 GiB. "
                            "Deleting it frees only part of the 150.0 GiB shortfall.")
            self.assertIn("The cleanup run freed nothing.", message)
            self.assertIn("/tmp/claude/largest: holds 60.0 GiB", message)
            self.assertIn("/tmp/claude/small/target: holds 20.0 GiB", message)
            self.assertLess(message.index("/tmp/claude/largest"), message.index("/tmp/claude/small/target"))
            lines = [
                "Free disk space fell below the 150.0 GiB alert line. 0.0 GiB remains.",
                "The cleanup run freed nothing.",
                "/tmp/claude/largest: holds 60.0 GiB. Deleting it removes its contents; their use is unknown.",
                "/tmp/claude/small/target: holds 20.0 GiB. Deleting it removes its contents; their use is unknown.",
                "/tmp/claude/third: holds 10.0 GiB. Deleting it removes its contents; their use is unknown.",
                "/tmp/claude/fourth: holds 8.0 GiB. Deleting it removes its contents; their use is unknown.",
                "/tmp/claude/fifth: holds 5.0 GiB. Deleting it removes its contents; their use is unknown.",
                session_fact,
                "Delete nothing: builds can fail when the disk is full.",
                "natedev checks your choice before it deletes anything.",
            ]
            self.assertEqual(message.splitlines(), lines)
            self.assertNotIn("the natedev session lists every folder", message)

    def test_crossing_recommends_one_insufficient_folder_with_its_remaining_risk(self) -> None:
        with self.crossing_delivery({}) as delivery:
            measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [
                sweep.OutsideCacheDirectory("/tmp/claude/only/target", 60 * GIB, 0, sweep.NoLargestGrowingChild()),
            ], 0)
            with mock.patch.object(sweep, "read_disk_measurement", return_value=measurement):
                self.finalize_crossing(0)
            recommendation = ("The largest folder is /tmp/claude/only/target at 60.0 GiB. "
                              "Deleting it frees only part of the 150.0 GiB shortfall; the natedev session lists every folder.")
            self.assertIn(recommendation, phone_text(delivery.attempts))
            message = delivery.attempts[0].text
            self.assertIn("/tmp/claude/only/target: holds 60.0 GiB", message)
            session_fact = ("The largest folder is /tmp/claude/only/target at 60.0 GiB. "
                            "Deleting it frees only part of the 150.0 GiB shortfall.")
            self.assertIn(session_fact, message)
            self.assertRegex(message, r"Delete nothing:.*builds can fail")
            self.assertLess(message.index(session_fact), message.index("Delete nothing:"))
            self.assertNotIn("the natedev session lists every folder", message)

    def test_crossing_labels_protected_sizes_without_a_holder(self) -> None:
        with self.crossing_delivery({}) as delivery:
            protected = (("/private/protected", 27 * GIB),)
            self.finalize_crossing(140, protected=protected)
            self.assertEqual(len(delivery.attempts), 2)
            for attempt in delivery.attempts:
                self.assertIn("/private/protected", attempt.text)
                self.assertIn("27.0 GiB", attempt.text)
                self.assertNotIn("in use", attempt.text)
                self.assertNotIn("pid ", attempt.text)
            recommendation = ("Review /private/protected first (recommended): deleting it frees 27.0 GiB, "
                              "which covers the 10.0 GiB shortfall.")
            self.assertIn(recommendation, phone_text(delivery.attempts))
            self.assertIn("In the natedev Claude session, say which folders to delete.", phone_text(delivery.attempts))
            message = delivery.attempts[0].text
            self.assertIn("It is protected", message)
            self.assertIn("The cleanup run freed nothing.", message)
            self.assertIn("holds 27.0 GiB", message)
            self.assertRegex(message, r"(?i)(no build record|no recorded evidence)")
            self.assertIn(recommendation, message)
            self.assertIn("Delete nothing: builds can fail when the disk is full.", message)
            self.assertLess(message.index(recommendation), message.index("Delete nothing:"))
            self.assertNotIn("Keep it", message)
            self.assertEqual(message.count("Review "), 1)
            self.assertEqual(sweep.floor_holders({"/private/protected": sweep.NoRecordedEvidence("/private/protected")},
                                                {"/private/protected": 27 * GIB}), [])

    def test_measured_target_folder_has_no_cargo_or_rebuild_claim(self) -> None:
        with self.crossing_delivery({}) as delivery:
            folder = str(delivery.base / "unknown" / "target")
            measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [
                sweep.OutsideCacheDirectory(folder, 26 * GIB, 0, sweep.NoLargestGrowingChild()),
            ], 0)
            with mock.patch.object(sweep, "read_disk_measurement", return_value=measurement):
                self.finalize_crossing(140)
            for attempt in delivery.attempts:
                self.assertIn(folder, attempt.text)
                self.assertNotIn("Cargo", attempt.text)
                self.assertNotIn("compiled", attempt.text)
                self.assertNotIn("rebuild", attempt.text)
            message = delivery.attempts[0].text
            self.assertIn("holds 26.0 GiB", message)
            self.assertIn("their use is unknown", message)

    def test_failure_after_an_interrupted_crossing_receipt_uses_no_crossing_key(self) -> None:
        replies: dict[str, list[int]] = {}
        with self.crossing_delivery(replies) as delivery:
            save = sweep.write_floor_record

            def stop_after_send(path: Path, record: sweep.FloorRecord) -> None:
                if delivery.attempts:
                    raise RuntimeError("fixture stops before receipt save")
                save(path, record)

            with mock.patch.object(sweep, "write_floor_record", side_effect=stop_after_send):
                with self.assertRaisesRegex(RuntimeError, "before receipt save"):
                    self.finalize_crossing(140)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev"])
            self.assertTrue(delivery.attempts[0].accepted)
            replies["natedev"] = [3, 0]
            with mock.patch.object(sweep, "free_bytes", return_value=140 * GIB):
                for expected in (sweep.FailurePending, sweep.FailureReported):
                    sweep.notify_floor_problem(tmp_sweep.CompletedSweepAssessment(1),
                        detail="new failed pass", lint_settings=conf.read("lint"))
                    attempt = delivery.attempts[-1]
                    self.assertEqual(attempt.key, "")
                    self.assertIn("new failed pass", attempt.text)
                    self.assertIsInstance(self.saved_record().failure, expected)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev"] * 3)
            self.assertEqual([attempt.accepted for attempt in delivery.attempts], [True, False, True])

    def test_unsaved_crossings_use_one_fixed_natedev_key_and_no_phone(self) -> None:
        with self.crossing_delivery({}) as delivery:
            with mock.patch.object(sweep, "write_floor_record", side_effect=OSError(errno.ENOSPC, "fixture full")):
                self.finalize_crossing(140)
                self.finalize_crossing(130)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "natedev"])
            self.assertEqual([attempt.key for attempt in delivery.attempts], ["natedev:unsaved-crossing"] * 2)
            self.assertEqual(sum(attempt.accepted for attempt in delivery.attempts), 1)
            interval = conf.read("lint").number("sweep_alert_interval_seconds") / 60
            for attempt in delivery.attempts:
                self.assertEqual(float(attempt.command[attempt.command.index("--repeat-minutes") + 1]), interval)

    def test_disk_floor_push_fit_falls_back_at_each_limit(self) -> None:
        _ = self.base()
        choices = sweep.AvailableFolderChoices((
            sweep.FolderChoice("/private/" + "large" * 60, 180 * GIB, sweep.FolderChoiceKind.OTHER),
            sweep.FolderChoice("/private/small", 20 * GIB, sweep.FolderChoiceKind.OTHER),
        ))
        named = sweep.disk_floor_push(140 * GIB, 150 * GIB, choices)
        unavailable = sweep.disk_floor_push(140 * GIB, 150 * GIB, sweep.UNAVAILABLE_FOLDER_CHOICES)
        for state, selected, rendered, fallback in (
            ("measured", choices, named, unavailable),
            ("unavailable", sweep.UNAVAILABLE_FOLDER_CHOICES, unavailable, sweep.NO_PHONE_ALERT),
        ):
            for offset in (-1, 0, 1):
                with self.subTest(state=state, offset=offset), redirect_stderr(io.StringIO()):
                    _ = write_config_fixture(conf.path("lint"), f"sweep_push_text_limit={len(rendered.message) + offset}\n")
                    push = sweep.fit_disk_floor_push(140 * GIB, 150 * GIB, selected, lint_settings=conf.read("lint"))
                    self.assertEqual(push, rendered if offset == 1 else fallback)

    def test_protected_recommendation_names_its_folder_among_several_choices(self) -> None:
        with self.crossing_delivery({}) as delivery:
            self.finalize_crossing(140, protected=(("/private/larger", 27 * GIB), ("/private/smaller", 20 * GIB)))
            recommendation = ("Review /private/larger first (recommended): deleting it frees 27.0 GiB, "
                              "which covers the 10.0 GiB shortfall.")
            text = phone_text(delivery.attempts)
            self.assertIn(recommendation, text)
            self.assertNotIn("/private/smaller", text)
            message = delivery.attempts[0].text
            self.assertIn("/private/larger: holds 27.0 GiB", message)
            self.assertIn("/private/smaller: holds 20.0 GiB", message)
            self.assertIn(recommendation, message)
            self.assertNotIn("Delete it (recommended)", message)
            self.assertEqual(sweep.floor_holders({path: sweep.NoRecordedEvidence(path) for path in
                ("/private/larger", "/private/smaller")}, {"/private/larger": 27 * GIB, "/private/smaller": 20 * GIB}), [])

    def test_phone_floor_crossing_sends_once_until_free_space_rises(self) -> None:
        with self.crossing_delivery({}) as delivery:
            base = delivery.base
            send = cast(mock.Mock, sweep.send_floor_alert)
            for free, expected in ((200, 0), (140, 1), (130, 1), (151, 1), (140, 2)):
                self.assertEqual(self.hold(base, 300 * GIB, [free * GIB, free * GIB], recorded=True)[0], 0)
                messages = phone_calls(send)
                self.assertEqual(len(messages), expected)
                for call in messages:
                    self.assertIn("Free disk space fell", call)
                self.assertEqual(sum(attempt.recipient == "user" for attempt in delivery.attempts), expected)
            self.assertEqual(self.saved_receipts(), (sweep.CrossingReceipt.ACCEPTED, sweep.CrossingReceipt.ACCEPTED))

    def test_phone_floor_crossing_retries_a_failed_delivery(self) -> None:
        with self.crossing_delivery({"natedev": [3, 0], "user": [3, 0]}) as delivery:
            base = delivery.base
            send = cast(mock.Mock, sweep.send_floor_alert)
            self.prior(200 * GIB, 0, push_at=time.time())
            self.assertEqual(self.hold(base, 300 * GIB, [200 * GIB, 200 * GIB], recorded=True)[0], 0)
            self.assertEqual(self.hold(base, 300 * GIB, [140 * GIB, 140 * GIB], recorded=True)[0], 0)
            self.assertEqual(self.saved_receipts(), (sweep.CrossingReceipt.PENDING, sweep.CrossingReceipt.PENDING))
            self.assertEqual(self.hold(base, 300 * GIB, [130 * GIB, 130 * GIB], recorded=True)[0], 0)
            self.assertEqual(send.call_count, 2)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user"] * 2)

    def test_workspace_failure_episodes_survive_contention_and_recover_independently(self) -> None:
        base = self.base()
        first, _ = cargo_target(base, "first", APP, time.time() - DAY)
        second, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
        with (mock.patch.object(sweep, "hold_floor", return_value=0),
              mock.patch.object(sweep, "sweep", return_value=1) as assess,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              mock.patch.object(os, "getcwd") as cwd,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            def run(tree: Path, assessed: int, expected: int) -> None:
                assess.return_value = assessed
                cwd.return_value = str(tree.parent.parent)
                self.assertEqual(sweep.main([sweep.TARGET_DIR_FLAG, str(tree.parent)]), assessed)
                self.assertEqual(send.call_count, expected)

            for tree, expected in ((first, 1), (second, 2)):
                run(tree, 1, expected)
                with (tree / sweep.LOCK_NAMES[0]).open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    calls = assess.call_count
                    self.assertEqual(sweep.main([sweep.TARGET_DIR_FLAG, str(tree.parent)]), 0)
                    self.assertEqual(assess.call_count, calls)
                run(tree, 1, expected)
            run(first, 0, 2)
            run(second, 1, 2)
            run(first, 1, 3)
            run(second, 0, 3)
            run(second, 1, 4)
        self.assertEqual(send.call_count, 4)
        messages = [cast(str, call.args[0]) for call in send.call_args_list]
        for message, tree in zip(messages, (first, second, first, second)):
            self.assertIn(str(tree.parent.parent), message)
            self.assertIn("status 1", message)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertTrue(all("phone" not in call.kwargs for call in send.call_args_list))

    def test_workspace_root_and_member_share_one_failure_episode(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "workspace", APP, time.time() - DAY)
        workspace = tree.parent.parent
        member = workspace / "member"
        member.mkdir()
        with (mock.patch.object(sweep, "cargo_roots", return_value=[str(tree.parent)]),
              mock.patch.object(sweep, "sweep", return_value=1) as assess,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              mock.patch.object(os, "getcwd", return_value=str(workspace)) as cwd,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            for folder in (workspace, member):
                cwd.return_value = str(folder)
                self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 1)
            self.assertEqual(send.call_count, 1)
            record, = base.glob("workspace-*.json")
            for folder in (member, workspace):
                cwd.return_value = str(folder)
                assess.return_value = 0
                self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 0)
                saved = sweep.read_floor_record(record)
                assert isinstance(saved, sweep.FloorRecord)
                self.assertIsInstance(saved.failure, sweep.NoFailure)
                assess.return_value = 1
                self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 1)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(list(base.glob("workspace-*.json")), [record])
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()

    def test_unavailable_workspace_roots_keep_the_reported_failure_episode(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "workspace", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "cargo_roots", side_effect=[[str(tree.parent)], [], [str(tree.parent)]]),
              mock.patch.object(sweep, "sweep", return_value=1) as assess,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              mock.patch.object(os, "getcwd", return_value=str(tree.parent.parent)),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 1)
            record, = base.glob("workspace-*.json")
            reported = record.read_bytes()
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 0)
            self.assertEqual(record.read_bytes(), reported)
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, 60), 1)
        self.assertEqual(assess.call_count, 2)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(list(base.glob("workspace-*.json")), [record])

    def test_clean_workspace_without_a_record_creates_no_failure_state_files(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "workspace", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "sweep", return_value=0),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.sweep_workspace(str(tree.parent), conf.read("lint"), False, 60), 0)
        self.assertEqual(list(base.glob("workspace-*.json")), [])
        self.assertEqual(list(base.glob("workspace-*-alert.lock")), [])
        send.assert_not_called()

    def test_temporary_sweep_failure_episode_survives_not_due_and_recovers(self) -> None:
        base = self.base()
        now = time.time()
        with (mock.patch.object(sweep, "hold_floor", return_value=0),
              mock.patch.object(tmp_sweep, "run", return_value=2) as assess,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              mock.patch.object(time, "time", return_value=now) as clock,
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 2)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 0)
            self.assertEqual(assess.call_count, 1)
            clock.return_value = now + tmp_sweep.RUN_INTERVAL_SECONDS
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 2)
            self.assertEqual(send.call_count, 1)
            assess.return_value = 0
            clock.return_value += tmp_sweep.RUN_INTERVAL_SECONDS
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 0)
            assess.return_value = 2
            clock.return_value += tmp_sweep.RUN_INTERVAL_SECONDS
            self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 2)
        self.assertEqual(send.call_count, 2)
        self.assertTrue((base / tmp_sweep.STAMP_NAME).is_file())
        self.assertTrue(all("status 2" in cast(str, call.args[0]) for call in send.call_args_list))
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertTrue(all("phone" not in call.kwargs for call in send.call_args_list))

    def test_failed_sweep_sends_only_a_natedev_alert(self) -> None:
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        _, output = cargo_target(base, "repo", APP, time.time() - DAY)
        with (mock.patch.object(sweep, "remove", return_value=sweep.ConfirmedDeletion(0, 1)),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            self.assertEqual(self.hold(base, 300 * GIB, [200 * GIB, 200 * GIB], recorded=True)[0], 1)
            failure_calls = [call for call in send.call_args_list if "sweep failed" in cast(str, call.args[0])]
            self.assertEqual(len(failure_calls), 1)
            cast(mock.Mock, sweep.send_phone_push).assert_not_called()
            self.assertNotIn("phone", failure_calls[0].kwargs)
        self.assertTrue(output.exists())

    def test_late_completion_uses_current_space_for_one_crossing(self) -> None:
        with self.crossing_delivery({}) as delivery:
            with mock.patch.object(sweep, "free_bytes", return_value=130 * GIB):
                for older_reading in (140, 200, 130):
                    sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(0), 0, older_reading * GIB, older_reading * GIB, 0),
                                               lint_settings=conf.read("lint"))
            send = cast(mock.Mock, sweep.send_floor_alert)
            self.assertEqual(send.call_count, 1)
            self.assertEqual([attempt.recipient for attempt in delivery.attempts], ["natedev", "user"])
        self.assertEqual(self.saved_receipts(), (sweep.CrossingReceipt.ACCEPTED, sweep.CrossingReceipt.ACCEPTED))

    def test_above_cleanup_floor_rearms_the_phone_crossing(self) -> None:
        with self.crossing_delivery({}) as delivery:
            base = delivery.base
            send = cast(mock.Mock, sweep.send_floor_alert)
            for free in (140, 320, 140):
                self.assertEqual(self.hold(base, 300 * GIB, [free * GIB], recorded=True)[0], 0)
        self.assertEqual(len(phone_calls(send)), 2)

    def test_contended_pass_keeps_crossing_and_failure_states(self) -> None:
        base = self.base()
        path = sweep.floor_record_path()
        previous = sweep.FloorRecord(time.time(), 140 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert(),
                                     phone_crossing=sweep.OpenPhoneCrossing("private-pending"),
                                     failure=sweep.FailurePending(sweep.FloorFailureEvent(1, "fixture failure")))
        sweep.write_floor_record(path, previous)
        with (path.with_suffix(".lock").open("a+") as lock,
              mock.patch.object(sweep, "alert_floor_lock"),
              mock.patch.object(sweep, "send_floor_alert") as send):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.hold(base, 300 * GIB, [140 * GIB], recorded=True)[0], 0)
        send.assert_not_called()
        self.assertEqual(sweep.read_floor_record(path), previous)

    def test_lock_alert_error_keeps_the_contended_pass_skipped(self) -> None:
        base = self.base()
        path = sweep.floor_record_path()
        previous = sweep.FloorRecord(time.time(), 140 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert(),
                                     phone_crossing=sweep.OpenPhoneCrossing("private-pending"),
                                     failure=sweep.FailurePending(sweep.FloorFailureEvent(1, "fixture failure")))
        sweep.write_floor_record(path, previous)
        error_output = io.StringIO()
        with (path.with_suffix(".lock").open("a+") as lock,
              mock.patch.object(sweep, "alert_floor_lock", side_effect=OSError("fixture lock alert failed")),
              mock.patch.object(sweep, "send_floor_alert") as send,
              redirect_stderr(error_output)):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.hold(base, 300 * GIB, [140 * GIB], recorded=True)[0], 0)
        send.assert_not_called()
        self.assertEqual(sweep.read_floor_record(path), previous)
        self.assertIn("fixture lock alert failed", error_output.getvalue())

    def test_failure_before_scan_reaches_the_finalizer(self) -> None:
        base = self.base()
        with (mock.patch.object(sweep, "recover_pending_trims", side_effect=OSError("fixture recovery failed")),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              redirect_stderr(io.StringIO())):
            self.assertEqual(self.hold(base, 300 * GIB, [200 * GIB], recorded=True)[0], 1)
        self.assertIn("fixture recovery failed", cast(str, send.call_args.args[0]))
        saved = self.saved_record()
        self.assertIsInstance(saved.failure, sweep.FailureReported)

    def test_notification_finishes_before_the_acquired_floor_lock_closes(self) -> None:
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")

        def assert_locked() -> None:
            held = sweep.try_lock(str(sweep.floor_record_path().with_suffix(".lock")))
            if held is not None:
                os.close(held)
            self.assertIsNone(held)

        def deliver(message: str, **settings: object) -> sweep.FloorChannelOutcome:
            self.assertIsInstance(settings["lint_settings"], conf.ConfigRead)
            if message.startswith("Free disk space fell"):
                assert_locked()
            return DELIVERED_NATEDEV_ALERT

        def push(message: object, **settings: object) -> sweep.FloorChannelOutcome:
            self.assertIsNotNone(message)
            self.assertIsInstance(settings["lint_settings"], conf.ConfigRead)
            assert_locked()
            return sweep.FloorChannelOutcome.DELIVERED

        with (mock.patch.object(sweep, "disk_floor_channels", return_value=sweep.FloorAlertChannels.NATEDEV_AND_PHONE),
              mock.patch.object(sweep, "send_floor_alert", side_effect=deliver) as send,
              mock.patch.object(sweep, "send_phone_push", side_effect=push) as phone):
            self.assertEqual(self.hold(base, 300 * GIB, [140 * GIB], recorded=True)[0], 0)
        self.assertEqual(len(phone_calls(send)), 1)
        self.assertEqual(phone.call_count, 1)
        guard = sweep.try_lock(str(sweep.floor_record_path().with_suffix(".lock")))
        self.assertIsNotNone(guard)
        if guard is not None:
            os.close(guard)

    def test_nonroot_failure_does_not_change_the_root_crossing(self) -> None:
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        root_path = sweep.floor_record_path()
        root = sweep.FloorRecord(time.time(), 200 * GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert())
        sweep.write_floor_record(root_path, root)
        token = sweep.FLOOR_FILESYSTEM.set(sweep.FilesystemFloor(2, str(base), 300 * GIB, (), False))
        try:
            with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
                sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(1), 0, 100 * GIB, 100 * GIB, 0),
                                           lint_settings=conf.read("lint"))
            self.assertEqual(send.call_count, 1)
            other = self.saved_record()
            self.assertIsInstance(other.phone_crossing, sweep.AbovePhoneFloor)
            with mock.patch.object(sweep, "send_floor_alert") as recovered_send:
                sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(0), 0, 100 * GIB, 100 * GIB, 0),
                                           lint_settings=conf.read("lint"))
            recovered_send.assert_not_called()
            recovered = self.saved_record()
            self.assertIsInstance(recovered.failure, sweep.NoFailure)
        finally:
            sweep.FLOOR_FILESYSTEM.reset(token)
        self.assertEqual(sweep.read_floor_record(root_path), root)

    def test_full_record_storage_still_delivers_the_crossing(self) -> None:
        _ = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        with (mock.patch.object(sweep, "free_bytes", return_value=140 * GIB),
              mock.patch.object(sweep, "write_floor_record", side_effect=OSError(errno.ENOSPC, "fixture full")),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              redirect_stderr(io.StringIO())):
            sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(0), 0, 140 * GIB, 140 * GIB, 0),
                                       lint_settings=conf.read("lint"))
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertNotIn("phone", send.call_args.kwargs)
        self.assertIn("Free disk space fell", cast(str, send.call_args.args[0]))

    def test_unavailable_record_allows_one_unsaved_delivery(self) -> None:
        _ = self.base()
        with (mock.patch.object(sweep, "locked_floor_record", side_effect=OSError(errno.ENOSPC, "fixture full")),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              redirect_stderr(io.StringIO())):
            sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(1), 0, 200 * GIB, 200 * GIB, 0),
                                       lint_settings=conf.read("lint"), scope=sweep.FloorFailureScope.DISCOVERY)
        self.assertEqual(send.call_count, 1)

    def test_failure_episode_needs_recovery_before_another_natedev_alert(self) -> None:
        _ = self.base()
        with (mock.patch.object(sweep, "free_bytes", return_value=200 * GIB),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            for outcome in (tmp_sweep.CompletedSweepAssessment(1), tmp_sweep.CompletedSweepAssessment(1),
                            tmp_sweep.SkippedSweepAssessment(),
                            tmp_sweep.CompletedSweepAssessment(1), tmp_sweep.CompletedSweepAssessment(0),
                            tmp_sweep.CompletedSweepAssessment(1)):
                sweep.notify_floor_problem(outcome, lint_settings=conf.read("lint"))
        self.assertEqual(send.call_count, 2)

    def test_successful_assessment_clears_an_undelivered_failure_without_a_push(self) -> None:
        _ = self.base()
        with (mock.patch.object(sweep, "free_bytes", return_value=320 * GIB),
              mock.patch.object(sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send):
            sweep.notify_floor_problem(tmp_sweep.CompletedSweepAssessment(1), lint_settings=conf.read("lint"),
                                       detail="private failure detail")
            pending = self.saved_record()
            self.assertIsInstance(pending.failure, sweep.FailurePending)
            assert isinstance(pending.failure, sweep.FailurePending)
            self.assertEqual(pending.failure.event.detail, "private failure detail")
            sweep.notify_floor_problem(tmp_sweep.CompletedSweepAssessment(0), lint_settings=conf.read("lint"))
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertNotIn("phone", send.call_args.kwargs)
        saved = self.saved_record()
        self.assertIsInstance(saved.failure, sweep.NoFailure)

    def test_failed_natedev_failure_delivery_retries_until_reported(self) -> None:
        _ = self.base()
        with (mock.patch.object(sweep, "free_bytes", return_value=200 * GIB),
              mock.patch.object(sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send):
            sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(1), 0, 200 * GIB, 200 * GIB, 0),
                                       lint_settings=conf.read("lint"), detail="private exception detail")
            pending = self.saved_record()
            self.assertIsInstance(pending.failure, sweep.FailurePending)
            sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(1), 0, 200 * GIB, 200 * GIB, 0),
                                       lint_settings=conf.read("lint"), detail="later failure detail")
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            cast(mock.Mock, sweep.send_phone_push).assert_not_called()
            self.assertNotIn("phone", call.kwargs)
            self.assertIn("private exception detail", cast(str, call.args[0]))
            self.assertNotIn("later failure detail", cast(str, call.args[0]))
        reported = self.saved_record()
        self.assertIsInstance(reported.failure, sweep.FailureReported)

    def test_low_disk_failure_sends_one_natedev_message_for_the_episode(self) -> None:
        _ = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        with (mock.patch.object(sweep, "free_bytes", return_value=140 * GIB),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            for status in (1, 1):
                sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(status), 0, 140 * GIB, 140 * GIB, 0),
                                           lint_settings=conf.read("lint"), detail="private exception detail")
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertNotIn("phone", send.call_args.kwargs)
        self.assertIn("private exception detail", cast(str, send.call_args.args[0]))

    def test_unreadable_free_space_does_not_stop_later_filesystems(self) -> None:
        base = self.base()
        first = sweep.FilesystemFloor(1, str(base / "bad"), 100, (), True)
        second = sweep.FilesystemFloor(2, str(base / "good"), 100, (), False)
        measured: list[str] = []
        def available(path: str) -> int:
            measured.append(path)
            if path == first.probe_path:
                raise OSError("fixture free-space read failed")
            return 200
        with (mock.patch.object(sweep, "discover_cargo_outputs", return_value=([], sweep.CargoOutputIndex())),
              mock.patch.object(sweep, "filesystem_floors", return_value=[first, second]),
              mock.patch.object(sweep, "free_bytes", side_effect=available),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send,
              redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.hold_floor(100, False), 1)
        self.assertIn(second.probe_path, measured)
        self.assertEqual(send.call_count, 1)

    def test_under_floor_with_removable_units_left_sends_no_push(self) -> None:
        base = self.base()
        _, first = cargo_target(base, "first", APP, time.time() - 2 * DAY)
        _, second = cargo_target(base, "second", DEMO, time.time() - DAY)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB - 1], recorded=True)[0], 0)
        self.assertTrue(first.exists() or second.exists())
        send.assert_not_called()

    def test_natedev_alert_hour_does_not_hold_back_push(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, alert_at=time.time() - 10 * 60)
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()

    def test_recent_push_holds_next_push_for_its_own_hour(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, push_at=time.time() - 10 * 60)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        send.assert_not_called()

    def test_push_older_than_hour_is_due_again(self) -> None:
        base = self.base()
        self.prior(10 * GIB, 0, push_at=time.time() - 61 * 60)
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()

    def test_failed_push_does_not_start_its_hour(self) -> None:
        base = self.base()
        with mock.patch.object(sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
            first = self.saved_record()
            self.assertIsInstance(first.push_history, sweep.NoDeliveredAlert)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 2)
        latest = self.saved_record()
        self.assertIsInstance(latest.push_history, sweep.DeliveredAlert)

    def test_delivered_push_stamp_uses_time_after_send(self) -> None:
        base = self.base()
        start = time.time()
        clock = [start]

        def deliver(message: str, *,
                    lint_settings: conf.ConfigRead, repeat: sweep.CrossingSendKey | sweep.NoCrossingSend = sweep.NO_CROSSING_SEND) -> sweep.FloorChannelOutcome:
            self.assertIn("removed", message)
            self.assertIsInstance(repeat, sweep.NoCrossingSend)
            self.assertIsInstance(lint_settings, conf.ConfigRead)
            clock[0] = start + 80
            return DELIVERED_NATEDEV_ALERT

        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "send_floor_alert", side_effect=deliver)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        record = self.saved_record()
        self.assertEqual(record.measured_at, start)
        self.assertEqual(record.push_history, sweep.DeliveredAlert(start + 80))

    def test_recent_push_allows_natedev_removal_alert(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        self.prior(10 * GIB, 0, push_at=time.time() - 10 * 60)
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        threshold = conf.read("lint").whole("sweep_large_removal_bytes") / GIB
        self.assertIn(f"removed more than {threshold:g} GiB", cast(str, send.call_args.args[0]))

    def test_failed_removal_stays_in_record_and_send_failure_keeps_status(self) -> None:
        base = self.base()
        tree, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        before = sweep.directory_blocks(str(tree.parent))
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with (mock.patch.object(sweep, "remove", return_value=sweep.ConfirmedDeletion(0, 1)),
              mock.patch.object(sweep, "send_floor_alert", return_value=FAILED_NATEDEV_ALERT) as send):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)
        self.assertEqual(status, 1)
        self.assertTrue(output_file.exists())
        record = self.saved_record()
        self.assertEqual(record.build_cache_bytes, before)
        self.assertEqual(send.call_count, 2)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        message = cast(str, send.call_args_list[0].args[0])
        self.assertIn("Not swept: CI's targets (0.0 GiB).\nCould not remove 1 path this sweep; the next sweep tries again.", message)

    def test_both_sends_failing_retry_and_one_delivery_stamps_hour(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with mock.patch.object(sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
            _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
            _, _ = cargo_target(base, "third", TOOL, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        self.assertEqual(send.call_count, 2)
        record = self.saved_record()
        self.assertIsInstance(record.alert_history, sweep.DeliveredAlert)

    def test_alert_delivery_failure_never_changes_exit_status(self) -> None:
        send_floor_alert = sweep.send_floor_alert
        send_holder_message = sweep.send_holder_message
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with (mock.patch.object(subprocess, "run", side_effect=OSError("offline")),
              mock.patch.object(sweep, "send_floor_alert", wraps=send_floor_alert),
              mock.patch.object(sweep, "send_holder_message", wraps=send_holder_message),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        self.assertIn("message to natedev failed", errors.getvalue())
        self.assertNotIn("phone alert failed", errors.getvalue())

    def test_one_channel_delivering_counts_and_both_results_print(self) -> None:
        sender = Path(self.enterContext(tempfile.TemporaryDirectory())) / "message/send.py"
        sender.parent.mkdir(parents=True)
        _ = sender.write_text(
            """import os
import sys

recipient = sys.argv[sys.argv.index("--to") + 1]
variable = "SWEEP_USER_EXIT" if recipient == "user" else "SWEEP_NATEDEV_EXIT"
raise SystemExit(int(os.environ[variable]))
""",
            encoding="utf-8",
        )
        output = io.StringIO()
        errors = io.StringIO()
        with (mock.patch.object(session_send, "SEND_SCRIPT", sender),
              fake_sender_process(sender) as run,
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "1", "SWEEP_USER_EXIT": "0"}),
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertEqual(sweep.send_floor_alert("disk notice"), sweep.FloorChannelOutcome.QUEUED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.DELIVERED)
        self.assertEqual(run.call_count, 2)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual([command[command.index("--to") + 1] for command in commands], ["natedev", "user"])
        self.assertEqual(commands[1][commands[1].index("--text-id") + 1], "nixos.disk_low")
        self.assertIn("shortfall=1", commands[1])
        self.assertEqual(run.call_args_list[0].kwargs["input"], "disk notice")
        self.assertIsNone(cast(object, run.call_args_list[1].kwargs["input"]))
        self.assertIn("message to natedev queued", output.getvalue())
        self.assertIn("phone alert delivered", output.getvalue())

        with (mock.patch.object(session_send, "SEND_SCRIPT", sender),
              fake_sender_process(sender),
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "0", "SWEEP_USER_EXIT": "3"}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.send_floor_alert("disk notice"), sweep.FloorChannelOutcome.DELIVERED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.FAILED)
        with (mock.patch.object(session_send, "SEND_SCRIPT", sender),
              fake_sender_process(sender),
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "3", "SWEEP_USER_EXIT": "3"}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.send_floor_alert("disk notice"), sweep.FloorChannelOutcome.FAILED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.FAILED)

    def test_phone_alert_wait_covers_configured_review_while_session_wait_stays_short(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory())) / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        with (mock.patch.object(review, "NOTIFY_CONFIG", config),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
              redirect_stdout(io.StringIO())):
            self.assertEqual(sweep.send_floor_alert("disk notice"), sweep.FloorChannelOutcome.DELIVERED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.DELIVERED)
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [170, 345])
        self.assertEqual(run.call_count, 2)

    def test_phone_disk_alert_keeps_action_and_build_shortfall_without_directory_dump(self) -> None:
        """F011: the disk action states how much to free and the body explains why."""
        text = sweep.floor_out_of_reach_text(2000, 2 * GIB, 9 * GIB, 10 * GIB,
            [], frozenset(), 0, sweep.UnavailableDiskMeasurement("missing"), 0)
        with (mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
              redirect_stdout(io.StringIO())):
            self.assertEqual(sweep.send_floor_alert(text), sweep.FloorChannelOutcome.DELIVERED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.DELIVERED)
        self.assertEqual(run.call_args_list[0].kwargs["input"], text)
        command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "nixos.disk_low")
        self.assertIn("shortfall=1", command)
        self.assertIn("free=9", command)
        self.assertNotIn("measurement unavailable", " ".join(command))

    def test_low_space_alert_stays_in_natedev_after_the_phone_review_fails(self) -> None:
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_phone_floor_gib=150\n")
        folder = sweep.OutsideCacheDirectory(str(base / "measured"), 26 * GIB, 0, sweep.NoLargestGrowingChild())
        measurement = sweep.AvailableDiskMeasurement(datetime.now(UTC), sweep.NoEarlierDiskMeasurement(), [folder], 0)
        with (mock.patch("scripts.notify.push_texts.approval_date", return_value=""),
              mock.patch.object(sweep, "free_bytes", return_value=140 * GIB),
              mock.patch.object(sweep, "read_disk_measurement", return_value=measurement),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            sweep.notify_floor_problem(sweep.FloorOutcome(tmp_sweep.CompletedSweepAssessment(0), 0, 140 * GIB, 140 * GIB, 0),
                                       lint_settings=conf.read("lint"))
        self.assertEqual(send.call_count, 1)
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertNotIn("phone", send.call_args.kwargs)
        message = cast(str, send.call_args.args[0])
        self.assertIn("140.0 GiB", message)
        self.assertIn("150.0 GiB", message)
        self.assertIn(f"{folder.path}: holds 26.0 GiB.", message)

    def test_nixos_disk_action_states_the_space_needed_to_restore_the_build_floor(self) -> None:
        for free in (149, 0, 290):
            with self.subTest(free=free):
                shortfall = str(300 - free)
                push = render("nixos.disk_low", free=str(free), shortfall=shortfall)
                assert isinstance(push.action, ActionRequired)
                self.assertIn(f"delete {shortfall} GiB", push.action.text)
        entry = next(entry for entry in catalog() if entry.id == "nixos.disk_low")
        for example in entry.examples:
            push = render_entry(entry, example.values)
            assert isinstance(push.action, ActionRequired)
            self.assertEqual(int(example.values["shortfall"]), 300 - int(example.values["free"]))
            self.assertIn(example.values["shortfall"] + " GiB", push.action.text)

    def test_failed_phone_send_reports_the_cause_printed_on_stdout(self) -> None:
        sender = Path(self.enterContext(tempfile.TemporaryDirectory())) / "message/send.py"
        sender.parent.mkdir(parents=True)
        _ = sender.write_text(
            """import sys

if sys.argv[sys.argv.index("--to") + 1] == "user":
    print("FAILED: Pushover keys are missing")
    raise SystemExit(3)
""",
            encoding="utf-8",
        )
        errors = io.StringIO()
        with (mock.patch.object(session_send, "SEND_SCRIPT", sender),
              fake_sender_process(sender),
              redirect_stdout(io.StringIO()), redirect_stderr(errors)):
            self.assertEqual(sweep.send_floor_alert("disk notice"), sweep.FloorChannelOutcome.DELIVERED)
            self.assertEqual(sweep.send_phone_push(render("nixos.disk_low", free="9", shortfall="1")),
                             sweep.FloorChannelOutcome.FAILED)
        self.assertIn("phone alert failed (3): stdout: FAILED: Pushover keys are missing", errors.getvalue())

    def test_natedev_only_channel_never_calls_phone(self) -> None:
        result = subprocess.CompletedProcess([], 1, "", "")
        with mock.patch.object(subprocess, "run", return_value=result) as run:
            self.assertEqual(sweep.send_floor_alert("disk notice"),
                             sweep.FloorChannelOutcome.QUEUED)
        self.assertEqual(run.call_count, 1)
        message_command = cast(list[str], run.call_args.args[0])
        self.assertIn("message/send.py", message_command[1])

    def test_queued_message_stamps_record_and_suppresses_next_alert(self) -> None:
        send_floor_alert = sweep.send_floor_alert
        send_holder_message = sweep.send_holder_message
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        results = [subprocess.CompletedProcess([], 1, "", "")]
        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with (mock.patch.object(subprocess, "run", side_effect=results) as run,
              mock.patch.object(sweep, "send_floor_alert", wraps=send_floor_alert),
              mock.patch.object(sweep, "send_holder_message", wraps=send_holder_message)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
            record = self.saved_record()
            self.assertIsInstance(record.alert_history, sweep.DeliveredAlert)
            _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        self.assertEqual(run.call_count, 1)

    def test_delivered_stamp_uses_time_after_send(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        start = time.time()
        clock = [start]

        def deliver(message: str, *,
                    lint_settings: conf.ConfigRead, repeat: sweep.CrossingSendKey | sweep.NoCrossingSend = sweep.NO_CROSSING_SEND) -> sweep.FloorChannelOutcome:
            self.assertIn("removed", message)
            self.assertIsInstance(repeat, sweep.NoCrossingSend)
            self.assertIsInstance(lint_settings, conf.ConfigRead)
            clock[0] = start + 80
            return DELIVERED_NATEDEV_ALERT

        _ = write_config_fixture(conf.path("lint"), "sweep_large_removal_bytes=1\n")
        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "send_floor_alert", side_effect=deliver)):
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        record = self.saved_record()
        self.assertEqual(record.measured_at, start)
        self.assertEqual(record.alert_history, sweep.DeliveredAlert(start + 80))

    def test_missing_and_corrupt_floor_records_have_named_state(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())
        path.parent.mkdir(exist_ok=True)
        _ = path.write_text("broken json")
        self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())

    def test_old_floor_record_has_no_push_and_push_stamp_round_trips(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        path.parent.mkdir(exist_ok=True)
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
        path.parent.mkdir(exist_ok=True)
        for value in ("yesterday", float("nan"), float("inf")):
            _ = path.write_text(json.dumps({"measured_at": 100.0, "free_bytes": 5, "build_cache_bytes": 8,
                                            "last_alert_at": None, "last_push_at": value}))
            self.assertEqual(sweep.read_floor_record(path), sweep.NoFloorRecord())

    def test_boolean_floor_fields_make_record_unavailable(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        path.parent.mkdir(exist_ok=True)
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
        lines = sweep.disk_measurement_lines(sweep.read_disk_measurement(), 0, 0, time.time(),
                                             False, int(conf.read("lint").words("sweep_message_path_widths")[0]))
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
        _ = path.write_text(json.dumps({
            "measured_at": "2026-10-04T19:53:00-07:00",
            "previous_measured_at": "2026-10-04T19:43:00-07:00",
            "outside_build_cache_totals": [{"label": "/tmp", "bytes": 2 * GIB, "growth_bytes": -2 * GIB}],
            "outside_build_caches": [
                {"path": "/tmp/new", "bytes": 2 * GIB, "growth_bytes": 2 * GIB,
                 "largest_child_path": None, "largest_child_growth_bytes": None},
            ],
        }))
        with mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB], recorded=True)[0], 0)
        message = cast(str, send.call_args.args[0])
        self.assertIn("Measured folders shrank 2.0 GiB", message)
        self.assertIn("/tmp/new: grew 2.0 GiB", message)
        self.assertIn("came after buildlog-disk's last measurement", message)

    def test_snapshot_growth_above_fall_covers_all_without_negative_remainder(self) -> None:
        now = time.time()
        measurement = sweep.AvailableDiskMeasurement(
            datetime.fromtimestamp(now - 60, PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 660, PACIFIC)),
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
        measured = datetime.fromtimestamp(now - 30, PACIFIC)
        measurement = sweep.AvailableDiskMeasurement(
            measured,
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, PACIFIC)),
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
            datetime.fromtimestamp(now, PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, PACIFIC)),
            [], 30 * GIB,
        )
        message = sweep.floor_alert_text(now, 35 * GIB, sweep.NoFloorRecord(), 0, 0,
                                         ["removed more than 32 GiB in one sweep"], measurement)
        self.assertIn("No earlier floor sweep", message)
        self.assertNotIn("fall beyond cache growth", message)

    def test_holder_message_uses_the_first_configured_path_width(self) -> None:
        _ = write_config_fixture(conf.path("lint"),
                                 "sweep_message_path_widths=24 16\n")
        holder = sweep.FloorHolder("/phase46/" + "a" * 70 + "/target", 2 * GIB, 46,
                                   "maps build output", "phase46")

        message = sweep.holder_message_text(holder, GIB, 3 * GIB)

        self.assertIn("/phase46/aaaaaaa…/target (2.0 GiB)", message)

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
            datetime.fromtimestamp(now, PACIFIC),
            sweep.EarlierDiskMeasurement(datetime.fromtimestamp(now - 600, PACIFIC)),
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

    def test_unreadable_parent_preserves_discovery_of_the_git_style_cache(self) -> None:
        base = self.base()
        target = base / ".git" / scratch.STYLE_TARGET_NAME
        _ = unit(target / "debug", "deps", "style", APP, time.time() - DAY)
        _ = (target / sweep.RUSTC_INFO).write_text("{}")
        scan = os.scandir

        def deny_parent(path: str) -> object:
            if path == str(base):
                raise PermissionError("the parent cannot be listed")
            return scan(path)

        with mock.patch.object(os, "scandir", side_effect=deny_parent):
            self.assertEqual(sweep.target_dirs([str(base)]), [str(target)])

    def test_finds_cargo_targets_and_nothing_inside_them(self) -> None:
        base = self.base()
        _ = cargo_target(base, "repo", APP, time.time())
        _ = cargo_target(base / "repo" / "target", "nested", DEMO, time.time())
        _ = cargo_target(base / ".git", "hidden", TOOL, time.time())
        _ = write(base / "uv" / scratch.CACHE_TAG)
        self.assertEqual(sweep.target_dirs([str(base)]),
                         [str(base / "repo" / "target"), str(base / "repo" / "target" / "nested" / "target")])

    def test_discovery_branches_preserve_depth_and_reset_only_at_targets(self) -> None:
        base = self.base()
        _ = write_config_fixture(conf.path("lint"), "sweep_floor_search_depth=1\n")
        _ = write(base / "Cargo.toml", 0)
        _ = write(base / "left" / "too-deep" / sweep.RUSTC_INFO, 0)
        target = base / "right"
        _ = write(target / sweep.RUSTC_INFO, 0)
        _ = write(target / "nested" / sweep.RUSTC_INFO, 0)
        _ = write(target / "debug" / ".fingerprint" / "Cargo.toml", 0)
        checkouts: list[str] = []
        self.assertEqual(sweep.target_dirs([str(base)], checkouts=checkouts),
                         [str(target), str(target / "nested")])
        self.assertEqual(checkouts, [str(base)])

    def test_cache_trace_targets_are_scratch_floor_targets_and_sccache_is_not_a_target(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        root = home / ".cache" / "trace-targets" / "rich-placement-1ha815wz"
        _ = unit(root / "debug", "deps", "hana", APP, time.time() - DAY)
        _ = (root / sweep.RUSTC_INFO).write_text("{}")
        # sccache's layout: two levels of one-character folders, then entries; no rustc info, no cargo tag.
        _ = write(home / ".cache" / "sccache" / "0" / "1" / "01d2c3b4a5968778")
        _ = write(home / ".cache" / "sccache" / "debug" / ".fingerprint" / "x" / "y")
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(home)}))
        self.assertIn("~/.cache", conf.read("lint").words("sweep_floor_roots"))
        found = sweep.target_dirs([root for root in conf.read("lint").words("sweep_floor_roots") if root.startswith("~/.cache")])
        self.assertEqual(found, [str(root)])

    def test_exhausted_cache_tells_the_holding_session_and_pages_only_when_none_was_told(self) -> None:
        base = self.base()
        busy_tree, _ = cargo_target(base, "busy", DEMO, time.time())
        lock_path = busy_tree / sweep.LOCK_NAMES[0]
        lock = os.open(lock_path, os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (mock.patch.object(sweep, "holder_sessions", return_value={os.getpid(): "hana-unit"}) as found,
              mock.patch.object(sweep, "send_holder_message") as tell,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            for delivered in (True, False):
                with self.subTest(delivered=delivered):
                    sweep.floor_record_path().unlink(missing_ok=True)
                    found.reset_mock()
                    tell.reset_mock()
                    send.reset_mock()
                    tell.return_value = (sweep.FloorChannelOutcome.DELIVERED if delivered
                                         else sweep.FloorChannelOutcome.FAILED)
                    self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB], recorded=True)[0], 0)
                    found.assert_called_once_with([os.getpid()])
                    self.assertEqual(tell.call_args.args[0], "hana-unit")
                    told = cast(str, tell.call_args.args[1])
                    self.assertIn(f"{busy_tree.parent} (", told)
                    self.assertIn(f"pid {os.getpid()} holds the cargo lock {lock_path}. This process is in your session", told)
                    self.assertIn("If the process is stale, stop it.", told)
                    message = cast(str, send.call_args.args[0])
                    if delivered:
                        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
                        self.assertNotIn("phone", send.call_args.kwargs)
                        self.assertIn("; session hana-unit was told", message)
                        self.assertIn("No phone alert was attempted for this event.", message)
                    else:
                        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
                        self.assertIn("; the message to session hana-unit failed", message)
                        self.assertNotIn("phone", send.call_args.kwargs)
                    record = self.saved_record()
                    self.assertIsInstance(record.push_history, sweep.DeliveredAlert)

    def test_enclosing_cargo_target_finds_a_cache_tagged_target(self) -> None:
        tree, program = cargo_tag_target(self.base(), "tagged", APP, time.time())
        self.assertEqual(
            sweep.enclosing_cargo_target(str(program)),
            sweep.EnclosingCargoTarget(str(tree.parent)),
        )

    def test_enclosing_cargo_target_includes_the_path_itself(self) -> None:
        base = self.base()
        outer_tree, _ = cargo_target(base, "outer", APP, time.time())
        inner_tree, _ = cargo_target(outer_tree, "inner", DEMO, time.time())
        inner = inner_tree.parent
        self.assertEqual(sweep.enclosing_cargo_target(str(inner)), sweep.EnclosingCargoTarget(str(inner)))

    def test_target_dirs_finds_cargo_tag_beside_a_build_tree(self) -> None:
        base = self.base()
        _, _ = cargo_tag_target(base, "repo", APP, time.time())

        self.assertEqual(sweep.target_dirs([str(base)]), [str(base / "repo" / "target")])

    def test_target_dirs_ignores_cargo_tag_without_a_build_tree_and_floor_preserves_it(self) -> None:
        base = self.base()
        registry = base / "registry"
        registry.mkdir()
        tag = registry / sweep.CACHE_TAG
        _ = tag.write_text(f"Signature: cargo\n{sweep.CARGO_TAG_LINE}\n")
        registry_file = write(registry / "index")
        _, removable = cargo_target(base, "removable", APP, time.time() - DAY)

        self.assertNotIn(str(registry), sweep.target_dirs([str(base)]))
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertTrue(tag.exists())
        self.assertTrue(registry_file.exists())
        self.assertFalse(removable.exists())

    def test_target_dirs_ignores_another_tools_cache_tag_beside_a_build_tree(self) -> None:
        base = self.base()
        root = base / "other" / "target"
        _ = unit(root / "debug", "deps", "other", APP, time.time())
        _ = (root / sweep.CACHE_TAG).write_text(
            scratch.CACHE_TAG_SIGNATURE + "\n"
            + "# This file is a cache directory tag created by another tool.\n"
        )

        self.assertNotIn(str(root), sweep.target_dirs([str(base)]))

    def test_floor_keeps_recent_tmp_output_when_an_older_rust_target_meets_the_floor(self) -> None:
        base = self.base()
        now = time.time()
        _, managed = cargo_target(base / "home" / "rust", "managed", APP, now - 3 * 3600)
        _, scratch = cargo_target(base / "scratch", "recent", DEMO, now - 10 * 60)

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertTrue(scratch.exists())
        self.assertFalse(managed.exists())
        self.assertNotIn(f"from {base / 'scratch' / 'recent' / 'target'}", output)
        self.assertIn(f"from {base / 'home' / 'rust' / 'managed' / 'target'}", output)

    def test_floor_takes_the_oldest_target_when_it_covers_the_shortfall(self) -> None:
        base = self.base()
        now = time.time()
        _, older = cargo_target(base, "older", APP, now - 9 * 3600)
        recent_tree, recent = cargo_target(base, "recent", DEMO, now - 60)
        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(older.exists())
        self.assertTrue(recent.exists())
        self.assertNotIn(f"from {recent_tree.parent}", output)

    def test_floor_scan_estimates_each_targets_candidates_without_shortfall_or_shared_link_credit(self) -> None:
        for shortfall in (1, GIB):
            with self.subTest(shortfall=shortfall):
                base = self.base()
                now = time.time() - DAY
                whole, whole_file = self.residue_target(base, "whole", now)
                tree, output = cargo_target(base, "unit", APP, now)
                outside_tree, outside_file = cargo_target(base, "outside", DEMO, now)
                os.link(outside_file, base / "outside-link")
                first_tree, shared = cargo_target(base, "shared-first", TOOL, now)
                second_tree, second = cargo_target(base, "shared-second", STALE, now)
                second.unlink()
                os.link(shared, second)
                busy_tree, busy_file = cargo_target(base, "busy", MY_TOOL, now)
                targets = [whole, tree.parent, outside_tree.parent, first_tree.parent, second_tree.parent]
                totals = {str(root): sweep.directory_blocks(str(root)) for root in targets}
                expected = {str(whole): whole_file.stat().st_blocks * 512 + (whole / sweep.RUSTC_INFO).stat().st_blocks * 512}
                for build_tree, payload in ((tree, output), (outside_tree, outside_file),
                                             (first_tree, shared), (second_tree, second)):
                    fingerprint, = (build_tree / ".fingerprint").glob("*/*.json")
                    expected[str(build_tree.parent)] = fingerprint.stat().st_blocks * 512 + (
                        payload.stat().st_blocks * 512 if build_tree == tree else 0)
                with mock.patch.object(sweep, "process_uses", return_value={
                        str(busy_tree.parent): sweep.TargetUse(123, "works in target")}):
                    status, _ = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], recorded=True)
                self.assertEqual(status, 0)
                row, = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
                rows = cast(list[dict[str, object]], row["idle_targets"])
                self.assertEqual({item["folder"] for item in rows}, set(totals))
                self.assertTrue(busy_file.exists())
                for item in rows:
                    root = cast(str, item["folder"])
                    self.assertEqual(item["bytes"], totals[root])
                    self.assertEqual(item.get("takeable_bytes", item["bytes"]), expected[root])
                    if root in {str(outside_tree.parent), str(first_tree.parent), str(second_tree.parent)}:
                        self.assertEqual(item["reason"], "links outside the target")
                    if expected[root] == totals[root]:
                        self.assertNotIn("takeable_bytes", item)
                        self.assertNotIn("reason", item)

    def test_floor_scan_reuses_target_discovery_for_nested_roots(self) -> None:
        base = self.base()
        outer, _ = self.residue_target(base, "outer", time.time() - DAY)
        nested, _ = cargo_target(outer, "proof", APP, time.time())
        with mock.patch.object(sweep, "target_dirs", wraps=sweep.target_dirs) as discover:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertEqual(discover.call_count, 1, "the nested-root inventory belongs to this pass")
        row, = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
        self.assertEqual({item["folder"] for item in cast(list[dict[str, object]], row["idle_targets"])},
                         {str(outer), str(nested.parent)})

    def test_floor_scan_distinguishes_internal_unclaimed_links_from_external_links(self) -> None:
        base = self.base()
        used = time.time() - DAY
        internal_tree, internal = cargo_target(base, "internal", APP, used)
        external_tree, external = cargo_target(base, "external", DEMO, used)
        unclaimed = internal_tree.parent / "tmp" / "keep"
        unclaimed.parent.mkdir()
        os.link(internal, unclaimed)
        os.link(external, base / "outside-link")
        expected: dict[str, tuple[int, str]] = {}
        for tree, reason in ((internal_tree, "links outside the take candidates"),
                             (external_tree, "links outside the target")):
            fingerprint, = (tree / ".fingerprint").glob("*/*.json")
            expected[str(tree.parent)] = (fingerprint.stat().st_blocks * 512, reason)

        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)[0], 0)

        row, = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
        rows = cast(list[dict[str, object]], row["idle_targets"])
        self.assertEqual({item["folder"] for item in rows}, set(expected))
        for item in rows:
            self.assertEqual((item["takeable_bytes"], item["reason"]), expected[cast(str, item["folder"])])
        self.assertTrue(unclaimed.exists())
        self.assertTrue((base / "outside-link").exists())

    def test_unstamped_outer_last_use_ignores_a_new_nested_targets_container(self) -> None:
        base = self.base()
        now = time.time()
        outer, residue = self.residue_target(base, "outer", now - 3 * DAY, stamp=False)
        nested, nested_file = cargo_target(outer, "proof", APP, now)
        os.utime(nested.parent.parent, (now, now))
        _, middle = cargo_target(base, "middle", DEMO, now - DAY)
        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(residue.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(nested_file.exists())
        self.assertIn(f"from {outer}, last used", output)

    def locked_floor(self, base: Path, started: float, *, pid: int = 321,
                     name: str = "floor.lock", record: str = "",
                     command: tuple[str, ...] = ("python3", "sweep.py", "--floor-only")) -> Path:
        """An actual contended flock, with its holder's private process record and config."""
        path = base / name
        guard = sweep.try_lock(str(path))
        assert guard is not None
        self.addCleanup(os.close, guard)
        _ = path.write_text(record or json.dumps({"pid": pid, "started_at": started}))
        proc = base / "proc"
        fake_process(proc, pid, argv=command)
        config = conf.path("lint")
        _ = write_config_fixture(config, "floor_lock_alert_minutes=7\n")
        _ = self.enterContext(mock.patch.object(sweep, "PROC_DIR", str(proc)))
        _ = self.enterContext(mock.patch.object(sweep, "lock_holder", return_value=pid))
        return path

    def saved_record(self) -> sweep.FloorRecord:
        saved = sweep.read_floor_record(sweep.floor_record_path())
        self.assertIsInstance(saved, sweep.FloorRecord)
        assert isinstance(saved, sweep.FloorRecord)
        return saved

    def saved_receipts(self) -> tuple[sweep.CrossingReceipt, sweep.CrossingReceipt]:
        crossing = self.saved_record().phone_crossing
        self.assertIsInstance(crossing, sweep.OpenPhoneCrossing)
        assert isinstance(crossing, sweep.OpenPhoneCrossing)
        return crossing.natedev, crossing.phone

    def saved_lock_alert(self) -> sweep.FloorLockAlertState:
        return self.saved_record().lock_alert

    def test_lock_transition_reuses_the_observed_clock_and_suppresses_reported_holds(self) -> None:
        holder = sweep.UnreadableFloorLockHolder(321)
        process = sweep.OtherFloorProcess("python3 other_tool.py")
        first = sweep.next_lock_alert(sweep.NoFloorLockAlert(), holder, process, 1000)
        self.assertEqual(first, sweep.LockAlertTransition(
            sweep.ObservedUnreadableFloorHold(321, 1000),
            sweep.FloorLockAlertCandidate(321, 1000, "python3 other_tool.py",
                                          sweep.ReportedUnreadableFloorHold(321, 1000))))
        self.assertEqual(sweep.next_lock_alert(first.lock_alert, holder, process, 1400), first)
        reported = sweep.ReportedUnreadableFloorHold(321, 1000)
        self.assertEqual(sweep.next_lock_alert(reported, holder, process, 1600),
                         sweep.LockAlertTransition(reported, sweep.NoLockAlertNeeded()))
        self.assertEqual(sweep.next_lock_alert(reported, holder, sweep.LiveSweepProcess(), 1600),
                         sweep.LockAlertTransition(sweep.NoFloorLockAlert(), sweep.NoLockAlertNeeded()))
        changed = sweep.next_lock_alert(reported, sweep.UnreadableFloorLockHolder(654),
                                        sweep.UnreadableFloorProcess(), 1700)
        self.assertEqual(changed, sweep.LockAlertTransition(
            sweep.ObservedUnreadableFloorHold(654, 1700),
            sweep.FloorLockAlertCandidate(654, 1700, "command is unreadable",
                                          sweep.ReportedUnreadableFloorHold(654, 1700))))

    def test_lock_observation_writes_only_when_its_state_changes(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken", command=("python3", "other_tool.py"))
        with (mock.patch.object(time, "time", return_value=1000) as clock,
              mock.patch.object(sweep, "write_floor_record", wraps=sweep.write_floor_record) as save,
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(self.saved_lock_alert(), sweep.ObservedUnreadableFloorHold(321, 1000))
            clock.return_value = 1200
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(save.call_count, 1)
            clock.return_value = 1420
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(self.saved_lock_alert(), sweep.ReportedUnreadableFloorHold(321, 1000))
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(save.call_count, 2)
            self.assertEqual(send.call_count, 1)
            with mock.patch.object(sweep, "lock_holder", return_value=None):
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
                self.assertEqual(save.call_count, 3)
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
                self.assertEqual(save.call_count, 3)
            self.assertEqual(self.saved_lock_alert(), sweep.NoFloorLockAlert())

    def test_missing_lock_observation_preserves_the_delivered_holder_receipt(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, command=("python3", "other_tool.py"))
        with (mock.patch.object(time, "time", return_value=1000),
              mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send):
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            receipt = self.saved_lock_alert()
            self.assertIsInstance(receipt, sweep.DeliveredFloorLockAlert)
            with mock.patch.object(sweep, "lock_holder", return_value=None):
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(self.saved_lock_alert(), receipt)
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
        self.assertEqual(send.call_count, 1)

    def test_lock_acquisition_clears_an_observation_through_the_shared_transition(self) -> None:
        base = self.base()
        previous = sweep.FloorRecord(1000, GIB, 0, sweep.NoDeliveredAlert(), sweep.NoDeliveredAlert(),
                                     lock_alert=sweep.ObservedUnreadableFloorHold(321, 900))
        sweep.write_floor_record(sweep.floor_record_path(), previous)
        with (mock.patch.object(sweep, "next_lock_alert", wraps=sweep.next_lock_alert) as transitions,
              mock.patch.object(sweep, "write_floor_record", wraps=sweep.write_floor_record) as saves):
            self.assertEqual(self.hold(base, 300 * GIB, [GIB], scanned=301 * GIB, recorded=True)[0], 0)
            self.assertEqual(self.hold(base, 300 * GIB, [GIB], scanned=301 * GIB, recorded=True)[0], 0)
        self.assertEqual(transitions.call_count, 2)
        self.assertTrue(all(isinstance(call.args[1], sweep.NoFloorLockHolder) for call in transitions.call_args_list))
        reset_records = [cast(sweep.FloorRecord, call.args[1]) for call in saves.call_args_list
                         if cast(sweep.FloorRecord, call.args[1]).measured_at == previous.measured_at]
        self.assertEqual(len(reset_records), 1)
        self.assertEqual(reset_records[0].lock_alert, sweep.NoFloorLockAlert())
        self.assertEqual(self.saved_lock_alert(), sweep.NoFloorLockAlert())

    def test_old_unreadable_hold_records_decode_delivery_as_named_states(self) -> None:
        _ = self.base()
        path = sweep.floor_record_path()
        for delivered, expected in ((False, sweep.ObservedUnreadableFloorHold(321, 1000)),
                                    (True, sweep.ReportedUnreadableFloorHold(321, 1000))):
            with self.subTest(delivered=delivered):
                _ = path.write_text(json.dumps({"measured_at": 1000, "free_bytes": GIB, "build_cache_bytes": 0,
                                               "lock_alert": {"pid": 321, "first_observed_at": 1000,
                                                              "alert_delivered": delivered}}))
                self.assertEqual(self.saved_lock_alert(), expected)

    def test_live_sweep_holder_past_the_alert_delay_sends_no_alert(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - 8 * 60)
        with mock.patch.object(time, "time", return_value=now), mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            sweep.alert_floor_lock(str(path), 2 * GIB, 300 * GIB)
            send.assert_not_called()

    def test_sweep_script_argument_does_not_suppress_another_holders_alert(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - 8 * 60,
                                 command=("python3", "other_tool.py", "sweep.py"))
        with mock.patch.object(time, "time", return_value=now), mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            sweep.alert_floor_lock(str(path), 2 * GIB, 300 * GIB)
            self.assertEqual(send.call_count, 1)

    def test_overdue_floor_lock_alert_is_once_per_hold_and_repeats_for_a_new_hold_by_the_same_pid(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - 8 * 60, command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time", return_value=now), mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            for _ in range(2):
                self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            self.assertEqual(send.call_count, 1)
            message = cast(str, send.call_args.args[0])
            for text in ("321", "python3 other_tool.py", "2.0 GiB", "300.0 GiB"):
                self.assertIn(text, message)
            cast(mock.Mock, sweep.send_phone_push).assert_not_called()
            _ = path.write_text(json.dumps({"pid": 321, "started_at": now - 9 * 60}))
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            self.assertEqual(send.call_count, 2)

    def overlap_floor_alert(self, path: Path, competing: Callable[[], object]) -> list[str]:
        """Pause one send while another real operation reaches the same floor state."""
        sending, finish_send, competing_started = Event(), Event(), Event()
        messages: list[str] = []

        def send(message: str, **settings: object) -> sweep.FloorChannelOutcome:
            cast(mock.Mock, sweep.send_phone_push).assert_not_called()
            self.assertIsInstance(settings["lint_settings"], conf.ConfigRead)
            messages.append(message)
            if len(messages) == 1:
                sending.set()
                if not finish_send.wait(5):
                    raise AssertionError("the competing floor operation did not release the delayed send")
            return DELIVERED_NATEDEV_ALERT

        def run_competing() -> object:
            competing_started.set()
            return competing()

        with mock.patch.object(sweep, "send_floor_alert", side_effect=send), ThreadPoolExecutor(2) as workers:
            first = workers.submit(sweep.alert_floor_lock, str(path), 2 * GIB, 300 * GIB)
            try:
                self.assertTrue(sending.wait(5), "the first overdue hold must reach the send")
                second = workers.submit(run_competing)
                self.assertTrue(competing_started.wait(5))
                try:
                    _ = second.result(timeout=1)
                except TimeoutError:
                    # A serialized operation can finish only after the delayed send returns.
                    pass
            finally:
                finish_send.set()
            _ = first.result(timeout=5)
            _ = second.result(timeout=5)
        return messages

    def test_overlapping_waiters_deliver_one_alert_for_the_same_hold(self) -> None:
        base = self.base()
        path = self.locked_floor(base, time.time() - DAY, command=("python3", "other_tool.py"))

        messages = self.overlap_floor_alert(path, lambda: sweep.alert_floor_lock(str(path), 2 * GIB, 300 * GIB))

        self.assertEqual(len(messages), 1)
        saved = self.saved_record()
        hold = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(saved.lock_alert, sweep.DeliveredFloorLockAlert(321, cast(float, hold["started_at"])))

    def test_delayed_lock_alert_and_new_measurement_preserve_each_others_records(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - DAY, name="alert.lock", command=("python3", "other_tool.py"))
        record_path = sweep.floor_record_path()
        sweep.write_floor_record(record_path, sweep.FloorRecord(
            now - 100, GIB, 3 * GIB, sweep.DeliveredAlert(now - 80), sweep.DeliveredAlert(now - 70)))

        def measure() -> int:
            _ = write_config_fixture(conf.path("lint"),
                                     f"sweep_floor_roots={base}\n" + "sweep_phone_floor_gib=0\n")
            with self.free_space([2 * GIB, 302 * GIB], scanned=302 * GIB):
                return sweep.hold_floor(300 * GIB, False)

        with mock.patch.object(time, "time", return_value=now):
            messages = self.overlap_floor_alert(path, measure)

        self.assertEqual(len(messages), 1)
        saved = sweep.read_floor_record(record_path)
        assert isinstance(saved, sweep.FloorRecord)
        self.assertEqual((saved.measured_at, saved.free_bytes, saved.build_cache_bytes), (now, 302 * GIB, 0))
        self.assertEqual((saved.alert_history, saved.push_history),
                         (sweep.DeliveredAlert(now - 80), sweep.DeliveredAlert(now - 70)))
        self.assertEqual(saved.lock_alert, sweep.DeliveredFloorLockAlert(321, now - DAY))

    def test_delayed_lock_alert_does_not_replace_a_newer_holds_receipt(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - 2 * DAY, command=("python3", "other_tool.py"))

        def newer_hold() -> None:
            _ = path.write_text(json.dumps({"pid": 321, "started_at": now - DAY}))
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)

        messages = self.overlap_floor_alert(path, newer_hold)

        self.assertEqual(len(messages), 2, "each of the two distinct holds must be reported once")
        saved = self.saved_record()
        self.assertEqual(saved.lock_alert, sweep.DeliveredFloorLockAlert(321, now - DAY))

    def test_recent_floor_lock_uses_its_start_time_and_the_configured_delay(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - 6 * 60, command=("python3", "other_tool.py"))
        os.utime(path, (now - 10 * DAY, now - 10 * DAY))
        with mock.patch.object(time, "time", return_value=now), mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            send.assert_not_called()
            config = conf.path("lint")
            _ = write_config_fixture(config, "floor_lock_alert_minutes=5\n")
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            send.assert_called_once()

    def test_failed_floor_lock_alert_is_retried_and_unreadable_holders_do_not_alert(self) -> None:
        base = self.base()
        now = time.time()
        path = self.locked_floor(base, now - DAY, command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time", return_value=now), mock.patch.object(
                sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send:
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            self.assertEqual(send.call_count, 2)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            for record_text in ("broken", json.dumps({"pid": 999, "started_at": now - DAY})):
                with self.subTest(record=record_text):
                    _ = path.write_text(record_text)
                    self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
                    send.assert_not_called()
        with mock.patch.object(sweep, "lock_holder", return_value=None), mock.patch.object(
                sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB], recorded=True)[0], 0)
            send.assert_not_called()

    def test_unreadable_hold_keeps_its_first_observation_until_the_configured_delay(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken", command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time") as clock, mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            for now in (1000, 1200, 1419):
                clock.return_value = now
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
                self.assertEqual(self.saved_lock_alert(), sweep.ObservedUnreadableFloorHold(321, 1000))
                send.assert_not_called()
            clock.return_value = 1420
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            send.assert_called_once()
            message = cast(str, send.call_args.args[0])
            self.assertIn("321 (python3 other_tool.py)", message)

    def test_unreadable_hold_resets_its_clock_for_a_new_pid(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken", command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time") as clock, mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            clock.return_value = 1000
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            clock.return_value = 1420
            with mock.patch.object(sweep, "lock_holder", return_value=654):
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
                self.assertEqual(self.saved_lock_alert(), sweep.ObservedUnreadableFloorHold(654, 1420))
                send.assert_not_called()
                clock.return_value = 1840
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            send.assert_called_once()
            self.assertIn("654 (command is unreadable)", cast(str, send.call_args.args[0]))

    def test_unreadable_hold_resets_its_clock_when_the_lock_is_free(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken", command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time") as clock, mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            clock.return_value = 1000
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            with mock.patch.object(sweep, "lock_holder", return_value=None):
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            clock.return_value = 1420
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            send.assert_not_called()
            self.assertEqual(self.saved_lock_alert(), sweep.ObservedUnreadableFloorHold(321, 1420))

    def test_unreadable_hold_retries_a_failed_delivery(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken", command=("python3", "other_tool.py"))
        with mock.patch.object(time, "time") as clock, mock.patch.object(
                sweep, "send_floor_alert", side_effect=[FAILED_NATEDEV_ALERT, DELIVERED_NATEDEV_ALERT]) as send:
            clock.return_value = 1000
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            clock.return_value = 1420
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(self.saved_lock_alert(), sweep.ObservedUnreadableFloorHold(321, 1000))
            clock.return_value = 1421
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            self.assertEqual(send.call_count, 2)

    def test_live_sweep_with_unreadable_identity_sends_no_alert(self) -> None:
        base = self.base()
        path = self.locked_floor(base, 100, record="broken")
        with mock.patch.object(time, "time") as clock, mock.patch.object(
                sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT) as send:
            for now in (1000, 1420, 1900):
                clock.return_value = now
                sweep.alert_floor_lock(str(path), GIB, 300 * GIB)
            send.assert_not_called()

    def test_lock_contention_above_the_floor_and_dry_runs_send_no_lock_alert(self) -> None:
        base = self.base()
        _ = self.locked_floor(base, time.time() - DAY, command=("python3", "other_tool.py"))
        with mock.patch.object(sweep, "send_floor_alert") as send:
            self.assertEqual(self.hold(base, 300 * GIB, [301 * GIB], recorded=True)[0], 0)
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB, 2 * GIB], dry_run=True, recorded=True)[0], 0)
            send.assert_not_called()

    def test_real_floor_lock_records_this_holds_pid_and_start(self) -> None:
        base = self.base()
        now = time.time()
        with mock.patch.object(time, "time", return_value=now):
            self.assertEqual(self.hold(base, 300 * GIB, [2 * GIB, 300 * GIB], recorded=True)[0], 0)
        record = cast(dict[str, object], json.loads((base / "floor.lock").read_text()))
        self.assertEqual((record["pid"], record["started_at"]), (os.getpid(), now))

    def test_floor_scan_records_the_post_scan_shortfall_and_idle_targets_oldest_first(self) -> None:
        base = self.base()
        now = time.time()
        recent_tree, _ = cargo_target(base, "a-recent", APP, now - 60)
        older_tree, _ = cargo_target(base, "z-older", DEMO, now - 9 * 3600)
        targets = [older_tree.parent, recent_tree.parent]
        expected = [{"folder": str(target), "last_use": used,
                     "bytes": sweep.directory_blocks(str(target)),
                     "takeable_bytes": sweep.directory_blocks(str(target))
                     - (target / sweep.RUSTC_INFO).stat().st_blocks * 512,
                     "reason": "output outside take candidates"}
                    for target, used in zip(targets, (now - 9 * 3600, now - 60))]
        with mock.patch.object(time, "time", return_value=now):
            status, _ = self.hold(base, 10 * GIB, [8 * GIB, 10 * GIB], scanned=10 * GIB - 1, recorded=True)
        self.assertEqual(status, 0)
        scans = [row for row in self.build_log_rows() if row.get("kind") == "floor_scan"]
        self.assertEqual(len(scans), 1)
        row = scans[0]
        self.assertEqual((row["at"], row["free_after_scan"], row["floor"], row["shortfall"]),
                         (store.utc_iso(now), 10 * GIB - 1, 10 * GIB, 1))
        self.assertEqual(row["idle_targets"], expected)

    def test_floor_dry_run_under_the_floor_writes_no_scan_row(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "kept", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [8 * GIB, 10 * GIB], True, scanned=10 * GIB - 1, recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        self.assertEqual(self.build_log_rows(), [])

    def test_floor_scan_above_the_floor_writes_no_scan_row(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "kept", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [8 * GIB, 11 * GIB], scanned=11 * GIB, recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        self.assertEqual(self.build_log_rows(), [])

    def test_floor_orders_old_tmp_and_rust_targets_by_last_use(self) -> None:
        base = self.base()
        now = time.time()
        _, older = cargo_target(base / "home" / "rust", "older", APP, now - 5 * 3600)
        _, scratch = cargo_target(base / "scratch", "old", DEMO, now - 2 * 3600)

        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertFalse(older.exists())
        self.assertTrue(scratch.exists())

    def test_floor_report_lists_targets_in_take_order_in_both_modes(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                base = self.base()
                now = time.time()
                targets: list[Path] = []
                outputs: list[Path] = []
                for position, name in enumerate(("oldest", "middle", "newest")):
                    tree, output = cargo_target(base, name, APP, now - (3 - position) * DAY)
                    _ = output.write_bytes(os.urandom(65536 * (position + 1)))
                    targets.append(tree.parent)
                    outputs.append(output)
                shortfall = sum(sweep.directory_blocks(str(target)) for target in targets)

                status, report = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], dry_run, recorded=True)

                self.assertEqual(status, 0)
                verb = "would take" if dry_run else "took"
                lines = [line for line in report.splitlines()
                         if line.startswith(f"lint sweep: the floor {verb} ")]
                self.assertEqual([line.split(" from ", 1)[1].split(", last used ", 1)[0]
                                  for line in lines], [str(target) for target in targets])
                self.assertEqual([output.exists() for output in outputs], [dry_run] * 3)

    def test_floor_takes_recent_output_when_older_output_cannot_meet_the_floor(self) -> None:
        base = self.base()
        now = time.time()
        older_tree, older = cargo_target(base, "older", APP, now - 2 * 3600)
        _, recent = cargo_target(base / "scratch", "recent", DEMO, now - 10 * 60)
        shortfall = sweep.directory_blocks(str(older_tree.parent)) + 1

        status, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertFalse(older.exists())
        self.assertFalse(recent.exists())
        self.assertIn(f"from {base / 'older' / 'target'}", output)
        self.assertIn(f"from {base / 'scratch' / 'recent' / 'target'}", output)

    def test_below_the_floor_a_held_scratch_target_is_left_for_a_managed_target(self) -> None:
        base = self.base()
        scratch_tree, scratch = cargo_target(base / "scratch", "held", APP, time.time())
        lock = os.open(scratch_tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _, managed = cargo_target(base, "managed", DEMO, time.time() - DAY)

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertTrue(scratch.exists())
        self.assertFalse(managed.exists())
        self.assertIn("1 left alone while a build or test run uses them", output)
        self.assertIn(f"from {base / 'managed' / 'target'}", output)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "the process scan reads /proc")
    def test_below_the_floor_a_target_a_test_binary_runs_from_is_left_alone(self) -> None:
        process_uses = sweep.process_uses
        base = self.base()
        tree, tested = cargo_target(base, "tested", APP, time.time() - DAY)
        _, idle = cargo_target(base, "idle", DEMO, time.time() - 3600)
        binary = tree / "deps" / "hana-f4b61d2f4762c609"
        test = run_from(binary)
        self.addCleanup(test.wait)
        self.addCleanup(test.kill)
        proc = base / "proc"
        proc.mkdir()
        (proc / str(test.pid)).symlink_to(Path("/proc") / str(test.pid), target_is_directory=True)

        def own_process_uses(targets: list[str]) -> dict[str, sweep.TargetUse]:
            return process_uses(targets, str(proc))

        with mock.patch.object(sweep, "process_uses", side_effect=own_process_uses):
            status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertTrue(tested.exists())
        self.assertTrue(binary.exists())
        self.assertFalse(idle.exists())
        self.assertIn(f"lint sweep: pid {test.pid} runs {binary}; {base / 'tested' / 'target'} left alone", output)
        self.assertIn("1 left alone while a build or test run uses them", output)

    def test_below_the_floor_a_tag_only_target_loses_output_and_is_named(self) -> None:
        base = self.base()
        _, output_file = cargo_tag_target(base, "tagged", APP, time.time() - DAY)
        root = base / "tagged" / "target"

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertFalse(output_file.exists())
        self.assertIn("the floor took ", output)
        self.assertIn(f"from {root},", output)

    def test_below_the_floor_the_least_recently_used_target_goes(self) -> None:
        base = self.base()
        now = time.time()
        _, idle = cargo_target(base, "idle", APP, now - 3 * 3600)
        idle_record = base / "idle" / "target" / "debug" / ".fingerprint" / f"idle-{APP}" / "bin-idle.json"
        os.utime(idle_record, (now - 3600, now - 3 * 3600))
        _, recent = cargo_target(base, "recent", DEMO, now - 5 * 3600)
        recent_record = base / "recent" / "target" / "debug" / ".fingerprint" / f"recent-{DEMO}" / "bin-recent.json"
        os.utime(recent_record, (now - 600, now - 5 * 3600))
        status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(idle.exists())
        self.assertTrue(recent.exists())
        self.assertIn("removed 1 build units", output)
        self.assertIn("the floor took ", output)
        self.assertIn(f" from {base / 'idle' / 'target'}, last used {sweep.when(now - 3600)}", output)
        self.assertEqual(sum("removed" in line for line in output.splitlines()), 1)

    def test_floor_removal_line_spans_oldest_to_newest_unit(self) -> None:
        base = self.base()
        now = time.time()
        _, newer = cargo_target(base, "unstamped", APP, now - 5 * 3600)
        _, older = cargo_target(base, "stamped", DEMO, now - 10 * 3600)
        stamp = base / "stamped" / "target" / sweep.USE_STAMP
        _ = stamp.touch()
        os.utime(stamp, (now - 3600, now - 3600))
        _, output = self.hold(base, 10 * GIB, [10 * GIB - 2 * SIZE, 10 * GIB], recorded=True)
        self.assertFalse(newer.exists())
        self.assertFalse(older.exists())
        self.assertIn(f"last used {sweep.when(now - 10 * 3600)} to {sweep.when(now - 5 * 3600)};", output)

    def test_a_fresh_stamp_outweighs_older_units(self) -> None:
        base = self.base()
        now = time.time()
        _, stamped = cargo_target(base, "stamped", APP, now - 5 * 3600)
        stamp = base / "stamped" / "target" / sweep.USE_STAMP
        _ = stamp.touch()
        os.utime(stamp, (now - 60, now - 60))
        _, unstamped = cargo_target(base, "unstamped", DEMO, now - 2 * 3600)
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertTrue(stamped.exists())
        self.assertFalse(unstamped.exists())
        self.assertIn(f"from {base / 'unstamped' / 'target'}", output)

    def test_shortfall_exhausts_oldest_target_before_next(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 5 * 3600)
        next_tree, first = cargo_target(base, "next", DEMO, now - 3 * 3600)
        second = unit(next_tree, "deps", "second", TOOL, now - 3600)
        scan = sweep.scan_roots([str(base / "old" / "target"), str(base / "next" / "target")],
                                [str(old_tree), str(next_tree)])
        old_group = next(group for group in scan.groups if group.build_tree == str(old_tree))
        shortfall = sweep.freed_by(old_group, scan, dict(scan.links)).bytes + 1
        _, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], recorded=True)
        self.assertFalse(old.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(output.count("the floor took"), 2)

    def test_dry_floor_lines_name_target_without_extra_removal_lines(self) -> None:
        base = self.base()
        now = time.time() - 3600
        _, output_file = cargo_target(base, "repo", APP, now)
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], dry_run=True, recorded=True)
        self.assertTrue(output_file.exists())
        self.assertIn(f"the floor would take ", output)
        self.assertIn(f" from {base / 'repo' / 'target'}, last used {sweep.when(now)}", output)
        self.assertNotIn("removed", output)

    def test_orphan_only_floor_sweep_names_its_target_and_size(self) -> None:
        base = self.base()
        tree, output_file = cargo_target(base, "repo", APP, time.time() - 3600)
        orphan = write(tree / "orphan")
        orphan_dep_info = dep_info(orphan)
        scan = sweep.scan_roots([str(base / "repo" / "target")], [str(tree)])
        remaining = dict(scan.links)
        orphaned = sum(sweep.freed_by(group, scan, remaining).bytes for group in scan.orphans)
        self.assertGreater(orphaned, 0)

        status, output = self.hold(base, 10 * GIB, [10 * GIB - orphaned, 10 * GIB], recorded=True)

        self.assertEqual(status, 0)
        self.assertFalse(orphan.exists())
        self.assertFalse(orphan_dep_info.exists())
        self.assertTrue(output_file.exists())
        self.assertIn(f"removed 2 orphaned files ({sweep.gib(orphaned)})", output)
        lines = [line for line in output.splitlines() if "the floor took" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"the floor took {sweep.gib(orphaned)} from {base / 'repo' / 'target'},", lines[0])

    def test_failed_group_has_no_floor_line_when_another_target_succeeds(self) -> None:
        base = self.base()
        send = self.enterContext(mock.patch.object(sweep, "send_floor_alert", return_value=DELIVERED_NATEDEV_ALERT))
        now = time.time()
        failed_tree, failed_output = cargo_target(base, "failed", APP, now - 2 * DAY)
        _, successful_output = cargo_target(base, "successful", DEMO, now - DAY)
        failed_bytes = failed_output.stat().st_blocks * 512 + sweep.directory_blocks(str(failed_tree / ".fingerprint"))
        scan = sweep.scan_roots([str(base / name / "target") for name in ("failed", "successful")],
                                [str(base / name / "target" / "debug") for name in ("failed", "successful")])
        shortfall = sum(sweep.freed_by(group, scan, dict(scan.links)).bytes for group in scan.groups)
        original_rmdir = os.rmdir
        failed_parent = (failed_tree / ".fingerprint").stat()

        def fail_one_group(path: str, *, dir_fd: int | None = None) -> None:
            matches = path == str(failed_tree / ".fingerprint" / f"failed-{APP}")
            if dir_fd is not None and path == f"failed-{APP}":
                matches = os.path.samestat(failed_parent, os.fstat(dir_fd))
            if matches:
                raise OSError("test removal failure")
            original_rmdir(path, dir_fd=dir_fd)

        errors = io.StringIO()
        with mock.patch.object(os, "rmdir", side_effect=fail_one_group), redirect_stderr(errors):
            status, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB], recorded=True)

        send.assert_called()
        cast(mock.Mock, sweep.send_phone_push).assert_not_called()
        self.assertEqual(status, 1)
        self.assertIn("could not remove", errors.getvalue())
        self.assertTrue(failed_output.exists() or (failed_tree / ".fingerprint" / f"failed-{APP}").exists())
        self.assertFalse(successful_output.exists())
        self.assertIn("removed 2 build units", output)
        lines = [line for line in output.splitlines() if "the floor took" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"from {base / 'successful' / 'target'},", lines[0])
        failed_trim, = (row for row in self.trim_rows() if row["folder"] == str(failed_tree.parent))
        self.assertEqual((failed_trim["bytes_freed"], failed_trim["extent"]), (failed_bytes, "partial"))

    def test_workspace_stamps_each_root_even_when_a_build_holds_it(self) -> None:
        base = self.base()
        first_tree, _ = cargo_target(base, "first", APP, time.time() - DAY)
        _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
        roots = [str(base / name / "target") for name in ("first", "second")]
        with mock.patch.object(sweep, "cargo_roots", return_value=roots), \
             mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, recent_minutes=60), 0)
            for root in roots:
                self.assertTrue((Path(root) / sweep.USE_STAMP).exists())
            for root in roots:
                os.utime(Path(root) / sweep.USE_STAMP, (1, 1))
            lock = os.open(first_tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
            self.addCleanup(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), False, recent_minutes=60), 0)
        for root in roots:
            self.assertGreater((Path(root) / sweep.USE_STAMP).stat().st_mtime, 1)

    def test_dry_workspace_sweep_leaves_every_root_unstamped(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
        roots = [str(base / name / "target") for name in ("first", "second")]
        with mock.patch.object(sweep, "cargo_roots", return_value=roots), \
             mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(None, conf.read("lint"), True, recent_minutes=60), 0)
        for root in roots:
            self.assertFalse((Path(root) / sweep.USE_STAMP).exists())

    def test_named_target_writes_no_use_stamp(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        root = base / "repo" / "target"
        with mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(str(root), conf.read("lint"), True, recent_minutes=60), 0)
        self.assertFalse((root / sweep.USE_STAMP).exists())

    def test_a_target_a_build_holds_is_left_alone(self) -> None:
        base = self.base()
        tree, held = cargo_target(base, "held", APP, time.time() - 5 * DAY)
        lock = os.open(tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _, other = cargo_target(base, "other", DEMO, time.time())
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertTrue(held.exists())
        self.assertFalse(other.exists())
        self.assertIn("1 left alone while a build or test run uses them", output)

    def write_build_hold(self, base: Path, since: float) -> None:
        directory = base / "build-hold"
        directory.mkdir()
        _ = (directory / "holder-session").write_text(json.dumps({
            "holder": "build-speed", "since": datetime.fromtimestamp(since, PACIFIC).isoformat(),
            "for": "timed builds", "release_eta": "unknown",
        }))

    def test_state_override_moves_the_floor_lock_and_deletion_records_together(self) -> None:
        base = self.base()
        for name in ("first state", "second state"):
            state = base / name
            with mock.patch.dict(os.environ, {tmp_sweep.STATE_ENV: str(state)}), \
                 mock.patch.object(sweep, "recover_pending_trims"), \
                 mock.patch.object(sweep, "free_bytes", return_value=0), \
                 mock.patch.object(sweep, "try_lock", return_value=None) as acquire, \
                 mock.patch.object(sweep, "alert_floor_lock"), redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.hold_floor(GIB, False), 0)
                acquire.assert_called_once_with(str(state / "floor.lock"))
                self.assertEqual(sweep.floor_record_path(), state / "floor.json")
                self.assertEqual(sweep.target_deletes_path(), state / tmp_sweep.TARGET_DELETES_NAME)
                self.assertTrue(state.is_dir())

    def test_own_deletes_do_not_hold_a_target_but_a_later_build_write_does(self) -> None:
        base = self.base()
        now = time.time()
        tree, removed = cargo_target(base, "external", APP, now - DAY)
        remaining = unit(tree, "deps", "remaining", DEMO, now - DAY)
        for path in tree.parent.rglob("*"):
            os.utime(path, (now - DAY, now - DAY))
        self.write_build_hold(base, now - 60)
        with mock.patch.object(time, "time", return_value=now):
            self.assertEqual(sweep.remove([sweep.Group("unit", str(tree), [str(removed)])]).failures, 0)
            # A coarse filesystem can date the delete up to two seconds after the clock reading.
            os.utime(tree / "deps", (now + 2, now + 2))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.build_hold_targets([str(tree.parent)], {}), {})
            recorded = cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text()))
            self.assertEqual(recorded[str(tree.parent)], now)
            self.assertFalse(removed.exists())
            self.assertTrue(remaining.exists())
            os.utime(tree / "deps", (now + 3, now + 3))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(sweep.build_hold_targets([str(tree.parent)], {}),
                                 {str(tree.parent): sweep.BuildHoldTarget("build-speed")})

    def test_target_delete_records_the_nearest_enclosing_target(self) -> None:
        base = self.base()
        now = time.time()
        outer_tree, _ = cargo_target(base, "outer", APP, now)
        inner_tree, removed = cargo_target(outer_tree, "inner", DEMO, now)
        with mock.patch.object(time, "time", return_value=now):
            sweep.record_target_deletes([str(removed)])
        self.assertEqual(json.loads(sweep.target_deletes_path().read_text()), {str(inner_tree.parent): now})

    def test_target_delete_outside_cargo_targets_records_nothing(self) -> None:
        base = self.base()
        plain = write(base / "plain/debug/app")
        sweep.record_target_deletes([str(plain)])
        self.assertFalse(sweep.target_deletes_path().exists())

    def test_delete_records_drop_missing_roots_and_roots_older_than_a_day(self) -> None:
        base = self.base()
        now = time.time()
        tree, removed = cargo_target(base, "current", APP, now - DAY)
        old_tree, _ = cargo_target(base, "old", DEMO, now - 2 * DAY)
        path = sweep.target_deletes_path()
        path.parent.mkdir(exist_ok=True)
        _ = path.write_text(json.dumps({str(old_tree.parent): now - DAY - 1,
                                        str(base / "missing"): now - 60}))
        with mock.patch.object(time, "time", return_value=now):
            self.assertEqual(sweep.remove([sweep.Group("unit", str(tree), [str(removed)])]).failures, 0)
        self.assertEqual(json.loads(path.read_text()), {str(tree.parent): now})
        self.assertFalse(removed.exists())

    def test_failed_doc_delete_record_keeps_that_doc_and_continues_to_the_next_target(self) -> None:
        base = self.base()
        roots = [cargo_target(base, name, digest, time.time() - DAY)[0].parent
                 for name, digest in (("first", APP), ("second", DEMO))]
        for root in roots:
            _ = write(root / "doc" / "search.index" / "index")
        original = sweep.record_target_deletes

        def record(paths: Sequence[str], *, known_targets: Sequence[str] = (), lint_settings: conf.ConfigRead) -> None:
            if str(roots[0]) in paths or str(roots[0]) in known_targets:
                raise OSError("private state write failed")
            original(paths, known_targets=known_targets, lint_settings=lint_settings)

        output = io.StringIO()
        with mock.patch.object(sweep, "record_target_deletes", side_effect=record), redirect_stdout(output), redirect_stderr(output):
            status = sweep.prune_doc_index([str(root) for root in roots], 0, False)
        self.assertEqual(status, 1)
        self.assertTrue((roots[0] / "doc" / "search.index" / "index").exists())
        self.assertFalse((roots[1] / "doc").exists())
        self.assertEqual(output.getvalue().count("private state write failed"), 1)

    def test_doc_delete_is_recorded_before_the_doc_folder_is_removed(self) -> None:
        base = self.base()
        tree, _ = cargo_target(base, "docs", APP, time.time() - DAY)
        doc = tree.parent / "doc"
        _ = write(doc / "search.index" / "index")
        original = shutil.rmtree
        observed: list[str] = []

        def remove(path: str) -> None:
            self.assertTrue(doc.exists())
            records = cast(dict[str, object], json.loads(sweep.target_deletes_path().read_text()))
            self.assertIn(str(tree.parent), records)
            observed.append(path)
            original(path)

        with mock.patch.object(shutil, "rmtree", side_effect=remove), redirect_stdout(io.StringIO()):
            status = sweep.prune_doc_index([str(tree.parent)], 0, False)
        self.assertEqual(status, 0)
        self.assertEqual(observed, [str(doc)])
        self.assertFalse(doc.exists())

    def test_doc_index_removal_records_the_target_delete(self) -> None:
        base = self.base()
        now = time.time()
        tree, _ = cargo_target(base, "docs", APP, now - DAY)
        doc = tree.parent / "doc"
        _ = write(doc / "search.index" / "index")
        with mock.patch.object(time, "time", return_value=now), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.prune_doc_index([str(tree.parent)], 0, False), 0)
        self.assertFalse(doc.exists())
        self.assertEqual(json.loads(sweep.target_deletes_path().read_text()), {str(tree.parent): now})

    def test_a_holders_idle_workspace_target_is_never_locked_or_scanned(self) -> None:
        base = self.base()
        since = time.time()
        tree, held = cargo_target(base, "holder", APP, since - 5 * DAY)
        other_tree, other = cargo_target(base, "other", DEMO, since - DAY)
        # Old directory times make the workspace rule the only protection for this target.
        for root in (tree.parent, other_tree.parent):
            for path in root.rglob("*"):
                os.utime(path, (since - DAY, since - DAY))
        before = {path.relative_to(tree.parent): path.read_bytes()
                  for path in tree.parent.rglob("*") if path.is_file()}
        self.write_build_hold(base, since)
        sweep.record_target_deletes([str(tree.parent)])
        record = cast(sessions.SessionRecord, cast(object, {
            "sessionId": "holder-session", "pid": 4242, "name": "build-speed",
        }))
        proc = base / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cwd").symlink_to(tree.parent.parent, target_is_directory=True)
        with (mock.patch.object(sessions, "live_sessions", return_value=[record]),
              mock.patch.object(sweep, "PROC_DIR", str(proc)),
              mock.patch.object(sweep, "lock_trees", wraps=sweep.lock_trees) as locks,
              mock.patch.object(sweep, "scan_roots", wraps=sweep.scan_roots) as scan,
              mock.patch.object(sweep, "directory_blocks", wraps=sweep.directory_blocks) as sizes):
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(held.exists())
        self.assertFalse(other.exists())
        self.assertEqual({path.relative_to(tree.parent): path.read_bytes()
                          for path in tree.parent.rglob("*") if path.is_file()}, before)
        locks.assert_called_once_with([str(other_tree)])
        scan.assert_called_once_with([str(other_tree.parent)], [str(other_tree)],
                                     lint_settings=conf.read("lint"))
        self.assertNotIn(mock.call(str(tree.parent)), sizes.call_args_list)
        self.assertIn(f"build-speed holds natedev builds; {tree.parent} left alone", output)
        self.assertIn("1 left alone while a build or test run uses them", output)

    def test_a_holders_recent_build_outside_its_workspace_keeps_every_file(self) -> None:
        base = self.base()
        since = time.time() - 60
        tree, held = cargo_target(base, "external", APP, since - DAY)
        for path in tree.parent.rglob("*"):
            os.utime(path, (since - DAY, since - DAY))
        # Only deps changed during the hold; the profile and fingerprint folders stayed old.
        os.utime(tree / "deps", (since + 30, since + 30))
        workspace = base / "workspace"
        workspace.mkdir()
        self.write_build_hold(base, since)
        record = cast(sessions.SessionRecord, cast(object, {
            "sessionId": "holder-session", "pid": 4242, "name": "build-speed",
        }))
        proc = base / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "cwd").symlink_to(workspace, target_is_directory=True)
        before = {path.relative_to(tree.parent): path.read_bytes()
                  for path in tree.parent.rglob("*") if path.is_file()}
        with (mock.patch.object(sessions, "live_sessions", return_value=[record]),
              mock.patch.object(sweep, "PROC_DIR", str(proc)),
              mock.patch.object(sweep, "lock_trees", wraps=sweep.lock_trees) as locks):
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertTrue(held.exists())
        self.assertEqual({path.relative_to(tree.parent): path.read_bytes()
                          for path in tree.parent.rglob("*") if path.is_file()}, before)
        locks.assert_not_called()
        self.assertIn(f"build-speed holds natedev builds; {tree.parent} left alone", output)

    def test_without_a_holder_file_recent_build_output_is_an_ordinary_candidate(self) -> None:
        base = self.base()
        tree, output_file = cargo_target(base, "external", APP, time.time() - DAY)
        with mock.patch.object(sweep, "lock_trees", wraps=sweep.lock_trees) as locks:
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertFalse(output_file.exists())
        locks.assert_called_once_with([str(tree)])
        self.assertNotIn("holds natedev builds", output)

    def test_a_holder_without_a_working_folder_still_protects_recent_builds(self) -> None:
        for missing in ("no session", "unreadable cwd"):
            with self.subTest(missing=missing):
                base = self.base()
                since = time.time() - 60
                tree, held = cargo_target(base, "external", APP, since - DAY)
                self.write_build_hold(base, since)
                records: list[sessions.SessionRecord] = []
                if missing == "unreadable cwd":
                    records.append(cast(sessions.SessionRecord, cast(object, {
                        "sessionId": "holder-session", "pid": 4242, "name": "build-speed",
                    })))
                with (mock.patch.object(sessions, "live_sessions", return_value=records),
                      mock.patch.object(sweep, "PROC_DIR", str(base / "missing-proc")),
                      mock.patch.object(sweep, "lock_trees", wraps=sweep.lock_trees) as locks):
                    status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], recorded=True)
                self.assertEqual(status, 0)
                self.assertTrue(held.exists())
                locks.assert_not_called()
                self.assertIn("build-speed holds natedev builds; its working folder was not found", output)
                self.assertIn(f"{tree.parent} left alone", output)

    def test_at_the_floor_or_while_another_sweep_holds_it_nothing_goes(self) -> None:
        base = self.base()
        _, output_file = cargo_target(base, "repo", APP, time.time() - 5 * DAY)
        self.assertEqual(self.hold(base, 10 * GIB, [10 * GIB], recorded=True), (0, ""))
        guard = sweep.try_lock(str(base / "floor.lock"))
        assert guard is not None
        self.addCleanup(os.close, guard)
        status, output = self.hold(base, 10 * GIB, [GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertIn("another sweep is holding it", output)
        self.assertTrue(output_file.exists())

    def test_dry_floor_runs_beside_a_held_floor_lock_but_a_real_pass_stops(self) -> None:
        base = self.base()
        tree, output_file = cargo_target(base, "repo", APP, time.time() - DAY)
        guard = sweep.try_lock(str(base / "floor.lock"))
        assert guard is not None
        self.addCleanup(os.close, guard)
        with mock.patch.object(sweep, "try_lock", wraps=sweep.try_lock) as locks:
            status, output = self.hold(base, 10 * GIB, [GIB, GIB], dry_run=True, recorded=True)
        self.assertEqual(status, 0)
        self.assertNotIn(mock.call(str(base / "floor.lock")), locks.call_args_list)
        self.assertIn(f"from {tree.parent}, last used", output)
        self.assertIn("the floor would take", output)
        self.assertNotIn("another sweep is holding it", output)
        self.assertTrue(output_file.exists())
        status, output = self.hold(base, 10 * GIB, [GIB], recorded=True)
        self.assertEqual(status, 0)
        self.assertIn("another sweep is holding it", output)
        self.assertTrue(output_file.exists())

    def test_floor_batch_process_rechecks_have_a_linear_path_resolution_bound(self) -> None:
        process_uses = sweep.process_uses
        realpath = os.path.realpath
        dirname = os.path.dirname
        counts: list[int] = []
        for size in (20, 40):
            base = self.base()
            proc = base / "proc"
            idle_processes(proc, 50)
            for number in range(size):
                _ = cargo_target(base, f"repo-{number:02d}", APP, time.time() - DAY)
            calls = 0
            # Five paths per pid on the initial check and every batch, plus scan bookkeeping.
            limit = 2 * (size + (size + 1) * 50 * 5) + 5000

            def counted_path(path: str, *, strict: bool = False) -> str:
                nonlocal calls
                calls += 1
                if calls > limit:
                    raise AssertionError(f"T={size}: exceeded linear realpath bound at {calls} calls")
                return realpath(path, strict=strict)

            def private_uses(targets: Sequence[str]) -> dict[str, sweep.TargetUse]:
                return process_uses(targets, str(proc))

            try:
                with (mock.patch.object(sweep, "process_uses", side_effect=private_uses) as checks,
                      mock.patch.object(os.path, "realpath", side_effect=counted_path),
                      mock.patch.object(os.path, "dirname", wraps=dirname) as parent):
                    status, output = self.hold(base, 10 * GIB, [GIB, GIB], dry_run=True, recorded=True)
                self.assertEqual(status, 0)
                self.assertEqual(checks.call_count, size + 1)
                self.assertEqual(output.count("the floor would take"), size)
                counts.append(calls)
                print(f"floor parent walks P=50, T={size}: {parent.call_count}")
            finally:
                print(f"floor realpath calls P=50, T={size}: {calls}; bound {limit}")
        self.assertLessEqual(counts[1], 2 * counts[0] + 500)

    def test_floor_only_skips_the_workspace_and_holds_the_configured_floor(self) -> None:
        config = self.base() / "lint.conf"
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}),
              mock.patch.object(socket, "gethostname", return_value="testhost"),
              mock.patch.object(sweep, "sweep_workspace") as workspace,
              mock.patch.object(tmp_sweep, "run_if_due",
                                return_value=tmp_sweep.SkippedSweepAssessment()) as scratch,
              mock.patch.object(sweep, "hold_floor", return_value=0) as floor):
            for setting, minutes in (("", 60.0), ("recent_minutes=180\n", 180.0)):
                with self.subTest(minutes=minutes):
                    _ = write_config_fixture(config, "sweep_free_floor_gib.testhost=7\n" + setting)
                    scratch.reset_mock()
                    floor.reset_mock()
                    lint_settings = conf.read("lint")
                    self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 0)
                    workspace.assert_not_called()
                    scratch.assert_called_once_with(False, lint_settings=lint_settings)
                    floor.assert_called_once_with(7 * GIB, False, lint_settings=lint_settings)

    def test_malformed_floor_is_an_error(self) -> None:
        config = self.base() / "lint.conf"
        _ = write_config_fixture(config, "sweep_free_floor_gib=lots\nsweep_free_floor_gib.natedev=lots\n")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}), redirect_stderr(io.StringIO()):
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


NO_MAC_CLAIMS: frozenset[str] = frozenset()


class MacFloorTests(SweepCase):
    def policy(self, base: Path, claims: frozenset[str] = NO_MAC_CLAIMS) -> sweep.MacFloor:
        return sweep.MacFloor((str(base),), floor_claims.LockedClaims(claims), base / "mirror")

    def mac_hold(self, policy: sweep.MacFloor, shortfall: int, *, dry_run: bool = False) -> str:
        output = io.StringIO()
        with (mock.patch.object(sweep, "free_bytes", return_value=GIB - shortfall),
              mock.patch.object(tmp_sweep, "lsof_paths", return_value=tmp_sweep.ProcessUsesKnown([])),
              redirect_stdout(output)):
            self.assertEqual(sweep.hold_floor(GIB, dry_run, policy=policy), 0)
        return output.getvalue()

    def test_live_mac_claim_keeps_the_mirror_target(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(base / "mirror", "held", APP, time.time() - DAY)
        _ = (tree.parent / sweep.USE_STAMP).write_text("")
        text = self.mac_hold(self.policy(base, frozenset({"held"})), 1)
        self.assertTrue(payload.exists())
        self.assertIn("busy_targets_skipped=1", text)
        self.assertIn("removed_bytes=0", text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_live_mac_claim_keeps_a_symlinked_target_in_another_root(self) -> None:
        mirror = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        other = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(other, "held", APP, time.time() - DAY)
        _ = (tree.parent / sweep.USE_STAMP).write_text("")
        (mirror / "hana").symlink_to(tree.parent, target_is_directory=True)
        policy = sweep.MacFloor((str(mirror), str(other)),
                                floor_claims.LockedClaims(frozenset({"hana"})), mirror)
        self.assertTrue(policy.claims.holds(str(tree.parent), mirror))
        text = self.mac_hold(policy, 1)
        self.assertTrue(payload.exists())
        self.assertIn("busy_targets_skipped=1", text)
        self.assertIn("removed_bytes=0", text)

    def test_unreadable_mac_claims_keep_every_target(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(base, "idle", APP, time.time() - DAY)
        _ = (tree.parent / sweep.USE_STAMP).write_text("")
        with mock.patch("scripts.mac_test.floor_claims.subprocess.Popen", side_effect=OSError("unreadable")):
            with floor_claims.locked_claims(1) as claims:
                text = self.mac_hold(sweep.MacFloor((str(base),), claims, base / "mirror"), 1)
        self.assertTrue(payload.exists())
        self.assertIn("busy_targets_skipped=1", text)

    def test_mac_claim_decoder_rejects_a_corrupt_private_record(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        claims = state / "claims"
        claims.mkdir()
        _ = (claims / "broken.json").write_text("not JSON")
        result = subprocess.run([sys.executable, str(Path(floor_claims.__file__))],
                                input="", text=True, capture_output=True, check=False,
                                env={**os.environ, "MAC_TEST_STATE_DIR": str(state)}, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("ClaimStateUnreadable", result.stderr)

    def test_no_proc_mac_sweep_takes_only_the_oldest_shortfall(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        old_tree, old = cargo_target(base, "old", APP, time.time() - 3 * DAY)
        new_tree, newer = cargo_target(base, "new", DEMO, time.time() - DAY)
        for tree, age in ((old_tree, 3 * DAY), (new_tree, DAY)):
            stamp = tree.parent / sweep.USE_STAMP
            _ = stamp.write_text("")
            os.utime(stamp, (time.time() - age, time.time() - age))
        with mock.patch.object(sweep, "process_uses", side_effect=AssertionError("Mac must use claims and lsof")):
            text = self.mac_hold(self.policy(base), 1)
        self.assertFalse(old.exists())
        self.assertTrue(newer.exists())
        self.assertIn("busy_targets_skipped=0", text)

    def test_mac_dry_run_lists_targets_and_deletes_nothing(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(base, "idle", APP, time.time() - DAY)
        _ = (tree.parent / sweep.USE_STAMP).write_text("")
        before = {path: path.read_bytes() for path in base.rglob("*") if path.is_file()}
        text = self.mac_hold(self.policy(base), 1, dry_run=True)
        self.assertEqual({path: path.read_bytes() for path in base.rglob("*") if path.is_file()}, before)
        self.assertTrue(payload.exists())
        self.assertIn("the floor would take", text)
        self.assertIn("removed_bytes=0", text)

    def test_mac_rejects_unmarked_outside_symlink_and_dotdot_targets(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(base, "unmarked", APP, time.time() - DAY)
        other_tree, other = cargo_target(outside, "other", DEMO, time.time() - DAY)
        _ = (other_tree.parent / sweep.USE_STAMP).write_text("")
        policy = self.policy(base)
        self.assertFalse(policy.allows(str(tree.parent)))
        self.assertFalse(policy.allows(str(other_tree.parent)))
        link = base / "link"
        link.symlink_to(other_tree.parent, target_is_directory=True)
        self.assertFalse(policy.allows(str(link)))
        self.assertFalse(policy.allows(str(base / ".." / outside.name / "other" / "target")))
        _ = self.mac_hold(policy, 1)
        self.assertTrue(payload.exists())
        self.assertTrue(other.exists())

    def test_mac_missing_or_lower_floor_refuses_with_one_line(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "mac_test.conf"
        original = config.read_text()
        for changed in (original.replace("sweep_floor_gib=150", "sweep_floor_gib=99"),
                        original.replace("sweep_floor_gib=150", "")):
            with self.subTest(changed=changed):
                _ = config.write_text(changed)
                output = io.StringIO()
                with redirect_stderr(output), mock.patch.object(sweep, "hold_floor") as take:
                    self.assertEqual(sweep.main(["--floor-only", "--host", "mac"]), 2)
                take.assert_not_called()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
        _ = config.write_text(original)

    def test_mac_entry_uses_mac_floor_and_skips_the_tmp_sweep(self) -> None:
        config = Path(os.environ["CONF_DIR"]) / "mac_test.conf"
        _ = write_config_fixture(config, "sweep_floor_gib=101")
        claims = floor_claims.LockedClaims(frozenset())
        with (mock.patch.object(floor_claims, "locked_claims", return_value=nullcontext(claims)),
              mock.patch.object(sweep, "hold_floor", return_value=0) as take,
              mock.patch.object(tmp_sweep, "run_if_due") as tmp):
            self.assertEqual(sweep.main(["--floor-only", "--host", "mac", "--dry-run"]), 0)
        self.assertEqual(take.call_args.args, (101 * GIB, True))
        self.assertIsInstance(cast(object, take.call_args.kwargs["policy"]), sweep.MacFloor)
        tmp.assert_not_called()

    def test_lost_claim_lock_or_unreadable_process_use_keeps_output(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, payload = cargo_target(base, "idle", APP, time.time() - DAY)
        _ = (tree.parent / sweep.USE_STAMP).write_text("")
        with mock.patch.object(tmp_sweep, "lsof_paths", return_value=tmp_sweep.ProcessUsesUnavailable()):
            self.assertIn(str(tree.parent), self.policy(base).uses([str(tree.parent)]))
        self.assertTrue(payload.exists())


class LockHolderTests(unittest.TestCase):
    def test_the_flock_holder_comes_from_the_locks_listing(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        lock = base / ".cargo-lock"
        _ = lock.write_text("")
        stat = os.stat(lock)
        device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
        locks = base / "locks"
        _ = locks.write_text(f"1: POSIX  ADVISORY  WRITE 99 {device} 0 EOF\n"
                             + f"2: -> FLOCK  ADVISORY  WRITE 77 {device} 0 EOF\n"
                             + f"3: FLOCK  ADVISORY  WRITE 4242 {device} 0 EOF\n")
        self.assertEqual(sweep.lock_holder(str(lock), str(locks)), 4242)
        self.assertIsNone(sweep.lock_holder(str(lock), str(base / "missing")))
        self.assertIsNone(sweep.lock_holder(str(base / "gone"), str(locks)))

    def test_a_read_that_skips_the_holders_line_is_read_again(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        lock = base / ".cargo-lock"
        _ = lock.write_text("")
        stat = os.stat(lock)
        device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
        other = "1: FLOCK  ADVISORY  WRITE 99 00:01:1 0 EOF\n"
        reads = [other, other + f"2: FLOCK  ADVISORY  WRITE 4242 {device} 0 EOF\n"]

        def skip_once(*_args: object, **_kwargs: object) -> io.StringIO:
            return io.StringIO(reads.pop(0))

        def never_held(*_args: object, **_kwargs: object) -> io.StringIO:
            return io.StringIO(other)

        with mock.patch.object(file_lock, "open", create=True, side_effect=skip_once):
            self.assertEqual(sweep.lock_holder(str(lock), "locks"), 4242)
        with mock.patch.object(file_lock, "open", create=True, side_effect=never_held) as opened:
            self.assertIsNone(sweep.lock_holder(str(lock), "locks"))
        self.assertEqual(opened.call_count, conf.read("lint").whole("sweep_proc_locks_reads"))

    @unittest.skipUnless(os.path.isfile(sweep.PROC_LOCKS), "the lock holder is read from /proc/locks")
    def test_a_live_flock_names_this_process(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        fd = os.open(base / ".cargo-lock", os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(sweep.lock_holder(str(base / ".cargo-lock")), os.getpid())


class HolderSessionTests(unittest.TestCase):
    def test_repeated_holder_queries_preserve_the_import_path(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tree, _ = cargo_target(base, "held", APP, time.time())
        holders = base / "holders"
        holders.mkdir()
        _ = (holders / "holder-session").write_text(json.dumps({
            "holder": "hana-unit", "since": datetime.now(PACIFIC).isoformat(),
            "for": "timed builds", "release_eta": "unknown",
        }))
        proc = base / "proc"
        (proc / "100").mkdir(parents=True)
        (proc / "100" / "cwd").symlink_to(tree.parent.parent, target_is_directory=True)
        record = cast(sessions.SessionRecord, cast(object, {
            "pid": 100, "sessionId": "holder-session", "name": "hana-unit",
            "messagingSocketPath": "/tmp/s.sock", "updatedAt": 1, "tmux": "", "formerNames": [],
        }))
        table = {
            100: process_table.Process(100, 1, "claude", "claude"),
            300: process_table.Process(300, 100, "cargo", "cargo build"),
        }
        with (mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": str(holders),
                                          tmp_sweep.STATE_ENV: str(base / "state")}),
              mock.patch.object(build_hold, "live_sessions", return_value=[]),
              mock.patch.object(sessions, "live_sessions", return_value=[record]),
              mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(sweep, "PROC_DIR", str(proc))):
            original_path = list(sys.path)
            for attempt in range(2):
                with (self.subTest(query="build_hold_targets", attempt=attempt),
                      mock.patch.object(sys, "path", list(original_path))):
                    self.assertEqual(sweep.build_hold_targets([str(tree.parent)], {}),
                                     {str(tree.parent): sweep.BuildHoldTarget("hana-unit")})
                    self.assertEqual(sys.path, original_path)
                with (self.subTest(query="holder_sessions", attempt=attempt),
                      mock.patch.object(sys, "path", list(original_path))):
                    self.assertEqual(sweep.holder_sessions([300]), {300: "hana-unit"})
                    self.assertEqual(sys.path, original_path)

    def test_a_pid_belongs_to_its_nearest_named_session(self) -> None:
        table = {
            1: process_table.Process(1, 0, "systemd", "/sbin/init"),
            100: process_table.Process(100, 1, "claude", "claude"),
            200: process_table.Process(200, 100, "zsh", "zsh"),
            300: process_table.Process(300, 200, "cargo", "cargo build"),
            400: process_table.Process(400, 1, "cargo-berth", "cargo-berth resolve"),
        }
        record = cast(sessions.SessionRecord, cast(object, {
            "pid": 100, "sessionId": "d5b35898", "name": "hana-unit", "messagingSocketPath": "/tmp/s.sock",
            "updatedAt": 1, "tmux": "", "formerNames": []}))
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(sessions, "live_sessions", return_value=[record])):
            self.assertEqual(sweep.holder_sessions([300, 400, 999]), {300: "hana-unit"})
        self.assertEqual(sweep.holder_sessions([]), {})


class BudgetTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        state = self.enterContext(tempfile.TemporaryDirectory())
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {tmp_sweep.STATE_ENV: state})))

    def config(self) -> str:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "lint.conf"
        _ = path.write_text(CONFIG)
        return str(path)

    def test_environment_budget_does_not_need_a_config_file(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        missing = root / "missing.conf"
        self.assertEqual(
            sweep.budget_bytes({sweep.BUDGET_ENV: "1024"}, conf.read(missing), None),
            (1024 * GIB, f"{sweep.BUDGET_ENV} in the environment"),
        )

    def test_workspace_recovers_doc_budget_separately_from_a_private_budget_file(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        budget_path = root / "lint.conf"
        _ = budget_path.write_text("# range: 0..\nsweep_budget_gib=17\n")
        target = root / "target"
        target.mkdir()
        repository = Path(__file__).resolve().parents[2] / "config" / "lint.conf"
        doc_budget = int(conf.read(repository).number("sweep_doc_index_mib") * sweep.MIB)
        environment = {key: value for key, value in os.environ.items() if key != "CONF_DIR"}
        environment.update({sweep.BUDGET_ENV: "", sweep.DOC_INDEX_ENV: ""})
        with (mock.patch.dict(os.environ, environment, clear=True),
              mock.patch.object(conf, "directory", return_value=root),
              mock.patch.object(sweep, "process_uses", return_value={}),
              mock.patch.object(sweep, "_build_trees", return_value=[]),
              mock.patch.object(sweep, "prune_doc_index", return_value=0) as prune,
              mock.patch.object(sweep, "sweep", return_value=0) as workspace):
            self.assertEqual(sweep.sweep_workspace(str(target), conf.read(budget_path), True, 60), 0)
        self.assertEqual(prune.call_args.args[1], doc_budget)
        self.assertEqual(workspace.call_args.args[2], 17 * GIB)

    def test_sweep_recovers_missing_live_recent_minutes_from_the_repository(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        live = root / "live"
        live.mkdir()
        _ = (live / "lint.conf").write_text("# range: 0..\nsweep_budget_gib=1\n")
        repository = root / "repository.conf"
        _ = repository.write_text("# range: 0..\nrecent_minutes=60\n")
        environment = {key: value for key, value in os.environ.items() if key != "CONF_DIR"}
        with (mock.patch.dict(os.environ, environment, clear=True),
              mock.patch.object(conf, "directory", return_value=live),
              mock.patch.object(conf, "_repository_path", return_value=repository),
              mock.patch.object(sweep, "sweep_workspace", return_value=0) as workspace,
              redirect_stderr(io.StringIO())):
            self.assertEqual(sweep.main([]), 0)
        self.assertEqual(workspace.call_args.args[3], 60.0)

    def test_one_sweep_shares_settings_with_its_workspace_budget(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = write_config_fixture(root / "lint.conf", "recent_minutes=60\nsweep_budget_gib=1\n")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(root), sweep.BUDGET_ENV: ""}),
              mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(sweep, "cargo_roots", return_value=[]),
              mock.patch.object(sweep, "hold_floor", return_value=0),
              redirect_stdout(io.StringIO())):
            self.assertEqual(sweep.main([]), 0)
        reads.assert_called_once_with("lint")

    def test_sweep_import_omits_unused_modules(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {**os.environ, "BUILD_HOLD_DIR": str(base / "holders"),
                       "BUILD_HOLD_RELEASE_DIR": str(base / "release"),
                       "BUILDLOG_DIR": str(base / "buildlog"),
                       "NOTIFIER_SESSIONS_DIR": str(base / "sessions"),
                       "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        omitted = ("scripts.lint.tmp_sweep", "scripts.buildlog.record", "scripts.buildlog.index")
        command = ("import sys; from scripts.lint import sweep; "
                   + f"assert not set({omitted!r}).intersection(sys.modules)")
        result = subprocess.run([sys.executable, "-c", command], cwd=base, env=environment,
                                capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sweep_import_loads_one_copy_of_record_index_and_step_outputs(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        loaded = fresh_import_modules(base, "lint", "sweep")
        for name in ("record", "index", "step_outputs"):
            with self.subTest(module=name):
                self.assertEqual(len(set(loaded) & {name, "scripts.buildlog." + name}), 1, loaded)

    def test_config_reads_as_lint_config_sh_does(self) -> None:
        values = conf.read(Path(self.config())).values()
        self.assertEqual(values["sweep"], "on")
        self.assertEqual(values["sweep_budget_gib"], "24")
        self.assertEqual(values["sweep_budget_gib.hana"], "96")
        self.assertEqual(values["sweep_budget_gib.unset"], "")
        with self.assertRaises(conf.Unusable):
            _ = conf.read(Path("/nonexistent/lint.conf")).values()

    def test_repo_key_then_default_key_then_built_in(self) -> None:
        config = self.config()
        self.assertEqual(sweep.budget_bytes({}, config, "hana"), (96 * GIB, f"sweep_budget_gib.hana in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "nateroids"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, "unset"), (24 * GIB, f"sweep_budget_gib in {config}"))
        self.assertEqual(sweep.budget_bytes({}, config, None), (24 * GIB, f"sweep_budget_gib in {config}"))
        missing = "/nonexistent/lint.conf"
        with self.assertRaises(conf.Unusable):
            _ = sweep.budget_bytes({}, missing, "hana")

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

    def test_a_shared_clone_names_the_repo_it_borrows_objects_from(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        repo = base / "hana"
        repo.mkdir()
        _ = (repo / "README").write_text("readme\n")
        git("init", "-q", "-b", "main", cwd=repo)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "first", cwd=repo)
        git("clone", "-q", "--shared", str(repo), str(base / "land60"), cwd=base)
        git("clone", "-q", "--shared", str(base / "land60"), str(base / "mergeclone"), cwd=base)
        self.assertEqual(sweep.repo_name(str(base / "land60")), "hana")
        self.assertEqual(sweep.repo_name(str(base / "mergeclone")), "hana")
        git("clone", "-q", str(base / "mergeclone"), str(base / "clone88ci"), cwd=base)
        self.assertEqual(sweep.repo_name(str(base / "clone88ci")), "hana")


if __name__ == "__main__":
    _ = unittest.main()
