"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import re
import unittest

from scripts.production import staff
from scripts.production.staff_test_support import fixture_member
from scripts.production.update_registration_test_support import RegistrationFixture

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_time_converts_midnight_and_daylight_change_to_document_zone(self) -> None:
        midnight = self.run_command("time", "2026-10-07T06:30:00+00:00")
        self.assertEqual(midnight.returncode, 0, midnight.stdout + midnight.stderr)
        self.assertIn("23:30 PDT", midnight.stdout)
        winter = self.run_command("time", "2026-11-01T10:30:00+00:00")
        self.assertEqual(winter.returncode, 0, winter.stdout + winter.stderr)
        self.assertIn("02:30 PST", winter.stdout)
        self.assertNotIn("UTC", midnight.stdout + winter.stdout)

    def test_time_accepts_one_digit_hour_in_document_zone(self) -> None:
        result = self.run_command("time", "9:05")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^time: ok — 09:05 P[DS]T\n$")

    def test_tenth_log_event_lists_every_doc_unit_without_state(self) -> None:
        alpha = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        beta = self.root / "beta"
        beta.mkdir()
        _ = (beta / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        _ = fixture_member("unit", name="beta", parent=alpha.parent, worktree=staff.PresentWorktree("natedev", beta))
        for number in range(1, 11):
            result = self.run_command("log", f"event {number}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("### STATE "), 1)
        self.assertEqual(len(re.findall(r"^- \d{2}:\d{2} [A-Z]{3}: event \d+$", log, re.MULTILINE)), 10)
        self.assertIn("alpha: not stated; last merged none; waits on not stated", log)
        self.assertIn("beta: not stated; last merged none; waits on not stated", log)

    def test_state_block_reports_recorded_checkpoint_after_the_branch_is_deleted(self) -> None:
        alpha = self.root / "alpha"
        (alpha / ".git").unlink()
        alpha.rmdir()
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(alpha))
        _ = self.git("commit", "--allow-empty", "-m", "Integrate completed work")
        commit = self.git("rev-parse", "alpha-branch")
        merged = commit[:7]
        history = self.root / "history/runs/alpha.jsonl"
        history.parent.mkdir(parents=True)
        _ = history.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint", "phase_id": "2",
                                          "working_dir": str(alpha), "checkpoint_commit": commit}) + "\n")
        self.env["UNIT_HISTORY_DIR"] = str(history.parent.parent)
        _ = self.git("worktree", "remove", str(alpha))
        _ = self.git("branch", "-D", "alpha-branch")
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-04T10:00", "text": "review colors"}]) + "\n",
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": [{"unit": self.fixture_staff_id("alpha"), "phase": "Phase 2 of 3: panels",
                                                    "wait": "review"}],
                                           "merges_held": ["alpha phase 2"],
                                           "open_for_user": ["stale state item"],
                                           "next": ["merge alpha phase 2 when its gate is green",
                                                    "send the restarts plan at 03:30"]}), encoding="utf-8")
        before = self.run_command("log", "compacting", "--state", str(state), "--before-compaction")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertIn(f"alpha: Phase 2 of 3: panels; last merged phase 2 ({merged}); waits on review", log)
        self.assertIn("Merges accepted but held: alpha phase 2", log)
        self.assertIn("Open for the user: review colors", log)
        self.assertNotIn("stale state item", log)
        self.assertIn("Next: merge alpha phase 2 when its gate is green; send the restarts plan at 03:30", log)

    def test_the_first_call_moves_the_scratchpad_state_without_rewriting_a_legacy_prompt(self) -> None:
        owner = staff.by_id(self.fixture_staff_id())
        assert owner is not None
        timer = self.state / "notifier" / f"showrunner-{owner.id}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(f"TARGET=session:{owner.id}\n", encoding="utf-8")
        scratch = self.root / "tmp" / "claude-1000" / "-project" / owner.current_session / "scratchpad"
        (scratch / "unit_status").mkdir(parents=True)
        _ = (scratch / "unit_status" / "blocks_open").write_text("alpha\n", encoding="utf-8")
        _ = (scratch / "showrunner_state.json").write_text('{"merges_held": ["from scratch"]}\n', encoding="utf-8")
        owner = self.fixture_staff_id()
        target = self.state / "showrunner" / owner / "session"
        target.mkdir(parents=True)
        _ = (target / "showrunner_state.json").write_text('{"merges_held": ["kept"]}\n', encoding="utf-8")
        prompt = self.state / "showrunner" / owner / "prompt.txt"
        _ = prompt.write_text("an old prompt with <SCRATCH>/unit_status\n", encoding="utf-8")
        env = {**self.env, "SHOWRUNNER_SCRATCH_ROOT": str(self.root / "tmp")}

        result = self.run_command("log", "first event", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"state-dir: ok — copied unit_status to {target}", result.stdout)
        self.assertEqual((target / "unit_status" / "blocks_open").read_text(encoding="utf-8"), "alpha\n")
        self.assertIn("kept", (target / "showrunner_state.json").read_text(encoding="utf-8"))
        self.assertEqual(prompt.read_text(encoding="utf-8"), "an old prompt with <SCRATCH>/unit_status\n")

        again = self.run_command("log", "second event", env=env)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("state-dir", again.stdout)

    def test_invalid_state_fails_log_without_writing(self) -> None:
        log = self.checkout / "production.log"
        _ = log.write_text("existing line\n", encoding="utf-8")
        for key, value in (("unknown", 1), ("units", "wrong"), ("merges_held", "wrong"), ("next", ["two\nlines"])):
            with self.subTest(key=key):
                state = self.root / f"{key}.json"
                _ = state.write_text(json.dumps({key: value}), encoding="utf-8")
                before = log.read_bytes()
                result = self.run_command("log", "event", "--state", str(state), "--before-compaction")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"log: failed — {state}: {key} must be", result.stdout)
                self.assertEqual(log.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
