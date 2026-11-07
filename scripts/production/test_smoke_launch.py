"""Smoke builds use the checkout configuration from any caller folder."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.git_test_support import commit_repository


COMMAND = Path(__file__).with_name("smoke_launch.sh")


class SmokeLaunchTests(unittest.TestCase):
    def test_both_cargo_calls_use_the_checkout_from_an_external_caller(self) -> None:
        self.assert_checkout_cargo(relative=False)

    def test_relative_checkout_uses_absolute_paths_for_both_cargo_calls(self) -> None:
        self.assert_checkout_cargo(relative=True)

    def assert_checkout_cargo(self, *, relative: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="smoke-launch-") as temporary:
            root = Path(temporary)
            checkout = root / "checkout with spaces"
            caller = root / "caller"
            programs = root / "programs"
            home = root / "home"
            for directory in (checkout, caller, programs, home):
                directory.mkdir()
            for name in ("bash", "git", "jq", "dirname", "date", "tee"):
                executable = shutil.which(name)
                if executable is None:
                    self.fail(f"The fixture needs {name}")
                (programs / name).symlink_to(executable)

            records = root / "commands.tsv"
            target = root / "missing target"
            environment = {
                "PATH": str(programs),
                "HOME": str(home),
                "CLAUDE_PYTHON": sys.executable,
                "USER_ZONE": "UTC",
                "GIT_CONFIG_NOSYSTEM": "1",
                "SMOKE_TEST_RECORDS": str(records),
                "SMOKE_TEST_TARGET": str(target),
            }

            def run(arguments: list[str], folder: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(arguments, cwd=folder, env=environment, text=True,
                                      capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)

            initialized = run(["git", "init", "--template=", str(checkout)], caller)
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            head = commit_repository(checkout, "Seed the smoke checkout", allow_empty=True, env=environment)

            commands = {
                "uname": '''printf 'uname\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
printf 'Linux\n'
''',
                "direnv": '''printf 'direnv\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
[[ "$1" == exec ]] || exit 2
shift 2
exec "$@"
''',
                "cargo": '''printf 'cargo\t%s\t%s\n' "$PWD" "$*" >> "$SMOKE_TEST_RECORDS"
case "$1" in
    build) exit 0 ;;
    metadata) printf '{"target_directory":"%s"}\n' "$SMOKE_TEST_TARGET" ;;
    *) exit 2 ;;
esac
''',
                # The fixture never connects to a port.
                "curl": "exit 7\n",
            }
            for name, body in commands.items():
                program = programs / name
                _ = program.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
                program.chmod(0o755)

            log = root / "smoke.log"
            argument = str(Path("..") / checkout.name) if relative else str(checkout)
            result = run(["bash", str(COMMAND), argument, head, str(log)], caller)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            failure = f"SMOKE FAILED: no binary at {target}/debug/hana"
            self.assertIn(failure, result.stdout)
            self.assertIn(failure, log.read_text())
            calls = [line.split("\t", 2) for line in records.read_text().splitlines()]
            cargo = [call for call in calls if call[0] == "cargo"]
            self.assertEqual(cargo, [
                ["cargo", str(checkout),
                 f"build --manifest-path {checkout}/Cargo.toml -p hana --bin hana"],
                ["cargo", str(checkout),
                 f"metadata --no-deps --format-version 1 --manifest-path {checkout}/Cargo.toml"],
            ])
            self.assertEqual([call[1] for call in calls if call[0] == "direnv"],
                             [str(checkout), str(checkout)])
            self.assertEqual([call[1] for call in calls if call[0] == "uname"], [str(caller)])


if __name__ == "__main__":
    _ = unittest.main()
