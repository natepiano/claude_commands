"""Keep group signals behind the shared pid check, including shell callers."""

from __future__ import annotations

import ast
from collections.abc import Callable
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import cast, override
from unittest.mock import MagicMock, patch

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib import process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment


def shell_group_kills(line: str) -> bool:
    """Find group operands, including commands quoted for a nested shell."""
    if "kill" not in line:
        return False
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return False  # A continued shell string is not a complete command line.
    command_position = True
    for position, word in enumerate(words):
        if word in {";", "&", "&&", "|", "||", "(", ")"}:
            command_position = True
            continue
        if not command_position:
            continue
        if word in {"if", "then", "while", "until", "do", "!", "exec", "command", "builtin", "env", "nohup", "sudo"}:
            continue
        if re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", word):
            continue
        command_position = False
        if Path(word).name in {"bash", "sh", "zsh", "dash", "ksh"}:
            for offset, option in enumerate(words[position + 1:], position + 1):
                if not option.startswith("-"):
                    break
                if "c" in option[1:] and offset + 1 < len(words):
                    if shell_group_kills(words[offset + 1]):
                        return True
                    break
        if Path(word).name != "kill":
            continue
        arguments = iter(words[position + 1:])
        options = True
        first = True
        for argument in arguments:
            if argument in {";", "&", "&&", "|", "||", ")"}:
                break
            if options and argument == "--":
                options = False
                continue
            if options and argument in {"-s", "-n"}:
                _ = next(arguments, "")
                first = False
                continue
            if options and first and re.fullmatch(r"-(?:[A-Za-z]+|\d+)", argument):
                first = False
                continue
            first = False
            if argument == "0" or re.match(r"^-(?:\d|\$|`)", argument):
                return True
    return False


def unguarded_group_signals(scripts: Path) -> list[str]:
    """Inspect source without executing any candidate signal."""
    findings: list[str] = []
    for path in sorted(scripts.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".sh", ".bash"}:
            with path.open("rb") as candidate:
                header = candidate.readline()
            if not header.startswith(b"#!"):
                continue
        relative = path.relative_to(scripts)
        source = path.read_text(encoding="utf-8")
        lines: set[int] = set()
        if path.suffix == ".py" or (source.startswith("#!") and "python" in source.partition("\n")[0]):
            tree = ast.parse(source, filename=str(path))
            direct_names = {"killpg"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "killpg":
                            direct_names.add(alias.asname or alias.name)
                            lines.add(node.lineno)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "killpg":
                    lines.add(node.lineno)
                elif isinstance(node, ast.Name) and node.id in direct_names:
                    lines.add(node.lineno)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if any(shell_group_kills(line) or re.search(r"\.\s*killpg\s*\(", line)
                           for line in node.value.replace("\\\n", "").splitlines()):
                        lines.add(node.lineno)
        else:
            for number, line in enumerate(source.replace("\\\n", "").splitlines(), 1):
                if (re.search(r"(?:\.\s*|\b)killpg\s*\(|\bfrom\s+os\s+import\s+killpg\b", line)
                        or shell_group_kills(line)):
                    lines.add(number)
        if relative == Path("lib/process_group.py"):
            allowed = next((node for node in ast.walk(ast.parse(source))
                            if isinstance(node, ast.FunctionDef) and node.name == "send_process_group"), None)
            if allowed is not None:
                send_line = next((node.lineno for node in ast.walk(allowed)
                                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                  and isinstance(node.func.value, ast.Name)
                                  and node.func.value.id == "os" and node.func.attr == "killpg"), 0)
                lines.discard(send_line)
        findings.extend(f"{relative}:{number}" for number in sorted(lines))
    return findings


class PrivateGroupFixture(unittest.TestCase):
    def private_child(self) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, start_new_session=True, text=True,
        )
        self.addCleanup(child.communicate, timeout=TEST_HANG_GUARD_SECS)
        return child


