"""Callback Git helpers share process failures without relabeling file reads."""

from __future__ import annotations

import errno
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import unit_files
from git_process import GitReadFailed


class UnitFilesTests(unittest.TestCase):
    def test_callback_helpers_preserve_git_failure_causes(self) -> None:
        for callback in (lambda: unit_files.git(Path('/repo'), 'show', 'HEAD:x'),
                         lambda: unit_files.file_text(Path('/repo'), 'HEAD', 'x')):
            for code in (errno.EMFILE, errno.ENOENT):
                with self.subTest(callback=callback, errno=code):
                    failure = OSError(code, 'PRIVATE diagnostic')
                    with mock.patch.object(subprocess, 'run', side_effect=failure):
                        with self.assertRaises(GitReadFailed) as refused:
                            _ = callback()
                    self.assertEqual(refused.exception.command[:3], ('git', '-C', '/repo'))
                    self.assertIs(refused.exception.__cause__, failure)
                    self.assertNotIn('PRIVATE', str(refused.exception))
            with self.subTest(callback=callback, status=128):
                with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(('git',), 128, b'', b'PRIVATE')):
                    with self.assertRaises(GitReadFailed) as refused:
                        _ = callback()
                self.assertIsInstance(refused.exception.__cause__, subprocess.CalledProcessError)
                self.assertNotIn('PRIVATE', str(refused.exception))

    def test_blob_bytes_and_text_output_are_preserved(self) -> None:
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(('git',), 0, b'line\r\n', b'')):
            self.assertEqual(unit_files.git(Path('/repo'), 'show', 'HEAD:x'), 'line\n')
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(('git',), 0, b'\xff\n', b'')):
            self.assertEqual(unit_files.file_text(Path('/repo'), 'HEAD', 'x'), '\ufffd\n')

    def test_production_document_failure_stays_a_file_error(self) -> None:
        failure = PermissionError('document unreadable')
        with mock.patch.object(unit_files, 'read_production'), mock.patch.object(Path, 'read_text', side_effect=failure):
            with self.assertRaises(PermissionError) as refused:
                _ = unit_files.unit_worktrees(Path('/unreadable.md'))
        self.assertIs(refused.exception, failure)


if __name__ == '__main__':
    _ = unittest.main()
