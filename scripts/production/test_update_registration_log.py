"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import re
import unittest

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
        doc = self.doc.read_text(encoding="utf-8")
        doc = doc.replace("\n## Gates", "| `beta-unit` | `docs/beta.md` | `/tmp/beta` | `beta` | `beta-session` | — | — |\n\n## Gates")
        _ = self.doc.write_text(doc, encoding="utf-8")
        for number in range(1, 11):
            result = self.run_command("log", f"event {number}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("### STATE "), 1)
        self.assertEqual(len(re.findall(r"^- \d{2}:\d{2} [A-Z]{3}: event \d+$", log, re.MULTILINE)), 10)
        self.assertIn("alpha-unit: not stated; last merged none; waits on not stated", log)
        self.assertIn("beta-unit: not stated; last merged none; waits on not stated", log)

    def test_state_block_matches_unit_and_merge_history_and_reads_outstanding(self) -> None:
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit phase 2 (abc1234)")
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-04T10:00", "text": "review colors"}]) + "\n",
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": [{"unit": "alpha-unit", "phase": "Phase 2 of 3: panels",
                                                    "wait": "review"}],
                                           "merges_held": ["alpha phase 2"],
                                           "open_for_user": ["stale state item"],
                                           "next": ["merge alpha phase 2 when its gate is green",
                                                    "send the restarts plan at 03:30"]}), encoding="utf-8")
        before = self.run_command("log", "compacting", "--state", str(state), "--before-compaction")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertIn("alpha-unit: Phase 2 of 3: panels; last merged phase 2 (abc1234); waits on review", log)
        self.assertIn("Merges accepted but held: alpha phase 2", log)
        self.assertIn("Open for the user: review colors", log)
        self.assertNotIn("stale state item", log)
        self.assertIn("Next: merge alpha phase 2 when its gate is green; send the restarts plan at 03:30", log)

    def test_the_first_call_moves_the_scratchpad_state_and_rewrites_the_tick_prompt(self) -> None:
        slug = self.doc.name.removesuffix("-production.md")
        timer = self.state / "notifier" / f"showrunner-{slug}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text("TARGET=session:old-showrunner\n", encoding="utf-8")
        scratch = self.root / "tmp" / "claude-1000" / "-project" / "old-showrunner" / "scratchpad"
        (scratch / "unit_status").mkdir(parents=True)
        _ = (scratch / "unit_status" / "blocks_open").write_text("alpha-unit\n", encoding="utf-8")
        _ = (scratch / "showrunner_state.json").write_text('{"merges_held": ["from scratch"]}\n', encoding="utf-8")
        target = self.state / "showrunner" / slug / "session"
        target.mkdir(parents=True)
        _ = (target / "showrunner_state.json").write_text('{"merges_held": ["kept"]}\n', encoding="utf-8")
        prompt = self.state / "showrunner" / slug / "prompt.txt"
        _ = prompt.write_text("an old prompt with <SCRATCH>/unit_status\n", encoding="utf-8")
        env = {**self.env, "SHOWRUNNER_SCRATCH_ROOT": str(self.root / "tmp")}

        result = self.run_command("log", "first event", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"state-dir: ok — copied unit_status to {target}", result.stdout)
        self.assertEqual((target / "unit_status" / "blocks_open").read_text(encoding="utf-8"), "alpha-unit\n")
        self.assertIn("kept", (target / "showrunner_state.json").read_text(encoding="utf-8"))
        self.assertIn(f"unit_status.sh {target}/unit_status ", prompt.read_text(encoding="utf-8"))

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