class ProcessGroupTests(PrivateGroupFixture):
    def test_invalid_ids_raise_without_sending_a_signal(self) -> None:
        invalid: tuple[object, ...] = (MagicMock(), True, False, 0, 1, -2, os.getpgrp())
        with patch.object(os, "killpg") as send:
            for pid in invalid:
                with self.subTest(pid=repr(pid)):
                    with self.assertRaises(ValueError):
                        _ = process_group.check_process_group(pid)
                    with self.assertRaises(ValueError):
                        process_group.send_process_group(pid, signal.SIGTERM)
                    send.assert_not_called()

    def test_private_child_group_reaches_the_os_send(self) -> None:
        child = self.private_child()
        with patch.object(os, "killpg") as send:
            process_group.send_process_group(child.pid, signal.SIGTERM)
            send.assert_called_once_with(child.pid, signal.SIGTERM)

    def test_cli_refuses_invalid_ids_without_sending_a_signal(self) -> None:
        invalid: tuple[object, ...] = (MagicMock(), True, False, 0, 1, -2, os.getpgrp())
        with patch.object(os, "killpg") as send, redirect_stderr(io.StringIO()):
            for pid in invalid:
                with self.subTest(pid=repr(pid)):
                    with patch.object(sys, "argv", ["process_group.py", str(pid), "TERM"]):
                        with self.assertRaises(SystemExit) as refused:
                            _ = runpy.run_path(str(Path(process_group.__file__)), run_name="__main__")
                    self.assertEqual(refused.exception.code, 2)
                    send.assert_not_called()

    def test_cli_sends_a_named_signal_to_a_private_child_group(self) -> None:
        child = self.private_child()
        with patch.object(os, "killpg") as send:
            self.assertEqual(process_group.main([str(child.pid), "KILL"]), 0)
            send.assert_called_once_with(child.pid, signal.SIGKILL)


