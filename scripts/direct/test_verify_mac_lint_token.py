"""Mac-allowed lint leaves token waiting through the test routing decision."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.lib.shell_test_support import shell_functions
from scripts.direct.verify_lint_scope_test_support import VERIFY


class VerifyMacLintTokenTests(unittest.TestCase):
    def test_token_wait_uses_shared_free_now_without_an_idle_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checker = root / "mac.py"
            _ = checker.write_text('''import os
from pathlib import Path
import sys
if sys.argv[1] == "reserve":
    Path(os.environ["TEST_RESERVATIONS"]).write_text(" ".join(sys.argv[1:]))
    raise SystemExit(0)
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
                            reservations = root / "reservations"
                            reservations.unlink(missing_ok=True)
                            script = 'set -euo pipefail\n' + shell_functions(VERIFY, "mac_retry_hook", "target_for_settings", "target_platform", "step_can_retry", "remote_step_kind", "ssh_reserve_hook", "ssh_retry_hook", "sync_token_state", "acquire_token")
                            script += r'''
TOKEN_USED=1 TOKEN_HELD=0 TOKEN_WAITER_FILE="" TOKEN_FIRST_WAIT_DONE=0
TOKEN_SUPERSEDED=0 TOKEN_WAIT_NOTE_MIN=1 TOKEN_CLOCK_NOW=0
BOARD_DIR=unused STEP_RUNS_ON=any_target STEP_FORCE_MAC=0 LOCAL_ONLY=0
PKG=sample HOLD_WAIT_DIR=$DOC_REPO_TOP
STEP_TARGET_TABLE=$'mac\teligible\t-\tfixture-settings\tmacos'
SSH_RETRYABLE_REASON=busy SSH_RETRY_AFTER_SETTINGS=""
SSH_RETIRED_SETTINGS=() SSH_RETRY_SETTINGS=() SSH_RETRY_DEADLINES=()
MAC_LEAVE_SETTINGS=fixture-settings MAC_RETRY_AFTER=0 LINT_LEFT_QUEUE_STATUS=75
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
                                "PY": sys.executable, "MAC_TEST_SCRIPT": str(checker),
                                "BUILD_TARGETS_SCRIPT": str(checker), "TEST_RESERVATIONS": str(reservations),
                                "DOC_REPO_TOP": directory, "TEST_MAC_STATE": state,
                                "TEST_MAC_CHECKS": str(calls),
                            }
                            result = subprocess.run(["bash", "-c", script], env=environment,
                                                    capture_output=True, text=True, check=False, timeout=30)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertEqual(result.stdout.strip(),
                                             "leave status=75 force=1" if state == "free" else "wait force=0")
                            self.assertEqual(reservations.exists(), state == "free")
                            self.assertTrue(calls.exists(), "token waiting never consulted the shared Mac decision")
                            self.assertEqual(calls.read_text().splitlines(), [
                                f"free-now --settings fixture-settings --repo-root {directory}",
                            ])


if __name__ == "__main__":
    _ = unittest.main()
