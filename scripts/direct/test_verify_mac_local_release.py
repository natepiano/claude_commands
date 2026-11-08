"""Real verify.sh Mac local release with disposable tools."""
from __future__ import annotations

import unittest
import os
from pathlib import Path
import subprocess
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacLocalReleaseTests(VerifyMacOffloadFixture):
    def test_renewer_cleanup_keeps_inherited_jobs(self) -> None:
        functions = shell_functions(Path(__file__).with_name("verify.sh"),
                                    "start_token_renewer", "stop_token_renewer")
        for renewal_side_job in (False, True):
            with self.subTest(renewal_side_job=renewal_side_job):
                ready, announce = os.pipe()
                try:
                    environment = {**self.environment, "TEST_FIRST_NAP": str(
                        self.runner_state / f"first-nap-{renewal_side_job}")}
                    script = functions + f'''
set -euo pipefail
TOKEN_HELD=1 TOKEN_RENEW_S=300 TOKEN_RENEWER_PID=""
renew_token() {{
    if [[ "$BASHPID" != "$$" && {'1' if renewal_side_job else '0'} == 1 ]]; then
        command sleep 300 &
        printf '%s\\n' "$!" >&{announce}
    fi
    return 0
}}
sleep() {{
    if [[ ! -f "$TEST_FIRST_NAP" ]]; then
        : > "$TEST_FIRST_NAP"
        return 0
    fi
    printf '%s\\n' "$BASHPID" >&{announce}
    exec env sleep "$@"
}}
{'true' if renewal_side_job else 'command sleep 300 & other=$!'}
start_token_renewer
{'IFS= read -r other' + ' <&' + str(ready) if renewal_side_job else 'true'}
IFS= read -r renewal_child <&{ready}
stop_token_renewer
kill -0 "$other"
if kill -0 "$renewal_child" 2>/dev/null; then exit 1; fi
kill "$other"
wait "$other" 2>/dev/null || true
printf 'owned child stopped; inherited job kept\\n'
'''
                    process = subprocess.Popen(
                        ["bash", "-c", script], env=environment,
                        pass_fds=(ready, announce), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, start_new_session=True)
                    self.addCleanup(self.stop_verify, process)
                    output, errors = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, output + errors)
                    self.assertEqual(output, "owned child stopped; inherited job kept\n")
                finally:
                    os.close(ready)
                    os.close(announce)

    def test_local_release_wins_before_another_mac_attempt(self) -> None:
        for released_by_command in (False, True):
            with self.subTest(released_by_command=released_by_command):
                # This test owns a separate fixture for each release variant.
                fixture = VerifyMacOffloadFixture("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                cycle = fixture.routing_fixture()
                process = fixture.start_routing()
                if released_by_command:
                    fixture.release_local_hold()
                else:
                    (Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").unlink()
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(len(fixture.runner_events()), 1)
                self.assertEqual(len(fixture.local_runs()), 1)
                self.assertFalse((Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
                fixture.assert_no_pending_calls(cycle)
                self.assertEqual(fixture.routing_record()["target"], "natedev")


if __name__ == "__main__":
    _ = unittest.main()
