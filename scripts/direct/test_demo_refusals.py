#!/usr/bin/env python3
"""The refusal demo runs from any directory and removes its scratch folders."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


DEMO = Path(__file__).resolve().with_name("demo_refusals.sh")


class RefusalDemoTests(unittest.TestCase):
    def test_five_refusals_run_in_order_and_remove_their_scratch_folders(self) -> None:
        self._check_demo(python310=False)

    def test_demo_skips_python310_without_datetime_utc(self) -> None:
        self._check_demo(python310=True)

    def _check_demo(self, *, python310: bool) -> None:
        real_allocator = shutil.which("mktemp")
        if not real_allocator:
            self.fail("the demo requires mktemp on PATH")
        git = shutil.which("git")
        if not git:
            self.fail("the recorder requires git on PATH")
        with tempfile.TemporaryDirectory(prefix="refusal-demo-test-", dir="/tmp") as temporary:
            root = Path(temporary)
            working = root / "working"
            scratch = root / "scratch"
            commands = root / "bin"
            home = root / "home"
            inherited_cache = root / "inherited-cache"
            for directory in (working, scratch, commands, home, inherited_cache):
                directory.mkdir()
            allocations = root / "allocations.txt"
            # Redirect real directory allocations so cleanup has an isolated oracle.
            allocator = commands / "mktemp"
            _ = allocator.write_text(
                r'''#!/bin/sh
if [ "$1" != "-d" ]; then exec "$DEMO_TEST_MKTEMP" "$@"; fi
created=$("$DEMO_TEST_MKTEMP" -d "$DEMO_TEST_SCRATCH/run.XXXXXXXX") || exit $?
printf "%s\n" "$created" >> "$DEMO_TEST_ALLOCATIONS"
printf "%s\n" "$created"
''',
                encoding="utf-8",
            )
            allocator.chmod(0o755)
            if python310:
                python = commands / "python3"
                _ = python.write_text(
                    f"#!{sys.executable}\n"
                    + "import datetime, runpy, sys\n"
                    + "sys.version_info = (3, 10, 0, 'final', 0)\n"
                    + "del datetime.UTC\n"
                    + "if sys.argv[1] == '-c': exec(sys.argv[2])\n"
                    + "else:\n"
                    + "    script = sys.argv.pop(1)\n"
                    + "    runpy.run_path(script, run_name='__main__')\n",
                    encoding="utf-8",
                )
                python.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(DEMO)],
                cwd=working,
                env={
                    "PATH": f"{commands}:{Path(git).parent}:/run/current-system/sw/bin:/usr/bin:/bin",
                    "HOME": str(home),
                    "XDG_CACHE_HOME": str(inherited_cache),
                    "LC_ALL": "C.UTF-8",
                    "DEMO_TEST_SCRATCH": str(scratch),
                    "DEMO_TEST_ALLOCATIONS": str(allocations),
                    "DEMO_TEST_MKTEMP": real_allocator,
                },
                capture_output=True,
                text=True,
                timeout=TEST_HANG_GUARD_SECS,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            refusals = (
                '--accepted-checks is required: name affected accepted checks or "none: <why>"',
                "Astra review due for F001; record it with findings.py astra",
                "Missing --keep for F001: route it with `findings.py route` or give the kept category and its evidence",
                "Missing phase: --phase-id is required for a recorded run",
                "Stale prompt phase: Phase 9 — Some other phase; open phase is Phase 1 — Demo phase",
            )
            after = 0
            for refusal in refusals:
                with self.subTest(refusal=refusal):
                    position = result.stdout.find(refusal, after)
                    self.assertGreaterEqual(position, after, result.stdout)
                    after = position + len(refusal)
            self.assertTrue(allocations.is_file(), "the demo must allocate its own scratch folder")
            created = allocations.read_text(encoding="utf-8").splitlines()
            self.assertTrue(created, "the demo must allocate its own scratch folder")
            for folder in created:
                self.assertFalse(Path(folder).exists(), f"the demo left {folder} behind")
            self.assertEqual(list(scratch.iterdir()), [], "the scratch root must be empty")
            self.assertEqual(list(inherited_cache.iterdir()), [], "the demo must not write to an inherited cache")


if __name__ == "__main__":
    _ = unittest.main()
