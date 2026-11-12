"""Resume messages keep the worker's live detached runs."""
from __future__ import annotations

import unittest

from scripts.lib.resume_message import AccountMove, BusyPass, NO_OWN_PASS, resume_message


class DetachedResumeTests(unittest.TestCase):
    def test_busy_and_idle_workers_receive_the_detached_run_context(self) -> None:
        context = "Detached run test-run remains live. Result: /scratch/test-run.json."
        for own in (BusyPass("dispatch-one"), NO_OWN_PASS):
            with self.subTest(own=own):
                text = resume_message("quota", move=AccountMove("codex1", "codex2"), own_pass=own, detached=context)
                self.assertIn(context, text)
                self.assertIn("moved from codex1 to codex2", text)
                self.assertNotIn("ended with no result", text)
                if isinstance(own, BusyPass):
                    self.assertIn("Continue pass dispatch-one", text)