class ShellProcessGroupTests(PrivateGroupFixture):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        scripts = Path(__file__).resolve().parents[1]
        for name in ("direct/board.sh", "direct/verify.sh", "lib/py", "lib/process_group.py",
                     "lib/process_start.py", "lib/conf.sh", "lib/conf.py"):
            destination = self.root / "scripts" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(scripts / name, destination)
        home = self.root / "home"
        home.mkdir()
        self.environment = {**os.environ, **config_environment(self.root / "config"),
                            "HOME": str(home), **interpreter_cache_environment(self.root / "cache"),
                            "VERIFY_PROCESS_GROUP_LIB": str(self.root / "scripts/lib")}

    def release_child_group(self, child: subprocess.Popen[str]) -> subprocess.CompletedProcess[str]:
        board = self.root / "scripts/direct/board.sh"
        session = self.root / "session"
        acquired = subprocess.run(
            ["bash", str(board), "acquire", str(session), "impl", "cargo", "--pid", str(child.pid)],
            env=self.environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(acquired.returncode, 0, acquired.stdout + acquired.stderr)
        stat = Path(f"/proc/{child.pid}/stat")
        if stat.exists():
            start = stat.read_text().rsplit(") ", 1)[1].split()[19]
        else:
            start = subprocess.check_output(["ps", "-p", str(child.pid), "-o", "lstart="], text=True).strip()
        _ = (session / "locks/cargo.d/owned").write_text(json.dumps({"group": child.pid, "start": start}))
        return subprocess.run(
            ["bash", str(board), "release", str(session), "impl", "cargo", "--pid", str(child.pid)],
            env=self.environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )

    def forward(self) -> Callable[[int, object], None]:
        """Load the real embedded handler without executing its fork or signals."""
        source = (self.root / "scripts/direct/verify.sh").read_text()
        embedded = source.split("STEP_OWNER_PY='", 1)[1].split("\n'\n", 1)[0]
        tree = ast.parse(embedded)
        statements = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "forward"]
        self.assertEqual(len(handlers), 1)
        module = ast.Module(body=[*statements, handlers[0]], type_ignores=[])
        namespace: dict[str, object] = {"child": self.private_child().pid}
        exec(compile(module, str(self.root / "scripts/direct/verify.sh"), "exec"), namespace)
        return cast(Callable[[int, object], None], namespace["forward"])

    def fake_helper(self, status: int) -> None:
        _ = (self.root / "scripts/lib/process_group.py").write_text(
            "import pathlib, sys\n"
            + f"pathlib.Path({str(self.root / 'helper-arguments')!r}).write_text(' '.join(sys.argv[1:]))\n"
            + "print('fixture helper diagnostic', file=sys.stderr)\n"
            + f"raise SystemExit({status})\n")

    def cancel_step(self, function: str, child: subprocess.Popen[str]) -> subprocess.CompletedProcess[str]:
        source = (self.root / "scripts/direct/verify.sh").read_text()
        definition = re.search(rf"^{function}\(\) \{{\n.*?^\}}", source, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(definition)
        assert definition is not None
        watch = self.root / "watch"
        watch.mkdir(exist_ok=True)
        driver = """
RUN_STEP_PID="$1"
STEP_RECORD_SCRIPT="$2"
TREE_WATCH_DIR="$3"
PY="$HOME/no-helper"
TREE_CHANGED_STATUS=77
CMD=check
RUN_STARTED=0
after_tree_change() { return 0; }
stop_tree_watch() { :; }
release_token() { :; }
note_event() { :; }
interrupt_hold_wait() { :; }
verify_cleanup() { :; }
final_pass_finish() { :; }
"""
        return subprocess.run(
            ["bash", "-c", driver + definition.group() + f"\n{function} 15\n", "fixture",
             str(child.pid), str(self.root / "scripts/direct/step_record.py"), str(watch)],
            env=self.environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_copied_verify_cancellation_uses_the_local_helper(self) -> None:
        self.fake_helper(0)
        child = self.private_child()
        for function, status, signum in (("cancel_tree_run", 77, "KILL"), ("verify_interrupted", 143, "15")):
            with self.subTest(function=function):
                result = self.cancel_step(function, child)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual((self.root / "helper-arguments").read_text(), f"{child.pid} {signum}")
                self.assertNotIn("process group helper failed", result.stderr)

    def test_verify_cancellation_reports_missing_or_refused_local_helpers(self) -> None:
        child = self.private_child()
        for function in ("cancel_tree_run", "verify_interrupted"):
            for failure in ("missing", "refused"):
                with self.subTest(function=function, failure=failure):
                    self.fake_helper(2)
                    if failure == "missing":
                        (self.root / "scripts/lib/process_group.py").unlink()
                    result = self.cancel_step(function, child)
                    self.assertIn("process group helper failed", result.stderr)
                    self.assertIsNone(child.poll(), "a refused helper must not fall back to an unchecked signal")

    def test_copied_board_releases_its_private_group_without_a_home_helper(self) -> None:
        child = self.private_child()
        result = self.release_child_group(child)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "session/locks/cargo.d").exists())
        self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)

    def test_board_helper_failure_keeps_the_claim_and_child_and_reports_the_cause(self) -> None:
        for failure in ("missing", "refused"):
            with self.subTest(failure=failure):
                self.fake_helper(2)
                if failure == "missing":
                    (self.root / "scripts/lib/process_group.py").unlink()
                child = self.private_child()
                result = self.release_child_group(child)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("process group helper failed", result.stderr)
                self.assertIn("process_group.py" if failure == "missing" else "fixture helper diagnostic", result.stderr)
                self.assertTrue((self.root / "session/locks/cargo.d").exists())
                self.assertIsNone(child.poll(), "helper refusal must not signal the individual child")
                shutil.rmtree(self.root / "session")

    def test_verify_forward_uses_its_local_helper_without_an_individual_signal(self) -> None:
        self.fake_helper(0)
        forward = self.forward()
        with patch.dict(os.environ, self.environment), patch.object(os, "kill") as individual:
            forward(signal.SIGTERM, None)
        individual.assert_not_called()
        self.assertTrue((self.root / "helper-arguments").read_text().endswith(f" {signal.SIGTERM}"))

    def test_verify_helper_failure_reports_the_cause_without_individual_fallback(self) -> None:
        forward = self.forward()
        for failure in ("missing", "refused"):
            with self.subTest(failure=failure):
                self.fake_helper(2)
                if failure == "missing":
                    (self.root / "scripts/lib/process_group.py").unlink()
                with patch.dict(os.environ, self.environment), patch.object(os, "kill") as individual:
                    with self.assertRaises(SystemExit) as refused:
                        forward(signal.SIGTERM, None)
                individual.assert_not_called()
                self.assertIn("process group helper failed", str(refused.exception))
                self.assertIn("process_group.py" if failure == "missing" else "fixture helper diagnostic", str(refused.exception))

    def test_verify_only_uses_individual_fallback_for_a_missing_group(self) -> None:
        self.fake_helper(3)
        forward = self.forward()
        with patch.dict(os.environ, self.environment), patch.object(os, "kill") as individual:
            forward(signal.SIGTERM, None)
        arguments = (self.root / "helper-arguments").read_text().split()
        individual.assert_called_once_with(int(arguments[0]), signal.SIGTERM)


