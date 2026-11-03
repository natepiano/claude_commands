"""Git answers and failures retain their process provenance."""

from __future__ import annotations

import errno
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_process import GitExitAnswers, GitReadFailed, run_git


class GitProcessTests(unittest.TestCase):
    def test_launch_failure_preserves_command_and_original_error(self) -> None:
        command = ("git", "hash-object", "--stdin")
        for code in (errno.EMFILE, errno.ENOENT):
            with self.subTest(errno=code):
                failure = OSError(code, "PRIVATE diagnostic")
                with mock.patch.object(subprocess, "run", side_effect=failure):
                    with self.assertRaises(GitReadFailed) as refused:
                        _ = run_git(command, input=b"body")
                self.assertEqual(refused.exception.command, command)
                self.assertIs(refused.exception.__cause__, failure)
                self.assertNotIn("PRIVATE", str(refused.exception))

    def test_unexpected_exit_retains_process_error_and_diagnostic(self) -> None:
        command = ("git", "-C", "/repo", "show", "HEAD:missing")
        for code in (1, 128, -9):
            with self.subTest(status=code):
                result = subprocess.CompletedProcess(command, code, b"partial", b"PRIVATE diagnostic")
                with mock.patch.object(subprocess, "run", return_value=result):
                    with self.assertRaises(GitReadFailed) as refused:
                        _ = run_git(command)
                cause = refused.exception.__cause__
                self.assertIsInstance(cause, subprocess.CalledProcessError)
                assert isinstance(cause, subprocess.CalledProcessError)
                self.assertEqual((cast(object, cause.cmd), cause.returncode, cast(object, cause.stdout), cast(object, cause.stderr)),
                                 (command, code, b"partial", b"PRIVATE diagnostic"))
                self.assertEqual(refused.exception.command, command)
                self.assertEqual(refused.exception.stderr, "PRIVATE diagnostic")
                self.assertNotIn("PRIVATE", str(refused.exception))

    def test_expected_negative_is_an_answer_but_unexpected_exit_is_not(self) -> None:
        command = ("git", "merge-base", "--is-ancestor", "old", "new")
        for code in (0, 1, 128):
            with self.subTest(status=code):
                result = subprocess.CompletedProcess(command, code, b"answer", b"")
                with mock.patch.object(subprocess, "run", return_value=result):
                    if code == 128:
                        with self.assertRaises(GitReadFailed):
                            _ = run_git(command, answers=GitExitAnswers(frozenset({0, 1})))
                    else:
                        self.assertIs(run_git(command, answers=GitExitAnswers(frozenset({0, 1}))), result)

    def test_stdout_can_stream_to_an_open_file(self) -> None:
        with tempfile.TemporaryFile() as output:
            result = run_git(("git", "hash-object", "--stdin"), input=b"hello\n", output=output)
            self.assertEqual(result.returncode, 0)
            _ = output.seek(0)
            self.assertEqual(output.read(), b"ce013625030ba8dba906f756967f9e9ca394464a\n")


if __name__ == "__main__":
    _ = unittest.main()
