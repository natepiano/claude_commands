"""Transfer refuses pane-shell units before stopping or journaling any move."""
from pathlib import Path

from scripts.production.transfer_unit_test_support import UnitTransferFixture
from scripts.production.unit_operation_test_support import PaneShellFixture, assert_pane_shell_refused


class TransferPaneShellTests(UnitTransferFixture):
    def pane_shell(self, *, live: bool) -> None:
        _ = self.seed_record()
        pane = PaneShellFixture(self, self.unit, Path(self.env["HERDR_PROC"]), live=live)
        assert_pane_shell_refused(self, pane, lambda: self.transfer("apply"),
                                 journal=self.root / "state/showrunner/demo/transfer_unit.json", operation="transfer")

    def test_live_director_in_pane_shell_refuses_before_stop(self) -> None:
        self.pane_shell(live=True)

    def test_exited_directors_pane_shell_refuses_before_stop(self) -> None:
        self.pane_shell(live=False)
