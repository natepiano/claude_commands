"""A failed CI read must not hide other runs in the progress table."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class CiTickTests(unittest.TestCase):
    def test_failed_run_read_still_prints_later_runs_and_rerun_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            tick = scripts / "validate_and_push/ci_tick.sh"
            tick.parent.mkdir(parents=True)
            _ = shutil.copy2(Path(__file__).with_name("ci_tick.sh"), tick)
            lib = scripts / "lib"
            lib.mkdir()
            clock = lib / "py"
            _ = clock.write_text("#!/bin/sh\nprintf 'UTC\\n'\n")
            clock.chmod(0o755)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            _ = gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
root = Path(os.environ['CI_TICK_TEST_ROOT'])
run = sys.argv[3]
with (root / 'read').open('a') as log:
    log.write(run + '\\n')
if run == '1' and (root / 'fail').exists():
    raise SystemExit(1)
print(json.dumps({'createdAt': '2026-10-29T00:00:00Z', 'updatedAt': '2026-10-29T00:00:01Z',
 'status': 'completed', 'conclusion': 'success', 'url': 'https://example.invalid/' + run,
 'jobs': [{'name': 'tests', 'status': 'completed', 'conclusion': 'success'}]}))
''')
            gh.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(root),
                   "CI_TICK_TEST_ROOT": str(root)}
            arguments = ["bash", str(tick), "owner/first", "1", "owner/second", "2"]
            failed = root / "fail"
            failed.touch()
            stopped = subprocess.run(arguments, env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(stopped.returncode, 0)
            self.assertEqual((root / "read").read_text().splitlines(), ["1", "2"])
            self.assertIn("error: gh run view failed", stopped.stdout)
            self.assertIn("| tests | n/a | green |", stopped.stdout)
            self.assertIn("second finished — success", stopped.stdout)
            failed.unlink()
            retried = subprocess.run(arguments, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(retried.returncode, 0, retried.stderr)
            self.assertIn("| tests | green | green |", retried.stdout)
            self.assertNotIn("error:", retried.stdout)
            self.assertEqual((root / "read").read_text().splitlines(), ["1", "2", "1", "2"])


if __name__ == "__main__":
    _ = unittest.main()
