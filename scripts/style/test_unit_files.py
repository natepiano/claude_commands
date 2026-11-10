"""Callback Git helpers share process failures without relabeling file reads."""

from __future__ import annotations

import errno
from contextlib import redirect_stderr
import io
import subprocess
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.style import unit_files
from scripts.style import style_ledger
from scripts.style.git_process import GitReadFailed
from scripts.production import session_staff, staff
from scripts.agents import remote
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

    def test_no_unit_rows_do_not_read_the_production_document(self) -> None:
        doc = Path('/unreadable.md')
        row = staff.StaffRow(staff.StaffId('private-owner'), 'showrunner', 'private', staff.STAFF_ROOT,
                             current_session=staff.StaffId('private-session'), doc=staff.LiveDoc(remote.local_machine(), doc))
        with mock.patch.object(staff, 'snapshot', return_value=staff.StaffSnapshot((row,))), mock.patch.object(
            Path, 'read_text', side_effect=PermissionError('document unreadable'),
        ) as read:
            self.assertEqual(unit_files.unit_worktrees(Path('/unreadable.md')), ())
        read.assert_not_called()


class UnitWorktreeAvailabilityTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.source_root)], check=True)
        _ = subprocess.run(['git', '-C', str(self.source_root), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)

    def test_a_missing_production_document_reports_the_unit_without_a_worktree_result(self) -> None:
        doc = self.source_root / 'missing.md'
        _ = doc.write_text('# Production\n')
        _ = recorded_unit('missing', 'held', self.root / 'held', doc)
        doc.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(unit_files.unit_worktrees(doc), ())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn('held', errors.getvalue())
        self.assertIn('No such file', errors.getvalue())

    def test_file_holders_follow_the_staff_parent_without_a_parent_document_field(self) -> None:
        parent_doc, child_doc = (self.source_root / name for name in ('parent.md', 'child.md'))
        for doc in (parent_doc, child_doc):
            _ = doc.write_text('# Production\n- **Merge branch:** main\n')
        parent = recorded_showrunner('parent', parent_doc)
        child = recorded_showrunner('child', child_doc)
        _ = staff.move(child.id, staff.ReportsTo(parent.id))
        folder = self.root / 'parent'
        _ = recorded_unit('parent', 'held', folder, parent_doc)
        _ = subprocess.run(['git', '-C', str(folder), 'branch', 'main'], check=True)
        _ = (folder / 'held.py').write_text('private change\n')
        with (style_ledger.open_ledger(folder) as ledger,
              mock.patch.object(staff, 'snapshot', wraps=staff.snapshot) as snapshot):
            holders = unit_files.unit_file_holders(folder, child_doc, ledger)
        snapshot.assert_called_once_with()
        self.assertIn('held.py', holders['held'])
        self.assertNotIn('Parent production', child_doc.read_text())

    def test_unreadable_parent_document_keeps_tickets_and_dirty_files_busy(self) -> None:
        parent_doc, child_doc = (self.source_root / name for name in ('parent.md', 'child.md'))
        for doc in (parent_doc, child_doc):
            _ = doc.write_text('# Production\n- **Merge branch:** main\n')
        _ = recorded_showrunner('parent', parent_doc)
        _ = recorded_showrunner('child', child_doc, 'parent')
        folder = self.root / 'parent'
        held = recorded_unit('parent', 'held', folder, parent_doc)
        _ = subprocess.run(['git', '-C', str(folder), 'branch', 'main'], check=True)
        _ = (folder / 'held.py').write_text('private change\n')
        parent_doc.unlink()
        with (style_ledger.open_ledger(folder) as ledger,
              mock.patch.object(ledger, 'ticket_open', return_value=({'files': ['ticket.py']},)) as tickets,
              redirect_stderr(io.StringIO()) as errors):
            holders = unit_files.unit_file_holders(folder, child_doc, ledger)
        tickets.assert_called_once_with(str(held.id))
        self.assertEqual(holders['held'], frozenset({'held.py', 'ticket.py', '.unit.toml'}))
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn('style: held:', errors.getvalue())
        self.assertIn('No such file', errors.getvalue())

    def test_detached_head_keeps_dirty_files_busy_and_other_units_continue(self) -> None:
        doc = self.source_root / 'production.md'
        _ = doc.write_text('# Production\n')
        folder, other = (self.root / name for name in ('detached', 'other'))
        _ = recorded_unit('style', 'detached', folder, doc)
        _ = recorded_unit('style', 'other', other, doc)
        _ = subprocess.run(['git', '-C', str(folder), 'branch', 'main'], check=True)
        _ = subprocess.run(['git', '-C', str(folder), 'checkout', '-q', '--detach'], check=True)
        _ = (folder / 'detached.py').write_text('private change\n')
        _ = (other / 'other.py').write_text('another change\n')
        rows = unit_files.unit_worktrees(doc)
        self.assertEqual(next(row.branch for row in rows if row.unit == 'detached'), 'HEAD')
        with (style_ledger.open_ledger(folder) as ledger,
              unit_files.report_unit_problems_once(),
              redirect_stderr(io.StringIO()) as errors):
            holders = unit_files.unit_file_holders(folder, doc, ledger)
            self.assertEqual(unit_files.unit_file_holders(folder, doc, ledger), holders)
        with (style_ledger.open_ledger(folder) as ledger,
              unit_files.report_unit_problems_once(),
              redirect_stderr(io.StringIO()) as next_errors):
            self.assertEqual(unit_files.unit_file_holders(folder, doc, ledger), holders)
        self.assertEqual(holders['detached'], frozenset({'detached.py', '.unit.toml'}))
        self.assertEqual(holders['other'], frozenset({'other.py', '.unit.toml'}))
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn('style: detached:', errors.getvalue())
        self.assertEqual(next_errors.getvalue(), errors.getvalue())

    def test_unreadable_worktree_keeps_open_tickets_busy(self) -> None:
        doc = self.source_root / 'production.md'
        _ = doc.write_text('# Production\n')
        owner = recorded_showrunner('style', doc)
        standby = fixture_member('unit', name='standby', parent=owner.id)
        with (style_ledger.open_ledger(self.source_root) as ledger,
              mock.patch.object(ledger, 'ticket_open', return_value=({'files': ['ticket.py']},)) as tickets,
              redirect_stderr(io.StringIO()) as errors):
            holders = unit_files.unit_file_holders(self.source_root, doc, ledger)
        tickets.assert_called_once_with(str(standby.id))
        self.assertEqual(holders, {'standby': frozenset({'ticket.py'})})
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn('staff worktree is unfilled', errors.getvalue())

    def test_file_holders_use_the_staff_parent_document(self) -> None:
        parent_doc = self.source_root / 'parent-production.md'
        _ = parent_doc.write_text('# Parent\n')
        parent = recorded_showrunner('parent', parent_doc)
        doc = self.source_root / 'style-production.md'
        _ = doc.write_text('# Style\n')
        owner = recorded_showrunner('style', doc)
        owner = staff.move(owner.id, staff.ReportsTo(parent.id))
        with mock.patch.object(unit_files, '_unit_file_sources', return_value=()) as sources:
            ledger = cast(style_ledger.Ledger, mock.create_autospec(style_ledger.Ledger, instance=True))
            resolver = session_staff.RowResolver(staff.snapshot())
            with mock.patch.object(ledger, 'staff_resolver', return_value=resolver):
                self.assertEqual(unit_files.unit_file_holders(self.source_root, doc, ledger), {})
        self.assertEqual(sources.call_args_list, [mock.call(owner, mock.ANY), mock.call(parent, mock.ANY)])

    def test_unavailable_rows_keep_the_live_unit_and_report_each_skipped_row(self) -> None:
        doc = self.source_root / 'style-production.md'
        _ = doc.write_text('# Style production\n- **Merge branch:** stale-branch\n')
        owner = recorded_showrunner('style', doc)
        _ = fixture_member('unit', name='standby', parent=owner.id)
        live_folder = self.root / 'live'
        live = recorded_unit('style', 'live', live_folder, doc)
        _ = subprocess.run(['git', '-C', str(live_folder), 'checkout', '-q', '-b', 'live-branch'], check=True)
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = unit_files.unit_worktrees(doc)
        self.assertEqual(result, (unit_files.UnitWorktree(live.id, 'live', 'live-branch', live_folder, 'main'),))
        messages = errors.getvalue().splitlines()
        self.assertEqual(len(messages), 1)
        self.assertIn('standby', messages[0])
        self.assertIn('staff worktree is unfilled', messages[0])
    def test_unreadable_branch_reports_one_line_and_keeps_other_rows(self) -> None:
        doc = self.source_root / 'style-production.md'
        _ = doc.write_text('# Style production\n')
        _ = recorded_unit('style', 'held', self.root / 'held', doc)
        with (mock.patch.object(session_staff.RowResolver, 'merge_branch',
                                return_value=session_staff.BranchUnreadable('first line\nsecond line')),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(unit_files.unit_worktrees(doc), ())
        self.assertEqual(errors.getvalue(), 'style: held: first line second line\n')


if __name__ == '__main__':
    _ = unittest.main()
