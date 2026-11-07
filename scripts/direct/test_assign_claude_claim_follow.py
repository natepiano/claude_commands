"""Test Claude follow refusal and claim behavior."""
from __future__ import annotations

import json
from typing import cast

from scripts.direct.assign_claude_test_support import AssignClaudeLauncherFixture


class AssignClaudeLauncherFollowTests(AssignClaudeLauncherFixture):
    def test_busy_or_missing_worker_preserves_the_pass_and_status(self) -> None:
        session = self.start_phase("busy-or-gone")
        transcript = self.claude_worker(session)
        status = session / "impl_status_fixture-impl"
        _ = status.write_text("previous\n")
        before = self.open_passes(session)
        original = transcript.read_bytes()
        for state in ("busy", "gone"):
            with self.subTest(state=state):
                _ = (self.root / "worker_state").write_text(state)
                result = self.worker_launch(session, to="fixture-impl")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("busy or gone", result.stderr)
                self.assertEqual(status.read_text(), "previous\n")
                self.assertEqual(self.open_passes(session), before)
                self.assertEqual(transcript.read_bytes(), original)

    def test_follow_claim_records_only_the_launcher_and_worker(self) -> None:
        session = self.start_phase("claim-fields")
        _ = self.claude_worker(session)
        sender = self.root / "send"
        _ = sender.write_text(sender.read_text().replace(
            "behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')",
            "claim = pathlib.Path(os.environ['STUB_SUMMARY']).parent / 'impl_claim_fixture-impl.json'\n"
            + "(root / 'claim-at-delivery.json').write_bytes(claim.read_bytes())\n"
            + "behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')"))
        result = self.worker_launch(session, to="fixture-impl")
        self.assertEqual(result.returncode, 0, result.stderr)
        claim = cast(dict[str, str | int], json.loads((self.root / "claim-at-delivery.json").read_text()))
        self.assertEqual(set(claim), {"pid", "started", "worker"})
        self.assertIsInstance(claim["started"], str)
        self.assertTrue(claim["started"])
        self.assertGreater(int(claim["pid"]), 0)
        self.assertEqual(claim["worker"], "fixture-impl")
