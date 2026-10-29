#!/usr/bin/env python3
"""Tests for tmp_sweep.py on fabricated /tmp roots and a fabricated /proc. Times are two days ahead
of the clock, so every file a test writes is old, ctime included, unless the test moves it."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

import tmp_sweep
if TYPE_CHECKING:
    from ..lib import scratch
    from ..message import sessions
else:
    import scratch  # on the path once tmp_sweep has put scripts/lib there
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
    import sessions

DAY = 86_400
LIVE_SESSION = "11111111-2222-3333-4444-555555555555"
ENDED_SESSION = "66666666-7777-8888-9999-000000000000"
PROJECT = "-home-natepiano-rust-hana"


def write(path: Path, text: str = "x" * 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text)
    return path


def fake_process(proc: Path, pid: int, *, cwd: str = "/", fds: tuple[str, ...] = (), maps: str = "") -> None:
    """A /proc/<pid> entry as the scan reads it."""
    entry = proc / str(pid)
    (entry / "fd").mkdir(parents=True)
    (entry / "cwd").symlink_to(cwd)
    for number, target in enumerate(fds):
        (entry / "fd" / str(number)).symlink_to(target)
    _ = (entry / "maps").write_text(maps)


BOOT = 1000


def fake_start(proc: Path, pid: int, start: float, *arguments: str) -> None:
    """A /proc/<pid> entry that started at start with arguments, as verify_running reads it."""
    _ = write(proc / "stat", f"cpu 0\nbtime {BOOT}\n")
    ticks = round((start - BOOT) * os.sysconf("SC_CLK_TCK"))
    _ = write(proc / str(pid) / "stat", f"{pid} (a b) S " + "0 " * 18 + f"{ticks} 0 0\n")
    _ = write(proc / str(pid) / "cmdline", "\0".join(arguments) + "\0")


class TmpSweepCase(unittest.TestCase):
    tmp: Path = Path()
    unit: Path = Path()
    delegate: Path = Path()
    session_root: Path = Path()
    project: Path = Path()
    proc: Path = Path()
    state: Path = Path()
    now: float = 0.0
    live: frozenset[str] | None = None

    @override
    def setUp(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.tmp = base / "claude"
        self.unit = self.tmp / "unit"
        self.delegate = self.tmp / "delegate"
        self.session_root = base / "claude-1000"
        self.project = self.session_root / PROJECT
        self.proc = base / "proc"
        self.state = base / "state"
        for folder in (self.unit, self.delegate, self.project, self.proc):
            folder.mkdir(parents=True)
        registry = base / "sessions"
        registry.mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "NOTIFIER_SESSIONS_DIR": str(registry), "SHUTDOWN_STATE_DIR": str(base / "shutdown"),
            "BUILDLOG_DIR": str(base / "buildlog"),
        })))
        self.now = time.time() + 2 * DAY
        _ = self.enterContext(mock.patch.object(tmp_sweep, "state_dir", return_value=self.state))
        # A private default root exposes callers that fail to set their session roots.
        default_session = write(base / "default-session" / ENDED_SESSION / "file").parents[1]
        _ = self.enterContext(mock.patch.object(tmp_sweep, "SESSION_TEMP_ROOT", str(default_session)))
        self.live = frozenset({LIVE_SESSION})
        _ = self.enterContext(mock.patch.object(tmp_sweep, "running_session_ids", side_effect=lambda: self.live))

    def roots(self) -> tmp_sweep.Roots:
        return tmp_sweep.default_roots(str(self.tmp), (str(self.delegate), str(self.unit)), (str(self.session_root),), (),
                                       (str(self.tmp / "scratch"),), (), (str(self.tmp / "session"),))

    def unreadable_sessions(self) -> None:
        self.live = None

    def recent(self, path: Path) -> None:
        os.utime(path, (self.now - 3600, self.now - 3600), follow_symlinks=False)

    def sweep(self, dry_run: bool = False) -> tmp_sweep.Sweep:
        return tmp_sweep.sweep(dry_run, self.roots(), self.now, str(self.proc))

    def run_once(self, dry_run: bool = False) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = tmp_sweep.run(dry_run, self.now, self.roots(), str(self.proc))
        return status, output.getvalue()

    def log(self) -> list[dict[str, object]]:
        lines = (self.state / tmp_sweep.LOG_NAME).read_text().splitlines()
        return [cast(dict[str, object], json.loads(line)) for line in lines]


class IdleTests(TmpSweepCase):
    def test_an_idle_entry_goes_and_a_recent_one_stays(self) -> None:
        idle = write(self.tmp / "shots" / "1.png")
        loose = write(self.tmp / "notes.txt")
        busy = write(self.tmp / "trace" / "deep" / "run.json")
        self.recent(busy)
        result = self.sweep()
        self.assertFalse(idle.parent.exists())
        self.assertFalse(loose.exists())
        self.assertTrue(busy.is_file())
        self.assertEqual(result.kept[tmp_sweep.RECENT], [str(self.tmp / "trace")])
        self.assertEqual(sorted(entry["path"] for entry in result.deleted),
                         [str(self.tmp / "notes.txt"), str(self.tmp / "shots")])

    def test_a_folders_own_time_does_not_count_unless_it_is_empty(self) -> None:
        touched = write(self.tmp / "touched" / "old.txt").parent
        self.recent(touched)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.recent(empty)
        _ = self.sweep()
        self.assertFalse(touched.exists())
        self.assertTrue(empty.is_dir())

    def test_a_link_out_of_an_entry_is_not_followed(self) -> None:
        outside = write(self.tmp.parent / "outside" / "kept")
        (write(self.tmp / "links" / "file").parent / "out").symlink_to(outside.parent)
        (self.tmp / "loose-link").symlink_to(outside)
        _ = self.sweep()
        self.assertFalse((self.tmp / "links").exists())
        self.assertFalse((self.tmp / "loose-link").is_symlink())
        self.assertTrue(outside.is_file())

    def test_a_read_only_folder_is_deleted(self) -> None:
        locked = write(self.tmp / "store" / "nix" / "file").parent
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.chmod(0o700) if locked.exists() else None)
        result = self.sweep()
        self.assertEqual(result.failed, [])
        self.assertFalse((self.tmp / "store").exists())

    def test_an_entry_that_cannot_be_read_stays(self) -> None:
        hidden = write(self.tmp / "hidden" / "inner" / "file").parent
        hidden.chmod(0o000)
        self.addCleanup(hidden.chmod, 0o700)
        result = self.sweep()
        self.assertTrue((self.tmp / "hidden").is_dir())
        self.assertEqual(result.kept[tmp_sweep.UNREADABLE], [str(self.tmp / "hidden")])

    def test_a_dry_run_deletes_nothing_and_logs_nothing(self) -> None:
        idle = write(self.tmp / "shots" / "1.png")
        status, output = self.run_once(dry_run=True)
        self.assertEqual(status, 0)
        self.assertTrue(idle.is_file())
        self.assertIn(f"tmp sweep: would delete {self.tmp / 'shots'} (0.0 GiB), last changed ", output)
        self.assertFalse((self.state / tmp_sweep.LOG_NAME).exists())


class ProcessUseTests(TmpSweepCase):
    def test_an_entry_a_process_works_in_holds_open_or_maps_stays(self) -> None:
        worked = write(self.tmp / "worked" / "a").parent
        held = write(self.tmp / "held" / "deep" / "log")
        mapped = write(self.tmp / "mapped" / "lib.so")
        loose = write(self.tmp / "loose.txt")
        idle = write(self.tmp / "idle" / "a").parent
        fake_process(self.proc, 10, cwd=str(worked / "sub"))
        fake_process(self.proc, 11, fds=("/dev/null", f"{loose} (deleted)", str(held)))
        fake_process(self.proc, 12, maps=f"7f00-7f01 r-xp 00000000 00:1f 42   {mapped}\n")
        result = self.sweep()
        for path in (worked, held, mapped, loose):
            self.assertTrue(path.exists(), path)
        self.assertFalse(idle.exists())
        self.assertEqual(sorted(result.kept[tmp_sweep.IN_USE]), [
            f"{self.tmp / 'held'}: pid 11 holds a file open in it",
            f"{loose}: pid 11 holds a file open in it",
            f"{self.tmp / 'mapped'}: pid 12 maps a file in it",
            f"{worked}: pid 10 works in it",
        ])

    def test_with_no_proc_and_no_lsof_nothing_goes(self) -> None:
        idle = write(self.tmp / "idle" / "a")
        self.proc.rmdir()
        with mock.patch.object(tmp_sweep, "LSOF", (str(self.tmp.parent / "no-lsof"),)):
            result = self.sweep()
        self.assertTrue(idle.is_file())
        self.assertEqual(result.deleted, [])
        self.assertIn(str(self.tmp / "idle"), result.kept[tmp_sweep.NO_PROC])


    def test_with_no_proc_an_lsof_scan_shows_which_processes_use_an_entry(self) -> None:
        idle = write(self.tmp / "idle" / "a")
        used = write(self.tmp / "used" / "a")
        self.proc.rmdir()
        lines = f"p42\\nfcwd\\nn{used.parent}\\nf3\\nn/elsewhere\\n"
        with mock.patch.object(tmp_sweep, "LSOF", ("printf", lines)):
            result = self.sweep()
        self.assertFalse(idle.exists())
        self.assertTrue(used.is_file())
        self.assertEqual(result.kept[tmp_sweep.IN_USE], [f"{used.parent}: pid 42 works in it"])

    def test_lsof_output_names_how_each_process_uses_a_path(self) -> None:
        text = "p7\nfcwd\nn/tmp/a\nftxt\nn/tmp/b/lib\nf4\nn/tmp/c/log\nf5\nn/var/x\np8\nfmem\nn/tmp/d\n"
        self.assertEqual(tmp_sweep.parse_lsof(text, "/tmp/"), [
            ("/tmp/a", 7, "works in it"), ("/tmp/b/lib", 7, "maps a file in it"),
            ("/tmp/c/log", 7, "holds a file open in it"), ("/tmp/d", 8, "maps a file in it")])


class ScratchRootTests(TmpSweepCase):
    def test_a_task_folder_stays_while_its_task_is_open_and_goes_when_its_time_passes(self) -> None:
        scratch_root = self.tmp / "scratch"
        run = write(scratch_root / "run-x-gate-abc123" / "a").parent
        row = write(scratch_root / "row-natedev-C5-copy-abc123" / "a").parent
        due = write(scratch_root / "until-due-abc123" / "a").parent
        later = write(scratch_root / "until-later-abc123" / "a").parent
        loose = write(scratch_root / "loose-abc123" / "a").parent
        tasks = ((run, "run:x"), (row, "row:natedev-C5"), (due, scratch.until_task(self.now - 60)),
                 (later, scratch.until_task(self.now + 3 * DAY)))
        for folder, task in tasks:
            scratch.write_record(folder, task, 0.0)
        self.recent(due / "a")
        result = self.sweep()
        self.assertFalse(due.exists())
        self.assertFalse(loose.exists())
        for folder in (run, row, later):
            self.assertTrue(folder.is_dir(), folder)
        self.assertEqual(sorted(result.kept[tmp_sweep.TASK_OPEN]), [str(run), str(later)])
        self.assertEqual(result.kept[tmp_sweep.ROW_WAITS], [str(row)])
        self.assertIn(str(scratch_root), result.kept[tmp_sweep.ROOT])
        _ = tmp_sweep.sweep(False, self.roots(), time.time() + 8 * DAY, str(self.proc))
        self.assertFalse(run.exists())
        self.assertTrue(row.is_dir())


class TempParentTests(TmpSweepCase):
    def test_entries_in_a_live_sessions_temp_root_and_a_run_temp_go_after_4_h(self) -> None:
        live_root = self.tmp / "session" / LIVE_SESSION
        idle = write(live_root / ".tmpAb12Cd" / "a").parent
        fresh = write(live_root / "pymp-x" / "a").parent
        self.recent(fresh / "a")
        gone = write(self.tmp / "session" / "ended-session" / "a").parent
        run_idle = write(self.unit / "run-1" / "tmp" / "tmp.abcdefgh" / "a").parent
        result = self.sweep()
        self.assertFalse(idle.exists())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(live_root.is_dir())
        self.assertFalse(gone.exists())
        self.assertFalse(run_idle.exists())
        self.assertIn(str(live_root), result.kept[tmp_sweep.SESSION_RUNS])

    def test_a_folders_ctime_is_no_change_in_a_temp_folder(self) -> None:
        top = self.tmp / "old"
        deeper = top / "inner" / "deeper"
        deeper.mkdir(parents=True)
        for folder in (deeper, deeper.parent):
            os.utime(folder, (1000, 1000))
        since = time.time() - 60
        self.assertEqual(tmp_sweep.last_change(str(top), tmp_sweep.Limit(since, tmp_sweep.RECENT, folder_ctime=False)),
                         (1000, 0))
        self.assertEqual(tmp_sweep.last_change(str(top), tmp_sweep.Limit(since, tmp_sweep.RECENT)), tmp_sweep.RECENT)
        # An empty folder falls back to its own times, and its own ctime is no change either.
        os.utime(deeper, (1000, 1000))
        self.assertEqual(tmp_sweep.last_change(str(deeper), tmp_sweep.Limit(since, tmp_sweep.RECENT, folder_ctime=False)),
                         (1000, 0))
        self.assertEqual(tmp_sweep.last_change(str(deeper), tmp_sweep.Limit(since, tmp_sweep.RECENT)), tmp_sweep.RECENT)


class TempFolderTests(TmpSweepCase):
    def test_only_an_idle_unused_tempfile_folder_in_a_temp_folder_goes(self) -> None:
        temp = self.tmp.parent / "temp"
        idle = write(temp / ".tmpAb12Cd" / "Cargo.toml").parent
        idle_mktemp = write(temp / "tmp.johjtSnxbj" / "a").parent
        idle_python = write(temp / "tmp8m5h288i" / "a").parent
        recent = write(temp / ".tmpQq11Rr" / "a").parent
        self.recent(recent / "a")
        used = write(temp / ".tmpUu22Vv" / "a").parent
        fake_process(self.proc, 10, cwd=str(used))
        others = [write(temp / ".tmpAb12Cde" / "a").parent, write(temp / ".tmpXy34Zw"),
                  write(temp / "notes" / "a").parent, write(temp / ".tmp-b12Cd" / "a").parent,
                  write(temp / "tmp.short" / "a").parent, write(temp / "tmpUPPERCASE" / "a").parent]
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(temp),), (),
                                       session_temp_roots=())
        result = tmp_sweep.sweep(False, roots, self.now, str(self.proc))
        for gone in (idle, idle_mktemp, idle_python):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(recent.is_dir())
        self.assertTrue(used.is_dir())
        for other in others:
            self.assertTrue(other.exists(), other)
        self.assertEqual(sorted(entry["path"] for entry in result.deleted),
                         sorted(str(gone) for gone in (idle, idle_mktemp, idle_python)))
        self.assertEqual(result.kept[tmp_sweep.IN_USE], [f"{used}: pid 10 works in it"])
        kept = [path for paths in result.kept.values() for path in paths]
        self.assertFalse([path for path in kept if any(str(other) in path for other in others)])

    def test_only_empty_folders_and_invoke_logs_in_the_wide_root_go(self) -> None:
        wide = self.tmp.parent / "wide"
        empty = wide / "mend-config-root-test-179035"
        nix_empty = wide / "nix-shell.aN6y2p"
        nested = wide / "p86_r10_crops" / "a" / "b"
        nix_nested = wide / "nix-shell.0tYoAS" / "hana-runtime-1466697-Gv2eJJ" / "artifacts"
        for folder in (empty, nix_empty, nested, nix_nested):
            folder.mkdir(parents=True)
        deep_file = write(wide / "crops-with-file" / "a" / "b" / "f").parents[2]
        full = write(wide / "loose-clone" / "a").parent
        nix_full = write(wide / "nix-build-x" / "a").parent
        tempfile_folder = write(wide / "tmp.johjtSnxbj" / "a").parent
        logs = [write(wide / "lint_invoke.1121749.27492.log"), write(wide / "buildlog.35612.67614768.log"),
                write(wide / "buildlog.35612.676.peak.cgroup")]
        other_file = write(wide / "notes.log")
        used = wide / "tui_pane_theme_loader_badparse_0_1"
        used.mkdir()
        fake_process(self.proc, 11, cwd=str(used))
        kept = [write(wide / "tmux-1000" / "default").parent, wide / ".X11-unix", wide / "ssh-empty"]
        for folder in kept[1:]:
            folder.mkdir()
        for path in (used, *kept[1:]):
            os.utime(path, (1000, 1000))
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(wide),), (), (str(wide),),
                                       session_temp_roots=())

        def wide_deleted(result: tmp_sweep.Sweep) -> list[str]:
            return sorted(entry["path"] for entry in result.deleted if Path(entry["path"]).parent == wide)

        early = tmp_sweep.sweep(False, roots, time.time() + 5 * 3600, str(self.proc))
        self.assertEqual(wide_deleted(early), sorted([str(tempfile_folder), *map(str, logs)]))
        empties = sorted(str(path) for path in (empty, nix_empty, nested.parents[1], nix_nested.parents[1]))
        self.assertEqual(sorted(early.kept[tmp_sweep.RECENT_WIDE]), empties)
        late = tmp_sweep.sweep(False, roots, time.time() + 8 * DAY, str(self.proc))
        self.assertEqual(wide_deleted(late), empties)
        self.assertEqual(late.kept[tmp_sweep.IN_USE], [f"{used}: pid 11 works in it"])
        for path in [full, nix_full, deep_file, other_file, used, *kept]:
            self.assertTrue(path.exists(), path)

    def test_a_verify_folder_stays_only_while_the_run_that_made_it_runs(self) -> None:
        wide = self.tmp.parent / "wide"
        made = time.time() - 100
        live = write(wide / "verify-hold.20.Ab12Cd" / "seconds").parent
        copy = write(live / "verify-copy.20.Ef34Gh" / "step").parent
        reused = write(wide / "verify-hold.21.Ab12Cd" / "seconds").parent
        other = write(wide / "verify-copy.22.Ab12Cd" / "step").parent
        gone = write(wide / "verify-hold.23.Ab12Cd" / "seconds").parent
        old_name = write(wide / "verify-hold.0a7oDb" / "seconds").parent
        for folder in (live, copy, reused, other, gone, old_name):
            os.utime(folder, (made, made))
        fake_start(self.proc, 20, made - 50, "bash", "/home/u/.claude/scripts/delegate/verify.sh", "test")
        fake_start(self.proc, 21, made + 50, "bash", "/home/u/.claude/scripts/delegate/verify.sh", "test")
        fake_start(self.proc, 22, made - 50, "python3", "copy_watch.py")
        roots = tmp_sweep.default_roots(str(self.tmp.parent / "plain"), (), (), (str(wide),), (), (str(wide),),
                                       session_temp_roots=())
        soon = tmp_sweep.sweep(False, roots, time.time() + 60, str(self.proc))
        self.assertEqual(sorted(entry["path"] for entry in soon.deleted), sorted(map(str, (reused, other, gone))))
        self.assertEqual(soon.kept[tmp_sweep.VERIFY_RUNS], [str(live)])
        self.assertEqual(soon.kept[tmp_sweep.RECENT], [str(old_name)])
        later = tmp_sweep.sweep(False, roots, time.time() + 8 * DAY, str(self.proc))
        self.assertEqual([entry["path"] for entry in later.deleted], [str(old_name)])
        self.assertTrue(copy.is_dir())

    def test_ps_elapsed_time_reads_in_seconds(self) -> None:
        self.assertEqual(tmp_sweep.elapsed_seconds("05:07"), 307)
        self.assertEqual(tmp_sweep.elapsed_seconds("02:05:07"), 7507)
        self.assertEqual(tmp_sweep.elapsed_seconds("3-02:05:07"), 3 * DAY + 7507)
        self.assertIsNone(tmp_sweep.elapsed_seconds(""))
        self.assertIsNone(tmp_sweep.elapsed_seconds("x-05:07"))

    def test_the_wide_root_is_tmp_alone_and_only_when_it_is_a_temp_folder(self) -> None:
        temp = self.tmp.parent / "temp"
        temp.mkdir()
        self.assertEqual(tmp_sweep.default_roots(str(self.tmp), (), (), (str(temp),), (), session_temp_roots=()).wide, ())
        self.assertEqual(tmp_sweep.default_roots(str(self.tmp), (), (), ("/tmp",), (), session_temp_roots=()).wide,
                         (os.path.realpath("/tmp"),))

    def test_roots_are_real_paths(self) -> None:
        """The Mac's /tmp is a link to /private/tmp, and lsof prints real paths."""
        link = self.tmp.parent / "link"
        link.symlink_to(self.tmp.parent)
        roots = tmp_sweep.default_roots(str(link / "claude"), (str(link / "claude" / "unit"),), (), (str(link),), (),
                                       session_temp_roots=())
        self.assertEqual(roots.plain, (str(self.tmp),))
        self.assertEqual(roots.runs, (str(self.unit),))
        self.assertEqual(roots.temp, (str(self.tmp.parent),))


