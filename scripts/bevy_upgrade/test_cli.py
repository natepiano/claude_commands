"""Exercise the real Bevy upgrade CLI with private fixtures and tool boundaries."""
from __future__ import annotations

import json
import shlex
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, final

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_test_support import CliFixture, ZONE, REGISTRY, upgrade_record, setUpModule as setUpModule


@final
class CliTests(CliFixture):
    def test_start_reads_the_production_checkout_and_writes_an_open_record(self) -> None:
        result = self.run_cli("start", "0.20")
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["slug"], "demo")
        self.assertEqual(current["production"], str(self.doc))
        self.assertEqual(current["checkout"], str(self.checkout))
        self.assertEqual(current["target"], "0.20")
        self.assertEqual(current["bevy"], ["0.19.1"])
        self.assertIsNone(current["closed"])
        self.assertIsNone(current["dependencies"]["adapter"]["answer"])
        self.assertIn(f"Bevy 0.20 upgrade for demo: {self.checkout}, on Bevy 0.19.1.", result.stdout)
        self.assertIn("| adapter | git branch=main @ 1234567 on Bevy 0.19.1 |", result.stdout)


    def test_all_non_start_verbs_with_no_record_print_the_same_guidance(self) -> None:
        for arguments in ((), ("status",), ("check",), ("approve", "adapter", "yes"),
                          ("decline", "adapter", "no"), ("forget", "adapter"), ("note", "adapter")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout,
                                 "no Bevy upgrade is tracked for demo; start one with /showrunner:bevy_upgrade start <version>\n")
                self.assertFalse((self.root / "state/demo.json").exists())


    def test_no_registered_production_has_the_specified_error(self) -> None:
        result = self.run_cli(explicit_production=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr,
                         "bevy_upgrade: this session runs no production; pass --production <doc>\n")


    def test_registered_production_comes_from_the_session_timer(self) -> None:
        timer = self.root / "notifier/showrunner-demo"
        timer.mkdir(parents=True)
        check_command = shlex.join([str(self.root / "production_check.sh"), str(self.doc)])
        _ = (timer / "conf").write_text(
            f'TARGET=session:no-real-session\nCHECK={check_command}\n',
            encoding="utf-8",
        )
        result = self.run_cli("start", "0.20", explicit_production=False)
        self.assert_table(result)
        self.assertEqual(self.read_record()["production"], str(self.doc))


    def test_approve_decline_forget_and_note_end_in_a_table(self) -> None:
        self.begin()
        words = '  yes, use this crate "as is"  '
        before = datetime.now(ZONE).date().isoformat()
        approved = self.run_cli("approve", "adapter", words)
        after = datetime.now(ZONE).date().isoformat()
        self.assert_table(approved)
        answer = self.read_record()["dependencies"]["adapter"]["answer"]
        self.assertIsNotNone(answer)
        assert answer is not None
        self.assertEqual(answer["answer"], "approved")
        self.assertEqual(answer["words"], words)
        self.assertIn(answer["at"], (before, after))
        self.assertIn(f'- adapter: approved {answer["at"]}: "{words}"', approved.stdout)
        declined = self.run_cli("decline", "adapter", "use a release instead")
        self.assert_table(declined)
        replaced = self.read_record()["dependencies"]["adapter"]["answer"]
        assert replaced is not None
        self.assertEqual(replaced["answer"], "declined")
        self.assertEqual(replaced["words"], "use a release instead")
        noted = self.run_cli("note", "adapter", "CI needs a feature")
        self.assert_table(noted)
        self.assertIn("Notes:\n- adapter: CI needs a feature", noted.stdout)
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["answer"], replaced)
        forgotten = self.run_cli("forget", "adapter")
        self.assert_table(forgotten)
        self.assertIsNone(self.read_record()["dependencies"]["adapter"]["answer"])
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["note"], "CI needs a feature")
        cleared = self.run_cli("note", "adapter")
        self.assert_table(cleared)
        self.assertEqual(self.read_record()["dependencies"]["adapter"]["note"], "")
        self.assertNotIn("Notes:", cleared.stdout)


    def test_every_successful_verb_refreshes_and_persists_changed_pins(self) -> None:
        self.begin()
        for index, arguments in enumerate((("status",), (), ("start", "0.20"),
                                           ("approve", "adapter", "yes"), ("decline", "adapter", "no"),
                                           ("forget", "adapter"), ("note", "adapter", "why")), start=1):
            with self.subTest(arguments=arguments):
                bevy = f"0.19.{index + 1}"
                self.write_lock(version=f"1.2.{index + 3}", bevy=bevy)
                result = self.run_cli(*arguments)
                self.assert_table(result)
                current = self.read_record()
                self.assertEqual(current["dependencies"]["adapter"]["pin"]["version"], f"1.2.{index + 3}")
                self.assertEqual(current["dependencies"]["adapter"]["bevy"], bevy)
                self.assertEqual(current["bevy"], [bevy])
                self.assertIn(f"on Bevy {bevy}", result.stdout)


    def test_each_successful_run_saves_once_after_the_verbs_change(self) -> None:
        calls = self.root / "save-calls"
        commands: tuple[tuple[str, ...], ...] = (
            ("start", "0.20"), ("check",), ("status",), (), ("start", "0.20"),
            ("approve", "adapter", "use this pin"), ("decline", "adapter", "wait"),
            ("forget", "adapter"), ("note", "adapter", "why"), ("note", "adapter"),
        )
        for index, arguments in enumerate(commands, start=1):
            with self.subTest(arguments=arguments):
                self.write_lock(version=f"1.2.{index}")
                calls.unlink(missing_ok=True)
                self.assert_table(self.run_cli(*arguments, count_saves=True))
                self.assertEqual(calls.read_text(encoding="utf-8"), "saved\n")
                current = self.read_record()["dependencies"]["adapter"]
                self.assertEqual(current["pin"]["version"], f"1.2.{index}")
                verb = arguments[0] if arguments else "status"
                answer = current["answer"]
                if verb in ("approve", "decline"):
                    assert answer is not None
                    assert len(arguments) > 2
                    self.assertEqual(answer["answer"], "approved" if verb == "approve" else "declined")
                    self.assertEqual(answer["words"], arguments[2])
                elif verb == "forget":
                    self.assertIsNone(answer)
                elif verb == "note":
                    self.assertEqual(current["note"], arguments[2] if len(arguments) > 2 else "")


    def test_failed_pin_reads_still_save_each_user_change_once(self) -> None:
        self.begin()
        original = self.read_record()
        (self.checkout / "Cargo.lock").unlink()
        calls = self.root / "save-calls"
        commands: tuple[tuple[str, ...], ...] = (
            ("approve", "adapter", "yes"), ("decline", "adapter", "no"),
            ("forget", "adapter"), ("note", "adapter", "why"), ("note", "adapter"),
        )
        for arguments in commands:
            with self.subTest(arguments=arguments):
                calls.unlink(missing_ok=True)
                result = self.run_cli(*arguments, count_saves=True)
                self.assert_table(result)
                self.assertTrue(result.stdout.startswith("pins could not be re-read: "))
                self.assertEqual(calls.read_text(encoding="utf-8"), "saved\n")
                current = self.read_record()
                self.assertEqual(current["pins_read"], original["pins_read"])
                item = current["dependencies"]["adapter"]
                self.assertEqual(item["pin"], original["dependencies"]["adapter"]["pin"])
                answer = item["answer"]
                if arguments[0] in ("approve", "decline"):
                    assert answer is not None
                    assert len(arguments) > 2
                    self.assertEqual(answer["answer"], "approved" if arguments[0] == "approve" else "declined")
                    self.assertEqual(answer["words"], arguments[2])
                elif arguments[0] == "forget":
                    self.assertIsNone(answer)
                else:
                    self.assertEqual(item["note"], arguments[2] if len(arguments) > 2 else "")


    def test_status_replaces_removed_dependencies_without_approving_new_ones(self) -> None:
        self.begin()
        self.assert_table(self.run_cli("approve", "adapter", "only this crate"))
        self.write_lock(name="replacement")
        self.assert_table(self.run_cli("status"))
        current = self.read_record()
        self.assertEqual(set(current["dependencies"]), {"replacement"})
        self.assertIsNone(current["dependencies"]["replacement"]["answer"])


    def test_non_start_verbs_keep_stored_pins_and_time_when_the_lock_is_unreadable(self) -> None:
        self.begin()
        old = self.read_record()
        read_at = datetime.fromisoformat(old["pins_read"]).astimezone(ZONE).strftime("%H:%M %Z")
        (self.checkout / "Cargo.lock").unlink()
        for arguments in (("status",), ("approve", "adapter", "use the stored pin"),
                          ("decline", "adapter", "no"), ("forget", "adapter"), ("note", "adapter", "why")):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assert_table(result)
                first_line = result.stdout.splitlines()[0]
                self.assertTrue(first_line.startswith(f"pins could not be re-read: {self.checkout / 'Cargo.lock'}: "))
                self.assertTrue(first_line.endswith(f"; showing pins read {read_at}"), first_line)
                current = self.read_record()
                self.assertEqual(current["pins_read"], old["pins_read"])
                self.assertEqual(current["dependencies"]["adapter"]["pin"], old["dependencies"]["adapter"]["pin"])
                self.assertEqual(current["bevy"], old["bevy"])
                self.assertIn("git branch=main @ 1234567 on Bevy 0.19.1", result.stdout)


    def test_start_refuses_an_unreadable_lock_and_writes_no_watch(self) -> None:
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("start", "0.20")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith(f"bevy_upgrade: {self.checkout / 'Cargo.lock'}: "))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse((self.root / "state/demo.json").exists())


    def test_record_refusals_print_one_error_and_preserve_the_watch(self) -> None:
        self.begin()
        original = self.read_record()
        cases = (
            (("start", "0.21"), "a watch for 0.20 is open; stop it first"),
            (("start", "0.20.0"), "target must be major.minor, such as 0.20"),
            (("approve", "missing", "yes"), "missing is not tracked; tracked: adapter"),
            (("decline", "missing", "no"), "missing is not tracked; tracked: adapter"),
            (("forget", "missing"), "missing is not tracked; tracked: adapter"),
            (("note", "missing", "why"), "missing is not tracked; tracked: adapter"),
        )
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments, count_saves=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"bevy_upgrade: {reason}\n")
                self.assertEqual(self.read_record(), original)
                self.assertFalse((self.root / "save-calls").exists())
                self.assertFalse((self.root / "fetch-calls").exists())


    def test_a_refused_production_doc_prints_one_error(self) -> None:
        _ = self.doc.write_text("# Missing context\n", encoding="utf-8")
        result = self.run_cli("start", "0.20")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "bevy_upgrade: production doc lacks Merge branch\n")


    def test_start_checks_from_fixtures_before_its_single_save(self) -> None:
        self.write_fixtures(ready=True)
        result = self.run_cli("start", "0.20", count_saves=True)
        self.assert_table(result)
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        current = self.read_record()
        self.assertEqual(current["target_bevy"], {"status": "released", "version": "0.20.0",
                                                "date": "2026-10-12", "since": "2026-10-12"})
        facts = current["dependencies"]["adapter"]["crates_io"]
        assert facts["status"] == "read"
        self.assertEqual(facts["ready"], "2.0.0")
        self.assertEqual(facts["repository"], "example/adapter")
        self.assertEqual(facts["checked"], current["checked"])
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(), [
            "https://crates.io/api/v1/crates/bevy/versions",
            "https://crates.io/api/v1/crates/adapter",
            "https://crates.io/api/v1/crates/adapter/2.0.0/dependencies",
            "https://crates.io/api/v1/crates/adapter/1.2.3/dependencies",
        ])
        self.assertIn("release ready | bump to 2.0.0", result.stdout)


    def test_check_refreshes_pins_checks_once_and_saves_once(self) -> None:
        self.begin()
        self.write_lock(version="1.2.4", bevy="0.19.2")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(),
                         ["https://crates.io/api/v1/crates/adapter",
                          "https://api.github.com/repos/example/adapter/commits/main",
                          "https://api.github.com/repos/example/adapter/issues?state=all&since=2026-10-12T00:00:00Z&sort=updated&direction=desc&per_page=100"])
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["pin"]["version"], "1.2.4")
        self.assertEqual(current["dependencies"]["adapter"]["bevy"], "0.19.2")
        self.assertIsNotNone(current["checked"])
        self.assertIn("| main @ abcdef0 on Bevy ^0.19; PR #7 open (port @ 1234567) | port in PR | wait for PR #7 |",
                      result.stdout)


    def test_hand_check_failed_pin_read_shows_stored_facts_and_saves_baseline_once(self) -> None:
        self.begin()
        before = (self.root / "state/demo.json").read_bytes()
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("check", count_saves=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.startswith("pins could not be re-read: "))
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", result.stdout)
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)


    def test_check_all_requested_entries_fail_exits_one_preserves_facts_and_saves_once(self) -> None:
        self.begin()
        old = self.read_record()["dependencies"]["adapter"]["crates_io"]
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("last check failed: crates.io: no fixture", result.stdout)
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["crates_io"], old)
        self.assertEqual(current["dependencies"]["adapter"]["error"], "crates.io: no fixture")
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")


    def test_check_one_failed_entry_and_one_successful_entry_exits_zero(self) -> None:
        with (self.checkout / "Cargo.lock").open("a", encoding="utf-8") as stream:
            _ = stream.write('[[package]]\nname = "missing-fixture"\nversion = "1.0.0"\n'
                             + f'source = "{REGISTRY}"\ndependencies = ["bevy_ecs"]\n')
        self.begin()
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["dependencies"]["adapter"]["error"], "")
        self.assertEqual(current["dependencies"]["missing-fixture"]["error"], "crates.io: no fixture")
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")
        self.assertEqual((self.root / "fetch-calls").read_text(encoding="utf-8").splitlines(), [
            "https://crates.io/api/v1/crates/missing-fixture", "https://crates.io/api/v1/crates/adapter",
            "https://api.github.com/repos/example/adapter/commits/main",
            "https://api.github.com/repos/example/adapter/issues?state=all&since=2026-10-12T00:00:00Z&sort=updated&direction=desc&per_page=100",
        ])


    def test_start_succeeds_when_all_upstream_lookups_fail(self) -> None:
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("start", "0.20", count_saves=True)
        self.assert_table(result)
        current = self.read_record()
        self.assertEqual(current["target_bevy"], {"status": "unread"})
        self.assertEqual(current["target_error"], "crates.io: no fixture")
        self.assertEqual(current["dependencies"]["adapter"]["crates_io"], {"status": "unread"})
        self.assertEqual(current["dependencies"]["adapter"]["error"], "crates.io: no fixture")
        self.assertIsNotNone(current["checked"])
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")


    def test_target_failure_does_not_make_a_successful_entry_check_fail(self) -> None:
        self.begin()
        current = self.read_record()
        current["target_bevy"] = {"status": "unread"}
        _ = (self.root / "state/demo.json").write_text(json.dumps(current), encoding="utf-8")
        bodies = cast(dict[str, object], json.loads((self.root / "fixtures.json").read_text(encoding="utf-8")))
        del bodies["https://crates.io/api/v1/crates/bevy/versions"]
        _ = (self.root / "fixtures.json").write_text(json.dumps(bodies), encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertEqual(self.read_record()["target_error"], "crates.io: no fixture")
        self.assertIn("(stale)", result.stdout)


    def test_check_without_eligible_entry_requests_exits_zero(self) -> None:
        self.write_fixtures(ready=True)
        self.begin()
        _ = (self.root / "fixtures.json").write_text("{}", encoding="utf-8")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertEqual((self.root / "save-calls").read_text(encoding="utf-8"), "saved\n")


    def test_start_schedules_under_record_lock_after_saving_initial_baseline(self) -> None:
        result = self.run_cli("start", "0.20", count_saves=True)
        self.assert_table(result)
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assertEqual(self.read_record()["announced"], {
            "adapter": {"state": "port in PR", "repository": "example/adapter", "items": {"7": "open"}}})
        self.assertTrue(self.tool_calls("notifier")[0]["record_exists"], "start scheduled before its save")
        self.assertTrue(self.tool_calls("notifier")[0]["locked"], "start scheduled outside the record lock")
        self.assertEqual(self.tool_args("notifier")[0][:7],
                         ["new", "bevy-upgrade-demo", "--every", "240", "--timeout", "300", "--run"])
        self.assertEqual(self.tool_args("send"), [])


    def test_scheduled_git_readiness_sends_once_with_waiting_answer_sentence(self) -> None:
        self.begin()
        self.clear_calls()
        self.make_branch_ready()
        first = self.run_cli("check", "--scheduled", count_saves=True)
        self.assert_silent_check(first)
        self.assertEqual(first.stderr, "")
        sends = self.tool_args("send")
        self.assertEqual(len(sends), 1)
        args = sends[0]
        self.assertEqual(args[args.index("--summary") + 1], "Bevy 0.20 upgrade: 1 change")
        text = args[args.index("--text") + 1]
        self.assertIn("- adapter: port in PR → git ready (main @ fedcba9)", text)
        self.assertIn("1 git ready (1 waiting for your answer)", text)
        self.assertTrue(text.endswith("Full table: /showrunner:bevy_upgrade"))
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.assertEqual(len(self.tool_args("send")), 1)
        self.assertEqual(self.retire_calls(), [])


    def test_scheduled_no_change_and_missing_baseline_send_nothing(self) -> None:
        self.begin()
        self.clear_calls()
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.assertEqual(self.tool_args("send"), [])
        current = self.read_record()
        current["announced"] = None
        self.store_record(current)
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.assertIsNotNone(self.read_record()["announced"])
        self.assertEqual(self.tool_args("send"), [])
        self.assertEqual(self.retire_calls(), [])


    def test_empty_showrunner_address_keeps_pending_changes_for_next_check(self) -> None:
        self.begin()
        self.clear_calls()
        baseline = self.read_record()["announced"]
        self.make_branch_ready()
        self.env["TEST_SHOWRUNNER_ADDRESS"] = ""
        absent = self.run_cli("check", "--scheduled")
        self.assert_silent_check(absent)
        self.assertEqual(absent.stderr, "bevy_upgrade: the showrunner was not told: no showrunner session\n")
        self.assertEqual(self.read_record()["announced"], baseline)
        self.assertEqual(self.tool_args("send"), [])
        self.env["TEST_SHOWRUNNER_ADDRESS"] = "uds:/test/session"
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.assertEqual(len(self.tool_args("send")), 1)
        self.assertNotEqual(self.read_record()["announced"], baseline)


    def test_skipped_and_queued_sends_do_not_advance_announced(self) -> None:
        self.begin()
        baseline = self.read_record()["announced"]
        self.make_branch_ready()
        for code, outcome in ((0, "SKIPPED: acknowledged"), (1, "QUEUED: away")):
            with self.subTest(outcome=outcome):
                self.env.update({"SEND_EXIT": str(code), "SEND_OUTCOME": outcome})
                result = self.run_cli("check", "--scheduled")
                self.assert_silent_check(result)
                self.assertEqual(result.stderr, f"bevy_upgrade: the showrunner was not told: {outcome}\n")
                self.assertEqual(self.read_record()["announced"], baseline)


    def test_status_reports_queued_changes_and_retires_after_saving_baseline_under_lock(self) -> None:
        self.begin()
        baseline = self.read_record()["announced"]
        self.clear_calls()
        self.make_branch_ready()
        self.env.update({"SEND_EXIT": "1", "SEND_OUTCOME": "QUEUED: away"})
        queued = self.run_cli("check", "--scheduled")
        self.assert_silent_check(queued)
        self.assertEqual(self.read_record()["announced"], baseline)
        self.assertEqual(len(self.tool_args("send")), 1)
        self.assertEqual(self.retire_calls(), [])
        self.clear_calls()
        result = self.run_cli("status", count_saves=True)
        self.assert_table(result)
        self.assertIn("Changed since the last report:\n- adapter: port in PR → git ready (main @ fedcba9)", result.stdout)
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        retired = self.retire_calls()
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0]["args"], self.retire_arguments())
        self.assertTrue(retired[0]["locked"])
        self.assertEqual(retired[0]["announced"], self.read_record()["announced"])
        self.assertNotEqual(retired[0]["announced"], baseline)
        events = self.tool_calls("tool")
        self.assertEqual([event["kind"] for event in events], ["notifier", "send"])
        self.assertEqual(cast(list[str], events[0]["args"])[0], "new")
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.assertEqual(self.tool_args("send"), [])


    def test_line_keeps_record_bytes_pending_changes_and_takes_no_lock(self) -> None:
        self.begin()
        self.make_branch_ready()
        self.env["TEST_SHOWRUNNER_ADDRESS"] = ""
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.clear_calls()
        self.write_lock(version="1.2.4")
        path = self.root / "state/demo.json"
        before = path.read_bytes()
        lock = path.with_suffix(".lock")
        lock.unlink()
        result = self.run_cli("line", count_saves=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("(1 waiting for your answer)", result.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(lock.exists(), "line acquired the record lock")
        self.assertEqual(self.retire_calls(), [])
        self.assertFalse((self.root / "save-calls").exists())
        self.assertEqual(self.tool_args("send"), [])
        self.assertTrue(any(args[0] == "new" for args in self.tool_args("notifier")))


    def test_line_json_reports_unanswered_git_pin_without_saving_or_sending(self) -> None:
        self.begin()
        self.make_branch_ready()
        self.env["TEST_SHOWRUNNER_ADDRESS"] = ""
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        self.clear_calls()
        path = self.root / "state/demo.json"
        before = path.read_bytes()
        lock = path.with_suffix(".lock")
        lock.unlink()
        result = self.run_cli("line", "--json", count_saves=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        update = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(set(update), {"update", "needs_user"})
        self.assertIs(update["needs_user"], True)
        self.assertIn("1 git ready (1 waiting for your answer)", str(update["update"]))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(lock.exists(), "line --json acquired the record lock")
        self.assertFalse((self.root / "save-calls").exists())
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertEqual(self.tool_args("send"), [])
        self.assertEqual([args[0] for args in self.tool_args("notifier")], ["new", "status"])


    def test_line_json_needs_no_user_for_answered_pins_or_pins_now_on_target(self) -> None:
        self.begin()
        self.make_branch_ready()
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        for verb in ("approve", "decline"):
            with self.subTest(answer=verb):
                self.assert_table(self.run_cli(verb, "adapter", "my answer"))
                result = self.run_cli("line", "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                update = cast(dict[str, object], json.loads(result.stdout))
                self.assertIs(update["needs_user"], False)
                self.assertIn("1 git ready", str(update["update"]))
                self.assertNotIn("waiting for your answer", str(update["update"]))
        self.assert_table(self.run_cli("forget", "adapter"))
        self.write_lock(bevy="0.20.0")
        before = (self.root / "state/demo.json").read_bytes()
        result = self.run_cli("line", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        update = cast(dict[str, object], json.loads(result.stdout))
        self.assertIs(update["needs_user"], False)
        self.assertIn("1 of 1 done", str(update["update"]))
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)


    def test_line_json_failed_pins_prints_one_object_using_stored_facts(self) -> None:
        self.begin()
        self.make_branch_ready()
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        before = (self.root / "state/demo.json").read_bytes()
        self.clear_calls()
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("line", "--json", count_saves=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        update = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(set(update), {"update", "needs_user"})
        self.assertIs(update["needs_user"], True)
        self.assertIn("(1 waiting for your answer)", str(update["update"]))
        self.assertIn("; pins not re-read since ", str(update["update"]))
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)
        self.assertFalse((self.root / "save-calls").exists())
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertEqual(self.tool_args("send"), [])


    def test_line_json_is_empty_with_no_watch_or_a_closed_watch(self) -> None:
        self.assert_silent_check(self.run_cli("line", "--json", count_saves=True))
        self.assertEqual(self.tool_args("notifier"), [])
        self.assertEqual(self.tool_args("send"), [])
        self.begin()
        self.assert_table(self.run_cli("stop"))
        self.clear_calls()
        before = (self.root / "state/demo.json").read_bytes()
        self.assert_silent_check(self.run_cli("line", "--json", count_saves=True))
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)
        self.assertEqual(self.tool_args("notifier"), [])
        self.assertEqual(self.tool_args("send"), [])
        self.assertFalse((self.root / "save-calls").exists())


    def test_status_with_failed_pins_saves_the_displayed_baseline_once(self) -> None:
        self.begin()
        current = self.read_record()
        current["announced"] = None
        self.store_record(current)
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("status", count_saves=True)
        self.assert_table(result)
        self.assertTrue(result.stdout.startswith("pins could not be re-read: "))
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assertIsNotNone(self.read_record()["announced"])


    def test_line_failed_pins_prints_one_sentence_and_preserves_record(self) -> None:
        self.begin()
        before = (self.root / "state/demo.json").read_bytes()
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("line", count_saves=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("; pins not re-read since ", result.stdout)
        self.assertNotIn("pins could not be re-read", result.stdout)
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)
        self.assertFalse((self.root / "save-calls").exists())


    def test_scheduled_failed_pins_neither_sends_saves_nor_closes(self) -> None:
        self.begin()
        current = self.read_record()
        current["dependencies"]["adapter"]["bevy"] = "0.20.0"
        self.store_record(current)
        before = (self.root / "state/demo.json").read_bytes()
        self.clear_calls()
        (self.checkout / "Cargo.lock").unlink()
        result = self.run_cli("check", "--scheduled", count_saves=True)
        self.assert_silent_check(result, 1)
        self.assertTrue(result.stderr.startswith("bevy_upgrade: pins could not be re-read: "))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)
        self.assertEqual(self.tool_args("send"), [])
        self.assertFalse((self.root / "save-calls").exists())
        self.assertFalse((self.root / "fetch-calls").exists())
        self.assertFalse(any(args[0] == "remove" for args in self.tool_args("notifier")))
        self.assertEqual(self.retire_calls(), [])


    def test_hand_check_closes_when_pins_are_done_and_removes_only_after_save(self) -> None:
        self.begin()
        self.clear_calls()
        self.write_lock(bevy="0.20.0")
        result = self.run_cli("check", count_saves=True)
        self.assert_table(result)
        self.assertIn("Bevy 0.20 upgrade: every dependency is on Bevy 0.20. The watch is closed.", result.stdout)
        self.assertIsNotNone(self.read_record()["closed"])
        removes = [call for call in self.tool_calls("notifier") if cast(list[str], call["args"])[0] == "remove"]
        self.assertEqual(len(removes), 1)
        self.assertIsNotNone(removes[0]["closed"], "job removed before closed record was saved")
        self.assertTrue(removes[0]["locked"], "closing hand check removed the job outside the record lock")
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")


    def test_failed_and_skipped_completion_retry_until_sent_then_close(self) -> None:
        self.begin()
        self.clear_calls()
        baseline = self.read_record()["announced"]
        self.write_lock(bevy="0.20.0")
        for code, outcome in ((1, "QUEUED: away"), (0, "SKIPPED: acknowledged")):
            with self.subTest(outcome=outcome):
                self.env.update({"SEND_EXIT": str(code), "SEND_OUTCOME": outcome})
                result = self.run_cli("check", "--scheduled")
                self.assert_silent_check(result)
                self.assertEqual(result.stderr, f"bevy_upgrade: the showrunner was not told: {outcome}\n")
                self.assertIsNone(self.read_record()["closed"])
                self.assertEqual(self.read_record()["announced"], baseline)
                self.assertFalse(any(args[0] == "remove" for args in self.tool_args("notifier")))
        self.env.update({"SEND_EXIT": "0", "SEND_OUTCOME": "SENT: test showrunner"})
        result = self.run_cli("check", "--scheduled", count_saves=True)
        self.assert_silent_check(result)
        self.assertIsNotNone(self.read_record()["closed"])
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        args = self.tool_args("send")[-1]
        self.assertEqual(args[args.index("--summary") + 1], "Bevy 0.20 upgrade: done")
        self.assertIn("The watch is closed.", args[args.index("--text") + 1])
        removes = [call for call in self.tool_calls("notifier") if cast(list[str], call["args"])[0] == "remove"]
        self.assertEqual(len(removes), 1)
        self.assertIsNotNone(removes[0]["closed"])
        self.assertTrue(removes[0]["locked"], "scheduled completion removed the job outside the record lock")
        self.assertEqual(self.retire_calls(), [])


    def test_stop_closed_status_refused_check_and_scheduled_job_cleanup(self) -> None:
        self.begin()
        result = self.run_cli("stop", count_saves=True)
        self.assert_table(result)
        self.assertIsNotNone(self.read_record()["closed"])
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assertIn(["remove", "bevy-upgrade-demo"], self.tool_args("notifier"))
        self.assertTrue(self.tool_calls("notifier")[-1]["locked"], "stop removed the job outside the record lock")
        status = self.run_cli("status")
        self.assert_table(status)
        self.assertTrue(status.stdout.startswith("This watch closed "))
        self.assertNotIn("Changed since the last report:", status.stdout)
        before = (self.root / "state/demo.json").read_bytes()
        self.clear_calls()
        hand = self.run_cli("check", count_saves=True)
        self.assertEqual(hand.returncode, 2)
        self.assertEqual(hand.stderr, "bevy_upgrade: the watch is closed; start a new one\n")
        self.assertEqual(hand.stdout, "")
        self.assertFalse((self.root / "save-calls").exists())
        scheduled = self.run_cli("check", "--scheduled", count_saves=True)
        self.assert_silent_check(scheduled)
        self.assertEqual(scheduled.stderr, "")
        self.assertEqual(self.tool_args("notifier"), [["remove", "bevy-upgrade-demo"]])
        self.assertTrue(self.tool_calls("notifier")[0]["locked"], "closed scheduled check removed the job outside the record lock")
        self.assertEqual(self.retire_calls(), [])
        self.assertEqual(self.tool_args("send"), [])
        self.assertEqual((self.root / "state/demo.json").read_bytes(), before)


    def test_line_with_no_watch_or_closed_watch_is_empty_and_saves_nothing(self) -> None:
        self.assert_silent_check(self.run_cli("line", count_saves=True))
        self.assertEqual(self.tool_args("notifier"), [])
        self.begin()
        self.assert_table(self.run_cli("stop"))
        self.clear_calls()
        self.assert_silent_check(self.run_cli("line", count_saves=True))
        self.assertEqual(self.tool_args("notifier"), [])
        self.assertFalse((self.root / "save-calls").exists())


    def test_failed_github_lookup_keeps_facts_and_line_marks_them_stale(self) -> None:
        self.begin()
        current = self.read_record()["dependencies"]["adapter"]
        bodies = cast(dict[str, object], json.loads((self.root / "fixtures.json").read_text()))
        bodies["https://api.github.com/repos/example/adapter/commits/main"] = "invalid JSON"
        _ = (self.root / "fixtures.json").write_text(json.dumps(bodies), encoding="utf-8")
        self.clear_calls()
        result = self.run_cli("check", "--scheduled")
        self.assert_silent_check(result, 1)
        failed = self.read_record()["dependencies"]["adapter"]
        for key in ("crates_io", "branch", "items"):
            self.assertEqual(failed[key], current[key])
        self.assertTrue(failed["error"].startswith("api.github.com: invalid response:"))
        line = self.run_cli("line")
        self.assertEqual(line.returncode, 0, line.stderr)
        self.assertTrue(line.stdout.rstrip().endswith("(stale)"), line.stdout)
        self.assertEqual(self.tool_args("send"), [])


    def test_every_hand_table_verb_saves_baseline_then_retires_queued_message_under_lock(self) -> None:
        self.begin()
        original = self.read_record()
        commands: tuple[tuple[str, ...], ...] = (
            ("start", "0.20"), ("status",), ("check",), ("approve", "adapter", "yes"),
            ("decline", "adapter", "wait"), ("forget", "adapter"), ("note", "adapter", "why"), ("stop",),
        )
        for arguments in commands:
            with self.subTest(verb=arguments[0]):
                current = cast(upgrade_record.Record, json.loads(json.dumps(original)))
                branch = current["dependencies"]["adapter"]["branch"]
                assert branch["status"] == "read"
                branch["requirement"] = "^0.20"
                self.store_record(current)
                self.clear_calls()
                result = self.run_cli(*arguments, count_saves=True)
                self.assert_table(result)
                self.assertIn("Changed since the last report:\n- adapter: port in PR → git ready (main @ abcdef0)", result.stdout)
                announced = self.read_record()["announced"]
                assert announced is not None
                self.assertEqual(announced["adapter"]["state"], "git ready")
                self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
                self.assertEqual(self.tool_args("send"), [])
                retired = self.retire_calls()
                self.assertEqual(len(retired), 1)
                self.assertEqual(retired[0]["args"], self.retire_arguments())
                self.assertTrue(retired[0]["locked"])
                self.assertEqual(retired[0]["announced"], announced)
                events = self.tool_calls("tool")
                self.assertEqual(events[-1], retired[0])
                if arguments[0] in ("start", "status", "check", "stop"):
                    self.assertEqual(len(events), 2)
                    job = events[0]
                    self.assertEqual(job["kind"], "notifier")
                    self.assertTrue(job["locked"], f"{arguments[0]} upkeep ran outside the record lock")
                    self.assertEqual(job["announced"], announced)


    def test_failed_retirement_keeps_hand_report_table_exit_code_and_saved_baseline(self) -> None:
        self.begin()
        self.clear_calls()
        self.env.update({"RETIRE_EXIT": "3", "RETIRE_OUTCOME": "FAILED: retirement unavailable"})
        result = self.run_cli("status", count_saves=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", result.stdout)
        self.assertEqual(result.stderr,
                         "bevy_upgrade: could not retire the queued message: FAILED: retirement unavailable\n")
        self.assertEqual((self.root / "save-calls").read_text(), "saved\n")
        self.assertEqual(len(self.retire_calls()), 1)
        self.assertEqual(self.retire_calls()[0]["announced"], self.read_record()["announced"])
        (self.checkout / "Cargo.lock").unlink()
        failed_check = self.run_cli("check")
        self.assertEqual(failed_check.returncode, 1)
        self.assertIn("| Dependency | Our pin | crates.io | Git | State | Next |", failed_check.stdout)
        self.assertEqual(failed_check.stderr, result.stderr)


    def test_two_scheduled_change_lines_use_plural_summary(self) -> None:
        self.begin()
        current = self.read_record()
        value = current["dependencies"]["adapter"]
        branch = value["branch"]
        assert branch["status"] == "read"
        branch["requirement"] = "^0.20"
        value["items"][0]["state"] = "merged"
        self.store_record(current)
        self.clear_calls()
        self.assert_silent_check(self.run_cli("check", "--scheduled"))
        sends = self.tool_args("send")
        self.assertEqual(len(sends), 1)
        args = sends[0]
        self.assertEqual(args[args.index("--summary") + 1], "Bevy 0.20 upgrade: 2 changes")
        text = args[args.index("--text") + 1]
        self.assertIn("- adapter: port in PR → git ready (main @ abcdef0)", text)
        self.assertIn("- adapter: PR #7 open → merged", text)
        self.assertEqual(self.retire_calls(), [])



if __name__ == "__main__":
    _ = unittest.main()
