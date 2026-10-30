"""Transfer moves the unit's pane without stopping, closing or restarting it."""
import json
from pathlib import Path
from typing import cast

from scripts.production.transfer_unit_test_support import UnitTransferFixture
from scripts.production.fake_unit_host import PaneShellFixture, record_shutdown, assert_shared_stop_record, calls


class TransferPaneShellTests(UnitTransferFixture):
    def test_seed_inherits_shared_stop_record_defaults(self) -> None:
        assert_shared_stop_record(self, self.seed_record,
            self.root / 'home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json')

    def pane_shell(self, *, live: bool) -> None:
        shutdown_log = self.seed_record()
        path = self.root / 'home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json'
        if live:
            saved = cast(dict[str, object], json.loads(path.read_text()))
            saved['progress'] = {'kind':'ready','at':'2026-10-09T21:50:00+00:00'}
            self.write(path, json.dumps(saved))
        pane = PaneShellFixture(self, self.unit, Path(self.env['HERDR_PROC']), live=live, env=self.env)
        record_shutdown(self, pane, path, destination=('other','alpha-unit','alpha'))
        self.configure_tab_move()
        self.assert_ok(self.transfer('apply'))
        self.assert_ok(self.transfer('verify'))
        herdr_calls = calls(self.env)
        self.assertEqual([call for call in herdr_calls if call[2:4] == ['pane', 'move']],
                         [['--session', 'agents', 'pane', 'move', 'w1:p1', '--new-tab', '--workspace', 'w2']])
        self.assertIn(['--session', 'agents', 'tab', 'rename', 'w2:t1', 'alpha'], herdr_calls)
        self.assertEqual(pane.close_calls(), [])
        self.assertEqual(pane.stop_calls, [])
        self.assertFalse(shutdown_log.exists(), 'transfer ran a stop or restart command')
        self.assertFalse(any(call[2:4] in (['agent', 'start'], ['pane', 'create'], ['tab', 'create'])
                             for call in herdr_calls), herdr_calls)
        self.assertNotIn('| alpha-unit |', (self.giving / 'docs/plans/demo-production.md').read_text())
        self.assertIn('| alpha-unit |', (self.receiving / 'docs/plans/other-production.md').read_text())
        self.assertTrue(self.unit.is_dir())

    def test_live_director_in_pane_shell_moves_its_tab_and_transfers(self) -> None:
        self.pane_shell(live=True)

    def test_exited_directors_pane_shell_moves_its_tab_and_transfers(self) -> None:
        self.pane_shell(live=False)
