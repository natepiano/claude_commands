"""The showrunner Stop hook checks only a targeted running production's question review."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

import showrunner_footer


HOOK = Path(__file__).with_name("stop-showrunner-footer.py")
SETTINGS = HOOK.parent.parent.parent / "settings.json"
ZONE = "America/Los_Angeles"
SESSION = "showrunner-session"


class OutstandingUserQuestion(TypedDict):
    since: str
    text: str
    question: NotRequired[str]
    reviewed: NotRequired[str]


class StopHookBlock(TypedDict):
    decision: Literal["block"]
    reason: str


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class QuestionLibraryTests(unittest.TestCase):
    def test_choices_are_not_checked_when_no_instance_targets_the_session(self) -> None:
        self.assertIsNone(showrunner_footer.block_reason([], "Run now (recommended), or wait?"))


    def test_read_production_skips_wrapped_doc(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            instance = root / "showrunner-demo"
            instance.mkdir()
            doc = root / "production.md"
            _ = doc.write_text("> **Status: PRODUCTION — wrapped.** demo\n- **User zone:** America/Los_Angeles — demo\n")
            _ = (instance / "conf").write_text(f"CHECK=zsh /x/production_check.sh {doc}\n")
            self.assertIsNone(showrunner_footer.read_production(instance))


class ShowrunnerQuestionHookTests(unittest.TestCase):
    root: Path = Path()
    notifier: Path = Path()
    instance: Path = Path()
    doc: Path = Path()
    outstanding: Path = Path()
    environment: dict[str, str] = {}
    next_due: datetime = datetime.min

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.notifier = self.root / "notifier"
        self.instance = self.notifier / "showrunner-demo"
        self.instance.mkdir(parents=True)
        agents = self.root / "rust/hanadocs/agents"
        agents.mkdir(parents=True)
        _ = (agents / "claude1.md").write_text("---\nrole: [unit]\n---\n")
        (self.root / ".local/state/agent-notes").mkdir(parents=True)
        holds = self.root / "holds"
        holds.mkdir()
        self.outstanding = self.root / "showrunner-state/outstanding/demo.json"
        self.outstanding.parent.mkdir(parents=True)
        self.doc = self.root / "demo-production.md"
        _ = self.doc.write_text(
            "> **Status: PRODUCTION — running.** demo\n"
            + "- **User zone:** America/Los_Angeles — demo\n"
        )
        _ = (self.instance / "conf").write_text(
            f"TARGET=session:{SESSION}\nCHECK=zsh /x/production_check.sh {self.doc}\n"
        )
        self.next_due = datetime.now(ZoneInfo(ZONE)) + timedelta(minutes=30)
        self.write_state()
        self.environment = {
            **os.environ, "HOME": str(self.root), "NOTIFIER_STATE_DIR": str(self.notifier),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner-state"),
            "BUILD_HOLD_DIR": str(holds),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-test"),
        }

    def write_state(self, *, enabled: bool = True) -> None:
        _ = (self.instance / "state").write_text(
            f"ENABLED={int(enabled)}\nNEXT_DUE={int(self.next_due.timestamp())}\n"
        )


    def run_raw(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK)], input=payload,
                              capture_output=True, text=True, check=False,
                              env=self.environment, timeout=15)

    def run_hook(self, reply: str = "Done.", **fields: object) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION, "last_assistant_message": reply, **fields}
        return self.run_raw(json.dumps(payload))


    def reason(self, reply: str, **fields: object) -> str:
        result = self.run_hook(reply, **fields)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(StopHookBlock, json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        return decision["reason"]

    def assert_passes(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


    def test_pending_question_kinds_round_trip_without_claiming_an_unavailable_review_completed(self) -> None:
        reviewed = showrunner_footer.PendingReviewedQuestion("merge", "Merge now (recommended), or wait?")
        unavailable = showrunner_footer.PendingReviewUnavailableQuestion(
            "path", "Choose the path.\nUse the short route?", "reviewer unavailable")
        showrunner_footer.add_pending_reviewed(self.instance, [reviewed])
        showrunner_footer.add_pending_reviewed(self.instance, [reviewed, unavailable])
        self.assertEqual(showrunner_footer.read_pending_reviewed(self.instance), [reviewed, unavailable])
        saved = cast(showrunner_footer.PendingReviewedRecord,
                     json.loads(showrunner_footer.pending_reviewed_path(self.instance).read_text()))
        self.assertEqual(saved["items"][1]["kind"], "review unavailable")
        self.assertNotIn("reviewed", saved["items"][1])

    def test_each_pending_question_is_needed_until_one_reply_carries_it(self) -> None:
        first = showrunner_footer.PendingReviewedQuestion("merge", "Merge now (recommended), or wait?")
        second = showrunner_footer.PendingReviewUnavailableQuestion(
            "path", "Choose the path.\nUse the short route?", "reviewer unavailable")
        showrunner_footer.add_pending_reviewed(self.instance, [first])
        showrunner_footer.add_pending_reviewed(self.instance, [second])
        self.assertIn("merge", self.reason("Done."))
        self.assertIn("path", self.reason("Merge now  (recommended),\n or wait?"))
        self.assertEqual(showrunner_footer.read_pending_reviewed(self.instance), [second])
        self.assert_passes(self.run_hook(second.question))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
        self.assert_passes(self.run_hook("Later reply with no repeated questions."))

    def test_retry_and_replacement_session_still_require_pending_text(self) -> None:
        item = showrunner_footer.PendingReviewedQuestion("path", "Which path should we use?")
        showrunner_footer.add_pending_reviewed(self.instance, [item])
        self.assertIn("paste its reviewed question", self.reason("Done."))
        retry = self.run_hook("Still missing.", stop_hook_active=True)
        self.assertIn("path", retry.stdout)
        _ = (self.instance / "conf").write_text(
            f"TARGET=session:replacement\nCHECK=zsh /x/production_check.sh {self.doc}\n")
        self.assertIn("path", self.reason_for_session("replacement", "A later turn."))
        self.assert_passes(self.run_hook(item.reviewed, session_id="replacement", stop_hook_active=True))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())

    def test_a_reply_that_shows_the_question_ends_its_wait(self) -> None:
        item = showrunner_footer.PendingReviewedQuestion("path", "Which path should we use?")
        showrunner_footer.add_pending_reviewed(self.instance, [item])
        self.assert_passes(self.run_hook(item.reviewed))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
        self.assert_passes(self.run_hook("A later turn."))

    def test_unregistered_choices_are_blocked_but_code_and_quotes_are_not(self) -> None:
        choice = "Run now (recommended), or wait."
        choice_reason = self.reason(choice)
        self.assertIn("record the item", choice_reason)
        self.assertIn("update_registration.py questions --state", choice_reason)
        self.assertIn("record the item", self.reason("Should we run now (recommended), or wait?"))
        for example in (f"> {choice}", f"```text\n{choice}\n```", f"`{choice}`"):
            with self.subTest(example=example):
                self.assert_passes(self.run_hook(example))
        item = {"since": "2026-10-06T08:00", "text": "run", "question": choice, "reviewed": choice}
        _ = self.outstanding.write_text(json.dumps([item]))
        self.assert_passes(self.run_hook(choice))
        self.assertIn("record the item", self.reason(choice + "\nDelete the work (recommended), or keep it."))
        code_choice = "Run `check.py` (recommended), or wait?"
        showrunner_footer.add_pending_reviewed(self.instance, [
            showrunner_footer.PendingReviewedQuestion("check", code_choice)])
        self.assert_passes(self.run_hook("Run `check.py` (recommended),\n or wait?"))

    def test_unregistered_requests_block_and_on_hook_retries(self) -> None:
        # Short excerpts of real showrunner replies that asked the user without registering.
        requests = ('Say "go" and I\'ll make the change.',
                    "Tell me if you want that traced in full, or if you're thinking of retiring it.",
                    "Want me to have it added?",
                    "Say go and I'll have cargo-liner do it.",
                    'Pick the conversation dated 23:12 that starts with "triggers and gates".',
                    "Should handler-enh go ahead with phases 1 and 2?")
        for request in requests:
            for retry in (False, True):
                with self.subTest(request=request, retry=retry):
                    reason = self.reason("The work is ready. " + request, stop_hook_active=retry)
                    self.assertIn(repr(request), reason)
                    self.assertIn("update_registration.py questions --state", reason)
                    self.assertIn("paste its reviewed question", reason)

    def test_unregistered_ask_names_the_command_that_records_it(self) -> None:
        question = "Was that yours?"
        reason = self.reason(question)
        self.assertIn(question, reason)
        self.assertIn("update_registration.py questions --state", reason)

    def test_each_outstanding_question_field_matches_normalised_question_prose(self) -> None:
        registered = "The diagnostic is ready.\nSay go and I'll run `check.py`."
        records: tuple[tuple[str, OutstandingUserQuestion], ...] = (
            ("text", {"since": "2026-10-16T08:00", "text": registered}),
            ("question", {"since": "2026-10-16T08:00", "text": "diagnostics", "question": registered}),
            ("reviewed", {"since": "2026-10-16T08:00", "text": "diagnostics", "reviewed": registered}),
        )
        for field, record in records:
            with self.subTest(field=field):
                _ = self.outstanding.write_text(json.dumps([record]))
                self.assert_passes(self.run_hook("Say  go and I'll run `check.py`."))
        shorter: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "Say go and I'll run"}
        _ = self.outstanding.write_text(json.dumps([shorter]))
        self.assert_passes(self.run_hook("Say go and I'll run the diagnostic."))

    def test_registered_question_does_not_authorise_another_ask_in_the_same_reply(self) -> None:
        first = "Should handler-enh go ahead with phases 1 and 2?"
        second = "Do you want the phase 3 labels?"
        item: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "hierarchy", "question": first}
        _ = self.outstanding.write_text(json.dumps([item]))
        reason = self.reason(first + " " + second)
        self.assertIn(second, reason)
        self.assertIn("update_registration.py questions --state", reason)
        item["reviewed"] = first + "\n" + second
        _ = self.outstanding.write_text(json.dumps([item]))
        self.assert_passes(self.run_hook(first + " " + second))

    def test_unrelated_production_and_code_only_text_do_not_register_a_user_ask(self) -> None:
        question = "Want me to have it added?"
        stranger: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": question}
        _ = self.outstanding.with_name("stranger.json").write_text(json.dumps([stranger]))
        code_only: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "`add.py`"}
        _ = self.outstanding.write_text(json.dumps([code_only]))
        self.assertIn(question, self.reason(question))
        _ = self.outstanding.write_text(json.dumps([stranger]))
        self.assert_passes(self.run_hook(question))

    def test_a_new_ask_beside_a_shown_question_is_registered_without_repeating_the_question(self) -> None:
        pending = showrunner_footer.PendingReviewedQuestion("route", "Which path should we use?")
        showrunner_footer.add_pending_reviewed(self.instance, [pending])
        request = "Say go and I'll run the diagnostic."
        self.assertIn(request, self.reason(pending.reviewed + "\n" + request, stop_hook_active=True))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
        item: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "diagnostics", "question": request}
        _ = self.outstanding.write_text(json.dumps([item]))
        self.assert_passes(self.run_hook(request, stop_hook_active=True))

    def test_relayed_peer_question_and_attributed_unit_choice_pass_unregistered(self) -> None:
        replies = (
            # A real relay paragraph to another showrunner, without its unrelated status report.
            '**Also sent to the hana showrunner:** distant picking is listed as "waiting for review", '
            + 'but it\'s already written into trunk Phase 123. Did you approve it? '
            + 'It stays "waiting for review" until hana answers.',
            "**To the hana showrunner:**\nDid you approve it?",
            'The handler-enh unit director said, in its own words:\n\n'
            + '"Run phases 1 and 2 (recommended), or wait."',
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.assert_passes(self.run_hook(reply))

    def test_peer_relay_does_not_hide_a_later_user_question_or_choice(self) -> None:
        relay = "**Also sent to the hana showrunner:** Did you approve it?"
        asks = ("Want me to have it added?",
                "Allow (recommended, since both requests are a day old): the shutdown unit runs the cancel once.")
        for ask in asks:
            with self.subTest(ask=ask):
                reason = self.reason(relay + "\n\n**Question for you:**\n" + ask)
                self.assertIn(ask, reason)
                self.assertIn("update_registration.py questions --state", reason)

    def test_recorded_choices_pass_with_list_markers_but_unrecorded_choices_block(self) -> None:
        question = "Run now (recommended), or wait?"
        item = showrunner_footer.PendingReviewedQuestion("run", question)
        # Registration stores the question in OUTSTANDING beside the pending copy the hook checks.
        _ = self.outstanding.write_text(json.dumps([{
            "since": "2026-10-06T08:00", "text": "run", "question": question, "reviewed": question}]))
        for marker in ("-", "*", "+", "1.", "1)"):
            with self.subTest(marker=marker):
                showrunner_footer.add_pending_reviewed(self.instance, [item])
                reply = f"{marker} Run  now (recommended), or wait?"
                self.assertIn("record the item", self.reason(
                    reply + f"\n{marker} Delete the work (recommended), or keep it?"))
                self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
                self.assert_passes(self.run_hook(reply))

    def test_recorded_list_choices_can_be_recalled_without_their_markers(self) -> None:
        for marker in ("-", "*", "+", "1.", "1)"):
            with self.subTest(marker=marker):
                question = f"{marker} Run now (recommended), or wait?"
                _ = self.outstanding.write_text(json.dumps([{
                    "since": "2026-10-06T08:00", "text": "run", "question": question, "reviewed": question}]))
                self.assert_passes(self.run_hook("Run  now (recommended), or wait?"))
                self.assert_passes(self.run_hook("2) Run now (recommended), or wait?"))

    def test_root_can_recall_a_lower_productions_recorded_choice(self) -> None:
        self.timer("kid")
        self.attach("kid", "demo")
        question = "Use the short route (recommended), or wait?"
        _ = self.outstanding.with_name("kid.json").write_text(json.dumps([
            {"since": "2026-10-06T08:00", "text": "choose route", "question": question, "reviewed": question}]))
        self.assert_passes(self.run_hook(question))
        self.assertIn("record the item", self.reason("Use the long route (recommended), or wait?"))


    def test_hook_skips_other_session_and_inactive_production(self) -> None:
        self.assert_passes(self.run_hook(session_id="other-session"))
        _ = self.doc.write_text(self.doc.read_text().replace("running", "wrapped"))
        self.assert_passes(self.run_hook())

    def test_hook_skips_reentry_agents_and_blank_replies(self) -> None:
        self.assert_passes(self.run_hook(stop_hook_active=True))
        self.assert_passes(self.run_hook(agent_id="subagent"))
        self.assert_passes(self.run_hook("   \n"))

    def test_hook_skips_missing_notifier_root(self) -> None:
        shutil.rmtree(self.notifier)
        self.assert_passes(self.run_hook())

    def test_hook_fails_open_for_malformed_json_and_unreadable_production(self) -> None:
        self.assert_passes(self.run_raw("{bad"))
        _ = (self.instance / "conf").write_text(
            f"TARGET=session:{SESSION}\nCHECK=zsh /x/production_check.sh {self.root / 'missing.md'}\n"
        )
        self.assert_passes(self.run_hook())

    def test_hook_fails_open_for_unknown_zone_and_bad_outstanding_json(self) -> None:
        # The zone is the user's setting; a setting that names no zone stands the hook down.
        self.environment["USER_ZONE"] = "Pacific/Nowhere"
        self.assert_passes(self.run_hook())
        self.environment["USER_ZONE"] = ZONE
        _ = self.outstanding.write_text("{bad")
        self.assert_passes(self.run_hook())

    def test_settings_registers_hook_after_banned_words(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        stop_group = cast(list[dict[str, str]], hooks["Stop"][0]["hooks"])
        commands = [item["command"] for item in stop_group]
        self.assertEqual(commands[-1],
                         '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/stop-showrunner-footer.py"')
        self.assertIn("stop-assistant-prose-banned-words.py", commands[-2])


    def reason_for_session(self, session: str, reply: str) -> str:
        result = self.run_hook(reply, session_id=session)
        decision = cast(dict[str, str], json.loads(result.stdout))
        return decision["reason"]


    def timer(self, slug: str) -> None:
        """Another production with a complete update timer, run by some other session."""
        instance = self.notifier / f"showrunner-{slug}"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            f"TARGET=session:{slug}-session\nCHECK=zsh /x/production_check.sh {self.doc}\n")

    def attach(self, child: str, parent: str) -> None:
        edge = self.root / "showrunner-state" / child / "parent"
        edge.parent.mkdir(parents=True, exist_ok=True)
        _ = edge.write_text(parent + "\n")

    def write_open_for(self, slug: str, *texts: str) -> None:
        _ = self.outstanding.with_name(f"{slug}.json").write_text(json.dumps(
            [{"since": "2026-10-06T08:00", "text": text} for text in texts]))


if __name__ == "__main__":
    _ = unittest.main()
