#!/usr/bin/env python3
"""Nextest width selection at the shared cargo invocation boundary."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import final, override


INVOKE = Path(__file__).with_name("invoke.sh")
NO_ENVIRONMENT_OVERRIDES: Mapping[str, str] = {}


def shell_lines(*lines: str) -> str:
    return "\n".join(lines)


@final
class NextestWidthTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        self.write_executable(
            fake_bin / "cargo",
            shell_lines("#!/usr/bin/env bash", "printf '<%s>' \"$@\"", "printf '\\n'", ""),
        )
        self.write_executable(
            fake_bin / "getconf",
            shell_lines(
                "#!/usr/bin/env bash",
                "[[ \"${1:-}\" == _NPROCESSORS_ONLN ]] || exit 1",
                "printf '%s\\n' \"${FAKE_LOGICAL_CORES:-6}\"",
                "",
            ),
        )
        config_reader = self.root / "lint_config.sh"
        _ = config_reader.write_text(
            shell_lines(
                "lint_config_enabled() { return 0; }",
                "lint_config_skip_notice() { :; }",
                "",
            ),
            encoding="utf-8",
        )
        buildlog = self.root / "buildlog"
        buildlog.mkdir()
        self.environment = {
            **os.environ,
            "BUILDLOG_DIR": str(buildlog),
            "BUILDLOG_OFF": "1",
            "LINT_CONFIG_READER": str(config_reader),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
        self.sysfs = self.root / "sysfs"
        self.write_topology(self.sysfs, [(0, 0), (0, 0), (1, 0), (1, 0)])
        self.other_sysfs = self.root / "other-sysfs"
        self.write_topology(self.other_sysfs, [(0, 0), (0, 1), (0, 2)])

    @staticmethod
    def write_executable(path: Path, contents: str) -> None:
        _ = path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def write_topology(root: Path, identities: list[tuple[int, int]]) -> None:
        for cpu, (package, core) in enumerate(identities):
            topology = root / f"cpu{cpu}" / "topology"
            topology.mkdir(parents=True)
            _ = (topology / "physical_package_id").write_text(f"{package}\n", encoding="utf-8")
            _ = (topology / "core_id").write_text(f"{core}\n", encoding="utf-8")

    def run_shell(
        self,
        body: str,
        *arguments: str,
        extra: Mapping[str, str] = NO_ENVIRONMENT_OVERRIDES,
    ) -> subprocess.CompletedProcess[str]:
        script = f"""