class OwnerTests(TmpSweepCase):
    def markers(self) -> list[Path]:
        return [write(self.unit / "active" / LIVE_SESSION, f"{self.unit / 'run'}\n"),
                write(self.delegate / "active" / ENDED_SESSION, f"{self.delegate / 'old'}\n")]

    def test_the_run_marker_folders_and_their_markers_never_go(self) -> None:
        markers = self.markers()
        for marker in markers:
            os.utime(marker, (self.now - 3 * DAY, self.now - 3 * DAY))
        result = self.sweep()
        for marker in markers:
            self.assertTrue(marker.is_file(), marker)
        self.assertEqual(sorted(result.kept[tmp_sweep.ROOT]), sorted(
            [str(self.delegate), str(self.unit), str(self.delegate / "active"), str(self.unit / "active")]))

    def test_a_run_folder_stays_while_its_director_runs(self) -> None:
        live = write(self.unit / "live" / "board.log").parent
        _ = write(live / "director", f"{LIVE_SESSION}\n")
        ended = write(self.delegate / "ended" / "board.log").parent
        _ = write(ended / "director", f"{ENDED_SESSION}\n")
        unowned = write(self.unit / "unowned" / "board.log").parent
        result = self.sweep()
        self.assertTrue(live.is_dir())
        self.assertFalse(ended.exists())
        self.assertFalse(unowned.exists())
        self.assertEqual(result.kept[tmp_sweep.DIRECTOR_RUNS], [str(live)])

    def test_a_run_folder_a_live_sessions_marker_names_stays_with_no_director_file(self) -> None:
        """A run no worker has dispatched in has no `director` file; its marker names it."""
        marked = write(self.unit / "marked" / "progress_history_state.json", "{}").parent
        _ = write(self.unit / "active" / LIVE_SESSION, f"{marked}\n")
        ended = write(self.delegate / "ended" / "board.log").parent
        _ = write(self.delegate / "active" / ENDED_SESSION, f"{ended}\n")
        result = self.sweep()
        self.assertTrue(marked.is_dir())
        self.assertTrue(ended.is_dir())
        self.assertEqual(sorted(result.kept[tmp_sweep.RunKeepReason.ACTIVE_MARKER.value]),
                         sorted([str(marked), str(ended)]))

    def test_a_run_folder_whose_history_opens_with_a_live_session_stays(self) -> None:
        history = self.tmp.parent / "history"
        runs = {LIVE_SESSION: self.unit / "started", ENDED_SESSION: self.unit / "started-ended"}
        for session, run in runs.items():
            log = write(history / f"{run.name}.jsonl", json.dumps(
                {"event_type": "run_started", "main_agent": {"session_id": session}}) + "\n")
            _ = write(run / "progress_history_state.json", json.dumps({"history_file": str(log)}))
        result = self.sweep()
        self.assertTrue(runs[LIVE_SESSION].is_dir())
        self.assertFalse(runs[ENDED_SESSION].exists())
        self.assertEqual(result.kept[tmp_sweep.DIRECTOR_RUNS], [str(runs[LIVE_SESSION])])

    def test_a_running_sessions_folder_stays_and_only_its_large_idle_entries_go(self) -> None:
        """The size line is three 4 KiB files: an entry at it goes, one a file under it stays."""
        live = self.project / LIVE_SESSION
        large = live / "scratchpad" / "merge"
        for name in ("a", "b", "c"):
            _ = write(large / name)
        small = live / "scratchpad" / "template"
        for name in ("a", "b"):
            _ = write(small / name)
        fresh = live / "scratchpad" / "fresh"
        for name in ("a", "b", "c"):
            self.recent(write(fresh / name))
        output = write(live / "tasks" / "b1.output")
        ended = write(self.project / ENDED_SESSION / "scratchpad" / "a").parents[1]
        other = write(self.project / "rust" / "kept")
        with mock.patch.object(tmp_sweep, "SESSION_ENTRY_MIN_BYTES", 3 * 4096):
            result = self.sweep()
        self.assertTrue((live / "scratchpad").is_dir())
        self.assertFalse(large.exists())
        self.assertTrue(small.is_dir())
        self.assertTrue(fresh.is_dir())
        self.assertTrue(output.is_file())
        self.assertFalse(ended.exists())
        self.assertFalse(other.exists())
        self.assertEqual(result.kept[tmp_sweep.SESSION_RUNS], [str(live)])
        self.assertEqual(sorted(result.kept[tmp_sweep.SMALL_IN_LIVE_SESSION]), [str(small), str(output)])
        self.assertIn(str(large), [entry["path"] for entry in result.deleted])

    def test_an_unreadable_session_record_keeps_session_and_run_folders(self) -> None:
        self.unreadable_sessions()
        session = write(self.project / ENDED_SESSION / "scratchpad" / "a")
        run = write(self.unit / "run" / "director", f"{ENDED_SESSION}\n")
        marked = write(self.unit / "marked" / "board.log")
        _ = write(self.unit / "active" / ENDED_SESSION, f"{marked.parent}\n")
        plain = write(self.tmp / "plain" / "a")
        result = self.sweep()
        self.assertTrue(session.is_file())
        self.assertTrue(run.is_file())
        self.assertTrue(marked.is_file())
        self.assertFalse(plain.exists())
        self.assertEqual(sorted(result.kept[tmp_sweep.NO_SESSIONS]),
                         [str(self.project / ENDED_SESSION), str(self.unit / "marked"), str(self.unit / "run")])

    def test_a_project_folder_left_empty_goes(self) -> None:
        _ = write(self.project / ENDED_SESSION / "scratchpad" / "a")
        _ = self.sweep()
        self.assertFalse(self.project.exists())
        self.assertTrue(self.session_root.is_dir())

    def test_only_dash_folders_are_projects(self) -> None:
        agent = write(self.session_root / "bash-edit-diff" / "a")
        self.assertEqual(self.roots().projects, (str(self.project),))
        _ = self.sweep()
        self.assertTrue(agent.is_file())


