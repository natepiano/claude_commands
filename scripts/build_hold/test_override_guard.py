"""Override checks against temporary installed files and this checkout."""

from __future__ import annotations

import ast
import io
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import override_guard


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
CHECKOUT_SCRIPT = SCRIPT_DIRECTORY / "build_hold.py"
SHELL_GUARD = SCRIPT_DIRECTORY / "override_guard.sh"
BASH = "/bin/bash"
PYTHON_RELATIVE = "build_hold/build_hold.py"
SHELL_RELATIVE = "direct/verify.sh"
EXPECTED_VARIABLES = (
    "BUILD_HOLD_DIR",
    "BUILD_HOLD_RELEASE_DIR",
    "MAC_TEST_STATE_DIR",
    "MAC_TEST_CONFIG",
    "BUILD_TARGETS_SCRIPT",
    "BUILD_TARGETS_CONFIG",
    "BUILD_TARGETS_NOW",
    "UNIT_ACTIVE_DIR",
    "PLAN_DELEGATE_ACTIVE_DIR",
)
SHELL_CALL = 'source "$1"; build_override_refusal "$2" "$3" "$4"'
READ_ONLY_HOME_LINE = (
    "build_hold.py: HOME is read-only in this shell, so the user's home cannot be read. "
    "Run again from a shell where HOME is not read-only.\n"
)


@dataclass(frozen=True)
class InstalledScript:
    home: Path
    script: Path
    relative: str


def temporary_root(test: unittest.TestCase) -> Path:
    return Path(test.enterContext(tempfile.TemporaryDirectory(prefix="override-guard-")))


def installed_script(root: Path, relative: str) -> InstalledScript:
    home = root / "home"
    script = home / ".claude" / "scripts" / relative
    script.parent.mkdir(parents=True)
    _ = script.write_text("# A file for the installed-file comparison.\n")
    return InstalledScript(home, script, relative)


def script_links(root: Path, script: Path) -> tuple[Path, Path]:
    symbolic = root / "symbolic_name"
    hard = root / "hard_name"
    symbolic.symlink_to(script)
    hard.hardlink_to(script)
    return symbolic, hard