set -euo pipefail
OSTYPE=linux-gnu
source "$1"
shift
run() {{
    printf '<%s>' "$@"
    printf '\\n'
    return "${{RUN_STATUS:-0}}"
}}
{body}
"""
        return subprocess.run(
            ["bash", "-c", script, "nextest-width-test", str(INVOKE), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={**self.environment, **extra},
        )

    def assert_success(self, result: subprocess.CompletedProcess[str], expected: str) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, expected)
        self.assertEqual(result.stderr, "")

    def test_physical_cores_are_distinct_package_and_core_pairs_and_cached(self) -> None:
        result = self.run_shell(
            shell_lines(
                'nextest_physical_cores "$1"',
                'printf "%s\\n" "$LINT_PHYSICAL_CORES"',
                'nextest_physical_cores "$2"',
                'printf "%s\\n" "$LINT_PHYSICAL_CORES"',
            ),
            str(self.sysfs),
            str(self.other_sysfs),
        )

        self.assert_success(result, "2\n2\n")

    def test_logical_cores_are_read_once_and_cached(self) -> None:
        result = self.run_shell(
            shell_lines(
                "nextest_logical_cores",
                'printf "%s\\n" "$LINT_LOGICAL_CORES"',
                "FAKE_LOGICAL_CORES=9",
                "nextest_logical_cores",
                'printf "%s\\n" "$LINT_LOGICAL_CORES"',
            ),
        )

        self.assert_success(result, "6\n6\n")

    def test_trial_arm_uses_100_minute_block_parity(self) -> None:
        result = self.run_shell(
            shell_lines(
                "for epoch in 0 5999 6000 12000; do",
                '    nextest_width_arm "$epoch"',
                '    printf "%s\\n" "$LINT_TEST_WIDTH_ARM"',
                "done",
            ),
        )

        self.assert_success(result, "physical\nphysical\nlogical\nphysical\n")

    def test_trial_mode_uses_physical_width_in_an_even_block(self) -> None:
        result = self.run_shell(
            shell_lines(
                'nextest_physical_cores "$1"',
                "unset EPOCHSECONDS",
                "EPOCHSECONDS=0",
                "LINT_TEST_WIDTH=trial",
                "run_nextest --workspace",
            ),
            str(self.sysfs),
        )

        self.assert_success(
            result,
            "<cargo><nextest><run><--test-threads><2><--workspace>\n",
        )

    def test_trial_mode_uses_logical_width_in_an_odd_block(self) -> None:
        result = self.run_shell(
            shell_lines(
                "unset EPOCHSECONDS",
                "EPOCHSECONDS=6000",
                "LINT_TEST_WIDTH=trial",
                "run_nextest --workspace",
            ),
        )

        self.assert_success(
            result,
            "<cargo><nextest><run><--test-threads><6><--workspace>\n",
        )

    def test_fixed_width_modes_select_the_named_core_count(self) -> None:
        cases = (("physical", "2"), ("logical", "6"))
        for mode, width in cases:
            with self.subTest(mode=mode):
                result = self.run_shell(
                    shell_lines(
                        'nextest_physical_cores "$1"',
                        f"LINT_TEST_WIDTH={mode}",
                        "run_nextest --workspace",
                    ),
                    str(self.sysfs),
                )
                self.assert_success(
                    result,
                    f"<cargo><nextest><run><--test-threads><{width}><--workspace>\n",
                )

    def test_caller_width_flags_are_kept_without_an_added_width(self) -> None:
        cases = (
            ("--test-threads 3", "<--test-threads><3>"),
            ("--test-threads=3", "<--test-threads=3>"),
            ("-j 4", "<-j><4>"),
            ("-j4", "<-j4>"),
        )
        for arguments, rendered_arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_shell(
                    shell_lines(
                        'nextest_physical_cores "$1"',
                        "LINT_TEST_WIDTH=physical",
                        f"run_nextest {arguments}",
                    ),
                    str(self.sysfs),
                )
                self.assert_success(
                    result,
                    f"<cargo><nextest><run>{rendered_arguments}\n",
                )

    def test_environment_width_is_kept_without_an_added_width(self) -> None:
        result = self.run_shell(
            shell_lines(
                'nextest_physical_cores "$1"',
                "LINT_TEST_WIDTH=physical",
                "run_nextest --workspace",
            ),
            str(self.sysfs),
            extra={"NEXTEST_TEST_THREADS": "5"},
        )

        self.assert_success(result, "<cargo><nextest><run><--workspace>\n")

    def test_width_flag_precedes_the_test_binary_separator(self) -> None:
        result = self.run_shell(
            shell_lines(
                'nextest_physical_cores "$1"',
                "LINT_TEST_WIDTH=physical",
                "run_nextest -- --nocapture",
            ),
            str(self.sysfs),
        )

        self.assert_success(
            result,
            "<cargo><nextest><run><--test-threads><2><--><--nocapture>\n",
        )

    def test_test_binary_width_flags_do_not_override_nextest_width(self) -> None:
        cases = ("--test-threads=16", "-j 3")
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_shell(
                    shell_lines(
                        'nextest_physical_cores "$1"',
                        "LINT_TEST_WIDTH=physical",
                        f"run_nextest -- {arguments}",
                    ),
                    str(self.sysfs),
                )
                rendered_arguments = "".join(f"<{argument}>" for argument in arguments.split())
                self.assert_success(
                    result,
                    f"<cargo><nextest><run><--test-threads><2><-->{rendered_arguments}\n",
                )

    def test_unreadable_core_count_adds_no_flag_and_preserves_status(self) -> None:
        result = self.run_shell(
            shell_lines(
                'nextest_physical_cores "$1"',
                "LINT_TEST_WIDTH=physical",
                "status=0",
                "run_nextest --workspace || status=$?",
                'printf "status=%s\\n" "$status"',
            ),
            str(self.root / "missing-sysfs"),
            extra={"RUN_STATUS": "7"},
        )

        self.assert_success(result, "<cargo><nextest><run><--workspace>\nstatus=7\n")


if __name__ == "__main__":
    _ = unittest.main()
