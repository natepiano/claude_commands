"""Check lightweight CLI operations and refusal before record changes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import cast
import unittest

from scripts.direct import step_record


class StepRecordCliTests(unittest.TestCase):
    def invoke(self, root: Path, arguments: list[str], *, lightweight: bool = True) -> subprocess.CompletedProcess[str]:
        source = '''import sys
from pathlib import Path
sys.platform = "linux"
script = sys.argv[1]
sys.argv = sys.argv[1:]
try:
    exec(compile(Path(script).read_text(), script, "exec"), {"__name__": "__main__", "__file__": script})
except SystemExit as error:
    status = error.code
else:
    status = 0
'''
        if lightweight:
            source += 'assert not {"argparse", "dataclasses", "inspect"}.intersection(sys.modules), sorted(sys.modules)\n'
        source += 'raise SystemExit(status)\n'
        return subprocess.run([sys.executable, "-S", "-c", source, str(Path(step_record.__file__)), *arguments],
            env={**os.environ, "VERIFY_STEPS_DIR": str(root / "steps"), "VERIFY_PROC_ROOT": str(root / "proc"),
                 "BUILDLOG_DIR": str(root / "logs")},
            capture_output=True, text=True, timeout=5, check=False)

    def fixture(self, root: Path) -> Path:
        process = root / "proc/42"
        process.mkdir(parents=True)
        _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
        boot = root / "proc/sys/kernel/random"
        boot.mkdir(parents=True)
        _ = (boot / "boot_id").write_text("boot")
        records = root / "steps"
        records.mkdir()
        path = records / "call.json"
        _ = path.write_text(json.dumps(dict(v=1, call_id="call", pid=42, proc_start="123", repo=str(root),
            kind="test", package=None, eligible_targets=["mac", "natedev"], runs_on={"kind": "any_target", "mac_targets": ["mac"]}, state="waiting_hold", updated="now")))
        return path

    def test_each_record_verb_avoids_heavy_imports_in_a_fresh_process(self) -> None:
        for verb in ("write", "stopped"):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.fixture(root)
                arguments = [verb, "--call-id", "call"]
                if verb == "write":
                    path.unlink()
                    arguments += ["--pid", "42", "--kind", "lint", "--runs-on", "any_target",
                                  "--eligible-targets", "studio,natedev", "--mac-targets", "studio",
                                  "--pinned-target", "studio", "--state", "waiting_ssh"]
                elif verb == "stopped":
                    log = root / "logs" / socket.gethostname().split(".")[0]
                    log.mkdir(parents=True)
                    _ = (log / "hold-builds-test.jsonl").write_text(json.dumps(dict(kind="hold_build", action="stopped",
                        call_id="call", boot_id="boot", pid=42, proc_start="123", id="event")) + "\n")
                    arguments += ["--consumed", str(root / "consumed")]
                result = self.invoke(root, arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if verb == "write":
                    row = cast(dict[str, object], json.loads(path.read_text()))
                    self.assertEqual(row["eligible_targets"], ["studio", "natedev"])
                    self.assertEqual(row["pinned_target"], "studio")
                else:
                    self.assertEqual((root / "consumed").read_text(), "event\n")
                    repeated = self.invoke(root, arguments)
                    self.assertEqual(repeated.returncode, 1, repeated.stderr)

    def test_bad_syntax_and_help_reach_argparse_before_any_record_change(self) -> None:
        for verb in ("write", "stopped"):
            for extra, status in ((["--unknown"], 2), (["--help"], 0)):
                with self.subTest(verb=verb, extra=extra), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    path = self.fixture(root)
                    before = path.read_bytes()
                    result = self.invoke(root, [verb, "--call-id", "call", *extra], lightweight=False)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assertIn("usage:", result.stdout + result.stderr)
                    self.assertEqual(path.read_bytes(), before)

    def test_invalid_write_values_refuse_without_record_changes(self) -> None:
        for flag, value in (("--kind", "bad"), ("--state", "bad"), ("--pid", "not-a-pid"),
                            ("--runs-on", "bad"), ("--call-id", "../outside")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.fixture(root)
                before = path.read_bytes()
                arguments = ["write", "--call-id", "call", "--pid", "42", "--kind", "lint",
                             "--runs-on", "any_target", "--state", "waiting_hold", flag, value]
                result = self.invoke(root, arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("step refused", result.stderr)
                self.assertEqual(path.read_bytes(), before)
