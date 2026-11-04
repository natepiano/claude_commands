"""Callback Git helpers share process failures without relabeling file reads."""

from __future__ import annotations

import errno
from contextlib import redirect_stderr
import io
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import unit_files
import style_ledger
from git_process import GitReadFailed
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, recorded_showrunner, recorded_unit


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
        with mock.patch.object(staff, 'snapshot', return_value=staff.StaffSnapshot(())), mock.patch.object(Path, 'read_text', side_effect=failure):
            with self.assertRaises(PermissionError) as refused:
                _ = unit_files.unit_worktrees(Path('/unreadable.md'))
        self.assertIs(refused.exception, failure)


class UnitWorktreeAvailabilityTests(StaffTestCase):
    def test_file_holders_use_the_staff_parent_document(self) -> None:
        parent_doc = self.source_root / 'parent-production.md'
        _ = parent_doc.write_text('# Parent\n')
        parent = recorded_showrunner('parent', parent_doc)
        doc = self.source_root / 'style-production.md'
        _ = doc.write_text('# Style\n')
        owner = recorded_showrunner('style', doc)
        _ = staff.move(owner.id, parent.id)
        with mock.patch.object(unit_files, 'unit_worktrees', return_value=()) as worktrees:
            ledger = cast(style_ledger.Ledger, mock.create_autospec(style_ledger.Ledger, instance=True))
            self.assertEqual(unit_files.unit_file_holders(self.source_root, doc, ledger), {})
        self.assertEqual([call.args[0] for call in worktrees.call_args_list], [parent_doc, doc])

    def test_unavailable_rows_keep_the_live_unit_and_report_each_skipped_row(self) -> None:
        doc = self.source_root / 'style-production.md'
        _ = doc.write_text('# Style production\n- **Merge branch:** main\n')
        owner = recorded_showrunner('style', doc)
        _ = fixture_member('unit', name='standby', parent=owner.id)
        live_folder = self.root / 'live'
        _ = recorded_unit('style', 'live', live_folder, doc)
        _ = subprocess.run(['git', '-C', str(live_folder), 'checkout', '-q', '-b', 'live-branch'], check=True)
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = unit_files.unit_worktrees(doc)
        self.assertEqual(result, (unit_files.UnitWorktree('live-unit', 'live-branch', live_folder, 'main'),))
        messages = errors.getvalue().splitlines()
        self.assertEqual(len(messages), 1)
        self.assertIn('standby-unit', messages[0])
        self.assertIn('staff worktree is unfilled', messages[0])

if __name__ == '__main__':
    _ = unittest.main()
