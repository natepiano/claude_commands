"""Transfer completes only after the stopped unit's own pane is safely closed."""
import json
from pathlib import Path
from typing import cast

from scripts.production.transfer_unit_test_support import UnitTransferFixture
from scripts.production.fake_unit_host import PaneShellFixture, record_shutdown, assert_shared_stop_record


class TransferPaneShellTests(UnitTransferFixture):
    def test_seed_inherits_shared_stop_record_defaults(self) -> None:
        assert_shared_stop_record(self, self.seed_record,
            self.root / 'home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json')

    def pane_shell(self, *, live: bool) -> None:
        _ = self.seed_record()
        path = self.root / 'home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json'
        if live:
            saved = cast(dict[str, object], json.loads(path.read_text()))
            saved['progress'] = {'kind':'ready','at':'2026-10-09T21:50:00+00:00'}
            self.write(path, json.dumps(saved))
        pane = PaneShellFixture(self, self.unit, Path(self.env['HERDR_PROC']), live=live, env=self.env)
        record_shutdown(self, pane, path, destination=('other','alpha-unit','alpha'))
        self.assert_ok(self.transfer('apply'))
        self.assert_ok(self.transfer('verify'))
        self.assertEqual(pane.close_calls(), [['--session','agents','pane','close','w1:p1']])
        self.assertEqual(sum(call.startswith('restart') for call in pane.stop_calls), 1)
        self.assertNotIn('| alpha-unit |', (self.giving / 'docs/plans/demo-production.md').read_text())
        self.assertIn('| alpha-unit |', (self.receiving / 'docs/plans/other-production.md').read_text())
        self.assertTrue(self.unit.is_dir())

    def test_live_director_in_pane_shell_stops_closes_and_transfers(self) -> None:
        self.pane_shell(live=True)

    def test_exited_directors_pane_shell_closes_and_transfers(self) -> None:
        self.pane_shell(live=False)
