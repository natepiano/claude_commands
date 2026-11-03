"""Real verify.sh token signals behavior."""
from __future__ import annotations

import signal
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture


class VerifyTokenSignalsTests(VerifyTokenOwnershipFixture):
    def test_sigterm_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGTERM)

    def test_sigint_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGINT)