class LogTests(TmpSweepCase):
    def test_each_run_logs_what_went_and_why_the_rest_stayed(self) -> None:
        _ = write(self.tmp / "shots" / "1.png")
        self.recent(write(self.tmp / "fresh.txt"))
        fake_process(self.proc, 10, cwd=str(write(self.tmp / "busy" / "a").parent))
        old: dict[str, object] = {"at": "2026-01-01T00:00:00-04:00", "deleted": []}
        _ = write(self.state / tmp_sweep.LOG_NAME, json.dumps(old) + "\n")
        status, output = self.run_once()
        self.assertEqual(status, 0)
        self.assertRegex(output, r"tmp sweep: deleted 1 entries \(0\.0 GiB\), kept 4: ")
        [entry] = self.log()
        deleted = cast(list[dict[str, object]], entry["deleted"])
        self.assertEqual([item["path"] for item in deleted], [str(self.tmp / "shots")])
        self.assertEqual(entry["kept"], {tmp_sweep.ROOT: 2, tmp_sweep.IN_USE: 1, tmp_sweep.RECENT: 1})
        kept_paths = cast(dict[str, list[str]], entry["kept_paths"])
        self.assertNotIn(tmp_sweep.RECENT, kept_paths)
        self.assertEqual(kept_paths[tmp_sweep.IN_USE], [f"{self.tmp / 'busy'}: pid 10 works in it"])
        self.assertIsInstance(entry["freed_gib"], float)

    def test_a_run_waits_an_hour_after_the_last(self) -> None:
        idle = write(self.tmp / "shots" / "1.png")
        with (mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              redirect_stdout(io.StringIO())):
            self.assertEqual(tmp_sweep.run_if_due(False, self.now), 0)
            self.assertFalse(idle.exists())
            idle = write(self.tmp / "shots" / "2.png")
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + 3500), 0)
            self.assertTrue(idle.is_file())
            self.assertEqual(tmp_sweep.run_if_due(False, self.now + 3600), 0)
            self.assertFalse(idle.exists())
        self.assertEqual(len(self.log()), 2)


