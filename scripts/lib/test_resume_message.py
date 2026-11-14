"""Resume messages keep the worker's live detached runs."""
from __future__ import annotations

import unittest

from scripts.lib.resume_message import (AccountMove, BusyPass, NO_OWN_PASS, PaneWorkerResume, ResumeWorkers,
                                        WorkersHeld, resume_message)
from scripts.production import staff


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


class HeldWorkersTests(unittest.TestCase):
    def test_held_workers_give_the_notice_and_no_worker_line(self) -> None:
        notice = "Codex workers have no capacity. Send no assign."
        resumable = resume_message("restart", workers=ResumeWorkers(
            panes=(PaneWorkerResume(staff.StaffId("worker-one"), BusyPass("dispatch-one")),)))
        self.assertIn("Worker worker-one: busy dispatch dispatch-one", resumable)
        for detached in ("", "Detached run test-run remains live."):
            with self.subTest(detached=detached):
                text = resume_message("restart", workers=WorkersHeld(notice), detached=detached)
                self.assertIn("Continue where you stopped.", text)
                self.assertIn(notice, text)
                self.assertIn(detached, text)
                self.assertNotIn("Worker ", text)
                self.assertNotIn("Resume your workers", text)
