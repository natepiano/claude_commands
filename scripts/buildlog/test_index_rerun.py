#!/usr/bin/env python3
"""Index retries preserve source logs and read legacy worker fields."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast, override
from unittest import mock

# Support both the package-qualified unittest command and directory discovery.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import index
from test_index import Record, call, encode, point_root_at, step


class IndexRerunTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    source: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        point_root_at(self, self.root)
        self.source = self.root / "natedev" / "2026-10.jsonl"
        self.source.parent.mkdir(parents=True)

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        with closing(index.read_only()) as connection:
            return cast(list[tuple[object, ...]], connection.execute(sql).fetchall())

    def row_without_id(self, table: str, record_id: str) -> dict[str, object]:
        with closing(index.read_only()) as connection:
            connection.row_factory = sqlite3.Row
            row = cast(sqlite3.Row, connection.execute(
                f"SELECT * FROM {table} WHERE id = ?", (record_id,),
            ).fetchone())
            self.assertIsNotNone(row, record_id)
            return {name: cast(object, row[name]) for name in row.keys() if name != "id"}

    def test_interrupted_update_preserves_source_and_reruns_without_loss(self) -> None:
        records = (
            step("old-step", seat="impl", delegate_session="old-unit"),
            call("old-call", seat="test", delegate_session="call-unit"),
            step("new-step", worker="review", unit_session="new-unit"),
            call("new-call", worker="fix", unit_session="new-call-unit"),
        )
        # A concurrent append's unfinished line must survive the interrupted read too.
        unfinished = encode(step("pending", seat="next", delegate_session="next-unit"))[:40]
        original = b"".join(encode(record) for record in records) + unfinished
        _ = self.source.write_bytes(original)
        inserted: list[object] = []
        add_step = index.ADDERS["step"]

        def stop_after_first_record(connection: sqlite3.Connection, record: Record, src: str) -> None:
            add_step(connection, record, src)
            inserted.append(record["id"])
            self.assertEqual(connection.execute("SELECT count(*) FROM steps").fetchone(), (1,))
            raise InterruptedError("stop after the first indexed record")

        with mock.patch.dict(index.ADDERS, {"step": stop_after_first_record}):
            with self.assertRaisesRegex(InterruptedError, "first indexed record"):
                _ = index.update()

        self.assertEqual(inserted, ["old-step"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.rows("SELECT id FROM steps"), [])
        self.assertEqual(self.rows("SELECT id FROM calls"), [])
        self.assertEqual(self.rows("SELECT path FROM files"), [])

        self.assertEqual(index.update(), len(records))
        expected_steps = [("new-step", "review", "new-unit"), ("old-step", "impl", "old-unit")]
        expected_calls = [("new-call", "fix", "new-call-unit"), ("old-call", "test", "call-unit")]
        self.assertEqual(self.rows("SELECT id, worker, unit_session FROM steps ORDER BY id"), expected_steps)
        self.assertEqual(self.rows("SELECT id, worker, unit_session FROM calls ORDER BY id"), expected_calls)
        self.assertEqual(self.rows("SELECT offset FROM files"), [(len(original) - len(unfinished),)])
        self.assertEqual(self.source.read_bytes(), original)

        self.assertEqual(index.update(), 0)
        self.assertEqual(self.rows("SELECT id, worker, unit_session FROM steps ORDER BY id"), expected_steps)
        self.assertEqual(self.rows("SELECT id, worker, unit_session FROM calls ORDER BY id"), expected_calls)
        self.assertEqual(self.source.read_bytes(), original)

    def test_old_and_new_field_names_produce_equal_rows(self) -> None:
        records = (
            step("old-step", seat="impl", delegate_session="unit-a"),
            step("new-step", worker="impl", unit_session="unit-a"),
            call("old-call", seat="test", delegate_session="unit-b"),
            call("new-call", worker="test", unit_session="unit-b"),
        )
        original = b"".join(encode(record) for record in records)
        _ = self.source.write_bytes(original)

        self.assertEqual(index.update(), len(records))

        for table, kind, worker, unit_session in (
            ("steps", "step", "impl", "unit-a"),
            ("calls", "call", "test", "unit-b"),
        ):
            with self.subTest(table=table):
                old = self.row_without_id(table, f"old-{kind}")
                new = self.row_without_id(table, f"new-{kind}")
                self.assertEqual(old["worker"], worker)
                self.assertEqual(old["unit_session"], unit_session)
                self.assertEqual(old, new)
        self.assertEqual(self.source.read_bytes(), original)

    def test_new_fields_win_even_when_empty_or_null(self) -> None:
        records: list[Record] = []
        expected: list[tuple[object, ...]] = []
        for label, worker, unit_session in (
            ("explicit", "new-worker", "new-unit"),
            ("empty", "", ""),
            ("null", None, None),
        ):
            expected.append((label, worker, unit_session))
            for factory in (step, call):
                records.append(factory(
                    label, seat="obsolete-worker", delegate_session="obsolete-unit",
                    worker=worker, unit_session=unit_session,
                ))
        original = b"".join(encode(record) for record in records)
        _ = self.source.write_bytes(original)

        self.assertEqual(index.update(), len(records))

        for table in ("steps", "calls"):
            with self.subTest(table=table):
                self.assertEqual(self.rows(f"SELECT id, worker, unit_session FROM {table} ORDER BY id"),
                                 sorted(expected))
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == "__main__":
    _ = unittest.main()
