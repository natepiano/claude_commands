#!/usr/bin/env python3
"""Tests for folder_audit.py on a fabricated ~/rust, temp folder and transcript set. A test moves
each folder's time back two days unless it wants the folder recent; a folder holds one file, since
the tmp sweep owns empty temp folders."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import unittest
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

import unittest.mock

if TYPE_CHECKING:
    from ..lib.socket_test_support import short_socket_directory
else:
    from scripts.lib.socket_test_support import short_socket_directory

import folder_audit
import scratch
import tmp_sweep

DAY = 86_400


def age(path: Path, days: float) -> None:
    then = time.time() - days * DAY
    for inner in sorted(path.rglob("*"), reverse=True):
        os.utime(inner, (then, then), follow_symlinks=False)
    os.utime(path, (then, then))


def stamp(path: Path, when: float) -> None:
    os.utime(path, (when, when), follow_symlinks=False)


def changed(path: Path) -> float:
    value = folder_audit.last_change(path)
    assert isinstance(value, float), value
    return value


def folder(path: Path, age_days: float = 2) -> Path:
    path.mkdir(parents=True)
    _ = (path / "file").write_text("x")
    age(path, age_days)
    return path


def git(repo: Path, *arguments: str) -> None:
    _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                       check=True, capture_output=True)


class FolderAuditTests(unittest.TestCase):
    root: Path = Path()
    rust: Path = Path()
    temp: Path = Path()
    recorded_ctimes: dict[str, float] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(short_socket_directory()))
        self.rust = self.root / "rust"
        self.temp = self.root / "tmp"
        self.rust.mkdir()
        self.temp.mkdir()
        self.recorded_ctimes = {}
        scan = os.scandir

        @contextmanager
        def recorded_scan(path: str | int | os.PathLike[str]) -> Generator[Iterator[os.DirEntry[str]]]:
            with scan(path) as entries:
                if isinstance(path, int):
                    yield entries
                    return
                recorded: list[os.DirEntry[str]] = []
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    fields = list(info)
                    fields[9] = self.recorded_ctimes.get(entry.path, info.st_mtime)
                    fake = unittest.mock.Mock(wraps=entry)
                    fake.configure_mock(**{"stat.return_value": os.stat_result(fields, {"st_blocks": info.st_blocks})})
                    fake.name = entry.name
                    fake.path = entry.path
                    recorded.append(cast(os.DirEntry[str], fake))
                yield iter(recorded)

        _ = self.enterContext(unittest.mock.patch.object(os, "scandir", side_effect=recorded_scan))
        state = self.root / "state"
        self.enterContext(unittest.mock.patch.dict(os.environ, {"FOLDER_SWEEP_STATE_DIR": str(state),
                                                                "SCRATCH_HOLDS": str(state / "holds.json")}))

    def test_copied_folder_needs_seven_days_since_its_last_metadata_change(self) -> None:
        now = time.time()
        proc = self.root / "proc"
        proc.mkdir()
        copied = folder(self.temp / "copied-backup", 8)
        old = folder(self.temp / "old-backup", 8)
        self.recorded_ctimes[str(copied / "file")] = now - 5 * 3600
        self.recorded_ctimes[str(old / "file")] = now - 8 * DAY
        found = folder_audit.audit(self.rust, [self.temp], frozenset[str](), now, files=[],
                                   removing=True, proc=str(proc), sockets=str(self.root / "unix"))
        self.assertTrue(copied.is_dir())
        self.assertFalse(old.exists())
        self.assertIn((old, folder_audit.REMOVED), [(entry.path, entry.outcome) for entry in found])

    def test_process_uses_distinguishes_known_empty_from_unavailable(self) -> None:
        proc = self.root / "proc"
        proc.mkdir()
        candidate = self.temp / "candidate"
        for paths in ([], [candidate]):
            with self.subTest(paths=paths):
                with unittest.mock.patch.object(tmp_sweep, "lsof_paths", side_effect=AssertionError("unexpected fallback")):
                    known = folder_audit.process_uses(paths, str(proc))
                self.assertIsInstance(known, tmp_sweep.ProcessUsesKnown)
                assert isinstance(known, tmp_sweep.ProcessUsesKnown)
                self.assertEqual(known.paths, [])
        with unittest.mock.patch.object(tmp_sweep, "LSOF", (str(self.root / "no-lsof"),)):
            unavailable = folder_audit.process_uses([candidate], str(self.root / "no-proc"))
        self.assertIsInstance(unavailable, tmp_sweep.ProcessUsesUnavailable)

    def test_unavailable_process_uses_keeps_due_folders(self) -> None:
        candidate = folder(self.temp / "candidate", 8)
        now = time.time()
        with (unittest.mock.patch.object(tmp_sweep, "LSOF", (str(self.root / "no-lsof"),)),
              unittest.mock.patch.object(folder_audit, "remove", side_effect=AssertionError("removed without a process scan"))):
            outcomes = folder_audit.judge([(candidate, now - 8 * DAY)], now, True,
                                         proc=str(self.root / "no-proc"), sockets=str(self.root / "no-sockets"))
        self.assertEqual(outcomes[candidate], tmp_sweep.NO_PROC)
        self.assertTrue(candidate.exists())

    def test_lsof_fallback_keeps_used_folders_and_judges_unused_folders(self) -> None:
        used = folder(self.temp / "used", 8)
        idle = folder(self.temp / "idle", 8)
        now = time.time()
        output = f"p42\nfcwd\nn{used}\n"
        completed = subprocess.CompletedProcess(tmp_sweep.LSOF, 1, output, "")
        with unittest.mock.patch.object(subprocess, "run", return_value=completed):
            outcomes = folder_audit.judge([(used, now - 8 * DAY), (idle, now - 8 * DAY)], now, False,
                                         proc=str(self.root / "no-proc"), sockets=str(self.root / "no-sockets"))
        self.assertEqual(outcomes, {used: "pid 42 works in it", idle: folder_audit.WOULD_REMOVE})

    def test_the_roots_file_ignores_comments_and_blank_lines(self) -> None:
        roots = self.root / "rust_roots.txt"
        _ = roots.write_text("# top\nhana\n\nbevy  # engine\n  hana videos\n")
        self.assertEqual(folder_audit.expected_rust_names(roots), frozenset({"hana", "bevy", "hana videos"}))
        self.assertIsNone(folder_audit.expected_rust_names(self.root / "missing.txt"))

    def test_only_unexpected_idle_folders_are_loose(self) -> None:
        _ = folder(self.rust / "hana")
        _ = folder(self.rust / "repro")
        stray = folder(self.rust / "hana_p34d_alone")
        _ = folder(self.rust / "fresh_try", age_days=0.1)
        _ = (self.rust / "notes.md").write_text("a file, not a folder")
        (self.rust / "link").symlink_to(self.rust / "hana")
        for name in ("claude", "claude-1000", "systemd-private-x", ".tmpAbC123", "tmux-1000"):
            _ = folder(self.temp / name)
        (self.temp / "empty").mkdir()
        age(self.temp / "empty", 2)
        loose_temp = folder(self.temp / "my-repro")
        in_scratch = folder(self.rust / "scratch" / "berth-phase1")
        loose = folder_audit.audit(self.rust, [self.temp], frozenset({"hana"}), time.time(), files=[])
        self.assertEqual([entry.path for entry in loose], [stray, in_scratch, loose_temp])
        self.assertEqual({(entry.maker, entry.outcome) for entry in loose}, {("", folder_audit.LISTED)})
        unread = folder_audit.audit(self.rust, [], None, time.time(), files=[])
        self.assertEqual([entry.path for entry in unread], [in_scratch])

    def test_idle_time_counts_the_newest_mtime_inside_and_git_times_never_ctime(self) -> None:
        repo = folder(self.rust / "hana_p34d_alone")
        deep = repo / "a" / "b"
        deep.mkdir(parents=True)
        _ = (deep / "new.rs").write_text("x")
        self.assertGreater(changed(repo), time.time() - 60)
        age(repo, 6)
        git(repo, "init", "-q")
        age(repo / "a", 6)
        stamp(repo, time.time() - 6 * DAY)
        self.assertGreater(changed(repo), time.time() - 60)  # git init wrote HEAD and the index folder
        too_deep = folder(self.rust / "deep", 6)
        nested = too_deep / "1" / "2" / "3" / "4"
        nested.mkdir(parents=True)
        _ = (nested / "f").write_text("x")
        age(too_deep, 6)
        stamp(nested / "f", time.time())
        self.assertLess(changed(too_deep), time.time() - 5 * DAY)
        target = folder(self.rust / "built", 6)
        (target / "target").mkdir()
        _ = (target / "target" / "out").write_text("x")
        age(target, 6)
        stamp(target / "target" / "out", time.time())
        self.assertLess(changed(target), time.time() - 5 * DAY)

    def test_an_idle_loose_folder_goes_once_armed_unless_a_guard_keeps_it(self) -> None:
        plain = folder(self.rust / "plain", 8)
        fresh = folder(self.rust / "fresh", 3)
        dirty = folder(self.rust / "dirty", 0)
        git(dirty, "init", "-q")
        age(dirty, 8)
        held = folder(self.rust / "held", 8)
        (held / "sub").mkdir()
        age(held, 8)
        _ = scratch.hold(held / "sub", "until:2099-01-01T0000Z", "test", time.time())
        used = folder(self.rust / "used", 8)
        proc = self.root / "proc"
        (proc / "12").mkdir(parents=True)
        (proc / "12" / "cwd").symlink_to(used)
        listening = folder(self.rust / "listening", 8)
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(listening / "s"))
        stamp(listening / "s", time.time() - 8 * DAY)
        age(listening, 8)
        table = self.root / "unix"
        _ = table.write_text(f"Num RefCount Protocol Flags Type St Inode Path\n0: 2 0 10000 1 1 7 {listening / 's'}\n")
        expected = frozenset[str]()

        def run(removing: bool) -> dict[str, str]:
            found = folder_audit.audit(self.rust, [], expected, time.time(), files=[], removing=removing,
                                       proc=str(proc), sockets=str(table))
            return {entry.path.name: entry.outcome for entry in found}

        dry = run(False)
        self.assertEqual(dry["plain"], folder_audit.WOULD_REMOVE)
        self.assertEqual(dry["fresh"], folder_audit.LISTED)
        self.assertEqual(dry["dirty"], tmp_sweep.UNSAVED_WORK)
        self.assertEqual(dry["held"], folder_audit.HELD)
        self.assertEqual(dry["used"], "pid 12 works in it")
        self.assertEqual(dry["listening"], folder_audit.LIVE_SOCKET)
        self.assertTrue(plain.is_dir())
        self.assertEqual(folder_audit.main(["arm"]), 0)
        self.assertTrue(folder_audit.armed())
        armed = run(folder_audit.armed())
        self.assertEqual(armed["plain"], folder_audit.REMOVED)
        self.assertFalse(plain.exists())
        for kept in (fresh, dirty, held, used, listening):
            self.assertTrue(kept.is_dir(), kept)
        log = (self.root / "state" / folder_audit.REMOVED_LOG).read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertEqual(folder_audit.main(["disarm"]), 0)
        self.assertFalse(folder_audit.armed())

    def test_a_removed_linked_worktree_is_pruned_from_its_source(self) -> None:
        source = self.root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "commit", "-q", "--allow-empty", "-m", "c")
        git(source, "remote", "add", "origin", str(source))
        git(source, "fetch", "-q", "origin")
        worktree = self.rust / "hana_p34d_alone"
        git(source, "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
        age(worktree, 8)
        for name in folder_audit.GIT_TIME_FILES:
            target = source / ".git" / "worktrees" / "hana_p34d_alone" / name
            if target.exists():
                stamp(target, time.time() - 8 * DAY)
        proc = self.root / "proc"
        proc.mkdir()
        found = folder_audit.audit(self.rust, [], frozenset[str](), time.time(), files=[], removing=True,
                                   proc=str(proc), sockets=str(self.root / "unix"))
        self.assertEqual([(entry.path, entry.outcome) for entry in found], [(worktree, folder_audit.REMOVED)])
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"], capture_output=True, text=True,
                                check=True).stdout
        self.assertNotIn(str(worktree), listed)

    def test_the_earliest_started_session_that_names_a_folder_is_its_maker(self) -> None:
        stray = folder(self.rust / "stray")
        other = folder(self.rust / "other")
        prefix = folder(self.rust / "stra")
        maker = self.root / "p" / "aaaa.jsonl"
        talker = self.root / "p" / "bbbb.jsonl"
        untimed = self.root / "p" / "cccc.jsonl"
        maker.parent.mkdir()
        _ = maker.write_text(f'{{"timestamp":"2026-10-20T10:00:00.000Z","input":{{"command":"mkdir -p {stray} && echo \\"x\\""}}}}\n')
        _ = talker.write_text(f'{{"timestamp":"2026-10-25T10:00:00.000Z","input":{{"command":"ls {stray} {other}/a"}}}}\n')
        _ = untimed.write_text(f'{{"toolUseResult":{{"stdout":"{other}"}},"input":{{"command":"cd {other}"}}}}\n')
        lister = self.root / "p" / "dddd.jsonl"
        _ = lister.write_text(f'{{"timestamp":"2026-10-01T10:00:00.000Z","toolUseResult":{{"stdout":"{stray}"}}}}\n')
        os.utime(untimed, (time.time(), time.time()))
        named = folder_audit.makers([stray, other, prefix, self.rust / "unnamed"], [maker, talker, untimed, lister])
        self.assertEqual(named, {stray: "aaaa", other: "bbbb"})

    def test_the_message_lists_each_folder_and_names_scratch(self) -> None:
        loose = [folder_audit.Loose(Path("/home/u/rust/a"), 0.0, "s"),
                 folder_audit.Loose(Path("/tmp/b"), 0.0, "s", folder_audit.REMOVED)]
        text = folder_audit.message(loose)
        self.assertIn("/home/u/rust/a (idle less than 7 days); /tmp/b (removed)", text)
        self.assertIn("scratch.py adopt", text)


if __name__ == "__main__":
    _ = unittest.main()