def child_environment(values: Mapping[str, str]) -> dict[str, str]:
    environment = {"PATH": os.defpath, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    environment.update(values)
    return environment


def refusal_line(script: Path, name: str, value: str, home: Path) -> str:
    return (
        f"{script.name}: {name} is set to '{value}'; the installed scripts under "
        f"{home / '.claude' / 'scripts'} do not run with it. Unset it and run again."
    )


def run_shell(program: str, arguments: Sequence[str], environment: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", program, "override-guard-test", str(SHELL_GUARD), *arguments],
        env=dict(environment), capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )


class OverrideGuardPythonTests(unittest.TestCase):
    def test_build_hold_imports_as_a_scripts_package_module(self) -> None:
        _ = temporary_root(self)
        environment = child_environment({})
        self.assertNotIn("PYTHONPATH", environment)
        result = subprocess.run(
            [sys.executable, "-c", "from scripts.build_hold import build_hold"],
            cwd=SCRIPT_DIRECTORY.parent.parent, env=environment,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_installed_script_refuses_each_listed_variable(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        self.assertEqual(override_guard.OVERRIDE_VARIABLES, EXPECTED_VARIABLES)
        for position, name in enumerate(EXPECTED_VARIABLES):
            with self.subTest(name=name):
                environment = {
                    later: str(root / later.lower())
                    for later in reversed(EXPECTED_VARIABLES[position:])
                }
                environment["HOME"] = str(root / "other_home")
                answer = override_guard.refusal(installed.script, installed.relative, installed.home, environment)
                self.assertEqual(answer, override_guard.OverridesRefused(
                    refusal_line(installed.script, name, environment[name], installed.home),
                ))

    def test_installed_script_refuses_an_empty_value(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        answer = override_guard.refusal(installed.script, installed.relative, installed.home, {"BUILD_HOLD_DIR": ""})
        self.assertEqual(answer, override_guard.OverridesRefused(
            refusal_line(installed.script, "BUILD_HOLD_DIR", "", installed.home),
        ))

    def test_installed_script_refuses_a_changed_home(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        changed = root / "other_home"
        changed.mkdir()
        for value in (str(changed), str(root / "absent_home"), ""):
            with self.subTest(home=value):
                answer = override_guard.refusal(installed.script, installed.relative, installed.home, {"HOME": value})
                self.assertEqual(answer, override_guard.OverridesRefused(
                    refusal_line(installed.script, "HOME", value, installed.home),
                ))

    def test_installed_script_runs_when_nothing_is_set(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        same_home = root / "home_link"
        same_home.symlink_to(installed.home, target_is_directory=True)
        for environment in ({}, {"HOME": str(installed.home)}, {"HOME": str(same_home)}):
            with self.subTest(environment=environment):
                self.assertEqual(
                    override_guard.refusal(installed.script, installed.relative, installed.home, environment),
                    override_guard.OverridesAllowed(),
                )

    def test_checkout_script_accepts_every_variable_and_a_changed_home(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        for name in EXPECTED_VARIABLES:
            for value in (str(root / "override"), ""):
                with self.subTest(name=name, value=value):
                    environment = {name: value, "HOME": str(root / "other_home")}
                    self.assertEqual(
                        override_guard.refusal(CHECKOUT_SCRIPT, PYTHON_RELATIVE, installed.home, environment),
                        override_guard.OverridesAllowed(),
                    )

    def test_a_link_to_the_installed_script_is_installed(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        value = str(root / "holders")
        for script in script_links(root, installed.script):
            with self.subTest(link=script.name):
                self.assertEqual(
                    override_guard.refusal(script, installed.relative, installed.home, {"BUILD_HOLD_DIR": value}),
                    override_guard.OverridesRefused(refusal_line(script, "BUILD_HOLD_DIR", value, installed.home)),
                )

    def test_absent_home_is_not_an_override(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        self.assertEqual(
            override_guard.refusal(installed.script, installed.relative, installed.home, {}),
            override_guard.OverridesAllowed(),
        )

    def test_user_home_ignores_HOME(self) -> None:
        _ = temporary_root(self)
        result = subprocess.run(
            [sys.executable, "-c", "import override_guard; print(override_guard.user_home())"],
            cwd=SCRIPT_DIRECTORY, env=child_environment({"HOME": "/nonexistent"}),
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(Path(pwd.getpwuid(os.getuid()).pw_dir)) + "\n")
        self.assertEqual(result.stderr, "")

    def test_refuse_overrides_exits_2_and_prints_the_line(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        value = str(root / "holders")
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(override_guard, "user_home", return_value=installed.home):
            with mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": value}, clear=True):
                with redirect_stdout(output), redirect_stderr(error):
                    with self.assertRaises(SystemExit) as raised:
                        override_guard.refuse_overrides(installed.script, installed.relative)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(error.getvalue(), refusal_line(installed.script, "BUILD_HOLD_DIR", value, installed.home) + "\n")
        self.assertEqual(output.getvalue(), "")

    def test_nothing_set_reads_no_file(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        with mock.patch.object(os.path, "samefile", side_effect=AssertionError("No override needs no file check")):
            self.assertEqual(
                override_guard.refusal(installed.script, installed.relative, installed.home, {}),
                override_guard.OverridesAllowed(),
            )


class OverrideGuardShellTests(unittest.TestCase):
    def test_changed_read_only_home_returns_one_refusal_line(self) -> None:
        root = temporary_root(self)
        result = run_shell(
            'source "$1"; readonly HOME; build_override_refusal "$2" "$3"',
            [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE],
            child_environment({"HOME": str(root / "other_home"), "BUILD_HOLD_DIR": str(root / "holders")}),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, READ_ONLY_HOME_LINE)
        self.assertEqual(result.stdout, "")

    def test_changed_read_only_home_exits_two_with_the_callers_pattern(self) -> None:
        root = temporary_root(self)
        result = run_shell(
            'set -euo pipefail; source "$1"; readonly HOME; build_override_refusal "$2" "$3" || exit 2',
            [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE],
            child_environment({"HOME": str(root / "other_home")}),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, READ_ONLY_HOME_LINE)
        self.assertEqual(result.stdout, "")

    def test_read_only_user_home_returns_one_refusal_line(self) -> None:
        _ = temporary_root(self)
        result = run_shell(
            'source "$1"; readonly HOME; build_override_refusal "$2" "$3"',
            [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE],
            child_environment({"HOME": pwd.getpwuid(os.getuid()).pw_dir}),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, READ_ONLY_HOME_LINE)
        self.assertEqual(result.stdout, "")

    def test_installed_script_refuses_each_listed_variable(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        for position, name in enumerate(EXPECTED_VARIABLES):
            with self.subTest(name=name):
                environment = {
                    later: str(root / later.lower())
                    for later in reversed(EXPECTED_VARIABLES[position:])
                }
                environment["HOME"] = str(root / "other_home")
                result = run_shell(
                    SHELL_CALL, [str(installed.script), installed.relative, str(installed.home)],
                    child_environment(environment),
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, refusal_line(installed.script, name, environment[name], installed.home) + "\n")
                self.assertEqual(result.stdout, "")

    def test_installed_script_refuses_an_empty_value(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        result = run_shell(
            SHELL_CALL, [str(installed.script), installed.relative, str(installed.home)],
            child_environment({"BUILD_HOLD_DIR": ""}),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, refusal_line(installed.script, "BUILD_HOLD_DIR", "", installed.home) + "\n")
        self.assertEqual(result.stdout, "")

    def test_installed_script_refuses_a_changed_home(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        changed = root / "other_home"
        changed.mkdir()
        for value in (str(changed), str(root / "absent_home"), ""):
            with self.subTest(home=value):
                result = run_shell(
                    SHELL_CALL, [str(installed.script), installed.relative, str(installed.home)],
                    child_environment({"HOME": value}),
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, refusal_line(installed.script, "HOME", value, installed.home) + "\n")
                self.assertEqual(result.stdout, "")

    def test_installed_script_runs_when_nothing_is_set(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        same_home = root / "home_link"
        same_home.symlink_to(installed.home, target_is_directory=True)
        for environment in ({}, {"HOME": str(installed.home)}, {"HOME": str(same_home)}):
            with self.subTest(environment=environment):
                result = run_shell(
                    SHELL_CALL, [str(installed.script), installed.relative, str(installed.home)],
                    child_environment(environment),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_checkout_script_accepts_every_variable_and_a_changed_home(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        for name in EXPECTED_VARIABLES:
            for value in (str(root / "override"), ""):
                with self.subTest(name=name, value=value):
                    result = run_shell(
                        SHELL_CALL, [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE, str(installed.home)],
                        child_environment({name: value, "HOME": str(root / "other_home")}),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")

    def test_a_link_to_the_installed_script_is_installed(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        value = str(root / "holders")
        for script in script_links(root, installed.script):
            with self.subTest(link=script.name):
                result = run_shell(
                    SHELL_CALL, [str(script), installed.relative, str(installed.home)],
                    child_environment({"BUILD_HOLD_DIR": value}),
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, refusal_line(script, "BUILD_HOLD_DIR", value, installed.home) + "\n")
                self.assertEqual(result.stdout, "")

    def test_shell_and_python_lists_are_equal(self) -> None:
        _ = temporary_root(self)
        result = run_shell(
            'source "$1"; printf "%s\\n" "$BUILD_OVERRIDE_VARIABLES"', [],
            child_environment({"BUILD_OVERRIDE_VARIABLES": "BUILDLOG_DIR"}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(override_guard.OVERRIDE_VARIABLES, EXPECTED_VARIABLES)
        self.assertEqual(result.stdout.split(), list(override_guard.OVERRIDE_VARIABLES))
        self.assertEqual(result.stderr, "")

    def test_shell_and_python_print_the_same_line(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, SHELL_RELATIVE)
        value = str(root / "holder's files")
        environment = {"BUILD_HOLD_DIR": value}
        expected = refusal_line(installed.script, "BUILD_HOLD_DIR", value, installed.home)
        answer = override_guard.refusal(installed.script, installed.relative, installed.home, environment)
        self.assertEqual(answer, override_guard.OverridesRefused(expected))
        result = run_shell(
            SHELL_CALL, [str(installed.script), installed.relative, str(installed.home)],
            child_environment(environment),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, expected + "\n")
        self.assertEqual(result.stdout, "")

    def test_home_lookup_ignores_HOME(self) -> None:
        _ = temporary_root(self)
        program = 'source "$1"; build_override_refusal "$2" "$3"; result=$?; declare -p HOME; exit "$result"'
        environment = child_environment({"HOME": "/nonexistent"})
        checkout = run_shell(program, [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE], environment)
        self.assertEqual(checkout.returncode, 0, checkout.stderr)
        self.assertEqual(checkout.stdout, 'declare -x HOME="/nonexistent"\n')
        self.assertEqual(checkout.stderr, "")
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        script = home / ".claude" / "scripts" / "lint" / "memory_gate.sh"
        with self.subTest(script="installed"):
            if not script.is_file():
                self.skipTest("The installed memory_gate.sh file is absent.")
            installed = run_shell(program, [str(script), "lint/memory_gate.sh"], environment)
            self.assertEqual(installed.returncode, 1, installed.stderr)
            self.assertEqual(installed.stderr, refusal_line(script, "HOME", "/nonexistent", home) + "\n")
            self.assertEqual(installed.stdout, 'declare -x HOME="/nonexistent"\n')

    def test_absent_home_is_not_an_override_and_stays_absent(self) -> None:
        root = temporary_root(self)
        installed = installed_script(root, PYTHON_RELATIVE)
        program = 'set -euo pipefail; source "$1"; build_override_refusal "$2" "$3" "${4-}"; [[ -z "${HOME+x}" ]]'
        for arguments in (
            [str(installed.script), PYTHON_RELATIVE, str(installed.home)],
            [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE],
        ):
            with self.subTest(script=arguments[0]):
                result = run_shell(
                    program, arguments, child_environment({}),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_nothing_set_starts_no_process(self) -> None:
        root = temporary_root(self)
        empty_path = root / "empty_path"
        empty_path.mkdir()
        environment = child_environment({"PATH": str(empty_path)})
        forks = root / "subshells"
        program = (
            'set -e; source "$1"; '
            'exec 3>"$4"; set -T; '
            'trap \'if (( BASH_SUBSHELL > 0 )); then printf "%s\\n" "$BASH_SUBSHELL" >&3; fi\' DEBUG; '
            'for ((call_number=0; call_number<2000; call_number++)); do '
            'build_override_refusal "$2" "$3"; done; [[ "$call_number" -eq 2000 ]]'
        )
        result = run_shell(program, [str(CHECKOUT_SCRIPT), PYTHON_RELATIVE, str(forks)], environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(forks.read_text(), "", "the override guard must not fork subshells")


def first_function_statement(source: str, function_name: str) -> ast.stmt:
    for statement in ast.parse(source).body:
        if not isinstance(statement, ast.FunctionDef) or statement.name != function_name:
            continue
        body = statement.body
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            body = body[1:]
        if not body:
            raise AssertionError(f"{function_name} has no statement after its docstring")
        return body[0]
    raise AssertionError(f"Function {function_name} is absent")


class GuardCallSiteTests(unittest.TestCase):
    def test_every_guarded_file_calls_the_guard(self) -> None:
        scripts = SCRIPT_DIRECTORY.parent
        for relative, first_read in (
            ("direct/verify.sh", ("HOME=", "$HOME")),
            ("lint/memory_gate.sh", ("BUILD_TARGETS_SCRIPT",)),
        ):
            with self.subTest(file=relative):
                lines = [line.strip() for line in (scripts / relative).read_text().splitlines()]
                call = f'build_override_refusal "${{BASH_SOURCE[0]}}" {relative} || exit 2'
                self.assertIn(call, lines)
                first_read_line = next(
                    number for number, line in enumerate(lines)
                    if any(name in line for name in first_read)
                )
                self.assertLess(lines.index(call), first_read_line)

        for relative, function_name in (
            ("build_hold/build_hold.py", "main"),
            ("build_targets/build_targets.py", "main"),
            ("mac_test/mac_test.py", "main"),
            ("mac_test/offload.py", "main"),
            ("mac_test/audit.py", "main"),
            ("production/dailies_input.py", "main"),
            ("buildlog/rust_release.py", "build_hold_active"),
        ):
            with self.subTest(file=relative, function=function_name):
                statement = first_function_statement((scripts / relative).read_text(), function_name)
                expected = ast.parse(f'override_guard.refuse_overrides(Path(__file__), "{relative}")').body[0]
                self.assertEqual(ast.dump(statement), ast.dump(expected))


if __name__ == "__main__":
    _ = unittest.main()