class ProcessGroupSourceTests(unittest.TestCase):
    def test_scripts_route_every_group_signal_through_the_shared_check(self) -> None:
        self.assertEqual(unguarded_group_signals(Path(__file__).resolve().parents[1]), [])

    def test_guard_finds_planted_python_shell_and_heredoc_sends(self) -> None:
        python_send = "os." + "killpg"
        shell_send = "ki" + "ll"
        cases = {
            "raw.py": f"import os\n{python_send}(pid, 15)\n",
            "alias.py": "import os as platform\nplatform." + "killpg(pid, 15)\n",
            "direct.py": "from os import " + "killpg as send\nsend(pid, 15)\n",
            "raw.sh": f'{shell_send} -TERM -- "-$CHILD_PID"\n',
            "numeric.sh": f"{shell_send} -9 -12345\n",
            "continued.sh": f'{shell_send} -TERM \\\n-- "-$CHILD_PID"\n',
            "no_extension": f'#!/bin/bash\n{shell_send} -- "-$CHILD_PID"\n',
            "lib/process_group.py": f"def send_process_group(pid, sig):\n    {python_send}(pid, sig)\n{python_send}(pid, sig)\n",
            "heredoc.sh": f"python3 - <<'PY'\nimport os\n{python_send}(pid, 15)\nPY\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory)
            for name, source in cases.items():
                (scripts / name).parent.mkdir(parents=True, exist_ok=True)
                _ = (scripts / name).write_text(source)
            _ = (scripts / "safe.sh").write_text(
                'python3 scripts/lib/process_group.py "$CHILD_PID" TERM\nkill -TERM "$CHILD_PID"\n')
            _ = (scripts / "prose.py").write_text(
                f"body = 'Type ! {shell_send} -TERM -12345 in the other session.'\n"
                + f'''reason = 'the body says to type "{shell_send} -TERM -12345"'\n''')
            found = {finding.split(":", 1)[0] for finding in unguarded_group_signals(scripts)}
            self.assertEqual(found, set(cases))

    def test_guard_finds_zero_groups_nested_shells_and_embedded_python_aliases(self) -> None:
        shell_send = "ki" + "ll"
        attribute = "kill" + "pg"
        cases = {
            "zero.sh": f"{shell_send} -TERM -- 0\n",
            "negative_zero.sh": f'{shell_send} -TERM -- "-0"\n',
            "nested.sh": f"bash -c '{shell_send} -TERM -- \"-$PID\"'\n",
            "nested_python.py": f"import subprocess\nsubprocess.run(['bash', '-c', '{shell_send} -TERM -- 0'])\n",
            "heredoc.sh": f"python3 - <<'PY'\nimport os as platform\nplatform.{attribute}(pid, 15)\nPY\n",
            "attribute.py": f"get_platform().{attribute}(pid, 15)\n",
            "bare.py": f"{attribute}(pid, 15)\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory)
            for name, source in cases.items():
                _ = (scripts / name).write_text(source)
            found = {finding.split(":", 1)[0] for finding in unguarded_group_signals(scripts)}
            self.assertEqual(found, set(cases))


if __name__ == "__main__":
    _ = unittest.main()
