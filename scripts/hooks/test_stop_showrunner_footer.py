"""The showrunner Stop hook checks only a targeted running production's footer."""

from __future__ import annotations

import json
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

import showrunner_footer
import turn_input


HOOK = Path(__file__).with_name("stop-showrunner-footer.py")
SWITCH = HOOK.with_name("showrunner_footer.py")
DAILIES = HOOK.parent.parent / "production" / "dailies_render.py"
SETTINGS = HOOK.parent.parent.parent / "settings.json"
ZONE = "America/Los_Angeles"
SESSION = "showrunner-session"
AGENT_LINE = "* claude1: week's usage unknown; refill time unknown; resets unknown"
WAITING_BLOCK = "\n\n\nWaiting on:\n\n* no ETA measured - x"
ONE_OPEN = "There is 1 open question for you."
TWO_OPEN = "There are 2 open questions for you."



class OutstandingUserQuestion(TypedDict):
    since: str
    text: str
    question: NotRequired[str]
    reviewed: NotRequired[str]


class StopHookBlock(TypedDict):
    decision: Literal["block"]
    reason: str


def human(text: str) -> dict[str, object]:
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def peer(text: str, sender: str = "natedev") -> dict[str, object]:
    return {"type": "user", "isMeta": True, "origin": {"kind": "peer", "name": sender},
            "message": {"role": "user", "content": f'<cross-session-message from-name="{sender}">\n{text}\n</cross-session-message>'}}


def tick() -> dict[str, object]:
    """The update timer's tick reaches the session through send.py, as a cross-session message."""
    return peer("Scheduled update (every 15 minutes, every unit checked).", "showrunner-demo")


def notification() -> dict[str, object]:
    return {"type": "user", "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": "<task-notification>\n<status>completed</status>\n</task-notification>"}}


def denial() -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
        "content": "Permission for this action was denied by the Claude Code auto mode classifier. Reason: [x]."}]}}


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class FooterLibraryTests(unittest.TestCase):
    def test_choices_are_not_checked_when_no_instance_targets_the_session(self) -> None:
        self.assertIsNone(showrunner_footer.block_reason([], "Run now (recommended), or wait?", {}))

    def test_reply_lines_keeps_waiting_block_after_footer(self) -> None:
        reply = "Done.\n\n---\n12:00 PDT update:\n\n* none active\n* no dailies scheduled" + WAITING_BLOCK
        self.assertEqual(showrunner_footer.reply_lines(reply), [
            "Done.", "", "---", "12:00 PDT update:", "", "* none active", "* no dailies scheduled",
            "", "", "Waiting on:", "", "* no ETA measured - x",
        ])

    def test_stamp_accepts_five_minutes_but_rejects_six_and_future(self) -> None:
        now = datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo(ZONE))
        self.assertEqual(showrunner_footer.stamp(["11:55 PDT update:"], now),
                         (now - timedelta(minutes=5), False))
        suffix = ["", "", "Waiting on:", "", "* no ETA measured - x"]
        at_five, quiet = showrunner_footer.stamp(["11:55 PDT update:", "", "* no dailies scheduled - nothing needed", *suffix], now)
        self.assertEqual(at_five, now - timedelta(minutes=5))
        self.assertTrue(quiet)
        at_six, quiet = showrunner_footer.stamp(["11:54 PDT update:", "", "* no dailies scheduled", *suffix], now)
        self.assertIsNone(at_six)
        self.assertFalse(quiet)
        future, _ = showrunner_footer.stamp(["12:01 PDT update:", "", "* no dailies scheduled", *suffix], now)
        self.assertIsNone(future)

    def test_next_run_text_handles_no_schedule_and_next_local_day(self) -> None:
        zone = ZoneInfo(ZONE)
        minute = datetime(2026, 10, 6, 23, 50, tzinfo=zone)
        self.assertEqual(showrunner_footer.next_run_text(
            int(datetime(2026, 10, 6, 23, 55, tzinfo=zone).timestamp()), minute, zone,
        ), "23:55")
        self.assertEqual(showrunner_footer.next_run_text(
            int(datetime(2026, 10, 7, 0, 5, tzinfo=zone).timestamp()), minute, zone,
        ), "00:05+1")

    def test_block_reason_requires_named_footer_states(self) -> None:
        parameter = inspect.signature(showrunner_footer.block_reason).parameters["states"]
        default = cast(object, parameter.default)
        self.assertIs(default, inspect.Parameter.empty)

    def test_read_production_skips_wrapped_doc(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            instance = root / "showrunner-demo"
            instance.mkdir()
            doc = root / "production.md"
            _ = doc.write_text("> **Status: PRODUCTION — wrapped.** demo\n- **User zone:** America/Los_Angeles — demo\n")
            _ = (instance / "conf").write_text(f"CHECK=zsh /x/production_check.sh {doc}\n")
            self.assertIsNone(showrunner_footer.read_production(instance))


class TurnInputTests(unittest.TestCase):
    def test_lines_read_backwards_across_chunk_boundaries_and_stop_at_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "transcript.jsonl"
            long_result: dict[str, object] = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 300}]}}
            empty: dict[str, object] = {"type": "assistant", "message": {"content": []}}
            entries = [peer("old turn"), empty,
                       notification(), long_result, denial(), empty]
            _ = path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
            with patch.object(turn_input, "CHUNK_BYTES", 7):
                lines = list(turn_input.lines_backwards(path))
                turn = turn_input.read_turn(path)
            self.assertEqual([json.loads(line) for line in reversed(lines)], entries)
            self.assertEqual(turn, turn_input.TurnInput(frozenset({"task-notification"}), True))
            self.assertIsNone(turn_input.read_turn(Path(scratch) / "missing.jsonl"))