class RunFolderTests(TmpSweepCase):
    def test_an_active_marker_keeps_a_run_after_its_session_stops(self) -> None:
        for root in (self.unit, self.delegate):
            with self.subTest(root=root.name):
                payload = write(root / "marked" / "tmp" / "old" / "file")
                _ = write(root / "active" / ENDED_SESSION, f"{payload.parents[2]}\n")
                _ = self.sweep()
                self.assertTrue(payload.is_file())

    def test_an_unended_roster_worker_keeps_its_idle_run(self) -> None:
        for status in ("starting", "running", "done", "failed"):
            with self.subTest(status=status):
                payload = write(self.unit / status / "board.log")
                _ = write(payload.parent / "mesh_roster.json", json.dumps({"impl": {"status": status}}))
                _ = self.sweep()
                self.assertTrue(payload.is_file())

    def test_a_paused_roster_worker_keeps_the_run_and_its_temp_entries(self) -> None:
        payload = write(self.delegate / "paused" / "tmp" / "old" / "file")
        roster = write(payload.parents[2] / "mesh_roster.json", json.dumps({"fix": {"status": "paused"}}))
        _ = self.sweep()
        self.assertTrue(payload.is_file())
        self.assertTrue(roster.is_file())

    def test_a_paused_unit_keeps_a_run_that_records_its_worktree(self) -> None:
        worktree = self.tmp.parent / "worktree"
        payload = write(self.unit / "paused-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["SHUTDOWN_STATE_DIR"]) / "account" / "sessions" / "unit.json", json.dumps({
            "session": {"kind": "unit", "cwd": str(worktree), "run_dirs": []},
            "progress": {"kind": "stopped"},
        }))
        other = write(self.delegate / "ended" / "board.log")
        result = self.sweep()
        self.assertTrue(payload.is_file())
        self.assertFalse(other.exists())
        self.assertIn(str(payload.parent), result.kept[tmp_sweep.RunKeepReason.SESSION_WORKTREE.value])

    def test_a_legacy_paused_unit_keeps_the_worktree_run(self) -> None:
        worktree = self.tmp.parent / "legacy-worktree"
        payload = write(self.delegate / "legacy-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["SHUTDOWN_STATE_DIR"]) / "account" / "record.json", json.dumps({
            "state": "down", "entries": [{"session": {"kind": "unit", "cwd": str(worktree), "run_dirs": []},
                                            "progress": {"kind": "stopped"}}],
        }))
        _ = self.sweep()
        self.assertTrue(payload.is_file())

    def test_a_live_units_worktree_keeps_a_run_without_a_director_record(self) -> None:
        worktree = self.tmp.parent / "worktree"
        payload = write(self.delegate / "live-unit" / "board.log")
        _ = write(payload.parent / "progress_history_state.json", json.dumps({"working_dir": str(worktree)}))
        _ = write(Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / "123.json", json.dumps({
            "pid": 123, "sessionId": LIVE_SESSION, "name": "unit", "messagingSocketPath": "",
            "updatedAt": 1, "cwd": str(worktree),
        }))
        _ = self.sweep()
        self.assertTrue(payload.is_file())

    def test_unreadable_ownership_records_keep_the_run(self) -> None:
        for name in ("mesh_roster.json", "progress_history_state.json", "director", "marker", "pause", "session"):
            with self.subTest(record=name):
                payload = write(self.unit / name / "tmp" / "old" / "file")
                run = payload.parents[2]
                if name == "marker":
                    broken = self.unit / "active" / ENDED_SESSION
                    broken.mkdir(parents=True)
                elif name == "director":
                    broken = run / name
                    broken.mkdir()
                elif name == "pause":
                    broken = write(Path(os.environ["SHUTDOWN_STATE_DIR"]) / "account" / "sessions" / "bad.json", "{")
                elif name == "session":
                    broken = write(Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / "bad.json", "{")
                else:
                    broken = write(run / name, "{")
                result = self.sweep()
                self.assertTrue(payload.is_file())
                self.assertIn(str(run), result.kept[tmp_sweep.RunKeepReason.UNREADABLE_RECORD.value])
                if broken.is_dir():
                    broken.rmdir()
                else:
                    broken.unlink()

    def test_an_ended_idle_run_is_still_removed(self) -> None:
        for root in (self.unit, self.delegate):
            with self.subTest(root=root.name):
                run = write(root / "ended" / "board.log").parent
                _ = write(run / "mesh_roster.json", json.dumps({"fix": {"status": "ended"}}))
                _ = write(run / "director", ENDED_SESSION)
                result = self.sweep()
                self.assertFalse(run.exists())
                self.assertIn(str(run), [entry["path"] for entry in result.deleted])


