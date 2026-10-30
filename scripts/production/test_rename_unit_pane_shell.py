"""Rename refuses pane-shell units before stopping or journaling any move."""
from pathlib import Path

from scripts.production.rename_unit_test_support import UnitRenameFixture
from scripts.production.unit_operation_test_support import PaneShellFixture, assert_pane_shell_refused


class RenamePaneShellTests(UnitRenameFixture):
    def pane_shell(self, *, live: bool) -> None:
        _ = self.seed_record()
        pane = PaneShellFixture(self, self.rust / "claude-demo-alpha", Path(self.env["HERDR_PROC"]), live=live)
        assert_pane_shell_refused(self, pane, lambda: self.rename("apply"),
                                 journal=self.state / "demo/rename_unit.json", operation="rename")

    def test_live_director_in_pane_shell_refuses_before_stop(self) -> None:
        self.pane_shell(live=True)

    def test_exited_directors_pane_shell_refuses_before_stop(self) -> None:
        self.pane_shell(live=False)
