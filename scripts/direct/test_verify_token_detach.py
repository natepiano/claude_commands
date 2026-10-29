"""Real verify.sh token detach behavior."""
from __future__ import annotations

import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY


class VerifyTokenDetachTests(VerifyTokenWaitFixture):
    def test_a_codex_worker_call_detaches_and_returns_without_building(self) -> None:
        result = self.verify(self.detach_environment(0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mesh_runs_calls(), [[
            "start", "--label", "verify.sh check sample", "--",
            "bash", str(VERIFY.resolve()), "check", "sample",
        ]])
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertEqual(self.records(), [])

    def test_a_caller_mesh_runs_cannot_detach_runs_attached(self) -> None:
        for status, note in ((3, False), (1, True)):
            with self.subTest(status=status):
                for path in (self.root / "mesh-runs-calls", self.root / "cargo-calls"):
                    path.unlink(missing_ok=True)
                result = self.verify(self.detach_environment(status))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.mesh_runs_calls()), 1)
                self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))
                self.assertEqual("could not detach" in result.stderr, note, result.stderr)

    def test_the_detached_run_itself_does_not_detach_again(self) -> None:
        environment = {**self.detach_environment(0), "UNIT_DETACHED_RUN": "1"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mesh_runs_calls(), [])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))



if __name__ == "__main__":
    _ = unittest.main()