class HandCleanupTests(TmpSweepCase):
    def ended_run(self, name: str) -> Path:
        run = write(self.unit / name / "board.log").parent
        _ = write(run / "mesh_roster.json", json.dumps({"fix": {"status": "ended"}}))
        return run

    def command(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(output),
              mock.patch.object(tmp_sweep, "default_roots", return_value=self.roots()),
              mock.patch.object(tmp_sweep, "PROC_DIR", str(self.proc)),
              mock.patch("time.time", return_value=self.now)):
            status = tmp_sweep.main(list(arguments))
        return status, output.getvalue()

    def test_plan_lists_keep_and_remove_reasons_and_changes_nothing(self) -> None:
        ended = self.ended_run("ended")
        paused = self.ended_run("paused")
        _ = write(paused / "mesh_roster.json", json.dumps({"fix": {"status": "paused"}}))
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((ended / "board.log").is_file())
        self.assertTrue((paused / "board.log").is_file())
        self.assertIn(f"remove {ended}: all recorded workers ended", output)
        self.assertIn(f"keep {paused}: a worker has not ended", output)
        self.assertIn(f"tmp sweep: plan {plan}", output)
        self.assertEqual(tmp_sweep.planned_removals(plan), [str(ended)])
        self.assertFalse(self.state.exists())

    def test_age_without_end_evidence_keeps_an_unused_folder(self) -> None:
        idle = write(self.tmp / "unknown" / "file").parent
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        self.assertIn(f"keep {idle}: no recorded end", output)
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((idle / "file").is_file())

    def test_apply_removes_only_planned_paths_and_logs_each_removal(self) -> None:
        ended = self.ended_run("ended")
        kept = self.ended_run("kept")
        _ = write(kept / "mesh_roster.json", json.dumps({"fix": {"status": "paused"}}))
        plan = self.tmp.parent / "plan.json"
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 0, output)
        added = self.ended_run("added-after-plan")
        _ = write(kept / "mesh_roster.json", json.dumps({"fix": {"status": "ended"}}))
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertFalse(ended.exists())
        self.assertTrue((added / "board.log").is_file())
        self.assertTrue((kept / "board.log").is_file())
        self.assertIn(f"tmp sweep: deleted {ended}", output)
        deleted = cast(list[dict[str, object]], self.log()[0]["deleted"])
        self.assertEqual([entry["path"] for entry in deleted], [str(ended)])

    def test_apply_rechecks_worker_marker_process_and_unreadable_record(self) -> None:
        for change in ("worker", "marker", "process", "unreadable"):
            with self.subTest(change=change):
                run = self.ended_run(change)
                plan = self.tmp.parent / f"{change}.json"
                status, output = self.command("--plan", str(plan))
                self.assertEqual(status, 0, output)
                if change == "worker":
                    _ = write(run / "mesh_roster.json", json.dumps({"fix": {"status": "paused"}}))
                elif change == "marker":
                    _ = write(self.unit / "active" / change, str(run) + "\n")
                elif change == "process":
                    fake_process(self.proc, 123, cwd=str(run))
                else:
                    _ = write(run / "mesh_roster.json", "{")
                status, output = self.command("--apply", str(plan))
                self.assertEqual(status, 0, output)
                self.assertTrue((run / "board.log").is_file())
                self.assertIn(f"keep {run}:", output)
                self.assertNotIn(f"deleted {run}", output)

    def test_apply_cannot_delete_a_path_outside_the_current_candidates(self) -> None:
        outside = write(self.tmp.parent / "outside" / "file").parent
        plan = write(self.tmp.parent / "plan.json", json.dumps({"version": 1, "entries": [
            {"path": str(outside), "action": "remove", "reason": "all recorded workers ended"},
        ]}))
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 0, output)
        self.assertTrue((outside / "file").is_file())
        self.assertIn(f"keep {outside}: no current sweep candidate", output)

    def test_an_existing_plan_is_preserved_and_bad_plans_remove_nothing(self) -> None:
        run = self.ended_run("ended")
        plan = write(self.tmp.parent / "plan.json", "not json")
        status, output = self.command("--plan", str(plan))
        self.assertEqual(status, 1, output)
        self.assertEqual(plan.read_text(), "not json")
        status, output = self.command("--apply", str(plan))
        self.assertEqual(status, 1, output)
        self.assertTrue((run / "board.log").is_file())


