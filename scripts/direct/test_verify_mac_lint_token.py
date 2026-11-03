"""Mac-allowed lint leaves token waiting through the test routing decision."""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.direct.shell_function_test_support import shell_functions
from scripts.direct.verify_lint_scope_test_support import VERIFY


class VerifyMacLintTokenTests(unittest.TestCase):
    def test_token_wait_uses_shared_free_now_without_an_idle_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checker = root / "mac.py"
            _ = checker.write_text('''import os
from pathlib import Path
import sys
with Path(os.environ["TEST_MAC_CHECKS"]).open("a") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\\n")
raise SystemExit(0 if os.environ["TEST_MAC_STATE"] == "free" else 1)
''')
            for command in ("lint", "check", "doc"):
                for state in ("free", "busy", "blocked", "same-job"):
                    for marker in ("", str(root / "absent-marker")):
                        with self.subTest(command=command, state=state, marker=marker):
                            calls = root / "checks"
                            calls.unlink(missing_ok=True)
                            script = 'set -euo pipefail\n' + shell_functions(VERIFY, "target_for_settings", "mac_retry_hook",
                                                                          "ssh_reserve_hook", "ssh_retry_hook", "acquire_token")
                            script += r'''
TOKEN_USED=1 TOKEN_HELD=0 TOKEN_WAITER_FILE="" TOKEN_FIRST_WAIT_DONE=0
TOKEN_SUPERSEDED=0 TOKEN_WAIT_NOTE_MIN=1 TOKEN_CLOCK_NOW=0
BOARD_DIR=unused STEP_RUNS_ON=mac_allowed STEP_FORCE_MAC=0
MAC_LEAVE_SETTINGS=fixture-settings MAC_RETRY_AFTER=0 LINT_LEFT_QUEUE_STATUS=75
LOCAL_ONLY=0 SSH_RETRY_AFTER_SETTINGS="" PKG="" BUILDLOG_CALL_ID=fixture
HOLD_WAIT_DIR=$DOC_REPO_TOP MAC_RUNNER=$MAC_TEST_SCRIPT
SSH_RETIRED_SETTINGS=() SSH_RETRY_SETTINGS=() SSH_RETRY_DEADLINES=()
STEP_TARGET_TABLE=$'mac\teligible\tallowed\tfixture-settings'
token_wait_clock() { TOKEN_CLOCK_NOW=0; }
count_token_waiters_ahead() { TOKEN_WAITERS_AHEAD=1; }
phase_wait_start() { :; }
phase_wait_finish() { :; }
token_wait_note() { :; }
sleep() { printf 'wait force=%s\n' "$STEP_FORCE_MAC"; exit 0; }
status=0
acquire_token || status=$?
printf 'leave status=%s force=%s\n' "$status" "$STEP_FORCE_MAC"
'''
                            environment = {
                                **os.environ, "CMD": command, "MAC_IDLE_MARKER": marker,
                                "PY": sys.executable, "MAC_TEST_SCRIPT": str(checker), "BUILD_TARGETS_SCRIPT": str(checker),
                                "DOC_REPO_TOP": directory, "TEST_MAC_STATE": state,
                                "TEST_MAC_CHECKS": str(calls),
                            }
                            result = subprocess.run(["bash", "-c", script], env=environment,
                                                    capture_output=True, text=True, check=False, timeout=30)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertEqual(result.stdout.strip(),
                                             "leave status=75 force=1" if state == "free" else "wait force=0")
                            self.assertTrue(calls.exists(), "token waiting never consulted the shared Mac decision")
                            expected_calls = [f"free-now --settings fixture-settings --repo-root {directory}"]
                            if state == "free":
                                expected_calls.append(
                                    f"reserve --repo-root {directory} --kind {command} --package  --pid PID"
                                    + f" --reservation-file {directory}/ssh-reservation --target mac")
                            actual_calls = calls.read_text().splitlines()
                            actual_calls = [re.sub(r"--pid \d+", "--pid PID", call) for call in actual_calls]
                            self.assertEqual(actual_calls, expected_calls)


if __name__ == "__main__":
    _ = unittest.main()
