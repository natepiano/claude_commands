"""Keep token-wait routing under queue placement."""
from __future__ import annotations

from dataclasses import replace
import os
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate


class VerifyMacLintTokenTests(unittest.TestCase):
    def test_token_wait_uses_placement_without_an_idle_marker(self) -> None:
        for command in ("lint", "check", "doc"):
            for state in ("free", "busy", "blocked", "same-job"):
                for marker in ("", "absent-marker"):
                    with self.subTest(command=command, state=state, marker=marker):
                        with PlacementFixture() as fixture, mock.patch.dict(os.environ, {"MAC_IDLE_MARKER": marker}):
                            request = placement_request(fixture)
                            argv = ("cargo", "clippy" if command == "lint" else command, "--workspace")
                            assert isinstance(request.local, wire.LocalAdmissionContext)
                            request = replace(request,
                                constraints=replace(request.constraints, command=argv),
                                local=replace(request.local, argv=argv), prerequisite=placement.TokenRequired())
                            eligibility = (placement.AllowedHost() if state == "free" else
                                placement.TemporaryExclusion("busy" if state == "same-job" else state))
                            evidence = publish_candidate(fixture, request, eligibility)
                            reply, = fixture.server.handle_request(request)
                            if state == "free":
                                self.assertIsInstance(reply, wire.Assignment)
                                assert isinstance(reply, wire.Assignment)
                                self.assertEqual(reply.assignment.destination, evidence.key.destination)
                            else:
                                self.assertIsInstance(reply, wire.PlacementWait)
                            self.assertEqual(request.episode in fixture.server.current_assignments, state == "free")


if __name__ == "__main__":
    _ = unittest.main()
