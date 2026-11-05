"""Migrate state files independently when their contents contain anomalies."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from typing import override

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import migrate_showrunner_state, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member


class ShowrunnerStateMigrationTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = fixture_member("showrunner", name="legacy")

    @property
    def state(self) -> Path:
        return Path(os.environ["SHOWRUNNER_STATE_DIR"])

    @property
    def owner(self) -> staff.StaffRow:
        return next(row for row in staff.snapshot().rows if row.name == "legacy")

    def write(self, relative: str, content: str) -> None:
        path = self.state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def files(self) -> dict[str, str]:
        return {path.relative_to(self.state).as_posix(): path.read_text()
                for path in self.state.rglob("*") if path.is_file()}

    def migrate(self) -> tuple[int, list[str]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = migrate_showrunner_state.migrate()
        return code, output.getvalue().splitlines()

    def split_folder(self, relative: str, content: str, expected: str) -> None:
        files = {"session/showrunner_state.json": '{"units": []}\n',
                 "session/dailies_judgment.json": '{"units": []}\n',
                 "session/dailies_state.json": '{}\n',
                 "session/stalls_prompt.txt": 'Keep the stalled unit notes.\n',
                 "parked_units.json": '["parked-unit"]\n',
                 "timer.conf": 'EVERY=15\n', relative: content}
        for name, value in files.items():
            self.write('legacy/' + name, value)
        code, lines = self.migrate()
        self.assertEqual(code, 0, lines)
        files[relative] = expected
        self.assertEqual(self.files(), {f"{self.owner.id}/{name}": value for name, value in files.items()})
        self.assertEqual(sorted(path.name for path in self.state.iterdir()), [self.owner.id])
        self.assertEqual([line for line in lines if line.startswith('anomaly:')],
                         ['anomaly: orphan-unit: no unit staff row'])
        self.assertEqual(lines[-1], f'moved {len(files)}; left in place 0; unavailable 0')

    def test_eta_reference_does_not_split_the_folder(self) -> None:
        content = '{"orphan-unit|phase 1": {"requested": true}}\n'
        self.split_folder('session/dailies_input_state/eta_seen.json', content, content)

    def test_judgment_reference_does_not_split_the_folder(self) -> None:
        self.split_folder('session/dailies_judgment.json',
                          '{"units": [{"unit": "orphan-unit", "note": "old work"}]}\n',
                          '{\n  "units": []\n}\n')

    def test_showrunner_reference_does_not_split_the_folder(self) -> None:
        content = '{"units": [{"unit": "orphan-unit", "phase": "old work"}]}\n'
        self.split_folder('session/showrunner_state.json', content, content)

    def test_unknown_folder_and_report_keep_their_contents(self) -> None:
        self.write('unknown/session/receipt.json', '{"receipt": "keep"}\n')
        self.write('reports/unknown.json', '{"report": "keep"}\n')
        before = self.files()
        code, lines = self.migrate()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), before)
        self.assertEqual(lines, [
            f'anomaly: {self.state / "unknown"}: no showrunner staff row; left in place',
            f'anomaly: {self.state / "reports/unknown.json"}: no showrunner staff row; left in place',
            'moved 0; left in place 2; unavailable 0'])

    def test_finished_rerun_keeps_contents_and_repeats_remaining_anomalies(self) -> None:
        self.write('legacy/session/dailies_input_state/eta_seen.json', '{"orphan-unit|1": {}}\n')
        self.write('unknown/prompt.txt', 'Keep the unknown owner.\n')
        first_code, first = self.migrate()
        self.assertEqual(first_code, 0, first)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.state.rglob('*') if path.is_file()}
        code, lines = self.migrate()
        self.assertEqual(code, 0, lines)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.state.rglob('*') if path.is_file()}, before)
        self.assertEqual([line for line in lines if line.startswith('anomaly:')],
                         [line for line in first if line.startswith('anomaly:')])
        self.assertEqual(lines[-1], 'moved 0; left in place 1; unavailable 0')
        self.assertEqual([line for line in lines if line.startswith('moved ')], [lines[-1]])

    def test_unequal_destination_keeps_one_file_and_moves_the_next(self) -> None:
        self.write('legacy/a.txt', 'Keep the old value.\n')
        self.write(f'{self.owner.id}/a.txt', 'Keep the new value.\n')
        self.write('legacy/z.txt', 'Move the later value.\n')
        code, lines = self.migrate()
        self.assertEqual(code, 0, lines)
        self.assertEqual(self.files(), {'legacy/a.txt': 'Keep the old value.\n',
                                       f'{self.owner.id}/a.txt': 'Keep the new value.\n',
                                       f'{self.owner.id}/z.txt': 'Move the later value.\n'})
        self.assertEqual(len([line for line in lines if line.startswith('anomaly:')]), 1)
        self.assertIn('unequal migration destination', lines[0])
        self.assertEqual(lines[-1], 'moved 1; left in place 1; unavailable 0')

    def test_read_error_keeps_one_file_and_moves_the_next(self) -> None:
        self.write('legacy/a/receipt.json', '{"receipt": "keep"}\n')
        self.write(f'{self.owner.id}/a', 'A file blocks the destination folder.\n')
        self.write('legacy/z.txt', 'Move the later value.\n')
        code, lines = self.migrate()
        self.assertEqual(code, 1, lines)
        self.assertEqual(self.files(), {'legacy/a/receipt.json': '{"receipt": "keep"}\n',
                                       f'{self.owner.id}/a': 'A file blocks the destination folder.\n',
                                       f'{self.owner.id}/z.txt': 'Move the later value.\n'})
        self.assertEqual(len([line for line in lines if line.startswith('anomaly:')]), 1)
        self.assertIn(str(self.state / 'legacy/a/receipt.json'), lines[0])
        self.assertEqual(lines[-1], 'moved 1; left in place 0; unavailable 1')

    def test_reader_policies_preserve_text_and_drop_invalid_judgments(self) -> None:
        unit = fixture_member('unit', name='known', parent=self.owner.id)
        other = fixture_member('showrunner', name='other')
        entries = [{'unit': 'known-unit', 'phase': 'first'},
                   {'unit': unit.id, 'phase': 'duplicate'},
                   {'unit': other.id, 'phase': 'wrong kind'}]
        self.write('legacy/session/showrunner_state.json', json.dumps({'units': entries}))
        self.write('legacy/session/dailies_judgment.json', json.dumps({'units': entries}))
        self.write('legacy/session/dailies_state.json', json.dumps({
            'known-unit': {'phase': 'legacy'}, unit.id: {'phase': 'fixed'}, 'orphan-unit': {'phase': 'old'}}))
        self.write('legacy/session/unit_status/decisions_seen', 'orphan-unit|Keep this decision.\n')
        self.write('legacy/session/unit_status/blocks_open', 'orphan-unit\t1\tKeep this block.\n')
        code, lines = self.migrate()
        self.assertEqual(code, 0, lines)
        files = self.files()
        base = f'{self.owner.id}/session/'
        self.assertEqual(json.loads(files[base + 'showrunner_state.json']), {'units': [
            {'unit': unit.id, 'phase': 'first'}, {'unit': unit.id, 'phase': 'duplicate'}, entries[2]]})
        self.assertEqual(json.loads(files[base + 'dailies_judgment.json']), {
            'units': [{'unit': unit.id, 'phase': 'first'}]})
        self.assertEqual(json.loads(files[base + 'dailies_state.json']), {
            'known-unit': {'phase': 'legacy'}, unit.id: {'phase': 'fixed'}, 'orphan-unit': {'phase': 'old'}})
        self.assertEqual(files[base + 'unit_status/decisions_seen'], 'orphan-unit|Keep this decision.\n')
        self.assertEqual(files[base + 'unit_status/blocks_open'], 'orphan-unit\t1\tKeep this block.\n')
        anomalies = [line for line in lines if line.startswith('anomaly:')]
        self.assertEqual(len(anomalies), 8, anomalies)
        self.assertEqual(lines[-1], 'moved 5; left in place 0; unavailable 0')
