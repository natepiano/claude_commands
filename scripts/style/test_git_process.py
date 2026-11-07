"""Git answers and failures retain their process provenance."""

from __future__ import annotations

import errno
import subprocess
import tempfile
import unittest
from io import BytesIO
from typing import cast
from unittest import mock


from scripts.style.git_process import (GitBlob, GitExitAnswers, GitReadFailed, MalformedBlobReply, MissingBlob,
                         TruncatedBlobReply, parse_status_z, read_batch_blob, run_git)


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

    def test_batch_blob_preserves_bytes_and_leaves_the_next_reply(self) -> None:
        content = b"line\n\0\xff"
        replies = BytesIO(b"abc blob 7\n" + content + b"\ndef blob 0\n\n")
        self.assertEqual(read_batch_blob(replies), GitBlob("abc", content))
        self.assertEqual(read_batch_blob(replies), GitBlob("def", b""))

    def test_missing_blob_leaves_the_next_reply(self) -> None:
        replies = BytesIO(b"HEAD:path with spaces missing\nabc blob 1\nx\n")
        self.assertIsInstance(read_batch_blob(replies), MissingBlob)
        self.assertEqual(read_batch_blob(replies), GitBlob("abc", b"x"))

    def test_malformed_blob_headers(self) -> None:
        for header in (b"", b"abc\n", b"abc tree 3\nxyz\n", b"abc blob -1\n", b"abc blob size\n"):
            with self.subTest(header=header):
                self.assertIsInstance(read_batch_blob(BytesIO(header)), MalformedBlobReply)

    def test_truncated_blob_body_or_separator(self) -> None:
        for reply in (b"abc blob 3\nab", b"abc blob 3\nabc", b"abc blob 3\nabc!"):
            with self.subTest(reply=reply):
                self.assertIsInstance(read_batch_blob(BytesIO(reply)), TruncatedBlobReply)

    def test_unreadable_blob_lengths_return_malformed_replies(self) -> None:
        for digits in (30, 5000):
            with self.subTest(digits=digits):
                replies = BytesIO(b"abc blob " + b"9" * digits + b"\n")
                self.assertIsInstance(read_batch_blob(replies), MalformedBlobReply)
        with self.subTest(digits=18), tempfile.TemporaryFile() as replies:
            _ = replies.write(b"abc blob " + b"9" * 18 + b"\n")
            _ = replies.seek(0)
            self.assertIsInstance(read_batch_blob(replies), MalformedBlobReply)

    def test_status_retains_both_rename_and_copy_paths_and_untracked_paths(self) -> None:
        output = "R  new name.py\0old name.py\0 C copied.py\0source.py\0?? untracked file.py\0"
        self.assertEqual(parse_status_z(output),
                         {"new name.py", "old name.py", "copied.py", "source.py", "untracked file.py"})


if __name__ == "__main__":
    _ = unittest.main()