class RunningSessionTests(unittest.TestCase):
    def test_running_session_ids_names_only_running_sessions(self) -> None:
        def record(pid: int, session_id: str) -> sessions.SessionRecord:
            return cast(sessions.SessionRecord, cast(object, {
                "pid": pid, "sessionId": session_id, "name": "", "messagingSocketPath": "",
                "updatedAt": 1, "tmux": "", "formerNames": []}))
        def running(found: sessions.SessionRecord) -> bool:
            return found["pid"] == 1
        registry = sessions.RegistryRead((
            sessions.RegistryObservation(record(1, LIVE_SESSION), {}),
            sessions.RegistryObservation(record(2, ENDED_SESSION), {}),
        ), ())
        with (mock.patch.object(sessions, "read_registry", return_value=registry),
              mock.patch.object(sessions, "running_session", side_effect=running)):
            self.assertEqual(tmp_sweep.running_session_ids(), frozenset({LIVE_SESSION}))

    def test_an_unreadable_record_reads_as_none(self) -> None:
        registry = sessions.RegistryRead((), (Path("/sessions/1.json"),))
        with mock.patch.object(sessions, "read_registry", return_value=registry):
            self.assertIsNone(tmp_sweep.running_session_ids())


if __name__ == "__main__":
    _ = unittest.main()
