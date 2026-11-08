"""A refused step must never report a successful verify call."""
from __future__ import annotations

from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


class VerifyStepRefusalTests(VerifyTokenWaitFixture):
    def test_darwin_recorder_refusal_returns_nonzero_before_the_step(self) -> None:
        startup = self.root / "darwin.sh"
        _ = startup.write_text('export OSTYPE=darwin\nuname() { printf "Darwin\\n"; }\n')
        result = self.verify({**self.environment, "BASH_ENV": str(startup),
                              "VERIFY_PROC_ROOT": str(self.root / "no-proc")})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step refused", result.stderr)
        self.assertFalse((self.root / "cargo-calls").exists())
