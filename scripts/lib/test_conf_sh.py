"""The sourced shell reader uses builtins and delegates faults to the Python reader."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import override
import unittest

from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SHELL_READER = Path(__file__).with_name("conf.sh")


class ShellConfTests(unittest.TestCase):
    shell: tuple[str, ...] = ("/bin/bash", "--noprofile", "--norc")
    root: Path = Path()
    config: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config"
        self.config.mkdir()
        programs = self.root / "bin"
        programs.mkdir()
        (programs / "bash").symlink_to("/bin/bash")
        python = programs / "python3"
        _ = python.write_text(
            '#!/bin/bash\nprintf "python\\n" >> "$FORK_LOG"\n'
            + f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        )
        python.chmod(0o755)
        cache = self.root / "cache"
        seed_interpreter_cache(cache, interpreter=python)
        self.env = {
            "PATH": str(programs), "HOME": str(self.root / "home"),
            "XDG_CACHE_HOME": str(cache),
            "CONF_DIR": str(self.config), "HOSTNAME": socket.gethostname(),
            "FORK_LOG": str(self.root / "python.log"),
            "SUBSHELL_LOG": str(self.root / "subshell.log"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }

    def write(self, contents: str) -> None:
        _ = (self.config / "example.conf").write_text(contents)

    def run_reader(self, key: str, *, trace: bool = False) -> subprocess.CompletedProcess[str]:
        script = 'PATH="$CONF_TEST_PATH"\n'
        if trace:
            script += (
                'if [[ -n "${ZSH_VERSION:-}" ]]; then\n'
                "trap 'if (( ZSH_SUBSHELL > 0 )); then printf child >> \"$SUBSHELL_LOG\"; fi' DEBUG\n"
                "else\nset -T\n"
                "trap 'if (( BASH_SUBSHELL > 0 )); then printf child >> \"$SUBSHELL_LOG\"; fi' DEBUG\nfi\n"
            )
        script += 'source "$1"\nconf_value example "$2"\n'
        return subprocess.run(
            [*self.shell, "-c", script, "conf-test", str(SHELL_READER), key],
            env={**self.env, "CONF_TEST_PATH": self.env["PATH"]},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_normal_integer_enum_and_text_reads_use_no_child_process(self) -> None:
        self.write("# whole: -2..4\ncount=4\n# one of: fast, slow\nmode=fast\npath=a=b\n")
        for key, expected in (("count", "4"), ("mode", "fast"), ("path", "a=b")):
            with self.subTest(key=key):
                result = self.run_reader(key, trace=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected + "\n")
                self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / "python.log").exists())
        self.assertFalse((self.root / "subshell.log").exists())

    def test_sections_comments_first_value_and_short_host_match_the_python_format(self) -> None:
        host = socket.gethostname().split(".", 1)[0]
        self.write(f"[section]\ncount=1\ncount=2\ncount.{host}=7 # here\ncount.{host}=8\n")
        result = self.run_reader("count", trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")
        self.assertFalse((self.root / "python.log").exists())
        self.assertFalse((self.root / "subshell.log").exists())

    def test_host_override_ignores_inherited_host_names(self) -> None:
        host = socket.gethostname().split(".", 1)[0]
        self.write(f"count=1\ncount.{host}=7\n")
        self.env.update(HOSTNAME=host + "-other", HOST=host + "-other")
        result = self.run_reader("count", trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")
        self.assertFalse((self.root / "python.log").exists())
        self.assertFalse((self.root / "subshell.log").exists())

    def test_cr_only_lines_delegate_to_the_python_reader(self) -> None:
        self.write("count=7\rother=9\r")
        expected = subprocess.run(
            [sys.executable, str(SHELL_READER.with_suffix(".py")), "get", "example", "count"],
            env=self.env, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )
        result = self.run_reader("count")
        self.assertEqual(result.returncode, expected.returncode, result.stderr)
        self.assertEqual(result.stdout, expected.stdout)
        self.assertEqual((self.root / "python.log").read_text(), "python\n")

    def test_crlf_lines_use_no_child_process(self) -> None:
        self.write("# whole: 1..9\r\ncount=7\r\nother=9\r\n")
        result = self.run_reader("count", trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")
        self.assertFalse((self.root / "python.log").exists())
        self.assertFalse((self.root / "subshell.log").exists())

    def test_open_whole_number_ranges_are_checked_without_delegation(self) -> None:
        for limits, value in (("..-2", "-3"), ("2..", "9"), ("..", "0")):
            with self.subTest(limits=limits):
                self.write(f"# whole: {limits}\ncount={value}\n")
                result = self.run_reader("count", trace=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, value + "\n")
        self.assertFalse((self.root / "python.log").exists())
        self.assertFalse((self.root / "subshell.log").exists())

    def test_missing_key_bad_enum_and_bad_integer_delegate_and_refuse_without_history(self) -> None:
        for contents in ("other=2\n", "count=   \n", "# one of: fast, slow\ncount=unknown\n",
                         "# whole: 1..4\ncount=9\n", "# whole: 1..4\ncount=bad\n"):
            with self.subTest(contents=contents):
                self.write(contents)
                (self.root / "python.log").unlink(missing_ok=True)
                result = self.run_reader("count")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.root / "python.log").read_text(), "python\n")
                self.assertIn("example.conf", result.stderr)
                self.assertIn("count", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_decimal_range_delegates_even_when_the_value_is_valid(self) -> None:
        for bounds in ("1.5..2.5", "1..4"):
            with self.subTest(bounds=bounds):
                self.write(f"# range: {bounds}\ncount=2\n")
                (self.root / "python.log").unlink(missing_ok=True)
                result = self.run_reader("count")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "2\n")
                self.assertEqual((self.root / "python.log").read_text(), "python\n")

    def test_fault_delegation_recovers_the_committed_value(self) -> None:
        self.write("# whole: 1..4\ncount=3\n")
        available_programs = os.environ.get("PATH", os.defpath)
        environment = {**self.env, "PATH": available_programs}
        for arguments in (("init", "-q"), ("config", "user.name", "Shell test"),
                          ("config", "user.email", "shell@example.invalid"),
                          ("add", "config/example.conf"), ("commit", "-q", "-m", "good value")):
            _ = subprocess.run(["git", *arguments], cwd=self.root, env=environment, check=True,
                               capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.write("# whole: 1..4\ncount=8\n")
        self.env["PATH"] += os.pathsep + available_programs
        result = self.run_reader("count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "3\n")
        self.assertEqual((self.root / "python.log").read_text(), "python\n")
        self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_sourcing_a_bare_filename_resolves_the_fault_reader(self) -> None:
        self.write("# whole: 1..4\ncount=1.5\n")
        result = subprocess.run(
            [*self.shell, "-c",
             'PATH="$CONF_TEST_PATH"; source conf.sh; conf_value example count'],
            cwd=SHELL_READER.parent, env={**self.env, "CONF_TEST_PATH": self.env["PATH"]},
            capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("expected a whole number", result.stderr)
        self.assertIn("example.conf", result.stderr)
        self.assertNotIn("Not a directory", result.stderr)
        self.assertEqual((self.root / "python.log").read_text(), "python\n")

    def test_next_read_observes_changed_file_in_the_same_shell(self) -> None:
        self.write("count=2\n")
        result = subprocess.run(
            [*self.shell, "-c", 'PATH="$CONF_TEST_PATH"; source "$1"; conf_value example count; '
             + 'printf "count=7\\n" > "$CONF_DIR/example.conf"; conf_value example count',
             "conf-test", str(SHELL_READER)],
            env={**self.env, "CONF_TEST_PATH": self.env["PATH"]},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2\n7\n")

    def test_default_folder_uses_the_private_home(self) -> None:
        folder = Path(self.env["HOME"]) / ".claude/config"
        folder.mkdir(parents=True)
        _ = (folder / "example.conf").write_text("count=7\n")
        del self.env["CONF_DIR"]
        result = self.run_reader("count", trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")
        self.assertFalse((self.root / "python.log").exists())


class ZshShellConfTests(ShellConfTests):
    shell: tuple[str, ...] = (shutil.which("zsh") or "/bin/zsh", "-f")

    def test_host_override_uses_the_zsh_host_without_hostname(self) -> None:
        host = socket.gethostname().split(".", 1)[0]
        self.write(f"count=1\ncount.{host}=7\n")
        del self.env["HOSTNAME"]
        self.env["HOST"] = socket.gethostname()
        result = self.run_reader("count", trace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")
        self.assertFalse((self.root / "python.log").exists())


if __name__ == "__main__":
    _ = unittest.main()
