"""Check concurrent reviews preserve phase ownership and detect edits."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.wait_for_test_state import wait_until
from pathlib import Path
import subprocess


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def run_concurrent_review_lenses(self, session: Path, lenses: tuple[str, ...]) -> dict[str, str]:
        finish = self.root / f"{session.name}-finish"
        environment = self.environment()
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        running = [subprocess.Popen(["bash", str(self.review_script), "--pool", "code",
                                     "--lease", f"builders:3:broad:{lens}:1",
                                     *self.review_command(session, lens, "")[2:]],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=environment) for lens in lenses]
        try:
            wait_until(lambda: all((session / f"review_slot_{lens}").exists() for lens in lenses)
                       or any(process.poll() is not None for process in running),
                       session, processes=running)
            finish.touch()
            completed = [(process, process.communicate(timeout=TEST_HANG_GUARD_SECS))
                         for process in running]
            for process, (_, errors) in completed:
                self.assertEqual(process.returncode, 0, errors)
            return {lens: (session / f"review_slot_{lens}").read_text().strip() for lens in lenses}
        finally:
            finish.touch()
            for process in running:
                if process.poll() is None:
                    process.terminate()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_a_parallel_phase_writing_its_own_files_during_a_review_leaves_the_review_passed(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-writer", "other.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("changed the working tree", result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "reviewed\n")
        log = "".join(path.read_text(encoding="utf-8") for path in phase_dir.glob("review_agent_1*.log"))
        self.assertIn("open phase 4's files changed during the review: other.txt", log)
        self.assertEqual(list(phase_dir.glob("review_tree_*")), [])

    def test_a_parallel_phase_commit_during_a_review_leaves_the_review_passed(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-commit", "other.txt", commit_change=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "reviewed\n")

    def test_beside_a_parallel_phase_an_edit_to_the_reviewed_phases_file_still_fails(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-own-edit", "reviewed.txt")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree: reviewed.txt", result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "error\n")

    def test_beside_a_parallel_phase_an_edit_no_phase_holds_still_fails(self) -> None:
        result, _ = self.side_by_side_review("parallel-unheld-edit", "loose.txt")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree: loose.txt", result.stderr)

    def test_a_assigned_reviewer_that_edits_fails_and_names_the_worker_and_path(self) -> None:
        session_dir = self.start_implementation("edited")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        edited = self.working_dir / "reviewed.txt"

        result = self.run_assigned_review(session_dir, worker, edit_path=str(edited))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        reviews = [
            event for event in self.pass_events("edited")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(reviews[-1].get("status"), "error")

    def test_a_reviewer_commit_is_detected_and_names_its_changed_path(self) -> None:
        session_dir = self.start_implementation("committed-edit")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)

        result = self.run_assigned_review(
            session_dir,
            worker,
            edit_path=str(self.working_dir / "reviewed.txt"),
            commit_change=True,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_a_change_to_an_existing_untracked_file_is_detected(self) -> None:
        session_dir = self.start_implementation("untracked-edit")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        untracked = self.working_dir / "notes.txt"
        _ = untracked.write_text("before review\n", encoding="utf-8")

        result = self.run_assigned_review(
            session_dir, worker, edit_path=str(untracked)
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("notes.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_both_lenses_run_at_once_without_overwriting_each_other(self) -> None:
        """The whole reason the lens exists: two reviewers, one session dir.

        Unsuffixed artifacts and a shared pass slot made the reviewers of
        a broad review destroy each other's work -- the last launched owned
        every file, and each start-pass closed the one before it as interrupted,
        leaving the ledger describing whichever happened to finish last.
        """
        session_dir = self.start_implementation("lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        lenses = ("adversary", "contract")

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in lenses
        ]
        for process in running:
            _, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, errors)

        for lens in lenses:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        recorded = [
            (event["event_type"], event.get("pass_kind"), event.get("team_slot"))
            for event in self.pass_events("lenses")
        ]
        self.assertEqual(
            sorted(entry for entry in recorded if entry[1] == "review"),
            sorted(
                [
                    ("pass_started", "review", slot)
                    for slot in ("impl", "test")
                ]
                + [
                    ("pass_finished", "review", slot)
                    for slot in ("impl", "test")
                ]
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in self.pass_events("lenses")],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for slot in ("impl", "test"):
            self.assertIn(f"[{slot}] register:", board)

    def test_ignored_code_flags_both_lenses_run_at_once_without_overwriting_each_other(self) -> None:
        """The whole reason the lens exists: two reviewers, one session dir.

        Unsuffixed artifacts and a shared pass slot made the reviewers of
        a broad review destroy each other's work -- the last launched owned
        every file, and each start-pass closed the one before it as interrupted,
        leaving the ledger describing whichever happened to finish last.
        """
        session_dir = self.start_implementation("lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        lenses = ("adversary", "contract")

        slots = self.run_concurrent_review_lenses(session_dir, lenses)
        self.assertEqual(set(slots.values()), {"review", "review2"})

        for lens in lenses:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            log = f"review_agent_1_{lens}-{slots[lens]}.log"
            self.assertTrue((session_dir / log).is_file())
            self.assertEqual((session_dir / f"review_agent_{lens}.log").readlink(), Path(log))

        recorded = [
            (event["event_type"], event.get("pass_kind"), event.get("team_slot"))
            for event in self.pass_events("lenses")
        ]
        self.assertEqual(
            sorted(entry for entry in recorded if entry[1] == "review"),
            sorted(
                [
                    ("pass_started", "review", slot)
                    for slot in slots.values()
                ]
                + [
                    ("pass_finished", "review", slot)
                    for slot in slots.values()
                ]
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in self.pass_events("lenses")],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for slot in slots.values():
            self.assertIn(f"[{slot}] register:", board)

    def test_three_lenses_run_at_once_each_in_a_worker_of_its_own(self) -> None:
        """The craft lens joins the other two in the `review` worker.

        No writer sits there, so its pass is a third record beside the two
        lenses' and closes neither of them.
        """
        session_dir = self.start_implementation("three-lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        workers = {"adversary": "test", "contract": "impl", "craft": "review"}

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in workers
        ]
        for process in running:
            _, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, errors)

        for lens in workers:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        events = self.pass_events("three-lenses")
        reviews = [event for event in events if event.get("pass_kind") == "review"]
        self.assertEqual(
            sorted((str(event["event_type"]), str(event.get("team_slot"))) for event in reviews),
            sorted(
                (event_type, slot)
                for event_type in ("pass_started", "pass_finished")
                for slot in workers.values()
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in events],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for lens, slot in workers.items():
            self.assertIn(f"[{slot}] register: {lens} review up", board)

    def test_ignored_code_flags_three_lenses_run_at_once_each_in_a_worker_of_its_own(self) -> None:
        """Each lens holds a separate reviewer and closes only its own pass."""
        session_dir = self.start_implementation("three-lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        workers = self.run_concurrent_review_lenses(session_dir, ("adversary", "contract", "craft"))
        self.assertEqual(set(workers.values()), {"review", "review2", "review3"})

        for lens in workers:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            log = f"review_agent_1_{lens}-{workers[lens]}.log"
            self.assertTrue((session_dir / log).is_file())
            self.assertEqual((session_dir / f"review_agent_{lens}.log").readlink(), Path(log))

        events = self.pass_events("three-lenses")
        reviews = [event for event in events if event.get("pass_kind") == "review"]
        self.assertEqual(
            sorted((str(event["event_type"]), str(event.get("team_slot"))) for event in reviews),
            sorted(
                (event_type, slot)
                for event_type in ("pass_started", "pass_finished")
                for slot in workers.values()
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in events],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for lens, slot in workers.items():
            self.assertIn(f"[{slot}] register: {lens} review follow-up to", board)


if __name__ == "__main__":
    _ = unittest.main()
