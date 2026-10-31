"""Rename completes a pane-shell unit through a clean stop and exact pane close."""
from pathlib import Path

from scripts.production.rename_unit_test_support import UnitRenameFixture
from scripts.production.fake_unit_host import PaneShellFixture, record_shutdown, assert_shared_stop_record


class RenamePaneShellTests(UnitRenameFixture):
    def test_seed_inherits_shared_stop_record_defaults(self) -> None:
        assert_shared_stop_record(self, self.seed_record,
            self.home / '.local/state/shutdown/owner@example.com/sessions/sid-alpha.json')

    def pane_shell(self, *, live: bool) -> None:
        _ = self.seed_record(progress='ready' if live else 'stopped')
        pane = PaneShellFixture(self, self.rust / 'claude-demo-alpha', Path(self.env['HERDR_PROC']), live=live, env=self.env)
        record_shutdown(self, pane, self.home / '.local/state/shutdown/owner@example.com/sessions/sid-alpha.json',
                        destination=('demo','gamma-unit','gamma'))
        self.assert_ok(self.rename('apply'))
        self.assert_ok(self.rename('verify'))
        self.assertEqual(pane.close_calls(), [['--session','agents','pane','close','w1:p1']])
        self.assertEqual(sum(call.startswith('restart') for call in pane.stop_calls), 1)
        self.assertTrue((self.home / 'worktrees/gamma').is_dir())
        self.assertIn('| gamma-unit |', self.doc.read_text())
        self.assertIs(self.journal()['done'], True)

    def test_live_director_in_pane_shell_stops_closes_and_renames(self) -> None:
        self.pane_shell(live=True)

    def test_exited_directors_pane_shell_closes_and_renames(self) -> None:
        self.pane_shell(live=False)
