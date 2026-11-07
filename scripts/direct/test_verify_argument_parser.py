"""Call the routing parser without a complete verify run."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyArgumentParserTests(unittest.TestCase):
    def test_parser_accepts_routing_flags_and_refuses_a_missing_reason(self) -> None:
        script = shell_functions(Path(__file__).with_name("verify.sh"), "parse_routing_arguments") + '''
CMD=test
VERIFY_CALL_ENVIRONMENT=()
parse_routing_arguments "$@"
set -- "${ARGS[@]}"
printf '%s\\n' "$MAC_ONLY" "$MAC_REASON" "$LOCAL_ONLY" "$NO_CACHE" "$PINNED_TARGET" "$@"
'''
        with tempfile.TemporaryDirectory() as directory:
            accepted = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script, "parser-test",
                 "--mac", "macOS graphics", "--no-cache", "sample", "exact test"],
                cwd=directory, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stdout.splitlines(),
                             ["1", "macOS graphics", "0", "1", "", "sample", "exact test"])
            self.assertEqual(accepted.stderr, "")
            refused = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", script, "parser-test", "sample", "--mac"],
                cwd=directory, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stdout, "")
            self.assertEqual(refused.stderr,
                             'verify.sh: --mac needs a reason: --mac "<why this command must run on macOS>".\n')


if __name__ == "__main__":
    _ = unittest.main()
