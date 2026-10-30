"""Working-set reports from a private process tree with production ownership labels."""

from __future__ import annotations

import builtins
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

import working_set


class WorkingSetTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = (self.proc / "meminfo").write_text(
            "MemTotal:       16777216 kB\nMemFree:         1048576 kB\n"
            + "MemAvailable:   6291456 kB\nSwapTotal:      4194304 kB\nSwapFree:       3145728 kB\n"
        )

    def process(self, pid: int, argv: list[str], pss_kib: int, swap_kib: int = 0,
                *, cwd: str = "/home/test/.claude") -> Path:
        process = self.proc / str(pid)
        process.mkdir()
        _ = (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        _ = (process / "comm").write_text(Path(argv[0]).name + "\n")
        _ = (process / "status").write_text(
            f"Name:\t{Path(argv[0]).name}\nPid:\t{pid}\nVmRSS:\t{pss_kib * 2} kB\nVmSwap:\t{swap_kib} kB\n"
        )
        _ = (process / "smaps_rollup").write_text(
            "00400000-7fffffff ---p 00000000 00:00 0 [rollup]\n"
            + f"Rss: {pss_kib * 2} kB\nPss: {pss_kib} kB\n"
            + f"Swap: {swap_kib} kB\nSwapPss: {swap_kib} kB\n"
        )
        (process / "exe").symlink_to(argv[0])
        (process / "cwd").symlink_to(cwd)
        return process

    def report(self) -> working_set.WorkingSet:
        original_open = cast(Callable[..., object], builtins.open)
        original_io_open = cast(Callable[..., object], io.open)
        original_scandir = cast(Callable[..., object], os.scandir)

        def check_path(path: object) -> None:
            if isinstance(path, (str, Path)):
                self.assertTrue(Path(path).is_relative_to(self.proc),
                                f"The report attempted a read outside the private process tree: {path}")

        def checked_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            check_path(path)
            self.assertFalse(any(flag in mode for flag in "wax+"), "The report must not write files")
            return original_open(path, mode, *args, **kwargs)

        def checked_io_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            check_path(path)
            self.assertFalse(any(flag in mode for flag in "wax+"), "The report must not write files")
            return original_io_open(path, mode, *args, **kwargs)

        def checked_scandir(path: object) -> object:
            check_path(path)
            return original_scandir(path)

        with (mock.patch.object(builtins, "open", side_effect=checked_open),
              mock.patch.object(io, "open", side_effect=checked_io_open),
              mock.patch.object(os, "scandir", side_effect=checked_scandir),
              mock.patch.object(subprocess, "run", side_effect=AssertionError("Report must read the fixture directly"))):
            return working_set.collect(self.proc)

    def test_categories_sum_resident_pss_and_swap_from_each_process(self) -> None:
        cases = [
            (101, ["/toolchain/bin/cargo", "check"], "cargo/rustc", 1024, 128),
            (102, ["/toolchain/bin/rustc", "app.rs"], "cargo/rustc", 2048, 256),
            (103, ["/toolchain/bin/rustdoc", "app.rs"], "cargo/rustc", 512, 64),
            (104, ["/usr/bin/claude", "--resume"], "Claude sessions", 4096, 512),
            (105, ["/usr/bin/node", "/opt/@anthropic-ai/claude-code/cli.js"], "Claude sessions", 1024, 128),
            (106, ["/usr/bin/codex", "resume"], "Codex", 8192, 1024),
            (107, ["/usr/bin/node", "/opt/@openai/codex/bin/codex.js"], "Codex", 2048, 256),
            (108, ["/usr/bin/python3.13", "/home/test/.claude/scripts/lint/tmp_sweep.py"], "Python helpers", 512, 64),
            (109, ["/bin/sleep", "60"], "other", 128, 16),
            (110, ["/usr/bin/node", "/opt/service.js", "claude", "codex"], "other", 256, 32),
            (111, ["/bin/bash", "-c", "python3 cargo rustc claude codex"], "other", 64, 8),
        ]
        expected: dict[str, tuple[int, int, int]] = {}
        for pid, argv, category, pss, swap in cases:
            _ = self.process(pid, argv, pss, swap)
            count, old_pss, old_swap = expected.get(category, (0, 0, 0))
            expected[category] = (count + 1, old_pss + pss, old_swap + swap)

        report = self.report()

        actual = {group.category: (group.count, group.pss_kib, group.swap_kib)
                  for group in report.groups if not group.script}
        self.assertEqual(actual, expected)
        self.assertEqual(report.unreadable, 0)
        self.assertEqual(report.machine.total_kib, 16777216)
        self.assertEqual(report.machine.available_kib, 6291456)
        self.assertEqual(report.machine.swap_used_kib, 1048576)
        self.assertEqual(report.machine.swap_total_kib, 4194304)

    def test_python_scripts_group_by_path_and_name_the_production_owner(self) -> None:
        script = "/home/test/.claude/scripts/lint/tmp_sweep.py"
        _ = self.process(201, ["/usr/bin/python3", script, "--dry-run"], 1024, 128)
        _ = self.process(202, ["/usr/bin/python3", "-u", "scripts/lint/tmp_sweep.py"], 2048, 256)
        _ = self.process(203, ["/usr/bin/python3", "/home/test/.claude/scripts/buildlog/record.py"], 512, 64)
        _ = self.process(204, ["/usr/bin/python3", "/home/test/.claude/scripts/buildlog/mac_idle.py"], 256, 32)
        _ = self.process(205, ["/usr/bin/python3", "/tmp/unowned.py"], 128, 16)
        _ = self.process(206, ["/usr/bin/python3", "/another/checkout/scripts/lint/tmp_sweep.py"], 64, 8)

        report = self.report()

        detail = {group.script: (group.owner, group.count, group.pss_kib, group.swap_kib)
                  for group in report.groups if group.script}
        self.assertEqual(detail, {
            script: ("resources-unit", 2, 3072, 384),
            "/home/test/.claude/scripts/buildlog/record.py": ("report-unit", 1, 512, 64),
            "/home/test/.claude/scripts/buildlog/mac_idle.py": ("verify-unit", 1, 256, 32),
            "/tmp/unowned.py": ("unassigned", 1, 128, 16),
            "/another/checkout/scripts/lint/tmp_sweep.py": ("resources-unit", 1, 64, 8),
        })
        total = next(group for group in report.groups if group.category == "Python helpers" and not group.script)
        self.assertEqual((total.count, total.pss_kib, total.swap_kib), (6, 4032, 504))
        text = working_set.render(report)
        self.assertIn(script, text)
        self.assertIn("[resources]", text)
        self.assertIn("[verify]", text)
        self.assertNotIn("resources-unit", text)
        self.assertNotIn("verify-unit", text)
        self.assertIn("[unassigned]", text)

    def test_verify_helpers_have_the_owner_named_in_builds_production(self) -> None:
        scripts = (
            "scripts/direct/step_record.py",
            "scripts/direct/local_release_wait.py",
            "scripts/direct/verify_saved.py",
            "scripts/lint/nextest_budget.py",
            "scripts/lint/scope.py",
            "scripts/lint/config_values.py",
            "scripts/hooks/pre-tool-use-checkpoint-doc-gate.py",
            "scripts/buildlog/test_port_lint.py",
            "scripts/buildlog/test_mac_idle.py",
            "scripts/direct/test_verify_doc.py",
        )
        for pid, script in enumerate(scripts, start=501):
            _ = self.process(pid, ["python3", script], 1024)
        report = self.report()
        owners = {group.script: group.owner for group in report.groups if group.script}
        for script in scripts:
            with self.subTest(script=script):
                self.assertEqual(owners["/home/test/.claude/" + script], "verify-unit")

    def test_phase_and_role_tests_have_their_production_owners(self) -> None:
        for script, owner in (
            ("scripts/direct/test_phase_process.py", "phases-unit"),
            ("scripts/production/test_roles_audit.py", "roles-unit"),
        ):
            with self.subTest(script=script):
                self.assertEqual(working_set.owner_for(script), owner)

    def test_owner_snapshot_loads_ordered_rules_and_rejects_bad_files(self) -> None:
        path = self.root / "owners.json"
        payload = {"generated_on": "2026-10-29", "sources": ["docs/plans/builds-production.md"],
                   "rules": [{"path": "scripts/a.py", "owner": "a-unit"},
                             {"path": "scripts/", "owner": "default-unit"}]}
        _ = path.write_text(json.dumps(payload))
        self.assertEqual(working_set.load_owners(path),
                         (("scripts/a.py", "a-unit"), ("scripts/", "default-unit")))
        for content in ("{", "[]", "{}", '{"generated_on": "today", "sources": [], "rules": []}',
                        json.dumps({**payload, "rules": [{"path": "scripts/a.py", "owner": 3}]})):
            with self.subTest(content=content):
                _ = path.write_text(content)
                with self.assertRaisesRegex(ValueError, str(path)):
                    _ = working_set.load_owners(path)
        path.unlink()
        with self.assertRaisesRegex(ValueError, str(path)):
            _ = working_set.load_owners(path)

    def test_node_options_do_not_hide_the_entry_script(self) -> None:
        claude = "/opt/@anthropic-ai/claude-code/cli.js"
        codex = "/opt/@openai/codex/bin/codex.js"
        cases = (
            (("--max-old-space-size=4096", claude), "Claude sessions"),
            (("--max-old-space-size", "4096", codex), "Codex"),
            (("-r", "setup.js", claude), "Claude sessions"),
            (("-rsetup.js", codex), "Codex"),
            (("--require", "setup.js", codex), "Codex"),
            (("--require=setup.js", claude), "Claude sessions"),
            (("--import", "setup.mjs", claude), "Claude sessions"),
            (("--loader", "loader.mjs", codex), "Codex"),
            (("--experimental-loader", "loader.mjs", codex), "Codex"),
            (("--inspect-port", "9230", claude), "Claude sessions"),
            (("--inspect", "--trace-warnings", codex), "Codex"),
            (("--", claude), "Claude sessions"),
            (("-e", claude), "other"),
            (("--eval=" + claude, codex), "other"),
            (("-p", codex), "other"),
            (("--require", claude, "/opt/service.js"), "other"),
            (("/opt/service.js", codex), "other"),
            (("--require",), "other"),
        )
        for runtime in ("node", "nodejs"):
            for options, expected in cases:
                with self.subTest(runtime=runtime, options=options):
                    self.assertEqual(working_set.classify((runtime, *options)), expected)

    def test_python_options_do_not_become_script_names(self) -> None:
        cases = (
            (("--check-hash-based-pycs", "always", "x.py"), "/work/x.py"),
            (("-uW", "ignore", "x.py"), "/work/x.py"),
            (("-W", "", "scripts/lint/tmp_sweep.py"), "/work/scripts/lint/tmp_sweep.py"),
            (("-uc", "print(1)"), "<inline>"),
            (("-uWignore", "x.py"), "/work/x.py"),
            (("-uX", "dev", "x.py"), "/work/x.py"),
            (("-uXdev", "x.py"), "/work/x.py"),
            (("-um", "unittest", "--help"), "module:unittest"),
            (("-umunittest", "--help"), "module:unittest"),
            (("-ucprint(1)", "x.py"), "<inline>"),
            (("--", "-x.py"), "/work/-x.py"),
            (("-", "x.py"), "<stdin>"),
            (("x.py", "-c", "print(1)"), "/work/x.py"),
        )
        for pid, (options, expected) in enumerate(cases, start=601):
            with self.subTest(options=options):
                self.assertEqual(working_set.python_script(("python3", *options), "/work"), expected)
                _ = self.process(pid, ["python3", *options], 1024, cwd="/work")
                report = self.report()
                actual = next(group.script for group in report.groups
                              if group.script and any(item.pid == pid for item in group.top))
                self.assertEqual(actual, expected)
                # Each fixture must remain separate from other cases with the same script.
                for child in (self.proc / str(pid)).iterdir():
                    child.unlink()
                (self.proc / str(pid)).rmdir()

    def test_top_processes_are_ordered_by_resident_pss_within_each_category(self) -> None:
        for pid, pss in ((301, 1024), (302, 4096), (303, 2048), (304, 8192)):
            _ = self.process(pid, ["/bin/sleep", "60"], pss, 16)
        _ = self.process(305, ["/usr/bin/rustc"], 16384, 32)

        report = self.report()

        other = next(group for group in report.groups if group.category == "other")
        self.assertGreaterEqual(len(other.top), 1)
        self.assertEqual(other.top[0].pid, 304)
        self.assertEqual([item.pss_kib for item in other.top],
                         sorted((item.pss_kib for item in other.top), reverse=True))
        self.assertNotIn(305, [item.pid for item in other.top])
        text = working_set.render(report)
        self.assertIn("304", text)
        self.assertIn("305", text)

    def test_missing_process_memory_is_reported_without_losing_readable_processes(self) -> None:
        vanished = self.process(401, ["/bin/sleep"], 1024)
        (vanished / "smaps_rollup").unlink()
        _ = self.process(402, ["/usr/bin/rustc"], 2048, 128)
        empty = self.proc / "403"
        empty.mkdir()
        _ = (empty / "cmdline").write_bytes(b"")

        report = self.report()

        self.assertEqual(report.unreadable, 1)
        totals = [group for group in report.groups if not group.script]
        self.assertEqual(sum(group.pss_kib for group in totals), 2048)
        self.assertEqual(sum(group.swap_kib for group in totals), 128)

    def test_command_prints_machine_available_and_swap_used_in_mib(self) -> None:
        report = self.report()
        output = io.StringIO()
        with (mock.patch.object(working_set, "collect", return_value=report),
              mock.patch.object(sys, "argv", ["working-set"]), redirect_stdout(output)):
            status = working_set.main()

        self.assertEqual(status, 0)
        self.assertIn("6144.0 MiB available", output.getvalue())
        self.assertIn("swap 1024.0 / 4096.0 MiB used", output.getvalue())
        for category in ("cargo/rustc", "Claude sessions", "Codex", "Python helpers", "other"):
            self.assertIn(category, output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
