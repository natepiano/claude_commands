"""Read assigned platforms and reject unknown SSH destinations."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts.build_targets import target_config

from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")


class VerifyTargetPlatformTests(unittest.TestCase):
    def platform(self, table: str, runs_on: str, target: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            bash = shutil.which("bash")
            assert bash is not None
            root = Path(directory)
            rows = [line.split("\t") for line in table.splitlines()]
            names = [row[0] for row in rows] or ["natedev"]
            settings = ["targets=" + ",".join(names)]
            for row in rows or [["natedev", "eligible", "-", "-", "-"]]:
                name, _, _, config, platform = row
                remote = config != "-"
                settings.extend((f"kind.{name}={'ssh' if remote else 'local'}",
                                 f"platform.{name}={platform if remote else 'linux'}"))
                if remote:
                    source = root / (name + ".conf")
                    source.touch()
                    settings.append(f"settings.{name}={source.name}")
            _ = (root / "build_targets.conf").write_text("\n".join(settings) + "\n")
            with mock.patch.dict(os.environ, {"CONF_DIR": directory}):
                try:
                    assigned = target_config.configured_ssh_target(target)
                except target_config.ConfigError:
                    return subprocess.CompletedProcess(["assigned-info", target], 1, "", "")
            environment = {
                "HOME": directory, "PATH": directory,
                "PY": str(Path(directory) / "no-python"),
                "BUILD_TARGETS_SCRIPT": str(Path(directory) / "no-build-targets.py"),
                "STEP_ASSIGNED_PLATFORM": assigned.platform, "STEP_ASSIGNED_TARGET": assigned.name,
                "STEP_RUNS_ON": runs_on,
            }
            script = shell_functions(VERIFY, "target_platform") + '\ntarget_platform "$1"\n'
            return subprocess.run(
                [bash, "--noprofile", "--norc", "-c", script, "platform-test", target],
                cwd=directory, env=environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS,
            )

    def test_macos_row_supplies_the_platform_without_an_interpreter(self) -> None:
        result = self.platform("linux-box\teligible\t-\t/linux.conf\tlinux\n"
                               + "mac-box\teligible\t-\t/mac.conf\tmacos", "any_target", "mac-box")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "macos\n")
        self.assertEqual(result.stderr, "")

    def test_linux_row_supplies_the_platform_without_an_interpreter(self) -> None:
        result = self.platform("mac-box\texcluded\tbusy\t/mac.conf\tmacos\n"
                               + "linux-box\teligible\t-\t/linux.conf\tlinux", "any_target", "linux-box")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "linux\n")
        self.assertEqual(result.stderr, "")

    def test_local_row_supplies_no_remote_platform(self) -> None:
        result = self.platform("natedev\teligible\t-\t-\t-", "any_target", "natedev")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_missing_mac_row_has_no_platform(self) -> None:
        result = self.platform("", "any_target", "mac")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_missing_linux_row_has_no_platform(self) -> None:
        result = self.platform("", "any_target", "aws")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    _ = unittest.main()
