"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import os
import unittest

from scripts.production.update_registration_test_support import RegistrationFixture

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_start_and_resume_retarget_only_updates_and_retire_old_session(self) -> None:
        doc_bytes = self.doc.read_bytes()
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok", first.stdout)
        initial_calls = self.notifier_calls()
        self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", "showrunner-example"]]), 1)
        updates = next(call for call in initial_calls if call[:2] == ["new", "showrunner-example"])
        self.assertEqual(updates[updates.index("--to") + 1], "session:current-session-id")
        self.assertIn("next_due", first.stdout)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", instance]]), 1)

        new_env = {**self.env, "CLAUDE_CODE_SESSION_ID": "resume-session-id"}
        resumed = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        repeated = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        # The showrunner's name is written nowhere: no registry, no doc line, no commit.
        self.assertFalse(self.config.exists())
        self.assertEqual(self.doc.read_bytes(), doc_bytes)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        calls = self.notifier_calls()
        self.assertEqual(len([call for call in calls if call[:2] == ["new", "showrunner-example"]]), 3)
        updates = [call for call in calls if call[:2] == ["new", "showrunner-example"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates],
                         ["session:current-session-id", "session:resume-session-id", "session:resume-session-id"])
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in calls if call[:2] == ["new", instance]]), 1)

    def test_on_demand_keeps_a_stopped_timer_as_the_showrunners_record(self) -> None:
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        _ = self.git("commit", "-qam", "dailies on demand")
        before = len(self.notifier_calls())
        resumed = self.run_command("register")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("register: ok — showrunner-example on demand: no scheduled dailies", resumed.stdout)
        calls = self.notifier_calls()[before:]
        self.assertIn(["new", "showrunner-example"], [call[:2] for call in calls])
        self.assertIn(["stop", "showrunner-example"], calls)
        self.assertNotIn(["remove", "showrunner-example"], calls)
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")
        again = self.run_command("register")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        # A re-register retargets the timer and stops it again, so it stays stopped.
        self.assertEqual([call[0] for call in self.notifier_calls()[before + len(calls):]],
                         ["new", "stop", "status"])
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")

    def test_a_new_production_with_no_updates_line_starts_quiet(self) -> None:
        self.without_updates_line()
        first = self.run_command("register")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok — showrunner-example on demand: no scheduled dailies", first.stdout)
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")
        self.assertNotIn("footer", first.stdout)

    def test_a_line_asking_for_a_schedule_still_schedules(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("every 15 minutes", "every 20 minutes"), encoding="utf-8")
        _ = self.git("commit", "-qam", "every 20 minutes")

        result = self.run_command("register")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = next(call for call in self.notifier_calls() if call[:2] == ["new", "showrunner-example"])
        self.assertEqual(updates[updates.index("--every") + 1], "20")
        self.assertNotIn(["stop", "showrunner-example"], self.notifier_calls())
        self.assertIn("next_due", result.stdout)

    def test_register_requires_the_environment_session_id(self) -> None:
        without_id = {key: value for key, value in self.env.items() if key != "CLAUDE_CODE_SESSION_ID"}
        missing_id = self.run_command("register", "--session", "first-showrunner", env=without_id)
        self.assertEqual(missing_id.returncode, 2)
        self.assertIn("register: failed", missing_id.stdout + missing_id.stderr)
        self.assertEqual(self.notifier_calls(), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_refuses_dirty_production_doc_without_side_effects(self) -> None:
        with self.doc.open("a", encoding="utf-8") as output:
            _ = output.write("\nA local note.\n")
        before = self.doc.read_bytes()
        result = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("register: failed — the production doc has uncommitted edits; commit or discard them first", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notifier_calls(), [])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_warns_for_a_session_not_named_for_the_production_and_still_registers(self) -> None:
        record = self.state / "sessions" / f"{os.getpid()}.json"
        _ = record.write_text(json.dumps({"pid": os.getpid(), "sessionId": "current-session-id", "name": "other"}),
                              encoding="utf-8")
        result = self.run_command("register")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("register: warn", result.stdout)
        self.assertIn("production example has showrunner other", result.stdout)
        self.assertIn("/showrunner:rename plan example other", result.stdout)
        self.assertIn("register: ok", result.stdout)
        self.assertNotEqual(self.notifier_calls(), [])

    def test_registered_prompt_keeps_status_save_dailies_call_and_render_state(self) -> None:
        result = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompts = list((self.state / "showrunner").rglob("prompt.txt"))
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0].read_text(encoding="utf-8")
        self.assertIn("unit_status.txt", prompt)
        self.assertIn("/showrunner:dailies gantt", prompt)
        state_dir = self.state / "showrunner" / self.doc.name.removesuffix("-production.md") / "session"
        self.assertEqual(prompt.count(f"Pass `--render-state {state_dir}/dailies_state.json` to the builder"), 1)
        self.assertIn(f"unit_status.sh {state_dir}/unit_status ", prompt)
        self.assertNotIn("<STATE_DIR>", prompt)
        self.assertEqual(prompt.count("--render-state"), 1)
        self.assertIn("America/Los_Angeles", prompt)
        self.assertIn(f"--production {self.doc}", prompt)
        self.assertNotIn("first-showrunner", prompt)


if __name__ == "__main__":
    _ = unittest.main()
