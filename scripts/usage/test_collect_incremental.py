"""Check cursor recovery and selective attribution with complete ledger comparisons."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, Literal, cast, override
from unittest.mock import patch

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.usage import collect
from scripts.usage.agents_log import Assignment, RoleLog
from scripts.usage.collect_test_support import FIXTURES, SESSION, THREAD, BOUNDARY


class IncrementalTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    path: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    db: sqlite3.Connection  # pyright: ignore[reportUninitializedInstanceVariable]
    resolver: collect.RoleResolver  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'rollout.jsonl'
        self.db = self.database(self.root / 'usage.db')
        self.addCleanup(self.db.close)
        self.resolver = collect.RoleResolver(RoleLog({}, ()), set())

    def database(self, path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        _ = db.executescript(collect.SCHEMA)
        collect.migrate_store(db)
        return db

    def fixture(self, family: Literal['claude', 'codex']) -> list[bytes]:
        source = (FIXTURES / 'basic' / 'claude' / 'project' / f'{SESSION}.jsonl' if family == 'claude'
                  else FIXTURES / 'basic' / 'codex' / '2026/10/10/rollout-example.jsonl')
        return source.read_bytes().splitlines(keepends=True)

    def collect_file(self, family: Literal['claude', 'codex'] = 'codex') -> int:
        return collect.collect_file(self.db, 'natedev', str(self.path), self.path, family, self.resolver)

    def snapshot(self, db: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
        return {table: [tuple(cast(Iterable[object], row)) for row in cast(Iterable[sqlite3.Row],
                    db.execute(f'SELECT * FROM {table} ORDER BY 1,2,3,4,5'))]
                for table in ('turns', 'edits', 'compactions', 'openings', 'folders')}

    def assert_full_scan(self, family: Literal['claude', 'codex']) -> None:
        expected = self.database(self.root / 'expected.db')
        try:
            _ = collect.collect_file(expected, 'natedev', str(self.path), self.path, family, self.resolver)
            self.assertEqual(self.snapshot(self.db), self.snapshot(expected))
        finally:
            expected.close()

    def test_unchanged_file_never_opens(self) -> None:
        _ = self.path.write_bytes(b''.join(self.fixture('codex')))
        _ = self.collect_file()
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged transcript opened')):
            self.assertEqual(self.collect_file(), 0)

    def test_append_reads_only_new_lines_and_preserves_context(self) -> None:
        for family in ('claude', 'codex'):
            with self.subTest(family=family):
                lines = self.fixture(family)
                _ = self.path.write_bytes(b''.join(lines[:3]))
                _ = self.collect_file(family)
                previous = self.path.stat().st_size
                with self.path.open('ab') as output:
                    _ = output.write(b''.join(lines[3:]))
                offsets: list[int] = []
                original = collect.complete_lines

                def traced(source: BinaryIO) -> Iterator[tuple[int, bytes]]:
                    for offset, line in original(source):
                        offsets.append(offset)
                        yield offset, line

                with patch.object(collect, 'complete_lines', traced):
                    _ = self.collect_file(family)
                self.assertTrue(offsets)
                self.assertEqual(min(offsets), previous)
                self.assert_full_scan(family)
                _ = self.db.execute('DELETE FROM cursors')
                for table in self.snapshot(self.db):
                    _ = self.db.execute(f'DELETE FROM {table}')
                self.db.commit()
                (self.root / 'expected.db').unlink()

    def test_truncation_replacement_and_same_size_rewrite_start_at_zero(self) -> None:
        data = b''.join(self.fixture('codex'))
        for change in ('truncate', 'replace', 'rewrite'):
            with self.subTest(change=change):
                _ = self.path.write_bytes(data)
                _ = self.collect_file()
                if change == 'replace':
                    other = self.root / 'replacement'
                    _ = other.write_bytes(data)
                    os.replace(other, self.path)
                else:
                    _ = self.path.write_bytes(data[:len(data)//2] if change == 'truncate' else data)
                with patch.object(collect, 'complete_lines', wraps=collect.complete_lines) as reader:
                    _ = self.collect_file()
                    self.assertEqual(reader.call_count, 1)
                context = cast(str, self.db.execute('SELECT context FROM cursors').fetchone()[0])
                self.assertTrue(collect.json_fields(context)['parser'])

    def test_killed_transaction_then_rerun_matches_full_scan(self) -> None:
        _ = self.path.write_bytes(b''.join(self.fixture('codex')))
        program = '''import os, sqlite3, sys
from pathlib import Path
from scripts.usage import collect
from scripts.usage.agents_log import RoleLog
db = sqlite3.connect(sys.argv[1])
db.row_factory = sqlite3.Row
original = collect.save_measurement
def interrupted(*args):
    original(*args)
    os._exit(73)
collect.save_measurement = interrupted
collect.collect_file(db, 'natedev', sys.argv[2], Path(sys.argv[2]), 'codex', collect.RoleResolver(RoleLog({}, ()), set()))
'''
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), '-c', program, str(self.root / 'usage.db'), str(self.path)],
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM cursors').fetchone()[0], 0)
        _ = self.collect_file()
        self.assert_full_scan('codex')

    def test_reattribution_resolves_only_agents_with_changed_assignments(self) -> None:
        _ = self.path.write_bytes(b''.join(self.fixture('codex')))
        _ = self.collect_file()
        with self.db:
            collect.reattribute(self.db, 'natedev', self.resolver)
        with patch.object(self.resolver, 'resolve', wraps=self.resolver.resolve) as resolve:
            with self.db:
                collect.reattribute(self.db, 'natedev', self.resolver)
            self.assertEqual(resolve.call_count, 0)
        changed = collect.RoleResolver(RoleLog({THREAD: (Assignment(BOUNDARY, 'impl', None),)}, ()), set())
        with patch.object(changed, 'resolve', wraps=changed.resolve) as resolve:
            with self.db:
                collect.reattribute(self.db, 'natedev', changed)
            self.assertGreater(resolve.call_count, 0)
        roles = [cast(str, row[0]) for row in cast(Iterable[sqlite3.Row], self.db.execute(
            'SELECT role FROM turns WHERE at >= ?', (BOUNDARY.isoformat(),)))]
        self.assertTrue(roles)
        self.assertTrue(all(role == 'impl' for role in roles))


if __name__ == '__main__':
    _ = unittest.main()
