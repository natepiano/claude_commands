"""Real verify.sh token check behavior."""
from __future__ import annotations

import subprocess
import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, TEST_HANG_GUARD_SECS


class VerifyTokenCheckTests(VerifyTokenWaitFixture):
    def test_check_keep_going_reaches_cargo_in_either_order_with_features(self) -> None:
        # A migration phase that fixes only some crates reads every crate's result (hana, 2026-10-26).
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]}])
        for words, expected in ((("--keep-going",), "check --workspace --lib --bins --keep-going\n"),
                                (("--features", "fast", "--keep-going"),
                                 "check --workspace --lib --bins --keep-going --features sample/fast\n"),
                                (("--keep-going", "--features", "fast"),
                                 "check --workspace --lib --bins --keep-going --features sample/fast\n"),
                                ((), "check --workspace --lib --bins\n")):
            with self.subTest(words=words):
                (self.root / "cargo-calls").unlink(missing_ok=True)
                result = subprocess.run(["bash", str(VERIFY), "check", "sample", *words], cwd=self.root,
                                        env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((self.root / "cargo-calls").read_text(), expected)



if __name__ == "__main__":
    _ = unittest.main()
