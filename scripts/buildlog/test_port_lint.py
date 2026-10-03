#!/usr/bin/env python3
"""Tests for port_lint.py: which steps stand in for a lint, and who makes it wait."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

import port_lint
import store
import treekey
from port_lint import Process, ProbeRow
from test_index import Record, encode, point_root_at, step
from test_treekey import GIT_ENVIRONMENT

SCRIPT = Path(__file__).with_name("port_lint.py")
CLIPPY = ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"]
MEND = ["env", "RUSTC_WRAPPER=", "cargo", "mend", "--all-targets", "--workspace"]
KEY = "k" * 64
RUSTC = "rustc 1.90.0 (1159e78c4 2025-09-14)"
WORKTREE = "/r/feature"


def row(name: str, status: str | None, directory: str, *kinds: str | dict[str, str]) -> ProbeRow:
    return {"name": name, "status": status, "directory": directory, "children": [{"kind": kind} for kind in kinds]}


def lint_step(record_id: str, started_at: str, **fields: object) -> Record:
    return step(
        record_id,
        **{"started_at": started_at, "argv": CLIPPY, "tree_key": KEY, "tree_changed": False, "rustc": RUSTC, **fields},
    )


class ArgvTests(unittest.TestCase):
    def test_clippy(self) -> None:
        self.assertTrue(port_lint.lint_argv("clippy", CLIPPY))
        self.assertTrue(port_lint.lint_argv("clippy", ["cargo", "clippy", "--all-targets", "--workspace", "--", "-D", "warnings"]))
        refused = [
            ["cargo", "clippy", "--workspace", "--", "-D", "warnings"],
            ["cargo", "clippy", "--workspace", "--all-targets", "-p", "hana", "--", "-D", "warnings"],
            ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings", "-W", "clippy::pedantic"],
            ["cargo", "clippy", "--workspace", "--all-targets"],
            ["cargo", "+nightly", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
            ["cargo", "clippy", "--workspace", "--lib", "--bins", "--tests", "--", "-D", "warnings"],
            MEND,
        ]
        for argv in refused:
            with self.subTest(argv=argv):
                self.assertFalse(port_lint.lint_argv("clippy", argv))

    def test_mend(self) -> None:
        self.assertTrue(port_lint.lint_argv("mend", MEND))
        self.assertTrue(port_lint.lint_argv("mend", [*MEND[:4], "--workspace", "--fix", "--all-targets"]))
        refused = [
            MEND[2:],
            [*MEND, "--manifest-path", "Cargo.toml"],
            [*MEND, "--fail-on-warn"],
            MEND[:-1],
            CLIPPY,
        ]
        for argv in refused:
            with self.subTest(argv=argv):
                self.assertFalse(port_lint.lint_argv("mend", argv))
        self.assertFalse(port_lint.lint_argv("sweep", ["sweep.py"]))

    def test_mend_stands_in_only_when_it_fixed_nothing(self) -> None:
        fix = [*MEND, "--fix"]
        self.assertTrue(port_lint.stands_in("mend", fix, 0))
        self.assertFalse(port_lint.stands_in("mend", fix, 2))
        # A --fix run whose output was not captured may have fixed something.
        self.assertFalse(port_lint.stands_in("mend", fix, None))
        self.assertTrue(port_lint.stands_in("mend", MEND, None))
        self.assertTrue(port_lint.stands_in("clippy", CLIPPY, None))

    def test_mend_never_stands_in_for_clippy(self) -> None:
        # The read-only mend `lint clippy` runs with a clippy-linked mend active exits 0
        # over warnings, and the build log cannot tell whether it ran clippy's lints.
        for fixes in (None, 0):
            with self.subTest(fixes=fixes):
                self.assertFalse(port_lint.stands_in("clippy", MEND, fixes))


class BusyTests(unittest.TestCase):
    def test_probe_rows(self) -> None:
        home = os.path.realpath(os.path.expanduser("~"))
        worktree = f"{home}/rust/feature"
        cases: list[tuple[str, list[ProbeRow], str | None]] = [
            ("busy inside", [row("feature", "busy", "~/rust/feature")], "agent feature"),
            ("shell in a member", [row("feature", "shell", "~/rust/feature/crates/hana")], "agent feature"),
            ("idle inside", [row("feature", "idle", "~/rust/feature")], None),
            ("no status", [row("Claude.app", None, "~/rust/feature")], None),
            ("busy in a parent", [row("nightly-rust", "busy", "~/rust"), row("app", "busy", "~")], None),
            ("busy in a sibling", [row("feature-two", "busy", "~/rust/feature-two")], None),
            (
                "thread child",
                [row("feature", "idle", "~/rust/feature", {"detached": "codex"}, "thread", "shell")],
                "a Codex seat of feature",
            ),
            ("detached app server only", [row("feature", "idle", "~/rust/feature", {"detached": "codex"})], None),
        ]
        for name, rows, who in cases:
            with self.subTest(name):
                found = port_lint.busy_row(rows, worktree)
                self.assertEqual(found[0] if found else None, who)

    def test_cargo_processes(self) -> None:
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        worktree = base / "feature"
        member = worktree / "crates" / "hana"
        member.mkdir(parents=True)
        processes = [
            Process(10, "cargo-port", str(worktree)),
            Process(11, "cargo-handler", str(worktree)),
            Process(12, "rustc", str(worktree)),
            Process(13, "cargo", str(base)),
            Process(14, "cargo-clippy", str(member)),
        ]
        self.assertEqual(port_lint.busy_cargo(processes, str(worktree)), ("cargo pid 14 (cargo-clippy)", str(member)))
        self.assertIsNone(port_lint.busy_cargo(processes[:4], str(worktree)))


class SweepTests(unittest.TestCase):
    def test_sweep_runs_while_clippy_defers(self) -> None:
        point_root_at(self, Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog")
        ran: list[list[str]] = []

        def execv(_path: object, args: list[str]) -> None:
            ran.append(args)

        _ = self.enterContext(mock.patch.object(port_lint, "deferral", return_value="port-lint: deferred — busy"))
        _ = self.enterContext(mock.patch("os.execv", execv))
        self.assertEqual(port_lint.main(["clippy"]), port_lint.DEFERRED)
        _ = port_lint.main(["sweep"])
        self.assertEqual(ran, [[str(port_lint.LINT), "sweep"]])


class ReuseTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)

    def write(self, *records: Record) -> None:
        path = self.root / "natedev" / "2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def found(self, command: str = "clippy", key: str = KEY) -> str | None:
        reusable = port_lint.find_reusable(command, "natedev", WORKTREE, key, RUSTC)
        return reusable.id if reusable else None

    def test_newest_matching_step(self) -> None:
        self.write(
            lint_step("old", "2026-10-02T10:00:00.000Z"),
            lint_step("pass", "2026-10-02T11:00:00.000Z", caller="cargo-port"),
            lint_step("other-argv", "2026-10-02T11:30:00.000Z", argv=CLIPPY[:3] + CLIPPY[4:]),
            lint_step("other-tree", "2026-10-02T11:40:00.000Z", tree_key="x" * 64),
            lint_step("changed", "2026-10-02T11:45:00.000Z", tree_key=None, tree_changed=True),
            lint_step("other-rustc", "2026-10-02T11:50:00.000Z", rustc="rustc 1.91.0"),
            lint_step("other-host", "2026-10-02T11:55:00.000Z", host="mac"),
            lint_step("other-worktree", "2026-10-02T11:56:00.000Z", worktree="/r/hana"),
            lint_step("sandbox", "2026-10-02T11:57:00.000Z", status=3),
            lint_step("usage", "2026-10-02T11:58:00.000Z", status=2),
            lint_step("killed", "2026-10-02T11:59:00.000Z", status=130),
        )
        self.assertEqual(self.found(), "pass")
        self.assertIsNone(self.found(key="y" * 64))
        self.assertIsNone(self.found("mend"))

    def test_a_failure_stands_in_too(self) -> None:
        self.write(
            lint_step("pass", "2026-10-02T11:00:00.000Z"),
            lint_step("fail", "2026-10-02T11:10:00.000Z", status=101, log="natedev/logs/2026-10/fail.log.gz"),
        )
        self.assertEqual(self.found(), "fail")

    def test_mend_with_fixes_does_not(self) -> None:
        self.write(
            lint_step("plain", "2026-10-02T10:00:00.000Z", step="mend", argv=MEND),
            lint_step("fixed", "2026-10-02T11:00:00.000Z", step="mend", argv=[*MEND, "--fix"], mend_fixes=1),
        )
        self.assertEqual(self.found("mend"), "plain")

    def test_clippy_reuses_only_stock_clippy(self) -> None:
        self.write(
            lint_step("stock", "2026-10-02T10:00:00.000Z"),
            lint_step("in-mend", "2026-10-02T11:00:00.000Z", step="mend", argv=MEND, warnings=3),
        )
        self.assertEqual(self.found(), "stock")
        # `lint mend` exits 0 over the same warnings, so its own reuse takes the newer step.
        self.assertEqual(self.found("mend"), "in-mend")

    def test_a_mend_step_alone_leaves_clippy_to_run(self) -> None:
        self.write(lint_step("in-mend", "2026-10-02T11:00:00.000Z", step="mend", argv=MEND, warnings=0))
        self.assertIsNone(self.found())


class CommandTests(unittest.TestCase):
    """port_lint.py as cargo-port runs it, in a scratch repo, on a temporary build log."""

    base: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        if shutil.which("rustc") is None:
            self.skipTest("no rustc on PATH")
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "buildlog"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _ = (self.repo / "lib.rs").write_text("\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "first"]):
            _ = subprocess.run(["git", *args], cwd=self.repo, env=self.environment(), check=True, capture_output=True)

    def environment(self) -> dict[str, str]:
        return {**os.environ, **GIT_ENVIRONMENT, "BUILDLOG_DIR": str(self.root)}

    def port_lint(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo,
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def record(self, **fields: object) -> None:
        rustc = subprocess.run(["rustc", "-V"], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()
        key = treekey.tree_key(str(self.repo))
        path = self.root / store.host_name() / "2026-10.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = lint_step(
            "s1",
            "2026-10-02T16:00:00.000Z",
            host=store.host_name(),
            worktree=str(self.repo),
            tree_key=key,
            rustc=rustc,
            caller="verify",
            seat="impl2",
            duration_s=41.6,
            **fields,
        )
        _ = path.write_bytes(encode(record))

    def calls(self) -> list[Record]:
        lines = [line for path in sorted(self.root.glob("*/*.jsonl")) for line in path.read_text().splitlines()]
        return [record for record in (cast(Record, json.loads(line)) for line in lines) if record["kind"] == "call"]

    def test_reused_pass(self) -> None:
        self.record()
        result = self.port_lint("clippy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^port-lint: clippy reused from verify impl2 at \d\d:\d\d, tree unchanged \(passed\)\n$")
        [call] = self.calls()
        expected: Record = {
            "tool": "port-lint",
            "caller": "cargo-port",
            "command": "port-lint clippy",
            "verb": "clippy",
            "outcome": "reused",
            "status": 0,
            "saved_s": 42,
            "reuses": "s1",
            "reason": None,
            "worktree": str(self.repo),
        }
        self.assertEqual({name: call[name] for name in expected}, expected)

    def test_replayed_failure_prints_its_log(self) -> None:
        log = "natedev/logs/2026-10/s1.log.gz"
        (self.root / log).parent.mkdir(parents=True)
        _ = (self.root / log).write_bytes(gzip.compress(b"error: unused import\n"))
        self.record(status=101, log=log)
        result = self.port_lint("clippy")
        self.assertEqual(result.returncode, 101, result.stderr)
        self.assertIn("tree unchanged (failed)\nerror: unused import\n", result.stdout)
        self.assertEqual([(call["outcome"], call["status"]) for call in self.calls()], [("replayed", 101)])

    def test_usage(self) -> None:
        for args in ((), ("doc",)):
            with self.subTest(args=args):
                result = self.port_lint(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: port-lint", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
