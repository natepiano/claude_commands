"""Test store migration through private staff records and state files."""
from __future__ import annotations

import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import override
from unittest import mock

from scripts.production import migrate_middle_stores, staff
from scripts.production.staff_test_support import (
    fixture_member, isolated_source_readers, isolated_staff_file, source_environment,
)


class MiddleStoreMigrationTests(unittest.TestCase):
    root: Path = Path()
    outstanding: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = source_environment(self.root / "sources")
        self.enterContext(mock.patch.dict(os.environ, environment))
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.enterContext(isolated_source_readers())
        self.outstanding = Path(environment["SHOWRUNNER_STATE_DIR"]) / "outstanding"
        self.outstanding.mkdir(parents=True)
        document = self.root / "current-production.md"
        _ = document.write_text("# Production\n\n## Production Context\n\n- **Formerly:** `alpha` `beta`\n")
        owner = fixture_member("showrunner", name="current", doc=staff.LiveDoc("natedev", document))
        timer = Path(environment["NOTIFIER_STATE_DIR"]) / "showrunner-current/conf"
        timer.parent.mkdir(parents=True)
        _ = timer.write_text(f"TARGET=session:{owner.current_session}\nCHECK=" +
                         shlex.join(["production_check.sh", str(document)]) + "\n")

    @property
    def owner(self) -> staff.StaffRow:
        return staff.snapshot().rows[0]

    def write(self, name: str, content: str) -> Path:
        path = self.outstanding / name
        _ = path.write_text(content)
        return path

    def migrate(self) -> tuple[int, list[str]]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = migrate_middle_stores.migrate("outstanding")
        return status, output.getvalue().splitlines()

    def test_former_names_merge_history_by_instant_and_rerun_preserves_bytes(self) -> None:
        first = '{"time":"2026-10-27T08:00:00-04:00","text":"first"}\n'
        equal = '{"time":"2026-10-27T12:00:00Z","text":"equal"}\n'
        last = '{"time":"2026-10-27T09:00:00-04:00","text":"last"}\n'
        target = self.write(f"{self.owner.id}.history.jsonl", first)
        _ = self.write("alpha.history.jsonl", last)
        _ = self.write("beta.history.jsonl", equal)
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual(target.read_text(), first + equal + last)
        self.assertEqual(sorted(path.name for path in self.outstanding.iterdir()), [target.name])
        self.assertEqual(lines[-1], "moved 2; left in place 0; unavailable 0")
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        self.assertEqual(self.migrate(), (0, ["moved 0; left in place 0; unavailable 0"]))
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)

    def test_unknown_owner_remains_with_one_anomaly_and_success(self) -> None:
        source = self.write("orphan.json", "keep orphan\n")
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual(source.read_text(), "keep orphan\n")
        self.assertEqual(lines, [f"anomaly: {source}: no showrunner row; left in place",
                                 "moved 0; left in place 1; unavailable 0"])
        self.assertEqual(self.migrate(), (status, lines))

    def test_unequal_json_remains_while_history_moves(self) -> None:
        source = self.write("alpha.json", '{"old":true}\n')
        target = self.write(f"{self.owner.id}.json", '{"new":true}\n')
        history = '{"time":"2026-10-27T12:00:00Z"}\n'
        _ = self.write("alpha.history.jsonl", history)
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual(source.read_text(), '{"old":true}\n')
        self.assertEqual(target.read_text(), '{"new":true}\n')
        self.assertEqual((self.outstanding / f"{self.owner.id}.history.jsonl").read_text(), history)
        self.assertEqual([line for line in lines if line.startswith("anomaly:")],
                         [f"anomaly: {source}: unequal migration destination {target}; left in place"])
        self.assertEqual(lines[-1], "moved 1; left in place 1; unavailable 0")

    def test_answer_records_merge_without_discarding_retained_answers(self) -> None:
        early = json.dumps({"time": "2026-10-27T12:00:00Z", "text": "early answer"}) + "\n"
        late = json.dumps({"time": "2026-10-27T13:00:00Z", "text": "late answer"}) + "\n"
        target = self.write(f"{self.owner.id}.answered.jsonl", late)
        _ = self.write("alpha.answered.jsonl", early)
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual(target.read_text(), early + late)
        self.assertEqual(lines[-1], "moved 1; left in place 0; unavailable 0")

    def test_undated_lines_follow_their_previous_dated_line(self) -> None:
        early = '{"time":"2026-10-27T12:00:00Z"}\n'
        middle = '{"time":"2026-10-27T12:30:00Z"}\n'
        late = '{"time":"2026-10-27T13:00:00Z"}\n'
        undated = '{"text":"no time"}\n{"time":"bad"}\n{"time":"2026-10-27T12:30:00"}\n'
        target = self.write(f"{self.owner.id}.history.jsonl", middle)
        _ = self.write("alpha.history.jsonl", late + early + undated)
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual(target.read_text(), early + middle + late + undated)
        self.assertEqual(lines[-1], "moved 1; left in place 0; unavailable 0")

    def test_empty_source_lock_is_removed_without_replacing_destination(self) -> None:
        target = self.write(f"{self.owner.id}.lock", "retained lock content")
        _ = self.write("alpha.lock", "")
        status, lines = self.migrate()
        self.assertEqual(status, 0, lines)
        self.assertEqual({path.name: path.read_text() for path in self.outstanding.iterdir()},
                         {target.name: "retained lock content"})
        self.assertEqual(lines[-1], "moved 1; left in place 0; unavailable 0")


if __name__ == "__main__":
    _ = unittest.main()
