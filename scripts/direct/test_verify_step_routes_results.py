"""Real verify step routing: results."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture, TARGETS, RUNNER


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_declined_lost_and_invalid_mac_results_return_to_waiting_mac(self) -> None:
        for answer in ("mac=declined\nreason=busy\n", "mac=lost\n", "invalid\n"):
            with self.subTest(answer=answer):
                root = self.fixture.root
                holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
                _ = (holders / "other").write_text("hold")
                observed = root / "pending-state"
                observed.unlink(missing_ok=True)
                marker = root / "mac-returned"
                marker.unlink(missing_ok=True)
                runner = Path(self.fixture.environment["VERIFY_MAC_RUNNER"])
                _ = runner.write_text(RUNNER.replace('"mac=passed\\nseconds=0\\n"', repr(answer))
                                      + '\n(Path(os.environ["PACKAGE_ROOT"]) / "mac-returned").touch()\n')
                targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
                _ = targets.write_text(TARGETS.replace(
                    'raise SystemExit(10 if any(holders.iterdir()) else 0)', '''
    marker = Path(os.environ["PACKAGE_ROOT"]) / "mac-returned"
    if marker.exists():
        record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
        (Path(os.environ["PACKAGE_ROOT"]) / "pending-state").write_text(record["state"])
        for path in holders.iterdir():
            path.unlink()
    raise SystemExit(10 if any(holders.iterdir()) else 0)'''))
                result = self.run_verify("test", "sample")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(observed.read_text(), "waiting_mac")
                self.assertTrue(any(event["args"][:2] == ["nextest", "run"] for event in self.fixture.cargo_events()))
                self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