class ShowrunnerFooterHookTests(unittest.TestCase):
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

    def footer(self, *, at: datetime | None = None, nothing_needed: bool = False,
               below: tuple[str, ...] = (), reports_to: str = "") -> str:
        minute = at or datetime.now(ZoneInfo(ZONE))
        days = (self.next_due.date() - minute.date()).days
        next_run = f"{self.next_due:%H:%M}" + (f"+{days}" if days > 0 else "")
        args = [sys.executable, str(DAILIES), "--footer", "--zone", ZONE,
                "--outstanding", str(self.outstanding)]
        for slug in below:
            args.extend(["--below", str(self.outstanding.with_name(f"{slug}.json"))])
        if reports_to:
            args.extend(["--reports-to", reports_to])
        if (self.instance / "state").read_text().startswith("ENABLED=1"):
            args.extend(["--next-run", next_run])
        if nothing_needed:
            args.append("--nothing-needed")
        if at is not None:
            args.extend(["--at", at.isoformat(timespec="minutes")])
        result = subprocess.run(args, capture_output=True, text=True, check=False,
                                env=self.environment, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.rstrip("\n")

    def run_raw(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(HOOK)], input=payload,
                              capture_output=True, text=True, check=False,
                              env=self.environment, timeout=15)

    def run_hook(self, reply: str = "Done.", **fields: object) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION, "last_assistant_message": reply, **fields}
        return self.run_raw(json.dumps(payload))

    def run_switch(self, action: str, session: str = SESSION) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SWITCH), action], capture_output=True,
                              text=True, check=False, env={**self.environment,
                                                           "CLAUDE_CODE_SESSION_ID": session}, timeout=10)

    def reason(self, reply: str, **fields: object) -> str:
        result = self.run_hook(reply, **fields)
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = cast(StopHookBlock, json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        return decision["reason"]

    def assert_passes(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def assert_blocked_with_current_footer(self, reply: str) -> str:
        before = self.footer()
        result = self.run_hook(reply)
        after = self.footer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        decision = cast(dict[str, str], json.loads(result.stdout))
        self.assertEqual(decision["decision"], "block")
        reason = decision["reason"]
        self.assertTrue(any(current in reason for current in (before, after)), reason)
        self.assertIn("Waiting on:", reason)
        return reason

    def transcript(self, *entries: dict[str, object]) -> str:
        path = self.root / "transcript.jsonl"
        earlier = [human("start"), {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}]
        _ = path.write_text("".join(json.dumps(entry) + "\n" for entry in (*earlier, *entries)))
        return str(path)

    def footers_off(self) -> None:
        switch = self.root / "showrunner-state/footers-off/demo"
        switch.parent.mkdir(parents=True, exist_ok=True)
        _ = switch.write_text("")

    def test_a_status_reply_on_a_turn_the_user_did_not_start_reaches_the_user(self) -> None:
        # User, 2026-10-18: the hook blocked the reply that showed a /showrunner:retire_unit result.
        status = "alpha-unit retired: its worktree and branch are removed."
        openings = (peer("From the showrunner: checkpoint merged."), tick(), notification())
        for opening in openings:
            with self.subTest(opening=opening["origin"], footers="on"):
                reason = self.reason(status, transcript_path=self.transcript(opening))
                self.assertTrue(reason.startswith(showrunner_footer.REASON_HEAD), reason)
        self.footers_off()
        for opening in openings:
            with self.subTest(opening=opening["origin"], footers="off"):
                self.assert_passes(self.run_hook(status, transcript_path=self.transcript(opening)))
                self.assert_passes(self.run_hook(status, transcript_path=self.transcript(opening),
                                                 stop_hook_active=True))

    def test_a_dash_answers_a_turn_the_user_did_not_start_with_footers_on_or_off(self) -> None:
        path = self.transcript(peer("ack"), notification())
        self.assert_passes(self.run_hook("-", transcript_path=path))
        self.assert_passes(self.run_hook(" -\n", transcript_path=path, stop_hook_active=True))
        self.footers_off()
        self.assert_passes(self.run_hook("-", transcript_path=path))

    def test_with_footers_off_a_reply_carries_no_counts_line(self) -> None:
        self.footers_off()
        _ = self.outstanding.write_text(json.dumps([{"since": "2026-10-16T19:00", "text": "Vault promotion"}]))
        self.assert_passes(self.run_hook("Vault promotion still waits on your choice."))
        for line in ("For you: 1 question", "For you: 1 project, 1 question"):
            with self.subTest(line=line):
                blocked = self.reason(f"Vault promotion still waits on your choice.\n\n{line}")
                self.assertIn(f"remove the line `{line}`", blocked)
        self.assertIn("remove the line `For you: 1 question`",
                      self.reason("For you: 1 question\n\nVault promotion still waits on your choice."))
        self.assert_passes(self.run_hook("-", transcript_path=self.transcript(peer("ack"))))

    def test_a_registered_item_still_needs_the_footer(self) -> None:
        _ = self.outstanding.write_text(json.dumps([{"since": "2026-10-16T19:00", "text": "Vault promotion"}]))
        reason = self.reason("Vault promotion still waits on you.", transcript_path=self.transcript(peer("ping")))
        self.assertTrue(reason.startswith(showrunner_footer.REASON_HEAD), reason)

    def test_matching_footer_passes_with_one_or_more_waiting_items(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        for waiting in (WAITING_BLOCK, WAITING_BLOCK + "\n* no ETA measured - y"):
            with self.subTest(items=waiting.count("\n* ")):
                self.assert_passes(self.run_hook("Done.\n" + footer + waiting))

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

    def test_each_pending_question_is_needed_with_footers_off_until_one_reply_carries_it(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
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

    def test_a_footer_rejection_after_the_question_was_shown_never_asks_for_it_again(self) -> None:
        item = showrunner_footer.PendingReviewedQuestion("path", "Which path should we use?")
        showrunner_footer.add_pending_reviewed(self.instance, [item])
        self.assertIn("footer", self.reason(item.reviewed))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
        self.assert_passes(self.run_hook("A later turn.\n" + self.footer() + WAITING_BLOCK, stop_hook_active=True))
        self.assert_passes(self.run_hook("-", transcript_path=self.transcript(tick())))

    def test_unregistered_choices_are_blocked_but_code_and_quotes_are_not(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
        choice = "Run now (recommended), or wait."
        choice_reason = self.reason(choice)
        self.assertIn("record the item", choice_reason)
        self.assertIn("update_registration.py footer --state", choice_reason)
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

    def test_unregistered_requests_block_with_footers_off_and_on_hook_retries(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
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
                    self.assertIn("update_registration.py footer --state", reason)
                    self.assertIn("paste its reviewed question", reason)

    def test_unregistered_ask_is_reported_before_a_missing_footer(self) -> None:
        question = "Was that yours?"
        reason = self.reason(question)
        self.assertIn(question, reason)
        self.assertIn("update_registration.py footer --state", reason)
        self.assertNotIn("footer missing or out of date", reason)

    def test_each_outstanding_question_field_matches_normalised_question_prose(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
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
        self.assertEqual(self.run_switch("off").returncode, 0)
        first = "Should handler-enh go ahead with phases 1 and 2?"
        second = "Do you want the phase 3 labels?"
        item: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "hierarchy", "question": first}
        _ = self.outstanding.write_text(json.dumps([item]))
        reason = self.reason(first + " " + second)
        self.assertIn(second, reason)
        self.assertIn("update_registration.py footer --state", reason)
        item["reviewed"] = first + "\n" + second
        _ = self.outstanding.write_text(json.dumps([item]))
        self.assert_passes(self.run_hook(first + " " + second))

    def test_unrelated_production_and_code_only_text_do_not_register_a_user_ask(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
        question = "Want me to have it added?"
        stranger: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": question}
        _ = self.outstanding.with_name("stranger.json").write_text(json.dumps([stranger]))
        code_only: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "`add.py`"}
        _ = self.outstanding.write_text(json.dumps([code_only]))
        self.assertIn(question, self.reason(question))
        _ = self.outstanding.write_text(json.dumps([stranger]))
        self.assert_passes(self.run_hook(question))

    def test_a_new_ask_beside_a_shown_question_is_registered_without_repeating_the_question(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
        pending = showrunner_footer.PendingReviewedQuestion("route", "Which path should we use?")
        showrunner_footer.add_pending_reviewed(self.instance, [pending])
        request = "Say go and I'll run the diagnostic."
        self.assertIn(request, self.reason(pending.reviewed + "\n" + request, stop_hook_active=True))
        self.assertFalse(showrunner_footer.pending_reviewed_path(self.instance).exists())
        item: OutstandingUserQuestion = {"since": "2026-10-16T08:00", "text": "diagnostics", "question": request}
        _ = self.outstanding.write_text(json.dumps([item]))
        self.assert_passes(self.run_hook(request, stop_hook_active=True))

    def test_relayed_peer_question_and_attributed_unit_choice_pass_unregistered(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
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
        self.assertEqual(self.run_switch("off").returncode, 0)
        relay = "**Also sent to the hana showrunner:** Did you approve it?"
        asks = ("Want me to have it added?",
                "Allow (recommended, since both requests are a day old): the shutdown unit runs the cancel once.")
        for ask in asks:
            with self.subTest(ask=ask):
                reason = self.reason(relay + "\n\n**Question for you:**\n" + ask)
                self.assertIn(ask, reason)
                self.assertIn("update_registration.py footer --state", reason)

    def test_recorded_choices_pass_with_list_markers_but_unrecorded_choices_block(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
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
        self.assertEqual(self.run_switch("off").returncode, 0)
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
        self.assertEqual(self.run_switch("off").returncode, 0)
        self.assert_passes(self.run_hook(question))
        self.assertIn("record the item", self.reason("Use the long route (recommended), or wait?"))

    def test_footer_stays_valid_when_a_reading_arrives_after_its_stamp(self) -> None:
        zone = ZoneInfo(ZONE)
        stamp = datetime.fromtimestamp(datetime.now(zone).timestamp() - 60, zone).replace(second=0, microsecond=0)
        account = self.root / "rust/hanadocs/agents/claude1.md"
        readings = self.root / ".local/state/agent-notes/readings.jsonl"
        before_at = stamp + timedelta(seconds=20)
        after_at = stamp + timedelta(minutes=1, seconds=10)

        def note(remaining: int, checked: datetime) -> None:
            _ = account.write_text(
                "---\nrole: [unit]\n"
                + f"weekly_remaining_usage: {remaining}\n"
                + f"weekly_usage_checked_at: {checked.isoformat()}\n"
                + f"resets: {(stamp + timedelta(days=3)).isoformat()}\n"
                + "limit_reset_count: 0\n---\n",
                encoding="utf-8",
            )

        def reading(remaining: int, at: datetime) -> str:
            # Legacy-spelled readings still land on the canonical footer line.
            return json.dumps({"account": "claude 1", "at": at.isoformat(), "remaining": remaining}) + "\n"

        note(81, before_at)
        _ = readings.write_text(reading(81, before_at), encoding="utf-8")
        footer = self.footer(at=stamp)
        self.assertIn("* claude1: 19%", footer)

        note(80, after_at)
        _ = readings.write_text(reading(81, before_at) + reading(80, after_at), encoding="utf-8")
        self.assertEqual(self.footer(at=stamp), footer)
        self.assert_passes(self.run_hook("Done.\n" + footer + WAITING_BLOCK))

    def test_missing_footer_blocks_with_exact_current_lines_and_active_agent(self) -> None:
        reason = self.assert_blocked_with_current_footer("Done.")
        self.assertIn("\n---\n", reason)
        self.assertIn(AGENT_LINE, reason)
        self.assertNotIn("### Agents", reason)

    def test_extra_line_after_footer_blocks(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        _ = self.assert_blocked_with_current_footer("Done.\n" + footer + WAITING_BLOCK + "\nAnother line")

    def test_missing_or_legacy_waiting_block_blocks(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        for reply in ("Done.\n" + footer,
                      "Done.\n" + footer + "\n— waiting on: you: x",
                      "Done.\n" + footer + "\n\n\nWaiting on:",
                      "Done.\n" + footer + "\n\nWaiting on:\n\n* no ETA measured - x",
                      "Done.\n" + footer + "\n\n\nWaiting on:\n\n- no ETA measured - x"):
            with self.subTest(reply=reply):
                _ = self.assert_blocked_with_current_footer(reply)

    def test_footer_twenty_minutes_old_blocks(self) -> None:
        old = datetime.now(ZoneInfo(ZONE)) - timedelta(minutes=20)
        _ = self.assert_blocked_with_current_footer("Done.\n" + self.footer(at=old) + WAITING_BLOCK)

    def test_current_footer_in_repeated_pst_hour_passes(self) -> None:
        """The reply's PST stamp selects the second 01:30 occurrence on the fall-back day."""
        pst_minute = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo(ZONE), fold=1)
        self.next_due = datetime(2026, 11, 1, 2, 0, tzinfo=ZoneInfo(ZONE))
        self.write_state()
        footer = self.footer(at=pst_minute)
        self.assertIn("01:30 PST update:", footer)

        class FixedClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return datetime.fromtimestamp(pst_minute.timestamp(), tz) if tz else pst_minute.replace(tzinfo=None)

        with patch.dict(os.environ, self.environment), patch.object(showrunner_footer, "datetime", FixedClock):
            self.assertIsNone(showrunner_footer.block_reason(
                [str(self.instance)], "Done.\n" + footer + WAITING_BLOCK,
                {"demo": showrunner_footer.FooterState.ON},
            ))

    def test_false_nothing_needed_claim_blocks_and_reason_drops_suffix(self) -> None:
        _ = self.outstanding.write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "choose the path"}]))
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        reason = self.assert_blocked_with_current_footer(
            "Done.\n" + footer + " - nothing needed\n\n\nWaiting on:\n\n* " + ONE_OPEN)
        self.assertIn(f"Waiting on:\n\n* {ONE_OPEN}\n* <item>", reason)
        self.assertFalse(any(line.startswith("* next dailies:") and line.endswith(" - nothing needed")
                             for line in reason.splitlines()))

    def test_nothing_needed_claim_passes_without_outstanding_items(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)), nothing_needed=True)
        self.assert_passes(self.run_hook("Done.\n" + footer + WAITING_BLOCK))

    def test_next_day_schedule_uses_local_weekday_in_reason(self) -> None:
        local = datetime.now(ZoneInfo(ZONE))
        self.next_due = (local + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
        self.write_state()
        reason = self.assert_blocked_with_current_footer("Done.")
        self.assertIn(f"* next dailies: {self.next_due:%a %H:%M %Z}", reason)

    def test_disabled_schedule_says_no_dailies_scheduled(self) -> None:
        self.write_state(enabled=False)
        reason = self.assert_blocked_with_current_footer("Done.")
        self.assertIn("no dailies scheduled", reason)

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

    def test_switch_off_allows_any_reply_and_on_requires_footer_and_waiting(self) -> None:
        self.assertEqual(self.run_switch("status").stdout, "demo footers on\n")
        self.assertEqual(self.run_switch("off").returncode, 0)
        self.assertEqual(self.run_switch("off").returncode, 0)
        self.assert_passes(self.run_hook("Done."))
        self.assert_passes(self.run_hook("Done." + WAITING_BLOCK))
        self.assert_passes(self.run_hook("Done.\n\nWaiting on:\n* malformed"))
        self.assert_passes(self.run_hook("Done.\n" + self.footer() + WAITING_BLOCK))
        self.assertEqual(self.run_switch("status").stdout, "demo footers off\n")
        self.assertEqual(self.run_switch("on").returncode, 0)
        self.assertIn("footer", self.reason("Done." + WAITING_BLOCK))
        _ = self.assert_blocked_with_current_footer("Done.\n" + self.footer())
        self.assert_passes(self.run_hook("Done.\n" + self.footer() + WAITING_BLOCK))

    def test_switch_survives_new_target_and_is_per_production(self) -> None:
        self.assertEqual(self.run_switch("off").returncode, 0)
        other = self.notifier / "showrunner-other"
        other.mkdir()
        _ = (other / "conf").write_text(f"TARGET=session:another\nCHECK=zsh /x/check.sh {self.doc}\n")
        _ = (other / "state").write_text((self.instance / "state").read_text())
        _ = (self.instance / "conf").write_text(
            f"TARGET=session:new-target\nCHECK=zsh /x/check.sh {self.doc}\n")
        self.assert_passes(self.run_hook("Done." + WAITING_BLOCK, session_id="new-target"))
        self.assertIn("footer", self.reason_for_session("another", "Done." + WAITING_BLOCK))
        self.assertEqual(self.run_switch("status", "new-target").stdout, "demo footers off\n")
        self.assertEqual(self.run_switch("status", "another").stdout, "other footers on\n")
        missing = self.run_switch("status", "not-targeted")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no production targets this session", missing.stderr + missing.stdout)

    def reason_for_session(self, session: str, reply: str) -> str:
        result = self.run_hook(reply, session_id=session)
        decision = cast(dict[str, str], json.loads(result.stdout))
        return decision["reason"]

    def test_two_productions_in_one_session_require_each_enabled_footer(self) -> None:
        other = self.notifier / "showrunner-other"
        other.mkdir()
        _ = (other / "conf").write_text(f"TARGET=session:{SESSION}\nCHECK=zsh /x/check.sh {self.doc}\n")
        _ = (other / "state").write_text("ENABLED=0\n")
        self.assertEqual(self.run_switch("status").stdout, "demo footers on\nother footers on\n")
        self.assertIn("footer", self.reason("Done.\n" + self.footer() + WAITING_BLOCK))
        other_footer = subprocess.run(
            [sys.executable, str(DAILIES), "--footer", "--zone", ZONE,
             "--outstanding", str(self.outstanding.with_name("other.json")),
             "--nothing-needed"],
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(other_footer.returncode, 0, other_footer.stderr)
        self.assert_passes(self.run_hook("Done.\n" + self.footer() + "\n"
                                         + other_footer.stdout.rstrip("\n") + WAITING_BLOCK))
        self.assertEqual(self.run_switch("off").returncode, 0)
        self.assertEqual(self.run_switch("on").returncode, 0)
        # Set one switch directly to represent a separately paused production.
        switch = self.root / "showrunner-state/footers-off/other"
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.touch()
        self.assertIn("footer", self.reason("Done." + WAITING_BLOCK))
        self.assert_passes(self.run_hook("Done.\n" + self.footer() + WAITING_BLOCK))

    def test_two_production_footers_pass_in_reverse_instance_order(self) -> None:
        other = self.notifier / "showrunner-other"
        other.mkdir()
        _ = (other / "conf").write_text(f"TARGET=session:{SESSION}\nCHECK=zsh /x/check.sh {self.doc}\n")
        _ = (other / "state").write_text("ENABLED=0\n")
        other_footer = subprocess.run(
            [sys.executable, str(DAILIES), "--footer", "--zone", ZONE,
             "--outstanding", str(self.outstanding.with_name("other.json")),
             "--nothing-needed"],
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )
        self.assertEqual(other_footer.returncode, 0, other_footer.stderr)
        reply = "Done.\n" + other_footer.stdout.rstrip("\n") + "\n" + self.footer() + WAITING_BLOCK
        self.assert_passes(self.run_hook(reply))

    def test_one_enabled_production_still_requires_waiting_block(self) -> None:
        other = self.notifier / "showrunner-other"
        other.mkdir()
        _ = (other / "conf").write_text(f"TARGET=session:{SESSION}\nCHECK=zsh /x/check.sh {self.doc}\n")
        _ = (other / "state").write_text("ENABLED=0\n")
        switch = self.root / "showrunner-state/footers-off/other"
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.touch()
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        _ = self.assert_blocked_with_current_footer("Done.\n" + footer)
        self.assert_passes(self.run_hook("Done.\n" + footer + WAITING_BLOCK))

    def test_two_productions_cannot_share_one_footer_span(self) -> None:
        other = self.notifier / "showrunner-other"
        other.mkdir()
        _ = (other / "conf").write_text(f"TARGET=session:{SESSION}\nCHECK=zsh /x/check.sh {self.doc}\n")
        _ = (other / "state").write_text((self.instance / "state").read_text())
        self.assertIn("footer", self.reason("Done.\n" + self.footer() + WAITING_BLOCK))

    def test_waiting_items_follow_eta_first_rules(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        tomorrow = (datetime.now(ZoneInfo(ZONE)) + timedelta(days=1)).strftime("%a")
        valid = [
            "* 19:45 (18:20–23:55) - startup Phase 16",
            "* 23:30 - later", "* 00:15+1 - overnight",
            f"* {tomorrow} 09:00 - tomorrow", "* no ETA measured - awaiting estimate",
        ]
        def reply(items: list[str]) -> str:
            return "Done.\n" + footer + "\n\n\nWaiting on:\n\n" + "\n".join(items)
        self.assert_passes(self.run_hook(reply(valid)))
        cases = [
            (["* startup Phase 16 ETA 19:45"], "ETA"),
            (["* startup 19:45 (18:20–23:55)"], "ETA"),
            (["* 19:45 PDT - startup"], "zone"),
            (["* 21:00 - later", "* 19:45 - earlier"], "order"),
            (["* no ETA measured - one", "* 19:45 - two"], "order"),
            (["* 23:30 - today", "* 00:15 - earlier today"], "order"),
        ]
        for items, rule in cases:
            with self.subTest(items=items):
                reason = self.reason(reply(items))
                self.assertIn(rule.lower(), reason.lower())
                self.assertIn("19:45 (18:20–23:55) - startup Phase 16", reason)
                self.assertIn("no ETA measured - <item>", reason)

    def waiting(self, footer: str, *bullets: str) -> str:
        return "Done.\n" + footer + "\n\n\nWaiting on:\n\n" + "\n".join(f"* {bullet}" for bullet in bullets)

    def write_open(self, *items: dict[str, str]) -> str:
        """Store what waits on the user and return the footer rendered with it."""
        _ = self.outstanding.write_text(json.dumps(list(items)))
        return self.footer(at=datetime.now(ZoneInfo(ZONE)))

    def test_open_questions_show_only_as_their_count(self) -> None:
        path = {"since": "2026-10-06T08:00", "text": "WESL: prepare first, or straight to the upgrade?"}
        merge = {"since": "2026-10-06T09:00", "text": "merge the branch today?"}
        footer = self.write_open(path)
        self.assert_passes(self.run_hook(self.waiting(footer, ONE_OPEN)))
        self.assert_passes(self.run_hook(self.waiting(footer, ONE_OPEN, "19:45 - startup Phase 16")))
        footer = self.write_open(path, merge)
        self.assert_passes(self.run_hook(self.waiting(footer, TWO_OPEN, "no ETA measured - startup")))
        refused = [
            ("the question restated", [path["text"]]),
            ("the question beside its count", [TWO_OPEN, path["text"]]),
            ("the wrong count", ["There are 3 open questions for you."]),
            ("the count for one", [ONE_OPEN]),
            ("the count twice", [TWO_OPEN, TWO_OPEN]),
            ("no count", ["19:45 - startup Phase 16"]),
        ]
        for name, bullets in refused:
            with self.subTest(name):
                reason = self.reason(self.waiting(footer, *bullets))
                self.assertIn(f"`* {TWO_OPEN}`", reason)
                self.assertIn("asked once, in full", reason)
                self.assertIn("only when the user asks", reason)

    def test_deferred_question_is_not_counted_until_its_time(self) -> None:
        footer = self.write_open(
            {"since": "2026-10-06T08:00", "text": "choose the path"},
            {"since": "2026-10-06T09:00", "text": "merge today?", "after": "2099-01-01T00:00"},
            {"since": "2026-10-06T10:00", "text": "rename the unit?", "after": "2026-10-07T00:00"},
        )
        self.assert_passes(self.run_hook(self.waiting(footer, TWO_OPEN, "no ETA measured - startup")))
        self.assertIn(f"`* {TWO_OPEN}`", self.reason(self.waiting(footer, "There are 3 open questions for you.")))

    def test_with_nothing_open_the_block_takes_no_line_for_the_user(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        self.assert_passes(self.run_hook(self.waiting(footer, "no ETA measured - startup")))
        for bullet in (ONE_OPEN, "you: choose the path"):
            with self.subTest(bullet=bullet):
                reason = self.reason(self.waiting(footer, bullet, "no ETA measured - startup"))
                self.assertIn(f"remove `* {bullet}`", reason)

    def test_missing_footer_reason_shows_the_count_line(self) -> None:
        reason = self.assert_blocked_with_current_footer("Done.")
        self.assertTrue(reason.endswith("Waiting on:\n\n* <item>"), reason)
        _ = self.write_open({"since": "2026-10-06T08:00", "text": "choose the path"},
                            {"since": "2026-10-06T09:00", "text": "merge today?"})
        reason = self.assert_blocked_with_current_footer("Done.")
        self.assertTrue(reason.endswith(f"Waiting on:\n\n* {TWO_OPEN}\n* <item>"), reason)
        self.assertIn("never restated", reason)

    def test_count_covers_every_production_the_session_runs(self) -> None:
        _ = self.outstanding.write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "choose the path"}]))
        _ = self.outstanding.with_name("other.json").write_text(json.dumps(
            [{"since": "2026-10-06T09:00", "text": "merge today?"}]))
        quiet = showrunner_footer.NoDailiesSchedule.PAUSED_OR_UNSCHEDULED
        productions = [showrunner_footer.Production(slug, self.doc, ZONE, quiet) for slug in ("demo", "other", "absent")]
        with patch.dict(os.environ, self.environment):
            self.assertEqual(showrunner_footer.open_questions(productions, {}), 2)
            _ = self.outstanding.with_name("other.json").write_text("{bad")
            self.assertIsNone(showrunner_footer.open_questions(productions, {}))
        self.assertIsNone(showrunner_footer.open_questions_error(["you: choose the path"], None))

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

    def test_root_count_covers_every_production_below_it(self) -> None:
        for slug in ("kid", "grandkid", "stranger"):
            self.timer(slug)
            self.write_open_for(slug, f"{slug}: merge today?")
        self.attach("kid", "demo")
        self.attach("grandkid", "kid")
        footer = self.write_open({"since": "2026-10-06T08:00", "text": "choose the path"})
        three = "There are 3 open questions for you."
        self.assert_passes(self.run_hook(self.waiting(footer, three)))
        self.assertIn(f"`* {three}`", self.reason(self.waiting(footer, ONE_OPEN)))
        self.write_open_for("grandkid")
        self.assert_passes(self.run_hook(self.waiting(footer, TWO_OPEN)))

    def test_a_showrunner_below_a_root_counts_only_its_own_items(self) -> None:
        for slug in ("top", "kid"):
            self.timer(slug)
            self.write_open_for(slug, f"{slug}: merge today?")
        self.attach("demo", "top")
        self.attach("kid", "demo")
        alone = self.write_open({"since": "2026-10-06T08:00", "text": "choose the path"})
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)), reports_to="top")
        self.assertIn("* reports to top", footer.splitlines())
        self.assert_passes(self.run_hook(self.waiting(footer, ONE_OPEN)))
        self.assertIn(f"`* {ONE_OPEN}`", self.reason(self.waiting(footer, TWO_OPEN)))
        self.assertIn("* reports to top", self.reason(self.waiting(alone, ONE_OPEN)).splitlines())

    def test_an_unreadable_list_below_the_root_skips_the_count_check(self) -> None:
        self.timer("kid")
        self.attach("kid", "demo")
        alone = self.write_open({"since": "2026-10-06T08:00", "text": "choose the path"})
        _ = self.outstanding.with_name("kid.json").write_text("{bad")
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)), below=("kid",))
        self.assertIn("* kid: needs-user list unreadable", footer.splitlines())
        for count in (ONE_OPEN, TWO_OPEN):
            with self.subTest(count=count):
                self.assert_passes(self.run_hook(self.waiting(footer, count)))
        # The root's footer must name the list it could not read.
        self.assertIn("* kid: needs-user list unreadable", self.reason(self.waiting(alone, ONE_OPEN)).splitlines())

    def test_a_lower_item_keeps_the_root_footer_from_saying_nothing_needed(self) -> None:
        self.timer("kid")
        self.attach("kid", "demo")
        quiet = self.footer(at=datetime.now(ZoneInfo(ZONE)), nothing_needed=True)
        self.assertTrue(quiet.endswith(" - nothing needed"))
        self.assert_passes(self.run_hook(self.waiting(quiet, "no ETA measured - x")))
        self.write_open_for("kid", "kid: merge today?")
        busy = self.footer(at=datetime.now(ZoneInfo(ZONE)), nothing_needed=True, below=("kid",))
        self.assertFalse(busy.endswith(" - nothing needed"))
        self.assert_passes(self.run_hook(self.waiting(busy, ONE_OPEN)))

    def test_produce_states_the_count_rule_in_the_hook_s_words(self) -> None:
        produce = " ".join((SETTINGS.parent / "commands/showrunner/produce.md").read_text(encoding="utf-8").split())
        self.assertIn(f"`{showrunner_footer.count_sentence(1)}`", produce)
        self.assertIn("`There are {n} open questions for you.`", produce)
        self.assertEqual(showrunner_footer.count_sentence(7), "There are 7 open questions for you.")
        self.assertNotIn("led by `quota: `", produce)
        self.assertNotIn("The user's items come first", produce)

    def test_old_quota_hold_bullet_has_no_footer_exemption(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        bullet = "quota: Codex usage is exhausted"
        reason = self.reason(self.waiting(footer, bullet))
        self.assertIn(f"remove `* {bullet}`", reason)

    def test_no_eta_items_and_malformed_leading_eta_cannot_hide_times(self) -> None:
        footer = self.footer(at=datetime.now(ZoneInfo(ZONE)))
        for item in (
            "no ETA measured - startup 19:45 (18:20–23:55)",
            "no ETA measured - startup ETA 19:45",
            "19:45 PDT (18:20–23:55) - item",
        ):
            with self.subTest(item=item):
                reason = self.reason("Done.\n" + footer + "\n\n\nWaiting on:\n\n* " + item)
                self.assertIn("ETA", reason)


if __name__ == "__main__":
    _ = unittest.main()
