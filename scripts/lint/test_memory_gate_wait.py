"""A new admission wait must not use its parent's queue identity."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment


class MemoryGateWaitTests(unittest.TestCase):
    def test_parent_queue_identity_does_not_age_a_new_launch_or_step(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            parent_wait = root / "parent-first-wait"
            _ = parent_wait.write_text("1\n")
            environment = {**os.environ, **config_environment(root),
                           "BUILDLOG_MEM_WAIT_SINCE": "1",
                           "BUILDLOG_MEM_WAIT_FIRST_FILE": str(parent_wait)}
            shell = r'''
source "$1"
# Capture the real admission arguments without a live service.
buildlog_memory_python() {
    local file since
    while (( $# )); do
        case "$1" in
            --since) since=$2; shift 2 ;;
            --shell-state) file=$2; shift 2 ;;
            *) shift ;;
        esac
    done
    printf '%s\n' "$since" >> "$TEST_WAIT_OBSERVED"
    printf 'BUILDLOG_MEM_OUTCOME=Granted\n' > "$file"
}
buildlog_wait_for_memory
buildlog_wait_for_memory /bin/true
[[ -z "$BUILDLOG_MEM_WAIT_FIRST_FILE" ]]
'''
            observed = root / "observed"
            result = subprocess.run(["bash", "-c", shell, "test", str(Path(__file__).with_name("memory_gate.sh"))],
                                    env={**environment, "TEST_WAIT_OBSERVED": str(observed)},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(all(int(since) > 1 for since in observed.read_text().splitlines()))
            self.assertEqual(len(observed.read_text().splitlines()), 2)
            self.assertEqual(parent_wait.read_text(), "1\n")
