"""The runner owns style writes while the Claude reviewer remains read-only."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from scripts.direct import review_launcher_test_support as support
from scripts.direct.wait_for_test_state import wait_for_file
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.git_test_support import commit_repository
from scripts.lint.style_review import BEGIN, END


class StyleLauncherTests(support.ReviewLauncherFixture):
    def commit_working_tree(self, message: str, *paths: str) -> str:
        return commit_repository(self.working_dir, message, *paths, allow_empty=True)

    def launch(self, session: Path, outcome: str = "Pass", *, since: str = "HEAD", until: str = "",
               fail: bool = False) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment["STUB_FINDINGS"] = ("1. minor: sample finding at reviewed.txt:1\n" if outcome == "Findings" else "") + (
            "No marked report\n" if outcome == "missing" else
            f"{BEGIN}\n| # | Rule | Result |\n| 1 | Scanner | Pass |\nStyle review: {outcome}\n{END}\n")
        environment["STUB_AGENT_CALLS"] = str(self.root / "style-agent-calls")
        if fail:
            environment["STUB_TURN_FAILURE"] = "1"
        if outcome != "missing" and since == "HEAD" and not fail:
            environment["STUB_EXPECT_PREPARED_STYLE"] = "1"
        command = self.review_command(session)
        command[2:2] = ["--phase", "3", "--style-since", since]
        if until:
            command[2:2] = ["--style-until", until]
        return subprocess.run(command, env=environment, text=True, capture_output=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def session_with_changes(self, name: str) -> Path:
        session = self.start_implementation(name)
        self.finish_implementation(session)
        _ = (self.working_dir / "reviewed.txt").write_text("new content\n")
        return session

    def state(self, session: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads((session / "phases/3/style_review_state.json").read_text()))

    def test_prepared_before_dispatch_assignment_once_and_marked_report_captured(self) -> None:
        session = self.session_with_changes("style-prepared")
        result = self.launch(session, "Findings")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state(session)
        self.assertEqual(state["state"], "complete")
        prompt = (session / "agent_prompt_received.txt").read_text()
        self.assertEqual(prompt.count("## Phase style review"), 1)
        self.assertNotIn("$diff", prompt)
        self.assertIn("Skip every write step", prompt)
        self.assertIn("+new content", (session / "prepared_at_dispatch.diff").read_text())
        self.assertIn("Style review: Findings", Path(str(state["report"])).read_text())
        self.assertIn("sample finding", (session / "phases/3/review_findings_1_contract.txt").read_text())

    def test_committed_range_excludes_worktree_changes_through_review_sh(self) -> None:
        session = self.session_with_changes("style-range")
        baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.working_dir, text=True).strip()
        end = self.commit_working_tree("Pinned range", "reviewed.txt")
        _ = (self.working_dir / "reviewed.txt").write_text("other work in flight\n")
        result = self.launch(session, since=baseline, until=end)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state(session)
        self.assertEqual(state["state"], "complete")
        self.assertEqual(state["until"], end)
        directory = Path(str(state["input_directory"]))
        self.assertEqual((directory / "style-review-additions.txt").read_text(), "+new content\n")
        calls = [cast(list[str], json.loads(line)) for line in (self.root / "style-agent-calls").read_text().splitlines()]
        dispatch, = calls
        self.assertEqual(dispatch[1], "readonly")
        self.assertNotIn("--sandbox", dispatch)

    def test_pass_and_rerun_use_new_inputs_without_turn_records(self) -> None:
        session = self.session_with_changes("style-rerun")
        first = self.launch(session)
        self.assertEqual(first.returncode, 0, first.stderr)
        old = self.state(session)
        old_input = Path(str(old["input_directory"]))
        before = (old_input / "style-review.diff").read_bytes()
        self.assertEqual(self.turn_records(session), [])
        _ = (self.working_dir / "reviewed.txt").write_text("retry content\n")
        second = self.launch(session)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.state(session)["state"], "complete")
        self.assertNotEqual(self.state(session)["input_directory"], old["input_directory"])
        self.assertEqual((old_input / "style-review.diff").read_bytes(), before)
        self.assertIn("+retry content", (session / "prepared_at_dispatch.diff").read_text())
        records = self.turn_records(session)
        self.assertEqual(records, [])

    def test_missing_error_and_failed_dispatch_remain_owed(self) -> None:
        for outcome, fail in (("missing", False), ("Error", False), ("Pass", True)):
            with self.subTest(outcome=outcome, fail=fail):
                session = self.session_with_changes(f"style-{outcome}-{fail}")
                result = self.launch(session, outcome, fail=fail)
                self.assertEqual(result.returncode, 1 if fail else 0, result.stdout + result.stderr)
                self.assertEqual(self.state(session)["state"], "owed")

    def test_off_and_empty_are_complete_without_assignment(self) -> None:
        for off in (True, False):
            with self.subTest(off=off):
                session = self.start_implementation(f"style-auto-{off}")
                self.finish_implementation(session)
                # start_phase writes a plan outside the tracked baseline; commit it for the empty case.
                _ = self.commit_working_tree("phase plan")
                config = self.root / "lint.conf"
                _ = config.write_text(f"style_review={'off' if off else 'on'}\n")
                environment = self.environment()
                environment["LINT_CONFIG_FILE"] = str(config)
                environment["CONF_DIR"] = str(Path(config).parent)
                command = self.review_command(session)
                command[2:2] = ["--phase", "3", "--style-since", "HEAD"]
                result = subprocess.run(command, env=environment, text=True, capture_output=True,
                                        check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.state(session)["state"], "complete")
                self.assertIn("Style review: " + ("Off" if off else "No changes to review"),
                              Path(str(self.state(session)["report"])).read_text())
                self.assertNotIn("## Phase style review", (session / "agent_prompt_received.txt").read_text())

    def test_preparation_error_leaves_style_owed_but_code_review_runs(self) -> None:
        session = self.session_with_changes("style-bad-base")
        result = self.launch(session, since="missing-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state(session)["state"], "owed")
        self.assertIn("invalid baseline", Path(str(self.state(session)["report"])).read_text())
        self.assertNotIn("## Phase style review", (session / "agent_prompt_received.txt").read_text())
        self.assertEqual((session / "phases/3/review_status_contract").read_text().strip(), "reviewed")

    def test_a_live_style_pass_refuses_another_dispatch_and_cancellation_allows_retry(self) -> None:
        session = self.session_with_changes("style-single-owner")
        environment = self.environment()
        finish = self.root / "finish-style-owner"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        command = self.review_command(session)
        command[2:2] = ["--phase", "3", "--style-since", "HEAD"]
        process = subprocess.Popen(command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for_file(session / "agent_prompt_received.txt")
            before = self.state(session)
            retry = list(command)
            retry[retry.index(str(session)) + 6] = "101"
            retry[2:2] = ["--lens", "adversary"]
            refused = subprocess.run(retry, env=environment, text=True, capture_output=True,
                                     check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn("style review in flight: pass-1_contract", refused.stderr)
            self.assertEqual(self.state(session), before)
            process.terminate()
            finish.touch()
            out, err = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 143, out + err)
            self.assertFalse(self.state(session)["in_flight"])
            completed = self.launch(session)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(self.state(session)["state"], "complete")
            self.assertNotEqual(self.state(session)["input_directory"], before["input_directory"])
        finally:
            finish.touch()
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_canceled_dispatch_keeps_style_owed(self) -> None:
        session = self.session_with_changes("style-cancel")
        environment = self.environment()
        finish = self.root / "finish-style-worker"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        command = self.review_command(session)
        command[2:2] = ["--phase", "3", "--style-since", "HEAD"]
        process = subprocess.Popen(command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for_file(session / "agent_prompt_received.txt")
            process.terminate()
            finish.touch()
            out, err = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 143, out + err)
            self.assertEqual(self.state(session)["state"], "owed")
        finally:
            finish.touch()
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
