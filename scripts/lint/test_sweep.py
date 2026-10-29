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
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock
from zoneinfo import ZoneInfo

import sweep
if TYPE_CHECKING:
    from ..build_hold import build_hold
    from ..message import process_table, sessions
    from ..notify import review
    from ..notify.push_texts import RenderedPush, catalog, render, render_entry
    from ..notify.user_action import ActionRequired
else:
    import build_hold
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    import process_table
    import sessions
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    import review
    from push_texts import RenderedPush, catalog, render, render_entry
    from user_action import ActionRequired

GIB = 1 << 30
SIZE = 64 * 1024
DAY = 86_400
APP = "0123456789abcdef"
DEMO = "1111111111111111"
TOOL = "2222222222222222"
MY_TOOL = "3333333333333333"
STALE = "4444444444444444"
PAGE_PATHS = ["/home/natepiano/rust/cargo-liner-berth/target: frees 26.0 GiB. Process 3104822 uses it now and can fail.",
              "/tmp/claude-1000/scratch: frees 86.0 GiB."]


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


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


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
    @override
    def setUp(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            sweep.FLOOR_STATE_ENV: str(state / "sweep"),
            sweep.BUILDLOG_DIR_ENV: str(state / "buildlog"),
        })))

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
        self.assertEqual({Path(path) for group in scan.orphans for path in group.entries}, target.orphans)
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
            status = sweep.sweep(roots, trees, total - 1, "test", False)

        self.assertEqual(status, 0)
        self.assertFalse(old_compile.exists())
        self.assertTrue(older_use.exists())

    def test_budget_dry_run_writes_no_removal_record(self) -> None:
        target = self.target()
        self.assertEqual(run_sweep(target, 0, dry_run=True)[0], 0)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_orphan_cleanup_writes_no_rebuild_removal_record(self) -> None:
        target = self.target()
        self.assertEqual(run_sweep(target, 1 << 40, dry_run=False)[0], 0)
        self.assertFalse(sweep.sweep_removals_path().exists())

    def test_removal_record_write_failure_does_not_fail_budget_sweep(self) -> None:
        target = self.target()
        blocked = Path(self.enterContext(tempfile.TemporaryDirectory())) / "file"
        _ = blocked.write_text("not a directory")
        errors = io.StringIO()
        with (mock.patch.object(sweep, "sweep_removals_path", return_value=blocked / "removals.jsonl"),
              redirect_stderr(errors)):
            self.assertEqual(run_sweep(target, 0, dry_run=False)[0], 0)
        self.assertFalse(target.app.exists())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("could not record removal", errors.getvalue())

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
            status = sweep.sweep(roots, trees, total - 1, "test", False)

        self.assertEqual(status, 0)
        self.assertTrue(recent.exists())
        self.assertFalse(older.exists())

    def test_budget_takes_recent_output_when_older_output_cannot_meet_the_budget(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        now = time.time()
        tree, older = cargo_target(base, "repo", APP, now - 2 * 3600)
        recent = unit(tree, "deps", "recent", DEMO, now - 10 * 60)

        with redirect_stdout(io.StringIO()):
            status = sweep.sweep([str(tree.parent)], [str(tree)], 0, "test", False)

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
        orphaned = sum(sweep.freed_by(group, scan, remaining) for group in scan.orphans)
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
        orphaned = sum(sweep.freed_by(group, scan, remaining) for group in scan.orphans)
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
    def test_removal_trim_keeps_newest_half_megabyte_and_next_append_does_not_trim(self) -> None:
        path = sweep.sweep_removals_path()
        path.parent.mkdir(parents=True)
        now = time.time()
        rows = [json.dumps({"at": now - 20000 + index, "path": f"/tmp/repo-{index}/target",
                            "cause": "floor take"}) + "\n" for index in range(16000)]
        _ = path.write_text("".join(rows))
        self.assertGreater(path.stat().st_size, sweep.MIB)
        sweep.record_sweep_removal("/tmp/new/target", "budget take")
        self.assertLessEqual(path.stat().st_size, sweep.MIB // 2)
        records = sweep.read_sweep_removals()
        self.assertGreater(len(records), 1)
        self.assertEqual(records[-1].path, "/tmp/new/target")
        self.assertEqual(records[-2].path, "/tmp/repo-15999/target")
        self.assertNotEqual(records[0].path, "/tmp/repo-0/target")
        with mock.patch.object(sweep, "parse_sweep_removals", wraps=sweep.parse_sweep_removals) as parse:
            sweep.record_sweep_removal("/tmp/next/target", "floor take")
        parse.assert_not_called()

    def test_unreadable_removal_records_leave_journal_reporting_available(self) -> None:
        folder = "/tmp/gate-check.hana"
        self.step(folder, 2 * DAY)
        self.removal(DAY, f"lint sweep: the floor took 1.0 GiB from {folder}/target, last used yesterday")
        self.step(folder, DAY // 2, 50, 3600)
        for failure in (PermissionError("denied"), OSError("read failed")):
            with self.subTest(failure=failure):
                errors = io.StringIO()
                unreadable = mock.Mock(spec=Path)
                unreadable.configure_mock(**{"open.side_effect": failure})
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
        root = Path(os.environ[sweep.BUILDLOG_DIR_ENV])
        path = root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in self.records))
        before = {item.relative_to(root): (item.read_bytes(), item.stat().st_mtime_ns)
                  for item in root.rglob("*") if item.is_file()}
        state = Path(os.environ[sweep.FLOOR_STATE_ENV])
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
        self.step(folder, DAY // 2, 50, 3600)
        self.assert_hours(self.report(), "Total",
                          {"floor take": 0, "budget take": 1, "tmp sweep": 0, "none": 0})
        events = sweep.read_sweep_removals()
        self.assertEqual(events, [sweep.SweepRemoval(self.now - DAY, str(target.root), "budget take")])

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

    def test_large_removal_record_drops_only_records_older_than_35_days(self) -> None:
        path = sweep.sweep_removals_path()
        path.parent.mkdir(parents=True)
        expired = json.dumps({"at": self.now - 36 * DAY, "path": "/tmp/old/target", "cause": "floor take"}) + "\n"
        retained = json.dumps({"at": self.now - 35 * DAY, "path": "/tmp/keep/target", "cause": "budget take"}) + "\n"
        _ = path.write_text(expired * (sweep.MIB // len(expired) + 1) + retained)
        with mock.patch.object(time, "time", return_value=self.now):
            sweep.record_sweep_removal("/tmp/new/target", "floor take")
        self.assertEqual(sweep.read_sweep_removals(), [
            sweep.SweepRemoval(self.now - 35 * DAY, "/tmp/keep/target", "budget take"),
            sweep.SweepRemoval(self.now, "/tmp/new/target", "floor take"),
        ])
        self.assertLess(path.stat().st_size, sweep.MIB)

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
        environ = {sweep.BUDGET_ENV: "1024", sweep.CONFIG_ENV: "/nonexistent/lint.conf"}
        with mock.patch.dict(os.environ, environ), redirect_stdout(output):
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
        environ = {sweep.BUDGET_ENV: "0", sweep.CONFIG_ENV: "/nonexistent/lint.conf"}
        with mock.patch.dict(os.environ, environ), redirect_stdout(output):
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
        f"Signature: 8a477f597d28d172789f06886806bc55\n{sweep.CARGO_TAG_LINE}\n"
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


def run_from(binary: Path, cwd: Path | None = None) -> subprocess.Popen[bytes]:
    """A sleeping process whose exe is a copy of sleep at binary, as a test binary runs from deps/."""
    sleep = shutil.which("sleep")
    assert sleep is not None
    _ = shutil.copy2(sleep, binary)
    # argv[0] stays "sleep": a multi-call coreutils picks its program by that name.
    return subprocess.Popen(["sleep", "60"], executable=binary, cwd=cwd)


class ProcessUseTests(SweepCase):
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

    def test_without_proc_nothing_is_in_use(self) -> None:
        self.assertEqual(self.uses(), {})


class FloorTests(SweepCase):
    def test_floor_removes_old_hard_links_in_order_and_keeps_newer_output(self) -> None:
        base = self.base()
        now = time.time()
        first_tree, first = cargo_target(base, "first", APP, now - 3 * DAY)
        second_tree, second = cargo_target(base, "second", DEMO, now - 2 * DAY)
        _, newer = cargo_target(base, "newer", TOOL, now - DAY)
        second.unlink()
        os.link(first, second)
        with mock.patch.object(sweep, "remove", wraps=sweep.remove) as removed:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE // 2, 10 * GIB])
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
        def use_during_size_walk(path: str, seen: set[sweep.InodeKey] | None = None) -> int:
            stamp = old_tree.parent / sweep.USE_STAMP
            if not stamp.exists():
                stamp.touch()
                os.utime(stamp, (now + 10, now + 10))
                clock[0] = now + 20
            return original(path, seen)
        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "directory_blocks", side_effect=use_during_size_walk)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertTrue(old.exists())
        self.assertFalse(next_output.exists())

    def test_floor_records_deletion_before_unlock_and_attributes_rebuild_during_pass(self) -> None:
        base = self.base()
        now = time.time()
        first_tree, _ = cargo_target(base, "first", APP, now - 3 * DAY)
        _, _ = cargo_target(base, "second", DEMO, now - 2 * DAY)
        clock = [now]
        original_remove, original_release = sweep.remove, sweep.release
        releases: list[float] = []
        def delete_at_known_time(groups: list[sweep.Group]) -> int:
            clock[0] = now + (10 if groups[0].build_tree == str(first_tree) else 30)
            return original_remove(groups)
        def rebuild_after_release(held: list[int]) -> None:
            try:
                if not releases:
                    self.assertEqual(sweep.read_sweep_removals(),
                                     [sweep.SweepRemoval(now + 10, str(first_tree.parent), "floor take")])
            finally:
                original_release(held)
            releases.append(clock[0])
            if len(releases) == 1:
                clock[0] = now + 20
        with (mock.patch.object(time, "time", side_effect=lambda: clock[0]),
              mock.patch.object(sweep, "remove", side_effect=delete_at_known_time),
              mock.patch.object(sweep, "release", side_effect=rebuild_after_release)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - SIZE - SIZE // 2, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertEqual(releases, [now + 10, now + 30])
        folder = str(first_tree.parent.parent)
        steps = [sweep.RebuildStep(folder, folder, now + 1, now + 5, 0, 1),
                 sweep.RebuildStep(folder, folder, now + 20, now + 21, 50, 1)]
        cold = sweep.rebuild_causes(steps, sweep.read_sweep_removals(), now, now + 40)
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
        self.assertEqual(sum(sweep.freed_by(group, parallel, parallel_links) for group in parallel.groups),
                         sum(sweep.freed_by(group, serial, serial_links) for group in serial.groups))

    def test_floor_locks_only_one_target_after_candidate_choice_and_stops_at_shortfall(self) -> None:
        base = self.base()
        now = time.time()
        old_tree, old = cargo_target(base, "old", APP, now - 3 * DAY)
        _, next_output = cargo_target(base, "next", DEMO, now - 2 * DAY)
        active: list[int] = []
        acquired: list[list[str]] = []
        original_locks, original_release = sweep.lock_trees, sweep.release
        original_scan, original_sizes = sweep.scan_roots, sweep.directory_blocks
        def lock_one(trees: list[str]) -> tuple[list[int], str | None]:
            self.assertEqual(active, [], "Two targets must never hold locks together")
            acquired.append(trees)
            held, blocked = original_locks(trees)
            active.extend(held)
            return held, blocked
        def unlock(held: list[int]) -> None:
            original_release(held)
            for fd in held:
                active.remove(fd)
        def scan_unlocked(roots: list[str], trees: list[str]) -> sweep.Scan:
            self.assertEqual(active, [], "Candidate scan must hold no cargo locks")
            return original_scan(roots, trees)
        def size_unlocked(path: str, seen: set[sweep.InodeKey] | None = None) -> int:
            self.assertEqual(active, [], "Size walks must hold no cargo locks")
            return original_sizes(path, seen)
        with (mock.patch.object(sweep, "lock_trees", side_effect=lock_one),
              mock.patch.object(sweep, "release", side_effect=unlock),
              mock.patch.object(sweep, "scan_roots", side_effect=scan_unlocked),
              mock.patch.object(sweep, "directory_blocks", side_effect=size_unlocked)):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])
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
        def busy_after_scan(trees: list[str]) -> tuple[list[int], str | None]:
            if trees == [str(old_tree)]:
                return [], str(old_tree / sweep.LOCK_NAMES[0])
            return original(trees)
        with mock.patch.object(sweep, "lock_trees", side_effect=busy_after_scan) as locks:
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])
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
        def change_after_scan(trees: list[str]) -> tuple[list[int], str | None]:
            if trees == [str(old_tree)]:
                _ = old.write_bytes(b"new build output")
            return original(trees)
        with mock.patch.object(sweep, "lock_trees", side_effect=change_after_scan):
            status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertTrue(old.exists())
        self.assertFalse(next_output.exists())

    def test_floor_records_removals_when_output_is_captured(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "repo", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        events = sweep.read_sweep_removals()
        self.assertEqual([(event.path, event.cause) for event in events],
                         [(str(tree.parent), "floor take")])

    def test_floor_dry_run_writes_no_removal_record(self) -> None:
        base = self.base()
        _, output = cargo_target(base, "repo", APP, time.time() - DAY)
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB], dry_run=True)
        self.assertEqual(status, 0)
        self.assertTrue(output.exists())
        self.assertFalse(sweep.sweep_removals_path().exists())

    def base(self) -> Path:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            sweep.FLOOR_STATE_ENV: str(base / "state"),
            sweep.BUILDLOG_DIR_ENV: str(base / "buildlog"),
            "BUILD_HOLD_DIR": str(base / "build-hold"),
        }))
        _ = self.enterContext(mock.patch.object(build_hold, "live_sessions", return_value=[]))
        _ = self.enterContext(mock.patch.object(sessions, "live_sessions", return_value=[]))
        _ = self.enterContext(mock.patch.object(sweep, "process_uses", return_value={}))
        _ = self.enterContext(mock.patch.object(sweep, "CI_TARGETS", (str(base / "ci-1"), str(base / "ci-2"))))
        _ = self.enterContext(mock.patch.object(
            sweep, "send_floor_alert", side_effect=AssertionError("unexpected floor alert send")))
        _ = self.enterContext(mock.patch.object(
            sweep, "send_holder_message", side_effect=AssertionError("unexpected session message send")))
        _ = self.enterContext(mock.patch.object(sweep, "holder_sessions", return_value={}))
        return base

    def test_base_blocks_real_phone_sends(self) -> None:
        """Guard the fixture patch that prevents real phone sends."""
        _ = self.base()
        self.assertIsInstance(sweep.send_floor_alert, mock.Mock)
        with self.assertRaisesRegex(AssertionError, "unexpected floor alert send"):
            _ = sweep.send_floor_alert("test", sweep.FloorAlertChannels.NATEDEV)

    def hold(self, base: Path, floor: int, free: list[int], dry_run: bool = False,
             scanned: int | None = None) -> tuple[int, str]:
        """free is the free space before the sweep and after it; scanned, the free space read once
        the caches are measured, is the first value unless given."""
        reads = [free[0], free[0] if scanned is None else scanned, *free[1:]]
        output = io.StringIO()
        with mock.patch.object(sweep, "free_bytes", side_effect=reads), redirect_stdout(output):
            status = sweep.hold_floor(
                floor,
                dry_run,
                [str(base)],
                str(base / "floor.lock"),
            )
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
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_not_called()
        record = sweep.read_floor_record(sweep.floor_record_path())
        assert isinstance(record, sweep.FloorRecord)
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
              mock.patch.object(sweep, "send_floor_alert", return_value=True) as send):
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_called_once()
        self.assertIn("free space fell more than 10 GiB beyond build-cache growth", cast(str, send.call_args.args[0]))

    def test_an_old_record_without_held_roots_does_not_fire_a_growth_alert(self) -> None:
        base = self.base()
        tree, output = cargo_target(base, "idle", APP, time.time() - DAY)
        path = sweep.floor_record_path()
        path.parent.mkdir()
        _ = path.write_text(json.dumps({
            "measured_at": time.time() - 60, "free_bytes": 30 * GIB,
            "build_cache_bytes": sweep.directory_blocks(str(tree.parent)),
            "last_alert_at": None, "last_push_at": None,
        }))
        previous = sweep.read_floor_record(path)
        assert isinstance(previous, sweep.FloorRecord)
        self.assertIsInstance(previous.held_roots, sweep.UnknownHeldRoots)
        with mock.patch.object(sweep, "send_floor_alert") as send:
            status, _ = self.hold(base, 10 * GIB, [9 * GIB, 11 * GIB])
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        send.assert_not_called()

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
                                  scanned=int(416.1 * GIB))
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
        with mock.patch.object(sweep, "send_floor_alert", return_value=True) as send:
            status, _ = self.hold(base, 400 * GIB, [int(293.5 * GIB), int(400.0 * GIB)],
                                  scanned=int(393.4 * GIB))
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
                                   scanned=int(416.1 * GIB))
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
        self.assertIn("No idle build cache is left to remove", message)
        self.assertIn("natedev still has only 9.0 GiB free. Builds need 10.0 GiB free", message)
        self.assertNotIn("Free ", message)
        lock_path = busy_tree / sweep.LOCK_NAMES[0]
        self.assertIn(f"- {busy_tree.parent} ({sweep.gib(held_bytes)}): pid {os.getpid()} holds the cargo lock {lock_path};"
                      + " no session found", message)
        self.assertIn(f"Not swept: CI's targets ({sweep.gib(ci_bytes)})", message)
        self.assertIn("No session above could be told, so the user also gets a page with these paths.", message)
        page = cast(RenderedPush, send.call_args.kwargs["phone"])
        self.assertIn(f"{busy_tree.parent}: frees {sweep.gib(held_bytes)}. Process {os.getpid()} uses it now", page.values["paths"])
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

        def deliver(_message: str, _channels: sweep.FloorAlertChannels, **_phone: object) -> bool:
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
        fake_module = Path(self.enterContext(tempfile.TemporaryDirectory())) / "scripts/lint/sweep.py"
        sender = fake_module.parents[1] / "message/send.py"
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
        real_run = subprocess.run
        with (mock.patch.object(sweep, "__file__", str(fake_module)),
              mock.patch.object(subprocess, "run", wraps=real_run) as run,
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "1", "SWEEP_USER_EXIT": "0"}),
              redirect_stdout(output), redirect_stderr(errors)):
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))
        self.assertEqual(run.call_count, 2)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual([command[command.index("--to") + 1] for command in commands], ["natedev", "user"])
        self.assertEqual(commands[1][commands[1].index("--text-id") + 1], "sweep.disk_floor")
        self.assertIn("shortfall=1.0 GiB", commands[1])
        self.assertEqual(run.call_args_list[0].kwargs["input"], "disk notice")
        self.assertIsNone(cast(object, run.call_args_list[1].kwargs["input"]))
        self.assertIn("message alert queued", output.getvalue())
        self.assertIn("phone alert delivered", output.getvalue())

        with (mock.patch.object(sweep, "__file__", str(fake_module)),
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "0", "SWEEP_USER_EXIT": "3"}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))
        with (mock.patch.object(sweep, "__file__", str(fake_module)),
              mock.patch.dict(os.environ, {"SWEEP_NATEDEV_EXIT": "3", "SWEEP_USER_EXIT": "3"}),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.assertFalse(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))

    def test_phone_alert_wait_covers_configured_review_while_session_wait_stays_short(self) -> None:
        config = Path(self.enterContext(tempfile.TemporaryDirectory())) / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        with (mock.patch.object(review, "NOTIFY_CONFIG", config),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
              redirect_stdout(io.StringIO())):
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [40, 220])
        self.assertEqual(run.call_count, 2)

    def test_phone_disk_alert_keeps_action_and_build_shortfall_without_directory_dump(self) -> None:
        """F011: the disk action states how much to free and the body explains why."""
        text = sweep.floor_out_of_reach_text(2000, 2 * GIB, 9 * GIB, 10 * GIB,
            [], frozenset(), 0, sweep.UnavailableDiskMeasurement("missing"), 0)
        with (mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run,
              redirect_stdout(io.StringIO())):
            self.assertTrue(sweep.send_floor_alert(text, sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))
        self.assertEqual(run.call_args_list[0].kwargs["input"], text)
        command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "sweep.disk_floor")
        self.assertIn("shortfall=1.0 GiB", command)
        self.assertIn("floor=10.0 GiB", command)
        self.assertNotIn("measurement unavailable", " ".join(command))

    def test_typical_disk_cleanup_example_uses_the_live_build_floor(self) -> None:
        entry = next(entry for entry in catalog() if entry.id == "sweep.disk_floor")
        typical = next(example for example in entry.examples if example.name == "typical")
        push = render_entry(entry, typical.values)
        actual = sweep.disk_floor_push(2 * GIB, 290 * GIB, 300 * GIB, PAGE_PATHS)
        self.assertEqual(push, actual)
        self.assertEqual(typical.values, {"shortfall": "10.0 GiB", "free": "290.0 GiB",
                                        "floor": "300.0 GiB", "removed": "2.0 GiB", "paths": "\n\n".join(PAGE_PATHS)})

    def test_disk_page_never_asks_the_user_to_free_space_by_hand(self) -> None:
        entry = next(entry for entry in catalog() if entry.id == "sweep.disk_floor")
        for example in entry.examples:
            with self.subTest(example=example.name):
                push = render_entry(entry, example.values)
                assert isinstance(push.action, ActionRequired)
                self.assertIn("Claude session named natedev", push.action.text)
                for words in ("terminal", "delete", "personal files", "free more space"):
                    self.assertNotIn(words, push.message)

    def test_disk_page_names_as_many_exact_paths_as_fit_one_push(self) -> None:
        long_paths = [f"/tmp/claude-1000/{'x' * 60}/{index}: frees 1.0 GiB." for index in range(6)]
        push = sweep.disk_floor_push(0, 0, 300 * GIB, long_paths)
        listed = push.values["paths"].split("\n\n")
        self.assertLess(len(push.message), sweep.PUSH_TEXT_LIMIT)
        self.assertTrue(listed)
        self.assertEqual(listed, long_paths[:len(listed)])
        self.assertEqual(sweep.disk_floor_push(0, 0, 300 * GIB, []).values["paths"], sweep.NO_PAGE_PATHS)

    def test_page_lists_busy_targets_then_the_deepest_measured_folders_by_size(self) -> None:
        def folder(path: str, size: int) -> sweep.OutsideCacheDirectory:
            return sweep.OutsideCacheDirectory(path, size * GIB, 0, sweep.NoLargestGrowingChild())
        measurement = sweep.AvailableDiskMeasurement(
            datetime.now(PACIFIC), sweep.NoEarlierDiskMeasurement(),
            [folder("/tmp/claude-1000", 90), folder("/tmp/claude-1000/scratch", 86), folder("/home/n/.cache/uv", 12)], 0)
        holders = [sweep.FloorHolder("/home/n/rust/a/target", 26 * GIB, 41, "runs x", None),
                   sweep.FloorHolder("/tmp/b/target", 3 * GIB, None, "holds the cargo lock y", None)]
        self.assertEqual(sweep.page_paths(holders, measurement), [
            "/home/n/rust/a/target: frees 26.0 GiB. Process 41 uses it now and can fail.",
            "/tmp/b/target: frees 3.0 GiB. A build uses it now and can fail.",
            "/tmp/claude-1000/scratch: frees 86.0 GiB.", "/home/n/.cache/uv: frees 12.0 GiB."])

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
        fake_module = Path(self.enterContext(tempfile.TemporaryDirectory())) / "scripts/lint/sweep.py"
        sender = fake_module.parents[1] / "message/send.py"
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
        with (mock.patch.object(sweep, "__file__", str(fake_module)),
              redirect_stdout(io.StringIO()), redirect_stderr(errors)):
            self.assertTrue(sweep.send_floor_alert("disk notice", sweep.FloorAlertChannels.NATEDEV_AND_PHONE,
                                                   phone=sweep.disk_floor_push(2 * GIB, 9 * GIB, 10 * GIB, PAGE_PATHS)))
        self.assertIn("phone alert failed (3): FAILED: Pushover keys are missing", errors.getvalue())

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

    def test_finds_cargo_targets_and_nothing_inside_them(self) -> None:
        base = self.base()
        _ = cargo_target(base, "repo", APP, time.time())
        _ = cargo_target(base / "repo" / "target", "nested", DEMO, time.time())
        _ = cargo_target(base / ".git", "hidden", TOOL, time.time())
        _ = write(base / "uv" / "CACHEDIR.TAG")
        self.assertEqual(sweep.target_dirs([str(base)]), [str(base / "repo" / "target")])

    def test_cache_trace_targets_are_scratch_floor_targets_and_sccache_is_not_a_target(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        root = home / ".cache" / "trace-targets" / "rich-placement-1ha815wz"
        _ = unit(root / "debug", "deps", "hana", APP, time.time() - DAY)
        _ = (root / sweep.RUSTC_INFO).write_text("{}")
        # sccache's layout: two levels of one-character folders, then entries; no rustc info, no cargo tag.
        _ = write(home / ".cache" / "sccache" / "0" / "1" / "01d2c3b4a5968778")
        _ = write(home / ".cache" / "sccache" / "debug" / ".fingerprint" / "x" / "y")
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(home)}))
        self.assertIn("~/.cache", sweep.FLOOR_ROOTS)
        found = sweep.target_dirs([root for root in sweep.FLOOR_ROOTS if root.startswith("~/.cache")])
        self.assertEqual(found, [str(root)])

    def test_exhausted_cache_tells_the_holding_session_and_pages_only_when_none_was_told(self) -> None:
        for delivered in (True, False):
            with self.subTest(delivered=delivered):
                base = self.base()
                busy_tree, _ = cargo_target(base, "busy", DEMO, time.time())
                lock_path = busy_tree / sweep.LOCK_NAMES[0]
                lock = os.open(lock_path, os.O_RDONLY | os.O_CREAT)
                self.addCleanup(os.close, lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                with (mock.patch.object(sweep, "holder_sessions", return_value={os.getpid(): "hana-unit"}) as found,
                      mock.patch.object(sweep, "send_holder_message", return_value=delivered) as tell,
                      mock.patch.object(sweep, "send_floor_alert", return_value=True) as send):
                    self.assertEqual(self.hold(base, 10 * GIB, [9 * GIB, 9 * GIB])[0], 0)
                found.assert_called_once_with([os.getpid()])
                self.assertEqual(tell.call_args.args[0], "hana-unit")
                told = cast(str, tell.call_args.args[1])
                self.assertIn(f"{busy_tree.parent} (", told)
                self.assertIn(f"pid {os.getpid()} holds the cargo lock {lock_path}. This process is in your session", told)
                self.assertIn("If the process is stale, stop it.", told)
                message = cast(str, send.call_args.args[0])
                if delivered:
                    self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV)
                    self.assertNotIn("phone", send.call_args.kwargs)
                    self.assertIn("; session hana-unit was told", message)
                    self.assertIn("The user gets no page while a session can end a use.", message)
                else:
                    self.assertEqual(send.call_args.args[1], sweep.FloorAlertChannels.NATEDEV_AND_PHONE)
                    self.assertIn("; the message to session hana-unit failed", message)
                    page = cast(RenderedPush, send.call_args.kwargs["phone"])
                    self.assertIn(f"{busy_tree.parent}: ", page.values["paths"])
                record = sweep.read_floor_record(sweep.floor_record_path())
                assert isinstance(record, sweep.FloorRecord)
                self.assertIsInstance(record.push_history, sweep.DeliveredAlert)

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
        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

        self.assertEqual(status, 0)
        self.assertTrue(tag.exists())
        self.assertTrue(registry_file.exists())
        self.assertFalse(removable.exists())

    def test_target_dirs_ignores_another_tools_cache_tag_beside_a_build_tree(self) -> None:
        base = self.base()
        root = base / "other" / "target"
        _ = unit(root / "debug", "deps", "other", APP, time.time())
        _ = (root / sweep.CACHE_TAG).write_text(
            "Signature: 8a477f597d28d172789f06886806bc55\n"
            + "# This file is a cache directory tag created by another tool.\n"
        )

        self.assertNotIn(str(root), sweep.target_dirs([str(base)]))

    def test_floor_keeps_recent_tmp_output_when_an_older_rust_target_meets_the_floor(self) -> None:
        base = self.base()
        now = time.time()
        _, managed = cargo_target(base / "home" / "rust", "managed", APP, now - 3 * 3600)
        _, scratch = cargo_target(base / "scratch", "recent", DEMO, now - 10 * 60)

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

        self.assertEqual(status, 0)
        self.assertTrue(scratch.exists())
        self.assertFalse(managed.exists())
        self.assertNotIn(f"from {base / 'scratch' / 'recent' / 'target'}", output)
        self.assertIn(f"from {base / 'home' / 'rust' / 'managed' / 'target'}", output)

    def test_floor_orders_old_tmp_and_rust_targets_by_last_use(self) -> None:
        base = self.base()
        now = time.time()
        _, older = cargo_target(base / "home" / "rust", "older", APP, now - 5 * 3600)
        _, scratch = cargo_target(base / "scratch", "old", DEMO, now - 2 * 3600)

        status, _ = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

        self.assertEqual(status, 0)
        self.assertFalse(older.exists())
        self.assertTrue(scratch.exists())

    def test_floor_takes_recent_output_when_older_output_cannot_meet_the_floor(self) -> None:
        base = self.base()
        now = time.time()
        older_tree, older = cargo_target(base, "older", APP, now - 2 * 3600)
        _, recent = cargo_target(base / "scratch", "recent", DEMO, now - 10 * 60)
        shortfall = sweep.directory_blocks(str(older_tree.parent)) + 1

        status, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB])

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

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

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
            status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

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

        status, output = self.hold(base, 10 * GIB, [10 * GIB - 1, 10 * GIB])

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
        status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
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
        _, output = self.hold(base, 10 * GIB, [10 * GIB - 2 * SIZE, 10 * GIB])
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
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
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
        shortfall = sweep.freed_by(old_group, scan, dict(scan.links)) + 1
        _, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB])
        self.assertFalse(old.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(output.count("the floor took"), 2)

    def test_dry_floor_lines_name_target_without_extra_removal_lines(self) -> None:
        base = self.base()
        now = time.time() - 3600
        _, output_file = cargo_target(base, "repo", APP, now)
        _, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB], dry_run=True)
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
        orphaned = sum(sweep.freed_by(group, scan, remaining) for group in scan.orphans)
        self.assertGreater(orphaned, 0)

        status, output = self.hold(base, 10 * GIB, [10 * GIB - orphaned, 10 * GIB])

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
        now = time.time()
        failed_tree, failed_output = cargo_target(base, "failed", APP, now - 2 * DAY)
        _, successful_output = cargo_target(base, "successful", DEMO, now - DAY)
        scan = sweep.scan_roots([str(base / name / "target") for name in ("failed", "successful")],
                                [str(base / name / "target" / "debug") for name in ("failed", "successful")])
        shortfall = sum(sweep.freed_by(group, scan, dict(scan.links)) for group in scan.groups)
        original_rmtree = shutil.rmtree

        def fail_one_group(path: str) -> None:
            if path == str(failed_tree / ".fingerprint" / f"failed-{APP}"):
                raise OSError("test removal failure")
            original_rmtree(path)

        errors = io.StringIO()
        with mock.patch.object(shutil, "rmtree", side_effect=fail_one_group), redirect_stderr(errors):
            status, output = self.hold(base, 10 * GIB, [10 * GIB - shortfall, 10 * GIB])

        self.assertEqual(status, 1)
        self.assertIn("could not remove", errors.getvalue())
        self.assertTrue(failed_output.exists() or (failed_tree / ".fingerprint" / f"failed-{APP}").exists())
        self.assertFalse(successful_output.exists())
        self.assertIn("removed 2 build units", output)
        lines = [line for line in output.splitlines() if "the floor took" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"from {base / 'successful' / 'target'},", lines[0])

    def test_workspace_stamps_each_root_even_when_a_build_holds_it(self) -> None:
        base = self.base()
        first_tree, _ = cargo_target(base, "first", APP, time.time() - DAY)
        _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
        roots = [str(base / name / "target") for name in ("first", "second")]
        with mock.patch.object(sweep, "cargo_roots", return_value=roots), \
             mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(None, "/nonexistent/lint.conf", False), 0)
            for root in roots:
                self.assertTrue((Path(root) / sweep.USE_STAMP).exists())
            for root in roots:
                os.utime(Path(root) / sweep.USE_STAMP, (1, 1))
            lock = os.open(first_tree / sweep.LOCK_NAMES[0], os.O_RDONLY | os.O_CREAT)
            self.addCleanup(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(sweep.sweep_workspace(None, "/nonexistent/lint.conf", False), 0)
        for root in roots:
            self.assertGreater((Path(root) / sweep.USE_STAMP).stat().st_mtime, 1)

    def test_dry_workspace_sweep_leaves_every_root_unstamped(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "first", APP, time.time() - DAY)
        _, _ = cargo_target(base, "second", DEMO, time.time() - DAY)
        roots = [str(base / name / "target") for name in ("first", "second")]
        with mock.patch.object(sweep, "cargo_roots", return_value=roots), \
             mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(None, "/nonexistent/lint.conf", True), 0)
        for root in roots:
            self.assertFalse((Path(root) / sweep.USE_STAMP).exists())

    def test_named_target_writes_no_use_stamp(self) -> None:
        base = self.base()
        _, _ = cargo_target(base, "repo", APP, time.time() - DAY)
        root = base / "repo" / "target"
        with mock.patch.dict(os.environ, {sweep.BUDGET_ENV: "1024"}), redirect_stdout(io.StringIO()):
            self.assertEqual(sweep.sweep_workspace(str(root), "/nonexistent/lint.conf", True), 0)
        self.assertFalse((root / sweep.USE_STAMP).exists())

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
        self.assertIn("1 left alone while a build or test run uses them", output)

    def write_build_hold(self, base: Path, since: float) -> None:
        directory = base / "build-hold"
        directory.mkdir()
        _ = (directory / "holder-session").write_text(json.dumps({
            "holder": "build-speed", "since": datetime.fromtimestamp(since, PACIFIC).isoformat(),
            "for": "timed builds", "release_eta": "unknown",
        }))

    def test_own_deletes_do_not_hold_a_target_but_a_later_build_write_does(self) -> None:
        base = self.base()
        now = time.time()
        tree, removed = cargo_target(base, "external", APP, now - DAY)
        remaining = unit(tree, "deps", "remaining", DEMO, now - DAY)
        for path in tree.parent.rglob("*"):
            os.utime(path, (now - DAY, now - DAY))
        self.write_build_hold(base, now - 60)
        with mock.patch.object(time, "time", return_value=now):
            self.assertEqual(sweep.remove([sweep.Group("unit", str(tree), [str(removed)])]), 0)
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

    def test_delete_records_drop_missing_roots_and_roots_older_than_a_day(self) -> None:
        base = self.base()
        now = time.time()
        tree, removed = cargo_target(base, "current", APP, now - DAY)
        old_tree, _ = cargo_target(base, "old", DEMO, now - 2 * DAY)
        path = sweep.target_deletes_path()
        path.parent.mkdir()
        _ = path.write_text(json.dumps({str(old_tree.parent): now - DAY - 1,
                                        str(base / "missing"): now - 60}))
        with mock.patch.object(time, "time", return_value=now):
            self.assertEqual(sweep.remove([sweep.Group("unit", str(tree), [str(removed)])]), 0)
        self.assertEqual(json.loads(path.read_text()), {str(tree.parent): now})
        self.assertFalse(removed.exists())

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
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
        self.assertEqual(status, 0)
        self.assertTrue(held.exists())
        self.assertFalse(other.exists())
        self.assertEqual({path.relative_to(tree.parent): path.read_bytes()
                          for path in tree.parent.rglob("*") if path.is_file()}, before)
        locks.assert_called_once_with([str(other_tree)])
        scan.assert_called_once_with([str(other_tree.parent)], [str(other_tree)])
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
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
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
            status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
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
                    status, output = self.hold(base, 10 * GIB, [10 * GIB - SIZE, 10 * GIB])
                self.assertEqual(status, 0)
                self.assertTrue(held.exists())
                locks.assert_not_called()
                self.assertIn("build-speed holds natedev builds; its working folder was not found", output)
                self.assertIn(f"{tree.parent} left alone", output)

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
        for setting, minutes in (("", 60.0), ("recent_minutes=180\n", 180.0)):
            with self.subTest(minutes=minutes):
                _ = config.write_text("[sweep]\nsweep_free_floor_gib.testhost=7\n" + setting)
                with (
                    mock.patch.dict(os.environ, {sweep.CONFIG_ENV: str(config)}),
                    mock.patch.object(socket, "gethostname", return_value="testhost"),
                    mock.patch.object(sweep, "sweep_workspace") as workspace,
                    mock.patch.object(sweep.tmp_sweep, "run_if_due", return_value=0) as scratch,
                    mock.patch.object(sweep, "hold_floor", return_value=0) as floor,
                ):
                    self.assertEqual(sweep.main([sweep.FLOOR_ONLY_FLAG]), 0)
                workspace.assert_not_called()
                scratch.assert_called_once_with(False)
                floor.assert_called_once_with(7 * GIB, False, recent_minutes=minutes)

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

        with mock.patch.object(sweep, "open", create=True, side_effect=skip_once):
            self.assertEqual(sweep.lock_holder(str(lock), "locks"), 4242)
        with mock.patch.object(sweep, "open", create=True, side_effect=never_held) as opened:
            self.assertIsNone(sweep.lock_holder(str(lock), "locks"))
        self.assertEqual(opened.call_count, sweep.PROC_LOCKS_READS)

    @unittest.skipUnless(os.path.isfile(sweep.PROC_LOCKS), "the lock holder is read from /proc/locks")
    def test_a_live_flock_names_this_process(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        fd = os.open(base / ".cargo-lock", os.O_RDONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(sweep.lock_holder(str(base / ".cargo-lock")), os.getpid())


class HolderSessionTests(unittest.TestCase):
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
    def config(self) -> str:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "lint.conf"
        _ = path.write_text(CONFIG)
        return str(path)

    def test_sweep_imports_in_a_fresh_interpreter(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {**os.environ, "BUILD_HOLD_DIR": str(base / "holders"),
                       "BUILD_HOLD_RELEASE_DIR": str(base / "release"),
                       "BUILDLOG_DIR": str(base / "buildlog"),
                       "NOTIFIER_SESSIONS_DIR": str(base / "sessions"),
                       "PYTHONPATH": str(Path(__file__).resolve().parent)}
        result = subprocess.run([sys.executable, "-c", "import sweep"], cwd=base, env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)

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
