"""Library specification tests using fake commands and a fake proc tree."""

from __future__ import annotations


import json
import io
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
import os
import signal
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast, final, override
from unittest.mock import Mock, call, patch

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import herdr
import fake_herdr
import trust_survey
from fake_herdr import CommandResponse
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, unit_membership

GIT_BINARY = shutil.which("git") or "git"


# herdr 0.9.0, Mac, 2026-10-16 19:47 EDT: verbatim startup-shell list entries.
STARTUP_PANE_JSON = '{"agent_status":"unknown","cwd":"/Users/natemccoy","focused":false,"foreground_cwd":"/Users/natemccoy","pane_id":"wG:p1","revision":0,"scroll":{"max_offset_from_bottom":0,"offset_from_bottom":0,"viewport_rows":61},"tab_id":"wG:t1","terminal_id":"term_65dfdc73afbe63","workspace_id":"wG"}'
STARTUP_WORKSPACE_JSON = '{"active_tab_id":"wG:t1","agent_status":"unknown","focused":false,"label":"~","number":1,"pane_count":1,"tab_count":1,"workspace_id":"wG"}'


def startup_shell(workspace_id: str) -> tuple[dict[str, object], dict[str, object]]:
    """Adapt the captured shell's identity and home directory for this fixture."""
    workspace = herdr.decode_object(STARTUP_WORKSPACE_JSON, "captured startup workspace")
    pane = herdr.decode_object(STARTUP_PANE_JSON, "captured startup pane")
    workspace.update(workspace_id=workspace_id, active_tab_id=workspace_id + ":t1")
    pane.update(workspace_id=workspace_id, pane_id=workspace_id + ":p1", tab_id=workspace_id + ":t1",
                cwd=str(Path.home()), foreground_cwd=str(Path.home()))
    return workspace, pane


class StartupClock:
    elapsed: float = 0.0


class WaitingAgentCLI:
    """A waiting CLI whose clock also advances the fake pane's processes."""

    def __init__(self, clock: StartupClock, ready_after: float, on_wait: Callable[[], None]) -> None:
        self.clock: StartupClock = clock
        self.ready_after: float = ready_after
        self.on_wait: Callable[[], None] = on_wait
        self.returncode: int | None = None  # Popen's external running-process state.
        self.killed: bool = False

    def __enter__(self) -> WaitingAgentCLI:
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        pass

    def poll(self) -> int | None:
        if self.clock.elapsed >= self.ready_after:
            self.returncode = 0
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if timeout is not None:
            self.clock.elapsed += timeout
            self.on_wait()
        if self.poll() is None:
            if timeout is None:
                raise AssertionError("unbounded collection of a running CLI")
            raise subprocess.TimeoutExpired(["fake agent start"], timeout)
        return "CLI completed", ""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


# The probe that each transient server start runs before it gives panes their own scopes.
SCOPE_PROBE = ["--user", "--scope", "--quiet", "--collect", "--", "true"]
REAL_SUBMIT_OPENING_PROMPT = herdr.Herdr.submit_opening_prompt
REAL_LOAD_FACTOR = herdr.load_factor
PROCESS_SESSIONS = herdr._process_sessions  # pyright: ignore[reportPrivateUsage]
REQUIRE_SHARED = herdr._require_shared  # pyright: ignore[reportPrivateUsage]


@final
class PromptSubmissionTests(unittest.TestCase):
    """Real scratch transcripts, with every pane command and elapsed second faked."""

    root: Path = Path()
    transcript: herdr.SessionTranscript = herdr.SessionTranscript(Path(), "fixture")
    path: Path = Path()
    launched_at: datetime = datetime(2026, 10, 17, tzinfo=timezone.utc)
    elapsed: float = 0
    ready_at: float = 0
    typed: list[tuple[str, str, float]] = []
    enters: list[tuple[str, tuple[str, ...]]] = []
    ready_panes: list[str] = []
    screen: str = ""
    typed_screen: str = ""
    drop_before: float = 0
    drop_all: bool = False
    working_until: float = 0
    turn_delay: float = 0
    events: list[tuple[float, Callable[[], None]]] = []
    submit_on_type: bool = False
    submit_on_enter: int = 0
    client: herdr.Herdr = cast(herdr.Herdr, Mock())
    start: Mock = Mock()
    read: Mock = Mock()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"}, clear=True)))
        # Test limits stay as written on a loaded machine.
        _ = self.enterContext(patch.object(herdr, "load_factor", return_value=1.0))
        self.transcript = herdr.SessionTranscript(self.root / ".claude3", "moved-session")
        self.path = self.transcript.config_dir / "projects/project/moved-session.jsonl"
        self.path.parent.mkdir(parents=True)
        self.launched_at = datetime.now(timezone.utc)
        self.elapsed = 0.0
        self.ready_at = 0.0
        self.typed = []
        self.enters = []
        self.ready_panes = []
        self.screen = "────────\n❯ \n────────"
        self.typed_screen = ""
        self.drop_before = 0
        self.drop_all = False
        self.working_until = 0
        self.turn_delay = 0
        self.events = []
        self.submit_on_type = False
        self.submit_on_enter = 0
        self.client = herdr.Herdr("agents")
        _ = self.enterContext(patch.object(herdr, "prompt_recipient_process",
            return_value=herdr.PromptRecipientProcess(23456, "fixture-prompt-recipient")))
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", side_effect=self.advance))
        self.start = cast(Mock, self.enterContext(patch.object(self.client, "start_command",
            return_value=subprocess.CompletedProcess(["fixture agent start"], 0, "", ""))))
        _ = self.enterContext(patch.object(self.client, "readiness", side_effect=self.readiness))
        self.read = cast(Mock, self.enterContext(patch.object(self.client, "run", side_effect=self.read_screen)))
        _ = self.enterContext(patch.object(self.client, "prompt", side_effect=self.type_prompt))
        _ = self.enterContext(patch.object(self.client, "send_keys", side_effect=self.enter_prompt))

    def advance(self, seconds: float) -> None:
        self.assertGreater(seconds, 0)
        self.elapsed += seconds
        due = [event for event in self.events if event[0] <= self.elapsed]
        self.events = [event for event in self.events if event[0] > self.elapsed]
        for _at, action in due:
            action()

    def readiness(self, pane: str) -> herdr.WorkerReadiness:
        self.ready_panes.append(pane)
        ready = pane == "sibling:p1" or self.elapsed >= self.ready_at
        status = "working" if self.elapsed < self.working_until else "idle"
        return {"agent_status": status if ready else "starting", "interactive_ready": ready}

    def read_screen(self, *arguments: str) -> str:
        self.assertIn(arguments, {("pane", "read", "destination:p1", "--source", "visible"),
                                  ("pane", "read", "destination:p1", "--source", "visible", "--format", "ansi")})
        return self.screen

    def write_turn(self, text: str, *, kind: str = "user", meta: bool = False,
                   seconds_after_launch: float = 1, blocks: bool = False) -> None:
        content: object = [{"type": "text", "text": text}] if blocks else text
        row = {"type": kind, "isMeta": meta,
               "timestamp": (self.launched_at + timedelta(seconds=seconds_after_launch)).isoformat(),
               "message": {"role": kind, "content": content}}
        with self.path.open("a") as stream:
            _ = stream.write(json.dumps(row) + "\n")

    def type_prompt(self, pane: str, text: str) -> None:
        self.typed.append((pane, text, self.elapsed))
        if self.drop_all or self.elapsed < self.drop_before:
            return
        if self.submit_on_type:
            self.write_turn(text, seconds_after_launch=self.elapsed + 1)
            self.screen = "────────\n❯ \n────────"
        elif self.turn_delay:
            self.events.append((self.elapsed + self.turn_delay,
                lambda: self.write_turn(text, seconds_after_launch=self.elapsed + 1)))
            self.screen = "────────\n❯ \n────────"
        else:
            self.screen = self.typed_screen or f"────────\n❯ {text}\n────────"

    def enter_prompt(self, pane: str, *keys: str) -> None:
        self.enters.append((pane, keys))
        if len(self.enters) == self.submit_on_enter:
            self.write_turn(self.typed[-1][1], seconds_after_launch=self.elapsed + 1)
            self.screen = "────────\n❯ \n────────"

    def submit(self, timeout_ms: int = 30_000, *, text: str = "resume the saved work") -> herdr.PromptSubmitOutcome:
        return self.client.submit_opening_prompt("destination:p1",
            herdr.OpeningPrompt(text, self.transcript), self.launched_at, timeout_ms)

    @contextmanager
    def unreadable_transcript(self, from_second: float, until_second: float) -> Generator[list[float]]:
        original_open = Path.open
        failures: list[float] = []

        @contextmanager
        def open_transcript(path: Path, mode: str) -> Generator[BinaryIO]:
            if path == self.path and mode == "rb" and from_second <= self.elapsed < until_second:
                failures.append(self.elapsed)
                raise PermissionError("scratch transcript temporarily unreadable")
            with original_open(path, mode) as stream:
                yield cast(BinaryIO, stream)

        with patch.object(Path, "open", open_transcript):
            yield failures

    def test_shared_tab_lost_enter_is_retried_once_without_typing_the_prompt_twice(self) -> None:
        self.submit_on_enter = 1
        outcome = self.client.start_agent("moved-session", "destination:p1", "claude",
            ["--resume", "moved-session"], herdr.OpeningPrompt("resume the saved work", self.transcript), 30_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).process,
                         herdr.PromptRecipientProcess(23456, "fixture-prompt-recipient"))
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", "resume the saved work")])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        cast(Mock, self.start).assert_called_once()
        self.assertEqual(set(self.ready_panes), {"destination:p1"})
        self.assertGreaterEqual(cast(Mock, self.read).call_count, 2)

    def test_no_user_turn_and_no_prompt_in_the_input_reports_not_submitted(self) -> None:
        self.drop_all = True
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 3)
        self.assertEqual(self.enters, [])
        self.assertLessEqual(self.elapsed, 90)
        self.assertTrue(all(at < 90 for _pane, _text, at in self.typed))
        self.assertGreaterEqual(self.typed[1][2] - self.typed[0][2], 10)
        self.assertGreaterEqual(self.typed[2][2] - self.typed[1][2], 10)
        self.assertIn("prompt typed 3 times", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(cast(herdr.PromptNotSubmitted, outcome).screen, self.screen)

    def test_slash_command_recorded_as_command_tags_is_submitted_once(self) -> None:
        text = "/showrunner:produce /x/doc.md resume"
        recorded = ("<command-message>showrunner:produce</command-message>\n"
                    "<command-name>/showrunner:produce</command-name>\n"
                    "<command-args>/x/doc.md resume</command-args>")
        self.drop_all = True
        self.events.append((8, lambda: self.write_turn(recorded, seconds_after_launch=8)))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=8))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    def test_slash_command_recorded_as_raw_text_is_submitted_once(self) -> None:
        text = "/showrunner:produce /x/doc.md resume"
        self.drop_all = True
        self.events.append((8, lambda: self.write_turn(text, seconds_after_launch=8)))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=8))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    def test_real_recorded_showrunner_resume_row_is_submitted_once(self) -> None:
        self.launched_at = datetime.fromisoformat("2026-10-18T04:38:39+00:00")
        text = "/showrunner:produce /home/natepiano/rust/cargo-liner-style/docs/cargo-liner-style-production.md resume"
        row = {"type": "user", "timestamp": "2026-10-18T04:38:44.088Z", "message": {"role": "user", "content": "<command-message>showrunner:produce</command-message>\n<command-name>/showrunner:produce</command-name>\n<command-args>/home/natepiano/rust/cargo-liner-style/docs/cargo-liner-style-production.md resume</command-args>"}}
        self.drop_all = True
        def record_turn() -> None:
            _ = self.path.write_text(json.dumps(row) + "\n")
        self.events.append((5.088, record_turn))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at,
                         datetime.fromisoformat("2026-10-18T04:38:44.088+00:00"))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    PRODUCE = "/showrunner:produce /x/doc.md resume"

    def write_rejection(self, command: str) -> None:
        """Claude's answer to a command its list lacks, as session cbc14765 recorded it on 2026-10-21."""
        at = (self.launched_at + timedelta(seconds=self.elapsed + 1)).isoformat()
        with self.path.open("a") as stream:
            for content in (f"Unknown command: {command}", "Args from unknown skill: /x/doc.md resume"):
                _ = stream.write(json.dumps({"type": "system", "subtype": "informational", "content": content,
                                             "isMeta": False, "level": "warning", "timestamp": at}) + "\n")

    def submit_command(self, accept: Callable[[str, int], bool], timeout_ms: int = 90_000) -> herdr.PromptSubmitOutcome:
        """Type into a session that answers `Unknown command` unless `accept(text, typing)` takes the text."""
        def type_text(pane: str, text: str) -> None:
            self.typed.append((pane, text, self.elapsed))
            if accept(text, len(self.typed)):
                self.write_turn(text, seconds_after_launch=self.elapsed + 1)
            else:
                self.write_rejection(text.split()[0])
        with patch.object(self.client, "prompt", side_effect=type_text):
            return self.submit(timeout_ms, text=self.PRODUCE)

    def command_file(self) -> Path:
        path = self.transcript.config_dir / "commands/showrunner/produce.md"
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Produce\n")
        return path

    def test_an_unknown_command_is_typed_once_more_after_its_list_settles(self) -> None:
        outcome = self.submit_command(lambda _text, typing: typing == 2)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([text for _pane, text, _at in self.typed], [self.PRODUCE] * 2)
        self.assertGreaterEqual(self.typed[1][2] - self.typed[0][2], herdr.PROMPT_SETTLE_SECONDS)
        self.assertEqual(self.enters, [])

    def test_a_command_unknown_twice_is_sent_as_its_file_in_a_plain_prompt(self) -> None:
        path = self.command_file()
        outcome = self.submit_command(lambda text, _typing: text.startswith("This session"))
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        fallback = (f"This session's command list does not have /showrunner:produce yet, so read {path.resolve()} "
                    + "in full and follow it as /showrunner:produce with the arguments: /x/doc.md resume")
        self.assertEqual([text for _pane, text, _at in self.typed], [self.PRODUCE, self.PRODUCE, fallback])
        self.assertLess(self.elapsed, 60)

    def test_a_command_unknown_twice_with_no_file_reports_the_refusal(self) -> None:
        outcome = self.submit_command(lambda _text, _typing: False)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        reason = cast(herdr.PromptNotSubmitted, outcome).reason
        self.assertIn("Claude answered Unknown command: /showrunner:produce; prompt typed 2 times", reason)
        self.assertEqual([text for _pane, text, _at in self.typed], [self.PRODUCE] * 2)
        self.assertFalse(cast(herdr.PromptNotSubmitted, outcome).never_typed)

    def test_rejections_count_only_this_prompts_command(self) -> None:
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        for command in ("/showrunner:produce-x", "/showrunner:producer", "/showrunner:produce", "/compat. Did you mean /compact?"):
            self.write_rejection(command)
        self.assertEqual(cursor.command_rejections(self.PRODUCE), 1)
        self.assertEqual(cursor.command_rejections("resume the saved work"), 0)

    def test_a_fallback_needs_a_slash_command_with_a_file(self) -> None:
        path = self.command_file()
        self.assertIsNone(herdr.command_fallback("resume the saved work", self.transcript))
        self.assertIsNone(herdr.command_fallback("/showrunner:missing x", self.transcript))
        self.assertIsNone(herdr.command_fallback("/../showrunner:produce", self.transcript))
        self.assertEqual(herdr.command_fallback("/showrunner:produce", self.transcript),
                         f"This session's command list does not have /showrunner:produce yet, so read {path.resolve()} "
                         + "in full and follow it as /showrunner:produce with no arguments")

    def test_slash_command_matching_requires_the_name_arguments_and_non_meta_user(self) -> None:
        for text, content, kind, meta, matches in (
            ("/check", "<command-name>/check</command-name>", "user", False, True),
            ("/check   /x/doc.md resume  ", "<command-name>/check</command-name>\n<command-args>/x/doc.md resume</command-args>", "user", False, True),
            ("/check /x/doc.md", "<command-name>/other</command-name>\n<command-args>/x/doc.md</command-args>", "user", False, False),
            ("/check /x/doc.md", "<command-name>/check</command-name>\n<command-args>/x/other.md</command-args>", "user", False, False),
            ("/check /x/doc.md", "<command-name>/check</command-name>", "user", False, False),
            ("/check", "<command-name>/check</command-name>", "user", True, False),
            ("/check", "<command-name>/check</command-name>", "assistant", False, False),
        ):
            with self.subTest(text=text, content=content, kind=kind, meta=meta):
                _ = self.path.write_text("")
                self.write_turn(content, kind=kind, meta=meta)
                outcome = herdr.submitted_prompt(self.transcript, text, self.launched_at)
                self.assertEqual(isinstance(outcome, herdr.PromptSubmitted), matches)

    def test_final_read_finds_a_turn_after_the_last_deadline_poll(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit(timeout_ms=5_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=5))
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_final_read_also_precedes_a_non_deadline_give_up(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
                self.screen = "────────\n❯ another task\n────────"
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_final_read_finds_a_turn_when_the_screen_read_fails_after_the_last_poll(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
                raise OSError("scratch pane read failed")
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_lost_prompt_is_typed_again_after_loading_finishes(self) -> None:
        self.drop_before = 7
        self.submit_on_type = True
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", "resume the saved work")] * 2)
        self.assertEqual(self.typed[0][2], 0)
        self.assertGreaterEqual(self.typed[1][2], 10)
        self.assertEqual(self.enters, [])

    def test_unreadable_accepted_turn_blocks_retyping_until_the_deadline(self) -> None:
        self.turn_delay = 8
        with self.unreadable_transcript(8, 20) as failures:
            outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertIn("transcript unreadable", failure.reason)
        self.assertIn("prompt typed 1 times", failure.reason)
        self.assertEqual(failure.screen, self.screen)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.elapsed, 16)
        self.assertTrue(any(at >= 10 for at in failures))
        self.assertIsInstance(herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at),
                              herdr.PromptSubmitted)

    def test_unreadable_accepted_turn_is_found_after_reads_recover_without_retyping(self) -> None:
        self.turn_delay = 8
        with self.unreadable_transcript(8, 13) as failures:
            outcome = self.submit(timeout_ms=30_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertTrue(failures)
        self.assertGreaterEqual(self.elapsed, 13)
        self.assertLess(self.elapsed, 30)

    def test_transcript_becoming_unreadable_at_the_final_check_blocks_retyping(self) -> None:
        self.drop_all = True
        self.write_turn("older context", kind="assistant", seconds_after_launch=-1)
        recipient = herdr.PromptRecipientProcess(23456, "same-recipient")

        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            if self.elapsed >= 10:
                self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)
            return recipient

        # Read errors start only after the recipient check at the retry boundary.
        original_open = Path.open
        failures: list[float] = []
        blocked = False

        def recipient_and_block_reads(session: str) -> herdr.PromptRecipientProcess:
            nonlocal blocked
            result = observe_recipient(session)
            blocked = self.elapsed >= 10
            return result

        @contextmanager
        def open_transcript(path: Path, mode: str) -> Generator[BinaryIO]:
            if path == self.path and mode == "rb" and blocked:
                failures.append(self.elapsed)
                raise PermissionError("scratch final transcript check unreadable")
            with original_open(path, mode) as stream:
                yield cast(BinaryIO, stream)

        with (patch.object(herdr, "prompt_recipient_process", side_effect=recipient_and_block_reads),
              patch.object(Path, "open", open_transcript)):
            outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("transcript unreadable", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertTrue(failures)
        self.assertEqual(failures[0], 10)
        self.assertEqual(self.elapsed, 16)

    def test_missing_composer_during_loading_recovers_and_retypes_the_lost_prompt(self) -> None:
        self.drop_before = 7
        self.submit_on_type = True
        self.events.append((1, lambda: setattr(self, "screen", "Restoring conversation")))
        self.events.append((7, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 2)
        self.assertEqual(self.typed[0][2], 0)
        self.assertGreaterEqual(self.typed[1][2], 10)
        self.assertEqual(self.enters, [])

    def test_missing_composer_waits_for_the_deadline_and_carries_the_last_screen(self) -> None:
        self.drop_all = True
        self.events.append((1, lambda: setattr(self, "screen", "Restoring conversation")))
        self.events.append((11, lambda: setattr(self, "screen", "Still loading conversation")))
        outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertIn("deadline", failure.reason)
        self.assertEqual(failure.screen, "Still loading conversation")
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.elapsed, 16)

    def test_accepted_prompt_with_a_turn_eight_seconds_late_is_never_typed_again(self) -> None:
        self.turn_delay = 8
        cursors: list[herdr.TranscriptCursor] = []
        original_read = herdr.TranscriptCursor.read_turns

        def read_turns(cursor: herdr.TranscriptCursor) -> list[tuple[datetime, dict[str, object]]]:
            cursors.append(cursor)
            return original_read(cursor)

        with patch.object(herdr.TranscriptCursor, "read_turns", autospec=True, side_effect=read_turns):
            outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.elapsed, 8)
        self.assertEqual(self.enters, [])
        self.assertGreater(len(cursors), 2)
        self.assertTrue(all(cursor is cursors[0] for cursor in cursors))

    def test_accepted_enter_with_a_turn_eight_seconds_late_is_not_retyped(self) -> None:
        def queued_enter(pane: str, *keys: str) -> None:
            self.enters.append((pane, keys))
            self.screen = "────────\n❯ \n────────"
            self.events.append((self.elapsed + 8,
                lambda: self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)))

        with patch.object(self.client, "send_keys", side_effect=queued_enter):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        self.assertGreaterEqual(self.elapsed, 13)

    def test_working_session_with_a_queued_prompt_waits_for_its_twenty_second_turn(self) -> None:
        self.turn_delay = 20
        self.working_until = 25
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.elapsed, 20)
        self.assertEqual(self.enters, [])

    def test_visible_empty_composer_is_required_even_when_herdr_reports_ready(self) -> None:
        self.screen = "Loading the saved conversation"
        self.events.append((4, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        self.submit_on_type = True
        outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.typed[0][2], 4)

    def test_other_text_in_the_composer_is_never_overwritten(self) -> None:
        self.screen = "────────\n❯ the user is editing this\n────────"
        self.assertIsInstance(self.submit(timeout_ms=1_000), herdr.PromptNotSubmitted)
        self.assertEqual((self.typed, self.enters), ([], []))

    def test_other_text_appearing_after_a_lost_prompt_blocks_retyping(self) -> None:
        self.drop_all = True
        self.events.append((3, lambda: setattr(self, "screen", "────────\n❯ another task\n────────")))
        outcome = self.submit(timeout_ms=30_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_changed_recipient_blocks_retyping_a_lost_prompt(self) -> None:
        self.drop_all = True
        original = herdr.PromptRecipientProcess(23456, "first-recipient")
        replacement = herdr.PromptRecipientProcess(34567, "replacement-process")
        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            return original if self.elapsed < 6 else replacement

        with patch.object(herdr, "prompt_recipient_process", side_effect=observe_recipient):
            outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("recipient changed", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_lost_prompt_is_not_retyped_after_a_deadline_inside_the_settle_window(self) -> None:
        self.drop_all = True
        outcome = self.submit(timeout_ms=9_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertLessEqual(self.elapsed, 9)
        self.assertEqual(self.enters, [])

    def test_remote_control_signal_still_requires_a_visible_empty_composer(self) -> None:
        self.ready_at = 60
        self.write_turn("/remote-control is active", kind="system")
        self.screen = "Restoring conversation"
        self.events.append((2, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        self.submit_on_type = True
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertGreaterEqual(self.typed[0][2], 2)

    def test_late_turn_during_the_final_recipient_check_prevents_retyping(self) -> None:
        self.drop_all = True
        recipient = herdr.PromptRecipientProcess(23456, "same-recipient")

        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            if self.elapsed >= 10:
                self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)
            return recipient

        with patch.object(herdr, "prompt_recipient_process", side_effect=observe_recipient):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_failure_carries_the_last_screen_without_ansi_and_only_its_last_forty_lines(self) -> None:
        self.drop_all = True
        failed_screen = "\n".join(f"\x1b[31mloading row {index}\x1b[0m" for index in range(45))
        self.events.append((1, lambda: setattr(self, "screen", failed_screen)))
        outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertEqual(failure.screen, "\n".join(f"loading row {index}" for index in range(5, 45)))
        self.assertIn("1", failure.reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(herdr.decode_prompt_outcome(herdr.encode_prompt_outcome(failure)), failure)

    def test_not_submitted_screen_round_trips_and_old_records_keep_the_not_read_state(self) -> None:
        failure = herdr.PromptNotSubmitted("lost after one typing", "────────\n❯ \n────────")
        encoded = herdr.encode_prompt_outcome(failure)
        self.assertEqual(json.loads(encoded), {"kind": "not submitted", "reason": failure.reason,
                                               "screen": failure.screen})
        self.assertEqual(herdr.decode_prompt_outcome(encoded), failure)
        legacy = herdr.PromptNotSubmitted("old failure")
        self.assertIsInstance(legacy.screen, herdr.PromptScreenNotRead)
        self.assertNotIn("screen", cast(dict[str, object], json.loads(herdr.encode_prompt_outcome(legacy))))
        self.assertEqual(herdr.decode_prompt_outcome('{"kind":"not submitted","reason":"old failure"}'), legacy)
        self.assertEqual(herdr.decode_prompt_outcome(herdr.encode_prompt_outcome(
            herdr.PromptNotSubmitted("empty visible screen", ""))),
            herdr.PromptNotSubmitted("empty visible screen", ""))

    def test_transcript_cursor_reads_only_appended_bytes_after_its_first_poll(self) -> None:
        self.write_turn("old context " * 1000, kind="assistant", seconds_after_launch=-1)
        first_size = self.path.stat().st_size
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        positions: list[int] = []
        original_open = Path.open

        @contextmanager
        def traced_open(path: Path, mode: str) -> Generator[BinaryIO]:
            self.assertEqual(mode, "rb")
            with original_open(path, mode) as raw:
                stream = cast(BinaryIO, raw)
                tracked = Mock(wraps=stream)

                def readline() -> bytes:
                    positions.append(stream.tell())
                    return stream.readline()

                cast(Mock, tracked.readline).side_effect = readline
                yield cast(BinaryIO, tracked)

        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        self.assertEqual(positions[0], 0)
        self.write_turn("resume the saved work")
        positions.clear()
        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(positions[0], first_size)
        self.assertTrue(all(position >= first_size for position in positions))
        positions.clear()
        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(positions, [self.path.stat().st_size])

    def test_transcript_readability_requires_every_matching_file_and_recovers(self) -> None:
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertEqual(cursor.read_turns(), [])
        self.assertIsInstance(cursor.readability, herdr.TranscriptReadable)
        self.write_turn("older context", kind="assistant", seconds_after_launch=-1)
        second = self.transcript.config_dir / "projects/other/moved-session.jsonl"
        second.parent.mkdir()
        _ = second.write_bytes(b"{}\n")
        with self.unreadable_transcript(0, 1) as failures:
            self.assertEqual(cursor.read_turns(), [])
            self.assertIsInstance(cursor.readability, herdr.TranscriptUnreadable)
        self.assertTrue(failures)
        self.assertIn(second, cursor.offsets)
        self.assertEqual(cursor.read_turns(), [])
        self.assertIsInstance(cursor.readability, herdr.TranscriptReadable)

    def test_transcript_cursor_waits_for_a_newline_and_a_complete_utf8_row(self) -> None:
        self.write_turn("resume the saved work – saved context")
        row = (json.dumps(json.loads(self.path.read_bytes()), ensure_ascii=False) + "\n").encode()
        split = row.index("–".encode()) + 1
        _ = self.path.write_bytes(row[:split])
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        with self.path.open("ab") as stream:
            _ = stream.write(row[split:-1])
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        with self.path.open("ab") as stream:
            _ = stream.write(b"\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(len(cursor.read_turns()), 1)

    def test_transcript_cursor_restarts_after_shrink_and_keeps_offsets_per_project(self) -> None:
        self.write_turn("historical context " * 100, kind="assistant", seconds_after_launch=-1)
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        _ = self.path.write_bytes(b"")
        self.write_turn("resume the saved work")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        # A replacement file cannot retain submit proof from rows it no longer contains.
        _ = self.path.write_bytes(b"{}\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        second = self.transcript.config_dir / "projects/other/moved-session.jsonl"
        second.parent.mkdir()
        self.write_turn("resume the saved work")
        _ = second.write_bytes(self.path.read_bytes())
        _ = self.path.write_bytes(b"{}\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)

    def test_empty_and_quoted_composers_share_the_readiness_parser(self) -> None:
        cases = (
            ("─── named composer ─\n❯ \xa0\n────────\n  footer", True, False),
            ("─── named composer ─\n❯ resume the saved work\n  ---\n  > quoted line\n────────\n  footer", False, True),
            ("────────\n  > resume the saved work\n────────", False, False),
            ("────────\n❯ resume the saved work\n────────\nAssistant output", False, False),
            # A new session's faint hint is not input, but coloured or bold typed text is.
            ("────────\r\n❯\xa0\x1b[0m\x1b[2mTry \"fix lint errors\"\x1b[0m\r\n────────\r\n  footer", True, False),
            ("────────\n❯ \x1b[38;2;200;100;50mresume the saved work\x1b[0m\n────────", False, True),
            ("────────\n❯ \x1b[1;2mhint\x1b[22mresume the saved work\x1b[0m\n────────", False, True))
        for screen, empty, contains in cases:
            with self.subTest(screen=screen):
                self.screen = screen
                self.assertEqual(self.client.prompt_input_empty("destination:p1"), empty)
                self.assertEqual(self.client.prompt_in_input("destination:p1", "resume the saved work"), contains)
                state = self.client.prompt_input_state("destination:p1", self.transcript, self.launched_at)
                self.assertEqual(isinstance(state, herdr.PromptInputReady), empty)

    def test_a_replacement_process_cannot_inherit_the_turn_written_during_submission(self) -> None:
        self.submit_on_type = True
        observations = 0

        def recipient(_session: str) -> herdr.PromptRecipientProcess:
            nonlocal observations
            observations += 1
            return (herdr.PromptRecipientProcess(23456, "first-recipient") if observations == 1
                    else herdr.PromptRecipientProcess(34567, "replacement-process"))

        with patch.object(herdr, "prompt_recipient_process", side_effect=recipient):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_prompt_is_typed_only_after_the_destination_becomes_ready_ten_seconds_later(self) -> None:
        self.ready_at = 10
        self.submit_on_type = True
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.typed[0][2], 10)
        self.assertEqual(self.enters, [])

    def test_meta_peer_turn_and_old_user_turn_cannot_prove_the_opening_prompt(self) -> None:
        self.write_turn("resume the saved work", meta=True)
        self.write_turn("resume the saved work", seconds_after_launch=-1)
        self.write_turn("resume the saved work", kind="assistant")
        self.write_turn("another task")
        self.drop_all = True
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.enters, [])

    def test_only_a_matching_new_user_turn_in_the_destination_transcript_proves_submit(self) -> None:
        sibling = self.path.with_name("sibling.jsonl")
        self.write_turn("resume the saved work", blocks=True)
        _ = self.path.rename(sibling)
        self.assertIsInstance(herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at),
                              herdr.PromptNotSubmitted)
        self.write_turn("context before\nresume the saved work\ncontext after", blocks=True)
        proof = herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at)
        self.assertEqual(proof, herdr.PromptSubmitted(self.launched_at + timedelta(seconds=1)))

    def test_a_ready_sibling_tab_never_makes_the_destination_ready(self) -> None:
        self.ready_at = 60
        outcome = self.submit(timeout_ms=1_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(set(self.ready_panes), {"destination:p1"})
        self.assertEqual((self.typed, self.enters), ([], []))
        self.assertLessEqual(self.elapsed, 1)

    def test_prompt_only_in_scrollback_is_never_flushed_with_enter(self) -> None:
        self.drop_all = True
        self.screen = "❯ resume the saved work\nPrevious response\n────────\n❯ \n────────"
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertTrue(1 <= len(self.typed) <= 3)
        self.assertEqual(self.enters, [])

    def test_an_old_bordered_prompt_above_a_new_unbordered_input_is_scrollback(self) -> None:
        self.screen = "────────\n❯ resume the saved work\n────────\nPrevious response\n❯ "
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_old_bordered_prompt_above_assistant_output_without_a_new_marker_gets_no_enter(self) -> None:
        self.screen = "────────\n❯ resume the saved work\n────────\nAssistant response is streaming"
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_assistant_output_with_a_footer_prefix_does_not_make_scrollback_live_input(self) -> None:
        self.screen = ("────────\n❯ resume the saved work\n────────\n"
                       "ctrl+c cancels the operation described above")
        self.assertFalse(self.client.prompt_in_input("destination:p1", "resume the saved work"))
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_bottom_composer_with_complete_footer_rows_still_gets_its_enter_retry(self) -> None:
        self.typed_screen = ("Assistant response above the composer\n────────\n❯ resume the saved work\n────────\n"
                       "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
                       "  ? for shortcuts · ctrl+c to interrupt\n")
        self.submit_on_enter = 1
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])

    def test_prompt_wrapped_inside_a_word_gets_one_enter_then_submission(self) -> None:
        self.typed_screen = "────────\n❯ resume the sav\ned work\n────────"
        self.submit_on_enter = 1
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", "resume the saved work")])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])

    @staticmethod
    def real_composer_screen(top_rule: str, *, marker_gap: str = " ", status_suffix: str = "",
                             mode: str = "⏵⏵ auto mode on · 1 shell",
                             input_text: str = "resume the saved work") -> str:
        # Synthetic screen modelled on live Claude panes; no live pane is read.
        return ("● Finished the previous task\n"
                "✻ Worked for 2m 10s\n"
                f"{top_rule}\n"
                f"❯{marker_gap}{input_text}\n"
                "────────────────────────────────────────────────────────────────\n"
                "  unit director for natedev | claude2 | claude-build-followups-two-accounts | "
                f"128,047 | Opus 5.5 high{status_suffix}\n"
                "  5-hour 100% left until 00:59 EDT | weekly 40% left until Thu 06:59 EDT\n"
                f"  {mode}\n"
                "\n"
                "  ● main\n"
                "  ◯ general-purpose  Check the regression   2m 10s · ↓ 127.6k tokens\n")

    def assert_real_composer_retries_once(self, text: str = "resume the saved work") -> None:
        self.submit_on_enter = 1
        outcome = self.submit(text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", text)])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        self.assertGreaterEqual(cast(Mock, self.read).call_count, 2)

    def test_named_composer_with_status_mode_and_agent_footer_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─")
        self.assert_real_composer_retries_once()

    def test_unnamed_composer_with_status_and_auto_cycle_footer_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("────────────────────────────────────────",
                                               mode="⏵⏵ auto mode on (shift+tab to cycle)")
        self.assert_real_composer_retries_once()

    def test_named_composer_with_remote_control_status_suffix_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── style ─", status_suffix="        /rc")
        self.assert_real_composer_retries_once()

    def test_named_composer_with_no_break_space_after_marker_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── ups ─", marker_gap="\xa0")
        self.assert_real_composer_retries_once()

    def test_multiline_prompt_with_rule_like_rows_gets_one_enter_retry(self) -> None:
        prompt = "resume the saved work\n--- context ---\n──── saved context ─\ncontinue here"
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─", input_text=prompt)
        self.assert_real_composer_retries_once(prompt)

    def test_indented_quote_after_rule_inside_live_prompt_gets_one_enter_retry(self) -> None:
        prompt = "resume the saved work\n---\n> quoted line\ncontinue here"
        visible_input = "resume the saved work\n  ---\n  > quoted line\n  continue here"
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─",
                                               input_text=visible_input)
        self.assert_real_composer_retries_once(prompt)

    def test_old_quoted_prompt_above_empty_live_composer_gets_no_enter(self) -> None:
        prompt = "resume the saved work\n---\n> quoted line\ncontinue here"
        old_input = "resume the saved work\n  ---\n  > quoted line\n  continue here"
        self.drop_all = True
        self.screen = ("─── old session ─\n❯ " + old_input + "\n────────\n● Finished that task\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(text=prompt), herdr.PromptNotSubmitted)
        self.assertTrue(self.typed)
        self.assertTrue(all((pane, text) == ("destination:p1", prompt) for pane, text, _at in self.typed))
        self.assertEqual(self.enters, [])

    def test_named_composer_with_rule_character_in_label_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("─── two─accounts ─")
        self.assert_real_composer_retries_once()

    def test_old_rule_containing_prompt_above_empty_live_composer_gets_no_enter(self) -> None:
        prompt = "resume the saved work\n--- context ---\n──── saved context ─\ncontinue here"
        self.drop_all = True
        self.screen = ("─── old session ─\n❯ " + prompt + "\n────────\n● Finished that task\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(text=prompt), herdr.PromptNotSubmitted)
        self.assertTrue(self.typed)
        self.assertTrue(all((pane, text) == ("destination:p1", prompt) for pane, text, _at in self.typed))
        self.assertEqual(self.enters, [])

    def test_named_composer_does_not_retry_prompt_visible_only_above_its_empty_input(self) -> None:
        self.drop_all = True
        self.screen = ("❯ resume the saved work\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertTrue(1 <= len(self.typed) <= 3)
        self.assertEqual(self.enters, [])

    def test_named_bordered_prompt_above_column_zero_output_gets_no_enter(self) -> None:
        self.screen = (self.real_composer_screen("──────────────────────── two-accounts ─")
                       + "ctrl+c cancels the operation described above\n")
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_real_composer_retry_still_rechecks_its_recipient_before_enter(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─")
        with patch.object(herdr, "prompt_recipient_process", side_effect=[
                herdr.PromptRecipientProcess(23456, "first-recipient"),
                herdr.PromptRecipientProcess(34567, "replacement-process")]):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("recipient changed", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_an_unsubmitted_input_gets_at_most_three_enter_retries(self) -> None:
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))] * 3)
        self.assertLessEqual(self.elapsed, 30)

    def test_destination_transcript_ready_signal_allows_input_while_process_observation_is_loading(self) -> None:
        self.ready_at = 60
        self.write_turn("/remote-control is active", kind="system")
        self.submit_on_type = True
        self.assertIsInstance(self.submit(timeout_ms=1_000), herdr.PromptSubmitted)
        self.assertEqual(self.typed[0][2], 0)

    def test_promptless_codex_restart_needs_no_transcript_or_submit(self) -> None:
        outcome = self.client.start_agent("codex-worker", "destination:p1", "codex", [],
                                          herdr.NoOpeningPrompt(), 1_000)
        self.assertIsInstance(outcome, herdr.PromptSubmissionNotRequired)
        self.assertEqual((self.typed, self.enters, self.ready_panes), ([], [], []))


class HerdrTests(unittest.TestCase):
    root: Path = Path()
    fake: Path = Path()
    log: Path = Path()
    responses_path: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}
    proc: Path = Path()
    process_pane_replies: dict[str, str] = {}
    @property
    def herdr(self) -> herdr.Herdr:
        return herdr.Herdr("fixture")

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "linux")))
        # Test limits stay as written on a loaded machine.
        _ = self.enterContext(patch.object(herdr, "load_factor", return_value=1.0))
        # Server start mechanics do not depend on the name. The one-session rule has its own tests.
        _ = self.enterContext(patch.object(herdr, "_require_shared"))
        # These fixtures specify process startup and argv; PromptSubmissionTests
        # exercises the real transcript/readiness path without this substitute.
        def submitted(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                      _launched_at: datetime, _timeout_ms: int) -> herdr.PromptSubmitOutcome:
            client.prompt(pane, opening.text)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))
        _ = self.enterContext(patch.object(herdr.Herdr, "submit_opening_prompt", submitted))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = self.root / "fake-herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = self.fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        self.fake.chmod(0o755)
        self.log = self.root / "log"
        self.responses_path = self.root / "responses.json"
        self.responses = {"default": {"stdout": "", "code": 0}}
        self.process_pane_replies = {}
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(self.fake), "SYSTEMD_RUN": str(self.fake), "SYSTEMCTL": str(self.fake),
            "HERDR_PROC": str(self.proc), "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses_path), "PATH": str(self.root / "no-tools"),
            "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
        }, clear=True)))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.configure([], "")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": []}}))

    def test_unmarked_child_cannot_inherit_its_directors_staff_identity(self) -> None:
        with patch.dict(os.environ, {"STAFF_ID": "s123456789ab", "SHOWRUNNER_UNIT": "old",
                                     "SHOWRUNNER_UNIT_ID": "alpha-unit", "KEPT_SETTING": "kept"}):
            environment = herdr.unmarked_environment()
        self.assertNotIn("STAFF_ID", environment)
        self.assertNotIn("SHOWRUNNER_UNIT", environment)
        self.assertNotIn("SHOWRUNNER_UNIT_ID", environment)
        self.assertEqual(environment["KEPT_SETTING"], "kept")

    def configure(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {"stdout": stdout, "code": code, "stderr": stderr}
        _ = self.responses_path.write_text(json.dumps(self.responses))

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def server_creation(self, session: str = "fixture", *, scopes: bool = True) -> list[str]:
        """The transient server unit's systemd-run argv; with pane scopes, the server's SHELL is the wrapper."""
        shell = [f"--setenv=SHELL={self.root}/home/.local/state/herdr/pane-shell",
                 "--setenv=HERDR_PANE_SHELL=/bin/sh"] if scopes else []
        return ["--user", f"--unit=herdr-{session}.service", "--collect", "-p", "CPUWeight=1000", "-p", "MemoryLow=1G",
                "-p", "MemorySwapMax=0", *shell, str(self.fake), "--session", session, "server"]

    def start_commands(self) -> list[list[str]]:
        """Startup commands, excluding read-only lifecycle observations."""
        return [command for command in self.commands()
                if command[2:4] not in (["agent", "get"], ["pane", "process-info"])]

    def configure_herdr(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.configure(["--session", "fixture", *arguments], stdout, code, stderr)

    def configure_tab_start(self, pane: str, label: str, session_id: str) -> list[str]:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": pane, "label": label}]}}))
        return ["agent", "start", label, "--kind", "claude", "--pane", pane,
                "--", "--session-id", session_id]

    def process(self, pid: int, parent: int, arguments: list[str], environment: dict[str, str]) -> Path:
        process = self.proc / str(pid)
        process.mkdir()
        _ = process.joinpath("stat").write_text(f"{pid} (fixture with ) parenthesis) S {parent} 0 0\n")
        _ = process.joinpath("cmdline").write_bytes("\0".join([*arguments, ""]).encode())
        _ = process.joinpath("environ").write_bytes("\0".join([*(f"{key}={value}" for key, value in environment.items()), ""]).encode())
        process.joinpath("cwd").symlink_to(self.root)
        for session in list(self.process_pane_replies):
            self.configure_process_panes(session)
        return process

    def configure_process_panes(self, session: str) -> None:
        """Give unchanged fake processes corresponding live panes, unless a test sets its own reply."""
        key = f"--session {session} pane list"
        response = self.responses.get(key)
        if response is not None and response != {"stdout": self.process_pane_replies.get(session), "code": 0, "stderr": ""}:
            return
        ids = {entry.split("=", 1)[1] for process in self.proc.iterdir() if process.name.isdecimal()
               for entry in (process / "environ").read_text().split("\0") if entry.startswith("HERDR_PANE_ID=")}
        text = json.dumps({"result": {"panes": [
            {"pane_id": pane, "workspace_id": pane.split(":")[0]} for pane in sorted(ids)]}})
        self.process_pane_replies[session] = text
        self.configure(["--session", session, "pane", "list"], text)

    def unix_socket(self, path: Path, inode: int, *, listening: bool = True) -> None:
        table = self.proc / "net" / "unix"
        table.parent.mkdir(exist_ok=True)
        flags = "00010000" if listening else "00000000"
        if not table.exists():
            _ = table.write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        with table.open("a") as output:
            _ = output.write(f"0000000000000000: 00000002 00000000 {flags} 0001 01 {inode} {path}\n")
        if listening:
            session = "default" if path.parent.name == "herdr" else path.parent.name
            self.configure_process_panes(session)

    def socket_fd(self, process: Path, inode: int, *, fd: int = 3) -> None:
        directory = process / "fd"
        directory.mkdir(exist_ok=True)
        directory.joinpath(str(fd)).symlink_to(f"socket:[{inode}]")

    def darwin(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "darwin")))
        _ = cast(object, self.enterContext(patch.object(sys, "platform", "darwin")))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_PS": str(self.fake), "HERDR_LSOF": str(self.fake), "LAUNCHCTL": str(self.fake),
            "SHUTDOWN_OPEN": str(self.fake),
        })))

        self.configure(["managername"], "Aqua\n")

    def darwin_environment(self, pid: int, command: str, environment: str) -> None:
        self.configure(["-o", "command=", "-p", str(pid)], command + "\n")
        self.configure(["eww", "-o", "command=", "-p", str(pid)], command + " " + environment + "\n")

    def darwin_sockets(self, text: str) -> None:
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"], text)

    def darwin_process_table(self, text: str) -> None:
        executables: list[str] = []
        commands: list[str] = []
        for line in text.splitlines():
            pid, parent, uid, command = line.split(maxsplit=3)
            executables.append(f"{pid} {parent} {uid} {command.split()[0]}")
            commands.append(f"{pid} {command}")
        self.configure(["-U", str(os.getuid()), "-o", "pid=,ppid=,uid=,comm="], "\n".join(executables))
        self.configure(["-U", str(os.getuid()), "-o", "pid=,command="], "\n".join(commands))

    def run_fake(self, argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        """Replay the command fake in process while Popen models the server child."""
        self.assertGreater(timeout, 0)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = fake_herdr.main(argv[1:])
        return subprocess.CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())

    def detached_server(self) -> tuple[Mock, Mock]:
        self.darwin()
        _ = self.enterContext(patch.object(herdr, "security_session", return_value=herdr.DesktopSession()))
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.run_fake))
        poll = Mock(return_value=None)
        child = SimpleNamespace(pid=42, poll=poll, wait=Mock(return_value=0))
        _ = self.enterContext(patch.object(os, "killpg"))
        self.darwin_sockets(f"p42\nf3\nn{self.root}/config/herdr/sessions/fixture/herdr.sock\n")
        spawn = self.enterContext(patch.object(subprocess, "Popen", return_value=child))
        return spawn, poll

    def test_security_session_distinguishes_aqua_background_and_failed_queries(self) -> None:
        self.darwin()
        for answer, code, expected in (("Aqua\n", 0, herdr.DesktopSession),
                                       ("Background\n", 0, herdr.NonDesktopSession),
                                       ("", 1, herdr.SessionKindUnreadable)):
            with self.subTest(answer=answer, code=code):
                self.configure(["managername"], answer, code, "manager unavailable" if code else "")
                self.assertIsInstance(herdr.security_session(), expected)
        self.assertEqual(self.commands(), [["managername"]] * 3)

    def test_direct_mac_server_start_never_detaches_from_a_background_session(self) -> None:
        import detached
        self.darwin()
        self.configure(["managername"], "Background\n")
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with patch.object(detached, "start_detached") as spawn:
            with self.assertRaisesRegex(herdr.MacDesktopTerminalRequired, "herdr must start from the Mac desktop: run this from a Mac terminal"):
                _ = herdr.start_server("fixture", 1000)
            spawn.assert_not_called()
        self.assertEqual(self.commands().count(["managername"]), 1)

    def test_non_desktop_restore_opens_attach_before_waiting_for_its_listener(self) -> None:
        self.darwin()
        status = "--session fixture status server"
        sockets = f"-n -P -a -U -u {os.getuid()} -F pfn"
        self.responses[status] = [{"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: stopped\n", "code": 0},
                                  {"stdout": "status: running\n", "code": 0}]
        self.responses[sockets] = [{"stdout": "", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        clock = StartupClock()
        def advance(seconds: float) -> None:
            clock.elapsed += seconds
        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=advance)):
            owned = herdr.restore_server("fixture", 2000, session_kind=herdr.NonDesktopSession())
        self.assertEqual(owned, herdr.WindowServerCreated())
        calls = self.commands()
        opener = ["-na", "/Applications/Nix Apps/Ghostty.app", "--args", "-e", str(self.fake),
                  "session", "attach", "fixture"]
        self.assertEqual(calls[2], opener)
        self.assertEqual(calls[3:5], [["--session", "fixture", "status", "server"]] * 2)
        self.assertFalse(any(row[2:] == ["server"] or row[2:4] == ["workspace", "close"] for row in calls))

    def test_window_bootstrap_closes_restored_workspaces_after_answering_before_returning(self) -> None:
        self.darwin()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        self.darwin_sockets("")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1"}, {"workspace_id": "w2"}]}}))
        with patch.object(herdr, "run_program", side_effect=self.run_fake):
            ownership = herdr.restore_server("fixture", 2000, session_kind=herdr.NonDesktopSession())
        self.assertIsInstance(ownership, herdr.WindowServerCreated)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            ["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
            ["-na", "/Applications/Nix Apps/Ghostty.app", "--args", "-e", str(self.fake),
             "session", "attach", "fixture"],
            ["--session", "fixture", "status", "server"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "workspace", "close", "w1"],
            ["--session", "fixture", "workspace", "close", "w2"]])

    def test_non_desktop_window_failure_or_unanswered_server_names_locked_or_signed_out_desktop(self) -> None:
        self.darwin()
        self.configure_herdr(["status", "server"], "status: stopped\n")
        execute = herdr.run_program
        def clocked_command(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 0.2)
            # The simulated deadline bounds library work, not OS scheduling of the fake.
            return execute(argv, timeout=TEST_HANG_GUARD_SECS)
        opener = ["-na", "/Applications/Nix Apps/Ghostty.app", "--args", "-e", str(self.fake),
                  "session", "attach", "fixture"]
        for failure in (True, False):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                self.darwin_sockets("")
                self.configure(opener, code=1 if failure else 0)
                clock = StartupClock()
                def advance(seconds: float) -> None:
                    clock.elapsed += seconds
                with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=advance)),
                      patch.object(herdr, "run_program", side_effect=clocked_command),
                      self.assertRaisesRegex(herdr.MacDesktopUnavailable, "the Mac desktop is locked or signed out: unlock it and restart")):
                    _ = herdr.restore_server("fixture", 200, session_kind=herdr.SessionKindUnreadable("no manager"))
                calls = self.commands()
                self.assertIn(opener, calls)
                self.assertFalse(any("--collect" in row or row[2:] == ["server"] for row in calls))
                self.assertLessEqual(clock.elapsed, 0.2)

    def test_non_desktop_restore_adopts_an_answering_server_and_refuses_a_listener_that_has_not_answered(self) -> None:
        self.darwin()
        # The result is independent of scheduling a Python command fake under load.
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.run_fake))
        self.darwin_sockets(f"p77\nf3\nn{self.root}/config/herdr/sessions/fixture/herdr.sock\n")
        self.configure_herdr(["status", "server"], "status: running\n")
        owned = herdr.restore_server("fixture", 1000, session_kind=herdr.NonDesktopSession())
        self.assertIsInstance(owned, herdr.ServerAdopted)
        self.log.unlink(missing_ok=True)
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaises(herdr.SessionTaken):
            _ = herdr.restore_server("fixture", 1000, session_kind=herdr.NonDesktopSession())
        self.assertFalse(any("-na" in row or row[2:4] == ["server", "stop"] for row in self.commands()))

    def test_non_desktop_listener_probe_timeout_preserves_the_session_without_blaming_the_desktop(self) -> None:
        self.darwin()
        self.configure_herdr(["status", "server"], "status: stopped\n")
        for failed_probe in ("status", "lsof"):
            with self.subTest(failed_probe=failed_probe):
                self.log.unlink(missing_ok=True)
                clock = StartupClock()
                def probe(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                    self.assertGreater(timeout, 0)
                    if argv[1] == ("--session" if failed_probe == "status" else "-n"):
                        clock.elapsed += timeout
                        raise subprocess.TimeoutExpired(argv, timeout)
                    clock.elapsed += 0.95
                    return self.run_fake(argv, timeout)
                with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)),
                      patch.object(herdr, "run_program", side_effect=probe),
                      self.assertRaises(herdr.HerdrFailure) as failed):
                    _ = herdr.restore_server("fixture", 1000, session_kind=herdr.NonDesktopSession())
                self.assertNotIsInstance(failed.exception, herdr.MacDesktopUnavailable)
                self.assertIn("fixture: could not check existing listener", str(failed.exception))
                self.assertIsInstance(failed.exception.__cause__, subprocess.TimeoutExpired)
                self.assertFalse(any("-na" in row or row[2:4] == ["server", "stop"] for row in self.commands()))

    def test_darwin_detaches_server_and_clears_restored_workspaces_after_it_answers(self) -> None:
        spawn, _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1"}, {"workspace_id": "w2"}]}}))
        self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerCreated)
        spawn.assert_called_once_with([str(self.fake), "--session", "fixture", "server"],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, env=herdr.unmarked_environment())
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            ["--session", "fixture", "status", "server"],
            ["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "workspace", "close", "w1"],
            ["--session", "fixture", "workspace", "close", "w2"]])

    def test_darwin_server_start_does_not_inherit_the_heal_proof(self) -> None:
        spawn, _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with patch.dict(os.environ, handoff):
            self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerCreated)
            spawn.assert_called_once()
            environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
            self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, environment)
            self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, environment)
            self.assertEqual(environment["IN_NIX_SHELL"], "impure")
            self.assertEqual(environment["NIX_LDFLAGS"], "-Lprepared")
            self.assertEqual(os.environ[herdr.nix_env.HEAL_SESSION_ENV], "healed-session")
            self.assertEqual(os.environ[herdr.nix_env.HEAL_DIRECTORY_ENV], str(self.root))

    def test_a_pane_of_the_heal_started_server_can_launch_another_session(self) -> None:
        spawn, _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        envrc = self.root / ".envrc"
        _ = envrc.write_text("use flake\n")
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with (patch.dict(os.environ, handoff),
              patch.object(herdr.nix_env, "ensure", side_effect=AssertionError("the heal proof must avoid another load")) as ensure,
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            _ = herdr.start_server("fixture", 90_000)
            restored = herdr.launch_agent("fixture", "space", "healed", str(self.root), {}, "claude",
                                          ["--resume", "healed-session"], herdr.NoOpeningPrompt(), 90_000, agent="healed")
            self.assertIsInstance(restored, herdr.AgentLaunched)
            ensure.assert_not_called()
            # A pane inherits the server environment, with its launch overlay on top.
            server_environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
            pane_overlay = cast(dict[str, str], opened.call_args.args[4])
            pane_environment = {**server_environment, **pane_overlay}
            with (patch.dict(os.environ, pane_environment, clear=True),
                  patch.object(herdr.nix_env, "ensure", return_value=herdr.nix_env.Loaded(pane_environment, envrc)) as load):
                result = herdr.launch_agent("fixture", "space", "other", str(self.root), {}, "claude",
                                           ["--resume", "another-session"], herdr.NoOpeningPrompt(), 90_000, agent="other")
                self.assertIsInstance(result, herdr.AgentLaunched)
                load.assert_called_once()
                self.assertEqual(cast(Path, load.call_args.args[0]), self.root)
            self.assertEqual(started.call_count, 2)
            second_overlay = cast(dict[str, str], opened.call_args.args[4])
            self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, second_overlay)
            self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, second_overlay)
            self.assertEqual(second_overlay["NIX_LDFLAGS"], "-Lprepared")

    def test_child_commands_drop_heal_proof_from_inherited_and_explicit_environments(self) -> None:
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root), "IN_NIX_SHELL": "impure"}
        with (patch.dict(os.environ, handoff),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["probe"], 0, "", "")) as run):
            for environment in (None, handoff):
                with self.subTest(environment=environment):
                    _ = herdr.run_program(["probe"], environment=environment)
                    child_environment = cast(dict[str, str], run.call_args.kwargs["env"])
                    self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, child_environment)
                    self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, child_environment)
                    self.assertEqual(child_environment["IN_NIX_SHELL"], "impure")
            self.assertEqual(handoff[herdr.nix_env.HEAL_SESSION_ENV], "healed-session")

    def test_agent_start_cli_does_not_inherit_the_heal_proof(self) -> None:
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root), "IN_NIX_SHELL": "impure"}
        process = WaitingAgentCLI(StartupClock(), 0, lambda: None)
        with (patch.dict(os.environ, handoff),
              patch.object(herdr.Herdr, "startup_processes", side_effect=herdr.HerdrFailure("fixture has no shell")),
              patch.object(subprocess, "Popen", return_value=process) as spawn):
            result = self.herdr.start_command(["agent-start"], "claude", "pane", 1.0)
        self.assertEqual(result.returncode, 0)
        spawn.assert_called_once()
        environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, environment)
        self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, environment)
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")

    def test_linux_client_discovery_in_an_isolated_copy_does_not_need_darwin_launcher(self) -> None:
        _ = self.process(42, 1, ["herdr", "session", "attach", "fixture"], {})
        isolated = self.root / "isolated/scripts/herdr"
        isolated.mkdir(parents=True)
        _ = shutil.copy2(Path(herdr.__file__), isolated / "herdr.py")
        library = isolated.parent / "lib"
        library.mkdir()
        _ = shutil.copy2(Path(herdr.__file__).resolve().parents[1] / "lib/nix_env.py", library / "nix_env.py")
        probe = (
            "import sys; sys.path.insert(0,sys.argv[1]); import herdr; "
            "herdr._PLATFORM='linux'; print(herdr.attached_clients('fixture'))"
        )
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(isolated)],
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ClientsAttached(count=1)\n")
        self.assertEqual(result.stderr, "")

    def test_darwin_running_server_is_adopted_without_spawning_or_clearing(self) -> None:
        spawn, _ = self.detached_server()
        self.configure_herdr(["status", "server"], "status: running\n")
        self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerAdopted)
        spawn.assert_not_called()
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_darwin_exited_child_fails_immediately_with_plain_message_and_existing_log(self) -> None:
        _, poll = self.detached_server()
        poll.return_value = 1
        self.configure_herdr(["status", "server"], "status: stopped\n")
        log = self.root / "config/herdr/sessions/fixture/herdr-server.log"
        log.parent.mkdir(parents=True)
        _ = log.write_text("server could not bind its socket\n")
        with patch.object(time, "sleep") as pause:
            with self.assertRaises(herdr.HerdrFailure) as raised:
                _ = herdr.start_server("fixture", 90_000)
            self.assertEqual(str(raised.exception), f"herdr server for fixture did not start; see {log}")
            pause.assert_not_called()
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_darwin_never_clears_a_server_answering_after_our_child_exited(self) -> None:
        _, poll = self.detached_server()
        poll.side_effect = [None, 1]
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr server for fixture did not start"):
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]] * 2 + [
            ["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"]])

    def test_darwin_unanswering_child_has_ten_second_limit_with_shorter_caller_bound(self) -> None:
        _ = self.detached_server()
        self.configure_herdr(["status", "server"], "status: stopped\n")
        for timeout_ms, seconds in ((90_000, 10.0), (250, 0.25)):
            clock = StartupClock()

            def advance(seconds: float) -> None:
                clock.elapsed += seconds

            with (self.subTest(timeout_ms=timeout_ms),
                  patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
                  patch.object(time, "sleep", side_effect=advance),
                  self.assertRaises(herdr.HerdrFailure) as raised):
                _ = herdr.start_server("fixture", timeout_ms)
            self.assertEqual(str(raised.exception), "herdr server for fixture did not start")
            self.assertAlmostEqual(clock.elapsed, seconds)

    def test_darwin_competing_listener_is_adopted_while_our_child_is_alive_or_after_it_exits(self) -> None:
        _, poll = self.detached_server()
        socket = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.darwin_sockets(f"p77\nf3\nn{socket}\np42\nf4\nn{socket.parent}/other.sock\n")
        for exited in (False, True):
            with self.subTest(exited=exited), patch.object(os, "kill") as stop:
                self.log.unlink(missing_ok=True)
                if exited:
                    poll.side_effect = [None, 1]
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerAdopted)
                if not exited:
                    stop.assert_called_once_with(42, signal.SIGTERM)
                self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))

    def test_darwin_status_answer_without_our_socket_never_clears_workspaces(self) -> None:
        _ = self.detached_server()
        self.darwin_sockets("")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        clock = StartupClock()

        def advance(seconds: float) -> None:
            clock.elapsed += seconds

        with (patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
              patch.object(time, "sleep", side_effect=advance),
              self.assertRaisesRegex(herdr.HerdrFailure, "did not start")):
            _ = herdr.start_server("fixture", 250)
        self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))

    def test_darwin_unreadable_startup_listener_fails_plainly_without_clearing(self) -> None:
        _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
                       code=1, stderr="inspection denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr server for fixture did not start"):
            _ = herdr.start_server("fixture", 90_000)
        self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))

    def test_darwin_status_probe_timeout_is_bounded_and_plain(self) -> None:
        spawn, _ = self.detached_server()
        clock = StartupClock()

        def timed_out(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv, [str(self.fake), "--session", "fixture", "status", "server"])
            self.assertLessEqual(timeout, 10.0)
            clock.elapsed += timeout
            raise subprocess.TimeoutExpired(argv, timeout)

        with (patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
              patch.object(herdr, "run_program", side_effect=timed_out),
              self.assertRaisesRegex(herdr.HerdrFailure, "herdr server for fixture did not start")):
            _ = herdr.start_server("fixture", 90_000)
        spawn.assert_not_called()
        self.assertEqual(clock.elapsed, 10.0)

    def test_darwin_spawn_failure_has_plain_start_message(self) -> None:
        spawn, _ = self.detached_server()
        spawn.side_effect = OSError("cannot execute")
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaises(herdr.HerdrFailure) as raised:
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(str(raised.exception), "herdr server for fixture did not start")

    def test_darwin_readiness_probes_share_the_startup_deadline(self) -> None:
        _ = self.detached_server()
        clock = StartupClock()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        budgets: list[float] = []

        def slow_probe(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            budgets.append(timeout)
            if argv[1] == "-n":
                clock.elapsed += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            clock.elapsed += 2
            return self.run_fake(argv, timeout)

        with (patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
              patch.object(herdr, "run_program", side_effect=slow_probe),
              self.assertRaisesRegex(herdr.HerdrFailure, "did not start")):
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(budgets, [10, 8, 6])
        self.assertEqual(clock.elapsed, 10)

    def test_darwin_stop_runs_only_named_server_stop_and_reports_refusal(self) -> None:
        self.darwin()
        herdr.stop_session("foo.service")
        self.configure(["--session", "foo.service", "server", "stop"], code=1, stderr="stop refused")
        with self.assertRaisesRegex(herdr.HerdrFailure, "stop refused"):
            herdr.stop_session("foo.service")
        self.assertEqual(self.commands(), [["--session", "foo.service", "server", "stop"]] * 2)

    def test_darwin_identity_preserves_registry_start_time_and_pins_locale_and_zone(self) -> None:
        self.darwin()
        started = "Thu Oct  1 12:00:00 2026"
        self.configure(["-o", "lstart=,tty=", "-p", "42"], f"  {started} ttys001\n")
        with patch.object(herdr, "run_program", wraps=herdr.run_program) as probe:
            identity = herdr.darwin_process_identity(42)
        self.assertEqual(identity.started, started)
        self.assertEqual(identity.terminal, 1)
        self.assertEqual(tuple(identity), (started, 1))
        self.assertEqual(probe.call_args.args[0], [str(self.fake), "-o", "lstart=,tty=", "-p", "42"])
        self.assertEqual(probe.call_args.kwargs["timeout"], 5)
        environment = cast(dict[str, str], probe.call_args.kwargs["environment"])
        self.assertEqual(environment, {**os.environ, "LC_ALL": "C", "TZ": "UTC"})

    def test_darwin_identity_distinguishes_no_terminal_and_refuses_missing_or_malformed_process(self) -> None:
        self.darwin()
        for terminal in ("??", "?", "-"):
            self.configure(["-o", "lstart=,tty=", "-p", "42"], f"Thu Oct 15 12:00:00 2026 {terminal}\n")
            self.assertEqual(herdr.darwin_process_identity(42).terminal, 0)
        for output in ("", "Thu Oct 15 12:00:00 2026", "malformed ttys001",
                       "Thu Oct 15 12:00:00 2026 ttys001\nThu Oct 15 12:00:00 2026 ??"):
            self.configure(["-o", "lstart=,tty=", "-p", "42"], output)
            with self.subTest(output=output), self.assertRaisesRegex(herdr.HerdrFailure, "process 42"):
                _ = herdr.darwin_process_identity(42)
        self.configure(["-o", "lstart=,tty=", "-p", "42"], code=1, stderr="process vanished")
        with self.assertRaisesRegex(herdr.HerdrFailure, "process vanished"):
            _ = herdr.darwin_process_identity(42)

    def test_darwin_open_files_preserves_absolute_paths_and_distinguishes_errors_from_no_matches(self) -> None:
        self.darwin()
        rollout = self.root / "account with spaces/sessions/rollout.jsonl"
        self.configure(["-a", "-p", "42", "-Fn"], f"p42\nf8\nn{rollout}\nf9\nnrelative\nf10\nn/dev/ttys001\n")
        self.assertEqual(herdr.darwin_open_files(42), [rollout, Path("/dev/ttys001")])
        self.assertEqual(self.commands(), [["-a", "-p", "42", "-Fn"]])
        self.configure(["-a", "-p", "42", "-Fn"], code=1)
        self.assertEqual(herdr.darwin_open_files(42), [])
        self.configure(["-a", "-p", "42", "-Fn"], code=1, stderr="permission denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "process 42 open files.*permission denied"):
            _ = herdr.darwin_open_files(42)

    def test_darwin_environment_uses_only_the_suffix_after_unchanged_arguments(self) -> None:
        self.darwin()
        command = 'claude --prompt "HERDR_PANE_ID=forged CLAUDE_CONFIG_DIR=/wrong"'
        self.darwin_environment(42, command, "HERDR_PANE_ID=w1:p1 CLAUDE_CONFIG_DIR=/real account VALUE=a=b")
        self.assertEqual(herdr.darwin_process_environment(42), {
            "HERDR_PANE_ID": "w1:p1", "CLAUDE_CONFIG_DIR": "/real account", "VALUE": "a=b"})
        self.darwin_environment(43, command, "")
        self.assertEqual(herdr.darwin_process_environment(43), {})

    def test_darwin_environment_refuses_a_changed_command_or_failed_ps(self) -> None:
        self.darwin()
        self.darwin_environment(42, "claude", "HERDR_PANE_ID=w1:p1")
        self.responses["-o command= -p 42"] = [
            {"stdout": "claude\n", "code": 0}, {"stdout": "claude HERDR_PANE_ID=forged\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(herdr.HerdrFailure, "stable command"):
            _ = herdr.darwin_process_environment(42)
        self.configure(["-o", "command=", "-p", "42"], code=1, stderr="vanished")
        with self.assertRaisesRegex(herdr.HerdrFailure, "vanished"):
            _ = herdr.darwin_process_environment(42)

    def test_darwin_unreadable_command_cannot_be_used_to_read_an_environment(self) -> None:
        self.darwin()
        self.darwin_environment(42, "(herdr)", "HERDR_SESSION=writing")
        with self.assertRaisesRegex(herdr.HerdrFailure, "arguments are unreadable"):
            _ = herdr.darwin_process_environment(42)
        self.assertEqual(self.commands(), [["-o", "command=", "-p", "42"]])

    def test_darwin_process_columns_preserve_spaced_executables_and_argument_states(self) -> None:
        self.darwin()
        uid = os.getuid()
        ghostty = "/Applications/Nix Apps/Ghostty.app/Contents/MacOS/ghostty"
        drive = "/Applications/Google Drive.app/Contents/MacOS/Google Drive"
        herdr_path = "/Applications/Nix Apps/herdr"
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            f"40 1 {uid} {ghostty}\n41 40 {uid} {drive}\n42 40 {uid} {herdr_path}\n" +
            f"43 40 {uid} {herdr_path}\n44 40 {uid} {herdr_path}\n45 40 {uid} {herdr_path}\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="],
            f"40 {ghostty} --working-directory '/work folder'\n41 {drive} --auto_restart_count=1\n" +
            f"42 {herdr_path} --session writing\n43 (herdr)\n44 changed-executable --session writing\n" +
            f"45 {herdr_path} 'unterminated\n")
        rows = herdr._darwin_processes()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([row.executable for row in rows], [ghostty, drive, *([herdr_path] * 4)])
        self.assertEqual(rows[0].arguments, herdr.ReadProcessArguments((ghostty, "--working-directory", "/work folder")))
        self.assertEqual(rows[1].arguments, herdr.ReadProcessArguments((drive, "--auto_restart_count=1")))
        self.assertEqual(rows[2].arguments, herdr.ReadProcessArguments((herdr_path, "--session", "writing")))
        for row in rows[3:]:
            self.assertIsInstance(row.arguments, herdr.UnreadableProcessArguments)
        self.assertEqual(self.commands(), [
            ["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            ["-U", str(uid), "-o", "pid=,command="]])

    def test_darwin_unreadable_herdr_arguments_report_unreadable_clients(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
                       f"42 1 {uid} /Applications/Nix Apps/herdr\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], "42 (herdr)\n")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_spaced_herdr_path_detects_attached_clients_and_unreadable_environment(self) -> None:
        self.darwin()
        uid = os.getuid()
        executable = "/Applications/Nix Apps/herdr"
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="], f"42 1 {uid} {executable}\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], f"42 {executable} session attach writing\n")
        self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
        self.configure(["-U", str(uid), "-o", "pid=,command="], f"42 {executable}\n")
        self.configure(["-o", "command=", "-p", "42"], code=1, stderr="permission denied")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_server_environment_uses_the_exact_listener_not_a_client(self) -> None:
        self.darwin()
        for name, directory in (("fixture", self.root / "config/herdr/sessions/fixture"),
                                ("default", self.root / "config/herdr")):
            with self.subTest(name=name):
                self.darwin_sockets(f"p70\nf3\nn{directory}/herdr.sock\n" +
                                    "p71\nf4\nn->0x1234\n")
                self.darwin_environment(70, "herdr server", "ACCOUNT=kept CLAUDE_CONFIG_DIR=/named account")
                self.darwin_environment(71, "herdr session attach fixture", "ACCOUNT=wrong")
                self.assertEqual(herdr.server_environment(name), {"ACCOUNT": "kept", "CLAUDE_CONFIG_DIR": "/named account"})
                self.assertNotIn(["eww", "-o", "command=", "-p", "71"], self.commands())

    def test_darwin_server_environment_refuses_missing_ambiguous_or_unreadable_holders(self) -> None:
        self.darwin()
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        for sockets in ("", f"p70\nf3\nn{path}\np71\nf3\nn{path}\n"):
            with self.subTest(sockets=sockets):
                self.darwin_sockets(sockets)
                with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket holder"):
                    _ = herdr.server_environment("fixture")
        self.darwin_sockets(f"p70\nf3\nn{path}\n")
        self.configure(["-o", "command=", "-p", "70"], code=1, stderr="environment unavailable")
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*environment unavailable"):
            _ = herdr.server_environment("fixture")

    def test_darwin_user_tmux_is_never_attributed_to_its_outer_herdr_pane(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 70 {uid} claude\n72 70 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}, {'pane_id': 'w1:p2', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "claude", "HERDR_PANE_ID=w1:p1 TMUX_PANE=%1")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p2 TMUX_PANE=")
        for pid in (71, 72):
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn{self.root}\n")
        self.assertEqual(herdr.herdr_place_of(71), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p2"))
        self.assertEqual([row.pid for row in herdr.herdr_processes()], [72])

    def test_darwin_process_discovery_uses_sockets_uid_cwd_and_real_environment(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
            f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n" +
            f"73 71 {uid + 1} claude\n74 71 {uid} claude HERDR_PANE_ID=forged\n" +
            f"75 71 {uid} codex\n76 71 {uid} claude\n77 1 {uid} unrelated\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}, {'pane_id': 'w1:p2', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 SHOWRUNNER_UNIT=prod SHOWRUNNER_UNIT_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        self.darwin_environment(74, "claude HERDR_PANE_ID=forged", "")
        self.configure(["-o", "command=", "-p", "75"], code=1, stderr="vanished")
        self.darwin_environment(76, "claude", "HERDR_PANE_ID=w1:p2")
        self.configure(["-a", "-p", "71", "-d", "cwd", "-Fn"], "p71\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "72", "-d", "cwd", "-Fn"], "p72\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "76", "-d", "cwd", "-Fn"], code=1, stderr="gone")
        rows = herdr.herdr_processes()
        self.assertCountEqual(rows, [
            herdr.HerdrProcess(71, herdr.HerdrPlace("writing", "w1:p1"), {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}, "zsh", "/work folder"),
            herdr.HerdrProcess(72, herdr.HerdrPlace("writing", "w1:p1"), {}, "claude", "/work folder"),
            herdr.HerdrProcess(76, herdr.HerdrPlace("writing", "w1:p2"), {}, "claude",
                               herdr.UnreadableCwd("process 76 cwd: gone"))])
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))
        self.assertEqual(herdr.herdr_place_of(74), herdr.NotInHerdr())
        for pid in (73, 77):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())

    def test_darwin_live_marked_process_remains_placed_when_cwd_is_unreadable(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
                       f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 SHOWRUNNER_UNIT=prod SHOWRUNNER_UNIT_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        for output, code, error in (("", 1, "permission denied"), ("p72\nfcwd\n", 0, ""),
                                    ("n/one\nn/two\n", 0, "")):
            with self.subTest(output=output, code=code):
                for pid in (71, 72):
                    self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], output, code, error)
                processes = {process.pid: process for process in herdr.herdr_processes()}
                self.assertCountEqual(processes, [71, 72])
                self.assertEqual(processes[71].marks, {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"})
                for process in processes.values():
                    self.assertIsInstance(process.working_directory, herdr.UnreadableCwd)
                    self.assertEqual(process.place, herdr.HerdrPlace("writing", "w1:p1"))
                self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))

    def test_darwin_attached_clients_exclude_other_users_and_noninteractive_commands(self) -> None:
        self.darwin()
        uid = os.getuid()
        commands = ["herdr session attach writing", "herdr --session writing", "herdr",
                    "herdr --session writing server", "herdr --session writing pane read p1",
                    "herdr --session writing agent list", "herdr --session writing status", "herdr session attach other"]
        table = "".join(f"{80 + index} 1 {uid} {command}\n" for index, command in enumerate(commands))
        self.darwin_process_table(table + f"99 1 {uid + 1} herdr session attach writing\n")
        self.darwin_environment(81, commands[1], "")
        self.darwin_environment(82, "herdr", "HERDR_SESSION=writing")
        self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(3))
        self.assertEqual(herdr.attached_clients("missing"), herdr.NoClientAttached())
        for pid in (83, 84, 85, 86, 99):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="], code=1, stderr="cannot read processes")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_attached_clients_skip_clients_confirmed_gone_during_environment_reads(self) -> None:
        self.darwin()
        uid = os.getuid()
        for stage in ("command", "environment", "confirmation"):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
                self.darwin_environment(81, "herdr", "HERDR_SESSION=writing")
                if stage == "environment":
                    self.configure(["eww", "-o", "command=", "-p", "81"], code=1)
                elif stage == "confirmation":
                    self.responses["-o command= -p 81"] = [
                        {"stdout": "herdr\n", "code": 0}, {"stdout": "", "code": 1}]
                    _ = self.responses_path.write_text(json.dumps(self.responses))
                else:
                    self.configure(["-o", "command=", "-p", "81"], code=1)
                with patch.object(os, "kill", side_effect=ProcessLookupError("no such process")) as probe:
                    self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
                probe.assert_called_once_with(81, 0)
                self.assertIn(["-o", "command=", "-p", "81"], self.commands())

    def test_darwin_attached_clients_skip_clients_confirmed_gone_between_process_scans(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], "82 herdr session attach writing\n")
        self.configure(["-o", "command=", "-p", "81"], code=1)
        with patch.object(os, "kill", side_effect=ProcessLookupError("no such process")) as probe:
            self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
        probe.assert_called_once_with(81, 0)
        self.assertEqual(self.commands(), [
            ["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            ["-U", str(uid), "-o", "pid=,command="],
            ["-o", "command=", "-p", "81"]])

    def test_darwin_attached_clients_preserve_unconfirmed_process_read_failures(self) -> None:
        self.darwin()
        uid = os.getuid()
        failures: list[tuple[str, CommandResponse, OSError | None]] = [
            ("pid still exists", {"stdout": "", "code": 1}, None),
            ("pid probe denied", {"stdout": "", "code": 1}, PermissionError("not permitted")),
            ("pid probe failed", {"stdout": "", "code": 1}, OSError("probe failed")),
            ("ps permission denied", {"stdout": "", "stderr": "permission denied", "code": 1},
             ProcessLookupError("no such process")),
            ("ps partial output", {"stdout": "herdr\n", "code": 1}, ProcessLookupError("no such process")),
            ("ps malformed output", {"stdout": "(herdr)\n", "code": 0}, ProcessLookupError("no such process")),
            ("ps empty success", {"stdout": "", "code": 0}, ProcessLookupError("no such process")),
        ]
        for stage in ("environment read", "argument scan"):
            for reason, response, probe_error in failures:
                with self.subTest(stage=stage, reason=reason):
                    self.log.unlink(missing_ok=True)
                    self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
                    self.darwin_environment(81, "herdr", "HERDR_SESSION=writing")
                    if stage == "argument scan":
                        self.configure(["-U", str(uid), "-o", "pid=,command="], "82 herdr session attach writing\n")
                    self.responses["-o command= -p 81"] = response
                    _ = self.responses_path.write_text(json.dumps(self.responses))
                    with patch.object(os, "kill", side_effect=probe_error, return_value=None) as probe:
                        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)
                    if reason in {"pid still exists", "pid probe denied", "pid probe failed"}:
                        probe.assert_called_once_with(81, 0)
                    else:
                        probe.assert_not_called()

    def test_darwin_handoff_finds_reparented_pane_shells_and_their_agents(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
                       f"70 1 {uid} herdr server\n71 1 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"],
                       json.dumps({"result": {"panes": [{"pane_id": "w1:p1", "label": "kept"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w1:p1"],
                       json.dumps({"result": {"process_info": {"shell_pid": 71}}}))
        for pid, command in ((71, "zsh"), (72, "claude")):
            self.darwin_environment(pid, command, "HERDR_PANE_ID=w1:p1")
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn/work\n")
        self.assertCountEqual([row.pid for row in herdr.herdr_processes()], [71, 72])
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))

    def test_darwin_unreadable_socket_listing_refuses_server_environment(self) -> None:
        self.darwin()
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
                       code=1, stderr="socket inspection denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket inspection denied"):
            _ = herdr.server_environment("fixture")
        self.assertEqual(herdr.herdr_processes(), [])

    def test_binary_missing_and_override(self) -> None:
        self.assertEqual(herdr.herdr_binary(), str(self.fake))
        with patch.dict(os.environ, {"HERDR_BIN": ""}), patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(herdr.HerdrMissing, "herdr is not installed"):
                _ = herdr.herdr_binary()
        with patch.dict(os.environ, {"HERDR_BIN": ""}), patch.object(shutil, "which", return_value="/fixture/herdr"):
            self.assertEqual(herdr.herdr_binary(), "/fixture/herdr")

    def test_argv_and_refusal_preserve_code_and_message(self) -> None:
        self.assertEqual(self.herdr.argv("pane", "list"), [str(self.fake), "--session", "fixture", "pane", "list"])
        self.configure_herdr(["pane", "list"], code=1, stderr=json.dumps({"error": {"code": "not_found", "message": "missing pane"}}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "not_found: missing pane") as raised:
            _ = self.herdr.panes()
        self.assertEqual(raised.exception.code, "not_found")

    def test_invalid_json_is_a_library_failure(self) -> None:
        self.configure_herdr(["pane", "list"], "[]")
        with self.assertRaises(herdr.HerdrFailure):
            _ = self.herdr.panes()

    def test_session_of_keeps_the_conversation_workspace_and_pane_after_claude_exits(self) -> None:
        pane, session_id = "w1:p2", "71356b6b-bf7b-4996-b27e-49da9f90b719"
        start = self.configure_tab_start(pane, "alpha", session_id)
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "production"}]}}))
        running = self.root / "claude-running"
        self.responses[" ".join(["--session", "fixture", *start])] = {
            "stdout": "{}", "code": 0, "write_files": {str(running): "4242"}}
        self.responses["--session fixture agent stop alpha"] = {
            "stdout": "{}", "code": 0, "require_paths": [str(running)], "remove_paths": [str(running)]}
        self.configure([], "")
        client = self.herdr
        self.assertEqual(client.session_of("alpha"), herdr.TabSession(pane, "alpha", "production", None))
        _ = client.run(*start)
        self.assertTrue(running.exists())
        expected = herdr.TabSession(pane, "alpha", "production", session_id)
        self.assertEqual(client.session_of("alpha"), expected)
        _ = client.run("agent", "stop", "alpha")
        self.assertFalse(running.exists())
        self.assertEqual(client.session_of("alpha"), expected)
        raw = herdr.json_object(herdr.json_items(client.result("pane", "list")["panes"], "panes")[0], "pane")
        self.assertEqual(raw["workspace_id"], "w1")
        self.assertEqual(raw["agent_session"], {"agent": "claude", "kind": "id",
                                               "source": "herdr:claude", "value": session_id})

    def test_a_failed_agent_start_does_not_supply_a_pane_conversation(self) -> None:
        start = self.configure_tab_start("w1:p2", "alpha", "71356b6b-bf7b-4996-b27e-49da9f90b719")
        self.configure_herdr(start, code=1, stderr="agent failed to start")
        with self.assertRaisesRegex(herdr.HerdrFailure, "agent failed to start"):
            _ = self.herdr.run(*start)
        self.assertEqual(self.herdr.panes(), [{"pane_id": "w1:p2", "label": "alpha",
                                               "workspace_id": "w1", "session_id": None}])

    def test_an_agent_start_with_a_missing_required_path_does_not_supply_a_conversation(self) -> None:
        start = self.configure_tab_start("w1:p2", "alpha", "71356b6b-bf7b-4996-b27e-49da9f90b719")
        self.responses[" ".join(["--session", "fixture", *start])] = {
            "stdout": "{}", "code": 0, "require_paths": [str(self.root / "missing")]}
        self.configure([], "")
        result = self.run_fake(self.herdr.argv(*start))
        self.assertEqual(result.returncode, 98)
        self.assertIn("fixture required path missing", result.stderr)
        self.assertEqual(self.herdr.session_of("alpha"), herdr.TabSession("w1:p2", "alpha", "", None))

    def test_tab_sessions_reads_panes_once_and_joins_workspace_labels_once(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p2", "workspace_id": "w2", "label": "alpha",
             "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sid-alpha"}},
            {"pane_id": "w2:p1", "workspace_id": "w1", "label": "beta", "agent_session": None}]}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "first"}, {"workspace_id": "w2", "label": "second"}]}}))
        client = self.herdr
        with patch.object(client, "panes", wraps=client.panes) as read_panes:
            self.assertEqual(client.tab_sessions(), [herdr.TabSession("w1:p2", "alpha", "second", "sid-alpha"),
                                                     herdr.TabSession("w2:p1", "beta", "first", None)])
        read_panes.assert_called_once_with()
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"],
                                           ["--session", "fixture", "workspace", "list"]])

    def test_tab_sessions_omits_panes_without_a_label(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1"}, {"pane_id": "w1:p2", "label": None},
            {"pane_id": "w1:p3", "label": "named"}]}}))
        self.assertEqual(self.herdr.tab_sessions(), [herdr.TabSession("w1:p3", "named", "", None)])

    def test_tab_sessions_keeps_panes_with_missing_or_unlabelled_workspaces(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "label": "missing"}, {"pane_id": "w2:p1", "label": "unlabelled"}]}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w2", "label": None}]}}))
        self.assertEqual(self.herdr.tab_sessions(), [herdr.TabSession("w1:p1", "missing", "", None),
                                                     herdr.TabSession("w2:p1", "unlabelled", "", None)])

    def test_session_of_and_lookup_share_exact_label_and_ambiguity_rules(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "label": "alpha"}, {"pane_id": "w1:p2", "label": "alpha-extra"},
            {"pane_id": "w2:p1", "label": "duplicate"}, {"pane_id": "w3:p1", "label": "duplicate"},
            {"pane_id": "w1:p3", "label": ""}, {"pane_id": "w1:p4", "label": None}]}}))
        client = self.herdr
        cases = [("alpha", herdr.PaneFound("w1:p1"), herdr.TabSession("w1:p1", "alpha", "", None)),
                 ("", herdr.PaneFound("w1:p3"), herdr.TabSession("w1:p3", "", "", None)),
                 ("Alpha", herdr.NoPane("Alpha"), herdr.NoPane("Alpha")),
                 ("alph", herdr.NoPane("alph"), herdr.NoPane("alph")),
                 ("missing", herdr.NoPane("missing"), herdr.NoPane("missing")),
                 ("duplicate", herdr.SeveralPanes("duplicate", 2), herdr.SeveralPanes("duplicate", 2))]
        for label, pane, tab in cases:
            with self.subTest(label=label):
                self.assertEqual(client.lookup(label), pane)
                self.assertEqual(client.session_of(label), tab)

    def test_panes_reads_session_values_and_allows_absent_conversations(self) -> None:
        sessions: tuple[object, ...] = (None, {}, {"value": None}, {"value": "sid-alpha"})
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": f"w1:p{index}", "workspace_id": "w2", "label": "alpha", "agent_session": session}
            for index, session in enumerate(sessions)]}}))
        self.assertEqual(self.herdr.panes(), [
            {"pane_id": f"w1:p{index}", "workspace_id": "w2", "label": "alpha", "session_id": session_id}
            for index, session_id in enumerate((None, None, None, "sid-alpha"))])

    def test_panes_rejects_invalid_workspace_ids(self) -> None:
        client = self.herdr
        workspaces: tuple[object, ...] = (None, 3, True, [], {})
        for workspace in workspaces:
            with self.subTest(workspace=workspace), patch.object(client, "result", return_value={"panes": [
                    {"pane_id": "w1:p1", "label": "alpha", "workspace_id": workspace}]}):
                with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane workspace id: expected text"):
                    _ = client.panes()

    def test_panes_rejects_invalid_conversation_objects_and_values(self) -> None:
        client = self.herdr
        sessions: tuple[object, ...] = ("sid-alpha", 3, True, [], {"value": 3}, {"value": False}, {"value": []}, {"value": {}})
        for session in sessions:
            with self.subTest(session=session), patch.object(client, "result", return_value={"panes": [
                    {"pane_id": "w1:p1", "label": "alpha", "workspace_id": "w1", "agent_session": session}]}):
                with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane agent session"):
                    _ = client.panes()

    def test_tab_sessions_rejects_invalid_workspace_labels(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": []}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": 3}]}}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr workspace label: expected text"):
            _ = self.herdr.tab_sessions()

    def test_session_list_keeps_running_only(self) -> None:
        rows: list[herdr.SessionEntry] = [
            {"name": "live", "running": True, "default": False, "session_dir": "/live", "socket_path": "/live/socket"},
            {"name": "stopped", "running": False, "default": True, "session_dir": "/stopped", "socket_path": "/stopped/socket"},
        ]
        self.configure(["--session", "default", "session", "list", "--json"], json.dumps({"sessions": rows}))
        self.assertEqual(herdr.live_sessions(), rows[:1])

    def test_server_start_skips_running_server(self) -> None:
        self.configure_herdr(["status", "server"], "server:\nstatus: running\n")
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        outcome = herdr.start_server("fixture", 10_000)
        self.assertIsInstance(outcome, herdr.ServerAdopted)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_server_start_does_not_poll_after_a_unit_creation_failure(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        creation = self.server_creation()
        self.configure(creation, code=1, stderr="fixture manager unavailable")
        with self.assertRaisesRegex(herdr.HerdrFailure, "fixture manager unavailable"):
            _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"], SCOPE_PROBE, creation])

    def test_server_start_deadline(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaisesRegex(herdr.HerdrFailure, "server fixture not answering after 0 ms"):
            _ = herdr.start_server("fixture", 0)
        self.assertEqual(self.commands()[-1], self.server_creation())

    def test_server_start_polls_after_launch(self) -> None:
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        outcome = herdr.start_server("fixture", 10_000)
        self.assertIsInstance(outcome, herdr.ServerCreated)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            SCOPE_PROBE,
            self.server_creation(),
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
            ["--session", "fixture", "workspace", "list"],
        ])

    def test_spawned_servers_close_every_restored_workspace_before_returning(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({
            "id": "cli:workspace:list", "result": {"type": "workspace_list", "workspaces": [
                {"workspace_id": "w1", "focused": True, "active_tab_id": "w1:t1",
                 "label": "fixture", "agent_status": "unknown", "number": 1,
                 "pane_count": 1, "tab_count": 1},
                {"workspace_id": "w2", "focused": False},
            ]},
        }))
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            SCOPE_PROBE,
            self.server_creation(),
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "workspace", "close", "w1"],
            ["--session", "fixture", "workspace", "close", "w2"],
        ])

    def test_the_shared_session_starts_its_boot_unit_and_keeps_its_restored_layout(self) -> None:
        shared = self.root / "config/herdr/shared-session"
        shared.parent.mkdir(parents=True)
        _ = shared.write_text("fixture\n")
        self.configure(["--user", "show", "-p", "FragmentPath", "--value", "herdr-fixture.service"],
                       "/etc/systemd/user/herdr-fixture.service\n")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        started = [
            ["--session", "fixture", "status", "server"],
            ["--user", "show", "-p", "FragmentPath", "--value", "herdr-fixture.service"],
            ["--user", "start", "herdr-fixture.service"],
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
        ]
        # While herdr resumes agents itself, the restored layout is cleared as for any session.
        _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), [*started, ["--session", "fixture", "workspace", "list"]])
        _ = shared.with_name("config.toml").write_text("[session]\nresume_agents_on_restore = false\n")
        self.log.unlink()
        _ = self.responses_path.write_text(json.dumps(self.responses | {"--session fixture status server": [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]}))
        _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), started)
        # The boot unit sets its own properties and shell: no probe, no wrapper.
        self.assertFalse((self.root / "home/.local/state/herdr").exists())

    def test_spawned_servers_report_session_and_workspace_when_close_fails(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        self.configure_herdr(["workspace", "close", "w1"], code=1, stderr="fixture close refused")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture:.*workspace w1:.*fixture close refused"):
            _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "workspace", "close", "w1"])

    def test_answering_server_with_inactive_spawned_unit_is_never_cleared(self) -> None:
        self.configure(["--user", "is-active", "herdr-fixture.service"], "inactive\n", code=3)
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        self.assertIsInstance(herdr.start_server("fixture", 10_000), herdr.ServerAdopted)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            SCOPE_PROBE,
            self.server_creation(),
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
        ])

    def test_stop_stops_only_named_active_unit(self) -> None:
        herdr.stop_session("fixture")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "server", "stop"], ["--user", "is-active", "herdr-fixture.service"],
            ["--user", "stop", "herdr-fixture.service"],
        ])

    def test_stop_checks_unit_after_server_refusal(self) -> None:
        self.configure_herdr(["server", "stop"], code=1, stderr="server failed")
        with self.assertRaises(herdr.HerdrFailure):
            herdr.stop_session("fixture")
        self.assertEqual(self.commands()[-1], ["--user", "stop", "herdr-fixture.service"])

    def share(self) -> None:
        folder = self.root / "config/herdr"
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / "shared-session").write_text("fixture\n")

    def test_programs_and_servers_never_inherit_a_units_marks(self) -> None:
        with patch.dict(os.environ, SHOWRUNNER_UNIT="show/demo-unit", SHOWRUNNER_UNIT_ID="id-demo", KEPT="yes"), \
                patch.object(subprocess, "run") as run:
            _ = herdr.run_program(["true"])
            given = cast(dict[str, str], run.call_args.kwargs["env"])
            self.assertEqual(given["KEPT"], "yes")
            self.assertNotIn("SHOWRUNNER_UNIT", given)
            self.assertNotIn("SHOWRUNNER_UNIT_ID", given)
            _ = herdr.run_program(["true"], environment={"SHOWRUNNER_UNIT": "explicit"})
            self.assertEqual(run.call_args.kwargs["env"], {"SHOWRUNNER_UNIT": "explicit"})

    def test_shared_session_is_agents_until_named(self) -> None:
        self.assertEqual(herdr.shared_session(), "agents")
        self.share()
        self.assertEqual(herdr.shared_session(), "fixture")

    def test_every_server_start_refuses_a_session_other_than_the_shared_one(self) -> None:
        self.share()
        starts: tuple[Callable[[], object], ...] = (
            lambda: herdr.start_server("writing", 10_000),
            lambda: herdr.restore_server("writing", 10_000, session_kind=herdr.DesktopSession()),
        )
        with patch.object(herdr, "_require_shared", REQUIRE_SHARED):
            for start in starts:
                with self.subTest(start=start), self.assertRaisesRegex(
                        herdr.HerdrFailure, "herdr session writing: every Claude session runs as a tab in "
                        + "herdr session fixture; open a tab there instead of starting another session"):
                    _ = start()
        self.assertEqual(self.commands(), [])

    def test_attach_hint_names_the_shared_session_workspace_and_tab(self) -> None:
        self.assertEqual(herdr.attach_hint("build-followups", "alpha"),
                         "herdr session attach agents, workspace build-followups, tab alpha")
        self.share()
        self.assertEqual(herdr.attach_hint("ad-hoc", "solo"),
                         "herdr session attach fixture, workspace ad-hoc, tab solo")

    def test_focus_tab_of_shows_the_workspace_then_the_tab_and_refuses_a_missing_pane(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"},
            {"pane_id": "w2:p3", "workspace_id": "w2", "tab_id": "w2:t2"}]}}))
        self.herdr.focus_tab_of("w2:p3")
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"],
                                           ["--session", "fixture", "workspace", "focus", "w2"],
                                           ["--session", "fixture", "tab", "focus", "w2:t2"]])
        self.log.unlink()
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane w9:p9 is not open in session fixture"):
            self.herdr.focus_tab_of("w9:p9")
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"]])

    def test_open_tab_creates_the_production_workspace_once_then_adds_tabs(self) -> None:
        options = ["--label", "boss", "--cwd", "/repo", "--env", "SHOWRUNNER_UNIT=prod", "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(["workspace", "create", *options], json.dumps({"result": {
            "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}}}))
        self.configure_herdr(["tab", "list"], json.dumps({"result": {"tabs": []}}))
        environment = {"SHOWRUNNER_UNIT": "prod", herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                       herdr.nix_env.HEAL_DIRECTORY_ENV: "/repo"}
        self.assertEqual(herdr.open_tab("fixture", "boss", "boss", "/repo", environment), "w1:p1")
        self.assertEqual(self.commands()[1:4], [["--session", "fixture", "workspace", "create", *options],
                                                ["--session", "fixture", "tab", "rename", "w1:t1", "boss"],
                                                ["--session", "fixture", "pane", "rename", "w1:p1", "boss"]])
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "boss"}]}}))
        tab = ["tab", "create", "--workspace", "w1", "--label", "alpha", "--cwd", "/wt", "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(tab, json.dumps({"result": {"root_pane": {"pane_id": "w1:p2"}}}))
        tabs = [{"tab_id": "w1:t1", "label": "boss", "pane_count": 1, "workspace_id": "w1"},
                {"tab_id": "w1:t2", "label": "alpha", "pane_count": 1, "workspace_id": "w1"}]
        for arguments in (["tab", "list", "--workspace", "w1"], ["tab", "list"]):
            self.configure_herdr(arguments, json.dumps({"result": {"tabs": tabs}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "tab_id": "w1:t1"}, {"pane_id": "w1:p2", "tab_id": "w1:t2"}]}}))
        self.assertEqual(herdr.open_tab("fixture", "boss", "alpha", "/wt", {}), "w1:p2")
        self.assertEqual([command[2:] for command in self.commands()[-8:]], [
            tab, ["pane", "rename", "w1:p2", "alpha"], ["tab", "list", "--workspace", "w1"], ["workspace", "list"],
            ["tab", "list", "--workspace", "w1"], ["pane", "list"], ["workspace", "list"], ["tab", "list"]])
        saved = herdr.saved_layout("fixture")
        self.assertEqual([(space["name"], [tab["name"] for tab in space["tabs"]]) for space in saved["workspaces"]],
                         [("boss", ["boss", "alpha"])])

    def test_move_pane_to_tab_keeps_running_pane_and_titles_only_the_created_tab(self) -> None:
        self.configure_herdr(["pane", "move", "w1:p2", "--new-tab", "--workspace", "w2"], json.dumps({"result": {
            "move_result": {"created_tab": {"tab_id": "w2:t7"}, "pane": {"pane_id": "w2:p9"}},
        }}))
        placed = self.herdr.move_pane_to_tab("w1:p2", "w2", "models")
        self.assertEqual(placed, herdr.TabWithPane("w2:p9", "w2:t7"))
        self.assertEqual([command[2:] for command in self.commands()], [
            ["pane", "move", "w1:p2", "--new-tab", "--workspace", "w2"],
            ["tab", "rename", "w2:t7", "models"],
        ])

    def test_fake_refuses_workspace_labels_before_reading_responses(self) -> None:
        self.responses_path.unlink()
        for workspace in ("sdlc", "wlower", "w", "wD-label"):
            for command in (["tab", "list"], ["pane", "move", "w1:p2", "--new-tab"]):
                with self.subTest(workspace=workspace, command=command):
                    result = self.run_fake([str(self.fake), "--session", "fixture", *command, "--workspace", workspace])
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(herdr.decode_object(result.stderr, "workspace error"), {
                        "error": {"code": "workspace_not_found", "message": f"workspace {workspace} not found"},
                        "id": "cli:tab:list",
                    })

    def test_fake_replays_responses_for_workspace_ids(self) -> None:
        for workspace in ("wD", "wH", "w12A"):
            with self.subTest(workspace=workspace):
                command = ["tab", "list", "--workspace", workspace]
                self.configure_herdr(command, "workspace tabs")
                result = self.run_fake([str(self.fake), "--session", "fixture", *command])
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "workspace tabs", ""))

    def test_move_pane_to_workspace_uses_its_id_and_returns_the_pane_after_ordering(self) -> None:
        _ = unit_membership("target", "models")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "source"}, {"workspace_id": "w2", "label": "target"}]}}))
        for pane, tab, moved in (("w1:p2", "w2:t7", "w2:p9"), ("w2:p9", "w2:t8", "w2:p11"),
                                  ("w2:p2", "w2:t9", "w2:p12")):
            self.configure_herdr(["pane", "move", pane, "--new-tab", "--workspace", "w2"], json.dumps({"result": {
                "move_result": {"created_tab": {"tab_id": tab}, "pane": {"pane_id": moved}}}}))
        self.configure_herdr(["tab", "list", "--workspace", "w2"], json.dumps({"result": {"tabs": [
            {"tab_id": "w2:t1", "label": "target", "pane_count": 1},
            {"tab_id": "w2:t2", "label": "apple-terminal", "pane_count": 1},
            {"tab_id": "w2:t7", "label": "models", "pane_count": 1}]}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w2:p1", "tab_id": "w2:t1"}, {"pane_id": "w2:p2", "tab_id": "w2:t2"},
            {"pane_id": "w2:p9", "tab_id": "w2:t7"}]}}))
        self.configure_herdr(["tab", "list"], json.dumps({"result": {"tabs": [
            {"tab_id": "w2:t1", "workspace_id": "w2", "label": "target"},
            {"tab_id": "w2:t8", "workspace_id": "w2", "label": "models"},
            {"tab_id": "w2:t9", "workspace_id": "w2", "label": "apple-terminal"}]}}))
        self.assertEqual(self.herdr.move_pane_to_workspace("w1:p2", "target", "models"), "w2:p11")
        self.assertEqual([command[2:] for command in self.commands()], [
            ["workspace", "list"], ["pane", "move", "w1:p2", "--new-tab", "--workspace", "w2"],
            ["tab", "rename", "w2:t7", "models"], ["workspace", "list"], ["tab", "list", "--workspace", "w2"],
            ["pane", "list"], ["pane", "move", "w2:p9", "--new-tab", "--workspace", "w2"],
            ["tab", "rename", "w2:t8", "models"], ["pane", "move", "w2:p2", "--new-tab", "--workspace", "w2"],
            ["tab", "rename", "w2:t9", "apple-terminal"], ["workspace", "list"], ["tab", "list"],
        ])
        self.assertEqual([tab["name"] for space in herdr.saved_layout("fixture")["workspaces"]
                          if space["name"] == "target" for tab in space["tabs"]], ["target", "models", "apple-terminal"])

    def test_order_tabs_puts_the_showrunner_first_then_moves_each_tab_out_of_place(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "boss", "focused": False, "active_tab_id": "w1:t2"},
            {"workspace_id": "w2", "label": "other", "focused": True, "active_tab_id": "w2:t1"}]}}))
        self.configure_herdr(["tab", "list", "--workspace", "w1"], json.dumps({"result": {"tabs": [
            {"tab_id": "w1:t1", "label": "boss", "pane_count": 1}, {"tab_id": "w1:t2", "label": "delta", "pane_count": 1},
            {"tab_id": "w1:t3", "label": "alpha", "pane_count": 1}]}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "tab_id": "w1:t1"}, {"pane_id": "w1:p2", "tab_id": "w1:t2"},
            {"pane_id": "w1:p3", "tab_id": "w1:t3"}, {"pane_id": "w2:p1", "tab_id": "w2:t1"}]}}))
        for pane, tab in (("w1:p3", "w1:t4"), ("w1:p2", "w1:t5")):
            self.configure_herdr(["pane", "move", pane, "--new-tab", "--workspace", "w1"], json.dumps({"result": {
                "move_result": {"created_tab": {"tab_id": tab}, "pane": {"pane_id": pane}}}}))
        self.assertEqual(herdr.Herdr("fixture").order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[])), {"w1:p3": "w1:p3", "w1:p2": "w1:p2"})
        acted = [command[2:] for command in self.commands() if command[2:4] not in (["workspace", "list"], ["tab", "list"],
                                                                                     ["pane", "list"])]
        self.assertEqual(acted, [["pane", "move", "w1:p3", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t4", "alpha"],
                                 ["pane", "move", "w1:p2", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t5", "delta"],
                                 ["tab", "focus", "w1:t5"], ["workspace", "focus", "w2"]])

    def test_retitle_names_the_pane_and_tab_then_orders_only_on_a_change(self) -> None:
        self.configure_herdr(["tab", "list", "--workspace", "w1"], json.dumps({"result": {"tabs": [
            {"tab_id": "w1:t1", "label": "boss"}, {"tab_id": "w1:t2", "label": "old-name"}]}}))
        for label, changed in (("old-name", True), ("new-name", False)):
            with self.subTest(label=label):
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                    {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1", "label": label}]}}))
                if not changed:
                    self.configure_herdr(["tab", "list", "--workspace", "w1"], json.dumps({"result": {"tabs": [
                        {"tab_id": "w1:t2", "label": "new-name"}]}}))
                self.log.unlink(missing_ok=True)
                with patch.object(herdr.Herdr, "order_tabs", return_value={}) as ordered:
                    self.assertEqual(herdr.Herdr("fixture").retitle("w1:p2", "new-name"), changed)
                acted = [command[2:] for command in self.commands() if command[3:4] == ["rename"]]
                self.assertEqual(acted, [["pane", "rename", "w1:p2", "new-name"], ["tab", "rename", "w1:t2", "new-name"]]
                                 if changed else [])
                self.assertEqual(ordered.call_args_list, [call("w1", herdr.SavedLayout(at=0.0, workspaces=[]))] if changed else [])
        with self.assertRaisesRegex(herdr.HerdrFailure, "no pane"):
            _ = herdr.Herdr("fixture").retitle("w9:p9", "new-name")

    def test_order_tabs_leaves_a_tab_with_several_panes_and_an_ordered_space_alone(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "boss", "focused": True, "active_tab_id": "w1:t1"}]}}))
        for pane, tab in (("w1:p3", "w1:t4"), ("w1:p2", "w1:t5")):
            self.configure_herdr(["pane", "move", pane, "--new-tab", "--workspace", "w1"], json.dumps({"result": {
                "move_result": {"created_tab": {"tab_id": tab}, "pane": {"pane_id": pane}}}}))
        for split_first, split_last, ordered in ((True, False, False), (False, True, False), (False, False, True)):
            with self.subTest(split_first=split_first, split_last=split_last, ordered=ordered):
                titles = ("boss", "alpha", "zed") if ordered else ("boss", "zed", "alpha")
                tabs = [{"tab_id": f"w1:t{index}", "label": title,
                         "pane_count": 2 if (index == 1 and split_first) or (index == 3 and split_last) else 1}
                        for index, title in enumerate(titles, 1)]
                self.configure_herdr(["tab", "list", "--workspace", "w1"], json.dumps({"result": {"tabs": tabs}}))
                panes = [{"pane_id": f"w1:p{index}", "tab_id": f"w1:t{index}"} for index in (1, 2, 3)]
                if split_first or split_last:
                    panes.append({"pane_id": "w1:p4", "tab_id": "w1:t1" if split_first else "w1:t3"})
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
                self.log.unlink(missing_ok=True)
                moved = herdr.Herdr("fixture").order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[]))
                self.assertEqual(moved, {"w1:p3": "w1:p3", "w1:p2": "w1:p2"} if split_first else {})
                acted = [command[2:] for command in self.commands() if command[3:4] in (["move"], ["rename"], ["focus"])]
                self.assertEqual(acted, [
                    ["pane", "move", "w1:p3", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t4", "alpha"],
                    ["pane", "move", "w1:p2", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t5", "zed"],
                    ["tab", "focus", "w1:t1"]] if split_first else [])

    def test_order_tabs_detects_a_unit_in_any_pane_of_an_unchanged_split_tab(self) -> None:
        _ = unit_membership("boss", "zulu")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "boss", "focused": True, "active_tab_id": "w1:t1"}]}}))
        self.configure_herdr(["tab", "list", "--workspace", "w1"], json.dumps({"result": {"tabs": [
            {"tab_id": f"w1:t{index}", "label": title, "pane_count": 2 if index == 2 else 1}
            for index, title in enumerate(("boss", "zulu", "zebra", "apple"), 1)]}}))
        for pane, tab in (("w1:p4", "w1:t5"), ("w1:p3", "w1:t6")):
            self.configure_herdr(["pane", "move", pane, "--new-tab", "--workspace", "w1"], json.dumps({"result": {
                "move_result": {"created_tab": {"tab_id": tab}, "pane": {"pane_id": pane}}}}))
        processes = [herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", "w1:p2"),
                                       {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "one"}, "claude", "/repo")]
        for split_panes in (("w1:p2", "w1:p5"), ("w1:p5", "w1:p2")):
            with self.subTest(split_panes=split_panes), patch.object(herdr, "herdr_processes", return_value=processes):
                panes = [{"pane_id": f"w1:p{index}", "tab_id": f"w1:t{index}"} for index in (1, 3, 4)]
                panes.extend({"pane_id": pane, "tab_id": "w1:t2"} for pane in split_panes)
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
                self.log.unlink(missing_ok=True)
                self.assertEqual(herdr.Herdr("fixture").order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[])),
                                 {"w1:p4": "w1:p4", "w1:p3": "w1:p3"})
                moved_panes = [command[4] for command in self.commands() if command[2:4] == ["pane", "move"]]
                self.assertEqual(moved_panes, ["w1:p4", "w1:p3"])

    def test_shared_session_is_never_stopped_and_close_agent_closes_only_the_tab(self) -> None:
        self.share()
        with self.assertRaisesRegex(herdr.HerdrFailure, "close one tab instead"):
            herdr.stop_session("fixture")
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "tab_id": "w1:t1"}, {"pane_id": "w1:p2", "tab_id": "w1:t2"}]}}))
        herdr.close_agent("fixture", "w1:p2")
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "tab", "close", "w1:t2"])
        self.assertNotIn(["--session", "fixture", "server", "stop"], self.commands())

    def test_focus_workspace_targets_the_named_session_and_reports_refusal(self) -> None:
        self.herdr.focus_workspace("w3")
        self.assertEqual(self.commands(), [["--session", "fixture", "workspace", "focus", "w3"]])
        self.configure_herdr(["workspace", "focus", "w3"], code=1, stderr="workspace no longer exists")
        with self.assertRaisesRegex(herdr.HerdrFailure, "workspace no longer exists"):
            self.herdr.focus_workspace("w3")

    def test_startup_cleanup_closes_only_the_untouched_home_shell_and_preserves_the_restored_workspace(self) -> None:
        shells = [startup_shell(name) for name in ("w1", "w2", "w3")]
        workspaces = [workspace for workspace, _ in shells]
        panes = [pane for _, pane in shells]
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": workspaces}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
        self.herdr.close_startup_workspaces("w2")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "workspace", "close", "w1"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "workspace", "close", "w3"]])

    def test_startup_cleanup_closes_the_shell_from_the_captured_mac_list_entries(self) -> None:
        workspace = herdr.decode_object(STARTUP_WORKSPACE_JSON, "captured startup workspace")
        pane = herdr.decode_object(STARTUP_PANE_JSON, "captured startup pane")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [workspace]}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [pane]}}))
        with (patch.object(herdr, "run_program", side_effect=self.run_fake),
              patch.object(Path, "home", return_value=Path("/Users/natemccoy"))):
            self.herdr.close_startup_workspaces("w2")
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "workspace", "close", "wG"])

    def test_startup_cleanup_preserves_workspace_changed_while_another_workspace_closes(self) -> None:
        for change in ("typed input", "renamed workspace", "removed workspace"):
            with self.subTest(change=change):
                self.log.unlink(missing_ok=True)
                shells = [startup_shell(name) for name in ("w1", "w2", "w3")]
                workspaces = [workspace for workspace, _ in shells]
                panes = [pane for _, pane in shells]
                self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": workspaces}}))
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
                def close_then_change(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                    result = self.run_fake(argv, timeout)
                    if argv[1:] == ["--session", "fixture", "workspace", "close", "w1"]:
                        if change == "typed input":
                            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                                {**pane, "revision": 1} if pane["pane_id"] == "w3:p1" else pane for pane in panes]}}))
                        else:
                            remaining = [workspace for workspace in workspaces if workspace["workspace_id"] != "w1"
                                         and (change != "removed workspace" or workspace["workspace_id"] != "w3")]
                            self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
                                {**workspace, "label": "my shell"} if workspace["workspace_id"] == "w3" else workspace
                                for workspace in remaining]}}))
                    return result
                with patch.object(herdr, "run_program", side_effect=close_then_change):
                    self.herdr.close_startup_workspaces("w2")
                self.assertEqual([command for command in self.commands() if command[2:4] == ["workspace", "close"]],
                                 [["--session", "fixture", "workspace", "close", "w1"]])

    def test_startup_cleanup_preserves_workspaces_with_activity_or_incomplete_evidence(self) -> None:
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.run_fake))
        workspace, pane = startup_shell("w1")
        scroll = herdr.json_object(pane["scroll"], "captured startup scroll")
        changed: list[tuple[str, dict[str, object], list[dict[str, object]]]] = [
            ("scrollback", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": 1}}]),
            ("typed input", {**workspace}, [{**pane, "revision": 1}]),
            ("second pane", {**workspace}, [{**pane}, {**pane, "pane_id": "w1:p2"}]),
            ("missing pane", {**workspace}, []),
            ("pane belongs elsewhere", {**workspace}, [{**pane, "pane_id": "w3:p1"}]),
            ("second tab", {**workspace, "tab_count": 2}, [{**pane}]),
            ("agent", {**workspace}, [{**pane, "agent": "claude"}]),
            ("agent status", {**workspace, "agent_status": "running"}, [{**pane}]),
            ("pane agent status", {**workspace}, [{**pane, "agent_status": "running"}]),
            ("cwd", {**workspace}, [{**pane, "cwd": str(self.root / "work")}]),
            ("foreground cwd", {**workspace}, [{**pane, "foreground_cwd": str(self.root / "work")}]),
            ("renamed", {**workspace, "label": "my shell"}, [{**pane}]),
            ("non-object scroll", {**workspace}, [{**pane, "scroll": []}]),
            ("null scroll", {**workspace}, [{**pane, "scroll": None}]),
            ("missing scroll offset", {**workspace}, [{**pane, "scroll": {
                key: value for key, value in scroll.items() if key != "max_offset_from_bottom"}}]),
            ("text scroll offset", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": "0"}}]),
            ("boolean revision", {**workspace}, [{**pane, "revision": False}]),
            ("boolean scrollback", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": False}}]),
            ("boolean tab count", {**workspace, "tab_count": True}, [{**pane}]),
            ("boolean pane count", {**workspace, "pane_count": True}, [{**pane}]),
        ]
        changed.extend((f"missing workspace {field}", {key: value for key, value in workspace.items() if key != field},
                        [{**pane}]) for field in ("label", "tab_count", "pane_count", "agent_status"))
        changed.extend((f"missing pane {field}", {**workspace},
                        [{key: value for key, value in pane.items() if key != field}])
                       for field in ("agent_status", "cwd", "foreground_cwd", "revision", "scroll"))
        for reason, candidate, members in changed:
            with self.subTest(reason=reason):
                self.log.unlink(missing_ok=True)
                self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [candidate]}}))
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": members}}))
                self.herdr.close_startup_workspaces("w2")
                self.assertEqual(self.commands(), [
                    ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "pane", "list"]])

    def test_promptless_start_never_sends_input(self) -> None:
        _ = self.herdr.start_agent("worker", "pane-1", "claude", ["-n", "solo"], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(self.start_commands(), [["--session", "fixture", "agent", "start", "worker", "--kind",
                                           "claude", "--pane", "pane-1", "--timeout", "90000", "--", "-n", "solo"]])

    def test_session_name_contract_refuses_before_external_commands(self) -> None:
        operations: tuple[Callable[[str], object], ...] = (
            herdr.server_unit, herdr.stop_session,
            lambda name: herdr.start_server(name, 10_000),
        )
        for name in ("", "a b", ".", "..", "a" * 65, "é", "a/b"):
            for operation in operations:
                with self.subTest(name=name, operation=operation):
                    with self.assertRaisesRegex(ValueError, "ASCII.*never"):
                        _ = operation(name)
        self.assertEqual(self.commands(), [])

    def test_valid_name_starts_and_stops_its_exact_unit(self) -> None:
        self.configure(["--session", "a-b", "status", "server"], "status: stopped")
        with self.assertRaises(herdr.HerdrFailure):
            _ = herdr.start_server("a-b", 0)
        herdr.stop_session("a-b")
        commands = self.commands()
        self.assertIn(self.server_creation("a-b"), commands)
        self.assertIn(["--user", "stop", "herdr-a-b.service"], commands)
        self.assertTrue(all("herdr-a b" not in part for command in commands for part in command))

    def test_service_suffix_in_a_session_name_never_aliases_another_unit(self) -> None:
        for name in ("foo", "foo.service"):
            self.configure(["--session", name, "status", "server"], "status: stopped")
            with self.assertRaises(herdr.HerdrFailure):
                _ = herdr.start_server(name, 0)
        self.assertEqual([command[1] for command in self.commands() if command[0] == "--user" and command != SCOPE_PROBE],
                         ["--unit=herdr-foo.service", "--unit=herdr-foo.service.service"])
        self.log.unlink()
        herdr.stop_session("foo.service")
        self.assertEqual(self.commands(), [
            ["--session", "foo.service", "server", "stop"],
            ["--user", "is-active", "herdr-foo.service.service"],
            ["--user", "stop", "herdr-foo.service.service"],
        ])

    def test_generated_name_fits_suffix_and_launches_under_the_contract(self) -> None:
        for text, suffix in (("space é" * 20, "123"), (".", ""), ("..", ""), ("", "")):
            name = herdr.generated_session_name(text, suffix)
            self.configure(["--session", name, "status", "server"], "status: running")
            _ = herdr.start_server(name, 10_000)
            self.assertLessEqual(len(name.encode("ascii")), 64)
            if suffix:
                self.assertTrue(name.endswith("-123"))
            self.assertEqual(self.commands()[-1], ["--session", name, "status", "server"])

    def test_attached_clients_count_only_interactive_clients_for_the_session(self) -> None:
        clients = [["herdr", "session", "attach", "fixture"], ["herdr", "--session", "fixture"],
                   ["herdr", "--session=fixture"], ["herdr", "--session", "other", "session", "attach", "fixture"]]
        excluded = [["herdr", "--session", "fixture", *command] for command in (
            ["server"], ["pane", "read", "p1"], ["agent", "list"], ["status"],
            ["status", "server"], ["workspace", "create"], ["session", "list"], ["--help"], ["--version"],
        )]
        excluded.extend([["herdr", "session", "attach", "other"], ["herdr", "--session", "other"],
                         ["echo", "herdr", "session", "attach", "fixture"]])
        for pid, argv in enumerate(clients + excluded, 100):
            _ = self.process(pid, 1, argv, {})
        self.assertEqual(herdr.attached_clients("fixture"), herdr.ClientsAttached(4))
        self.assertEqual(herdr.attached_clients("missing"), herdr.NoClientAttached())

    def test_attached_clients_handle_default_unreadable_and_vanished_processes(self) -> None:
        vanished = self.process(10, 1, ["herdr"], {})
        vanished.joinpath("cmdline").unlink()
        _ = self.process(11, 1, ["herdr"], {})
        self.assertEqual(herdr.attached_clients("default"), herdr.ClientsAttached(1))
        with patch.dict(os.environ, {"HERDR_PROC": str(self.root / "missing")}):
            self.assertIsInstance(herdr.attached_clients("default"), herdr.ClientsUnreadable)
        vanished.joinpath("cmdline").mkdir()
        self.assertIsInstance(herdr.attached_clients("default"), herdr.ClientsUnreadable)

    def test_attached_clients_skip_vanished_clients_but_report_permission_errors(self) -> None:
        disappearing = self.process(10, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        _ = self.process(11, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        original = Path.read_bytes
        for field in ("cmdline", "environ"):
            for failure in (FileNotFoundError, ProcessLookupError, PermissionError):
                with self.subTest(field=field, failure=failure.__name__):
                    def read(path: Path) -> bytes:
                        if path == disappearing / field:
                            raise failure("client process read")
                        return original(path)

                    with patch.object(Path, "read_bytes", read):
                        result = herdr.attached_clients("writing")
                    if failure is PermissionError:
                        self.assertEqual(result, herdr.ClientsUnreadable("client process read"))
                    else:
                        self.assertEqual(result, herdr.ClientsAttached(1))

    def test_bare_client_uses_environment_session_but_explicit_argv_wins(self) -> None:
        _ = self.process(10, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        _ = self.process(11, 1, ["herdr", "--session", "other"], {"HERDR_SESSION": "writing"})
        excluded = [self.process(pid, 1, arguments, {"HERDR_SESSION": "writing"})
                    for pid, arguments in enumerate((["herdr", "server"], ["herdr", "agent", "list"],
                                                     ["claude"]), 20)]
        original = Path.read_bytes

        def read(path: Path) -> bytes:
            if path.name == "environ" and path.parent in excluded:
                raise AssertionError("non-client environment was opened")
            return original(path)

        with patch.object(Path, "read_bytes", read):
            self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
            self.assertEqual(herdr.attached_clients("other"), herdr.ClientsAttached(1))
            self.assertEqual(herdr.attached_clients("default"), herdr.NoClientAttached())

    def test_process_discovery_reads_environ_only_under_socket_owned_servers(self) -> None:
        server = self.process(10, 1, ["handoff-server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["zsh"], {})
        _ = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "p1"})
        unrelated = self.process(20, 1, ["claude"], {"HERDR_PANE_ID": "p2"})
        _ = (server / "cgroup").write_text("0::/user.slice/app.slice/herdr-fixture.service\n")
        _ = (unrelated / "cgroup").write_text("0::/user.slice/app.slice/herdr-other.service\n")
        original = Path.read_bytes
        opened: list[Path] = []

        def read(path: Path) -> bytes:
            opened.append(path)
            if path == unrelated / "environ":
                raise AssertionError("unrelated environment was opened")
            return original(path)

        with patch.object(Path, "read_bytes", read):
            rows = herdr.herdr_processes()
        self.assertEqual([(row.pid, row.place) for row in rows], [(12, herdr.HerdrPlace("fixture", "p1"))])
        self.assertIn(self.proc / "12/environ", opened)
        self.assertNotIn(server / "environ", opened)

    def test_handoff_survivors_keep_their_unit_after_reparenting(self) -> None:
        server = self.process(30, 1, ["herdr", "server", "--handoff-import", "/fixture/import", "token"], {})
        self.unix_socket(self.root / "config/herdr/sessions/writing/herdr.sock", 100)
        self.socket_fd(server, 100)
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "p1", "HERDR_SESSION": "stale",
                              "SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
        _ = self.process(5, 1, ["subreaper"], {})
        # setsid in the replacement does not change these inherited memberships.
        for membership in ("0::", "1:name=systemd:"):
            for process in (server, shell):
                _ = (process / "cgroup").write_text(f"{membership}/user.slice/app.slice/herdr-writing.service\n")
            _ = (claude / "cgroup").write_text(f"{membership}/user.slice/app.slice/herdr-writing.service/agents\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(orphan_parent=parent, membership=membership):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows},
                                     {11: herdr.HerdrPlace("writing", "p1"), 12: herdr.HerdrPlace("writing", "p1")})
                    self.assertEqual(next(row.marks for row in rows if row.pid == 12),
                                     {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "p1"))
                    self.assertEqual(herdr.herdr_places(), {row.pid: row.place for row in rows})

    def test_shared_scope_handoff_uses_preserved_shells_without_reading_unrelated_environments(self) -> None:
        servers = [self.process(pid, 1, ["herdr", "server"], {}) for pid in (30, 40)]
        for server, name in zip(servers, ("writing", "other")):
            self.unix_socket(self.root / f"config/herdr/sessions/{name}/herdr.sock", int(server.name))
            self.socket_fd(server, int(server.name))
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "stale",
                              "SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
        other_shell = self.process(21, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        unrelated = self.process(20, 1, ["unrelated"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(5, 1, ["subreaper"], {})
        for name, pid in (("writing", 11), ("other", 21)):
            self.configure(["--session", name, "pane", "list"],
                           json.dumps({"result": {"panes": [{"pane_id": "w1:p1"}]}}))
            self.configure(["--session", name, "pane", "process-info", "--pane", "w1:p1"],
                           json.dumps({"result": {"process_info": {"shell_pid": pid}}}))
        original = Path.read_bytes

        def read(path: Path) -> bytes:
            if path.name == "environ" and path.parent in (*servers, unrelated, self.proc / "5"):
                raise AssertionError("unrelated environment was opened")
            return original(path)

        for membership in ("0::", "1:name=systemd:"):
            for process in (*servers, shell, claude, other_shell, unrelated):
                _ = (process / "cgroup").write_text(f"{membership}/user.slice/session-7.scope\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(parent=parent, membership=membership), patch.object(Path, "read_bytes", read):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows}, {
                        11: herdr.HerdrPlace("writing", "w1:p1"),
                        12: herdr.HerdrPlace("writing", "w1:p1"),
                        21: herdr.HerdrPlace("other", "w1:p1"),
                    })
                    self.assertEqual(next(row.marks for row in rows if row.pid == 12),
                                     {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "w1:p1"))
                    self.assertEqual(herdr.herdr_places(), {row.pid: row.place for row in rows})

    def test_handoff_survivors_in_pane_scopes_keep_their_unit_server(self) -> None:
        server = self.process(30, 1, ["herdr", "server", "--handoff-import", "/fixture/import", "token"], {})
        self.unix_socket(self.root / "config/herdr/sessions/writing/herdr.sock", 100)
        self.socket_fd(server, 100)
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "stale"})
        # A pane process whose parent exited: only its scope still names the pane.
        daemon = self.process(13, 1, ["daemon"], {"HERDR_PANE_ID": "w1:p1"})
        unrelated = self.process(20, 1, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(5, 1, ["subreaper"], {})
        self.configure(["--session", "writing", "pane", "list"],
                       json.dumps({"result": {"panes": [{"pane_id": "w1:p1"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w1:p1"],
                       json.dumps({"result": {"process_info": {"shell_pid": 11}}}))
        slice_path = "/user.slice/user-1000.slice/user@1000.service/app.slice"
        expected = {pid: herdr.HerdrPlace("writing", "w1:p1") for pid in (11, 12, 13)}
        for membership in ("0::", "1:name=systemd:"):
            _ = (server / "cgroup").write_text(f"{membership}{slice_path}/herdr-writing.service\n")
            for process in (shell, claude, daemon):
                _ = (process / "cgroup").write_text(f"{membership}{slice_path}/{herdr.PANE_SLICE}/run-p11.scope\n")
            _ = (unrelated / "cgroup").write_text(f"{membership}{slice_path}/{herdr.PANE_SLICE}/run-p20.scope\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(orphan_parent=parent, membership=membership):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows}, expected)
                    self.assertEqual(herdr.herdr_place_of(13), herdr.HerdrPlace("writing", "w1:p1"))
                    self.assertEqual(herdr.herdr_places(), expected)
            # Without a handoff the parent walk explains the scope; one live read verifies its pane.
            _ = (shell / "stat").write_text("11 (zsh) S 30 0 0\n")
            _ = (unrelated / "cgroup").write_text(f"{membership}/user.slice/session-7.scope\n")
            self.log.unlink(missing_ok=True)
            with self.subTest(handoff=False, membership=membership):
                self.assertEqual(herdr.herdr_places(), expected)
                self.assertEqual([command for command in self.commands() if command[2:4] == ["pane", "list"]],
                                 [["--session", "writing", "pane", "list"]])

    def test_server_start_gives_each_pane_shell_a_scope_through_the_wrapper(self) -> None:
        real = self.root / "bin/zsh"
        real.parent.mkdir()
        _ = real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        for state in ("", str(self.root / "state")):
            with self.subTest(state=state), patch.dict(os.environ, {"SHELL": str(real), "XDG_STATE_HOME": state}):
                self.log.unlink(missing_ok=True)
                self.configure_herdr(["status", "server"], "status: stopped\n")
                with self.assertRaises(herdr.HerdrFailure):
                    _ = herdr.start_server("fixture", 0)
                wrapper = Path(state or self.root / "home/.local/state") / "herdr/pane-shell"
                creation = [*self.server_creation()[:9], f"--setenv=SHELL={wrapper}",
                            f"--setenv=HERDR_PANE_SHELL={real}", *self.server_creation()[11:]]
                self.assertEqual(self.commands()[-2:], [SCOPE_PROBE, creation])
                self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)
                self.assertEqual(wrapper.read_text(), "\n".join([
                    "#!/bin/sh",
                    "real=${HERDR_PANE_SHELL:-/bin/sh}",
                    "unset HERDR_PANE_SHELL",
                    "SHELL=$real; export SHELL",
                    f'exec {self.fake} --user --scope --quiet --collect --slice=app-agents.slice -- "$real" "$@"',
                    "",
                ]))
                self.assertEqual([path.name for path in wrapper.parent.iterdir()], ["pane-shell"])
                self.log.unlink()
                _ = subprocess.run([str(wrapper), "-l"], check=True, env={
                    "HERDR_PANE_SHELL": str(real), "FAKE_HERDR_LOG": str(self.log),
                    "FAKE_HERDR_RESPONSES": str(self.responses_path)})
                self.assertEqual(self.commands(), [["--user", "--scope", "--quiet", "--collect",
                                                    "--slice=app-agents.slice", "--", str(real), "-l"]])

    def test_pane_shell_falls_back_to_bin_sh(self) -> None:
        plain = self.root / "not-executable"
        _ = plain.write_text("")
        wrapper = self.root / "home/.local/state/herdr/pane-shell"
        for shell in (None, "zsh", str(plain), str(self.root / "missing"), str(wrapper)):
            with self.subTest(shell=shell):
                with patch.dict(os.environ, {} if shell is None else {"SHELL": shell}):
                    self.log.unlink(missing_ok=True)
                    self.configure_herdr(["status", "server"], "status: stopped\n")
                    with self.assertRaises(herdr.HerdrFailure):
                        _ = herdr.start_server("fixture", 0)
                self.assertEqual(self.commands()[-1], self.server_creation())

    def test_server_start_without_pane_scopes_runs_the_plain_shell_and_says_so_once(self) -> None:
        self.configure(SCOPE_PROBE, code=1, stderr="Failed to connect to bus\nsecond line\n")
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(herdr.HerdrFailure):
            _ = herdr.start_server("fixture", 0)
        self.assertEqual(self.commands()[-2:], [SCOPE_PROBE, self.server_creation(scopes=False)])
        self.assertEqual(errors.getvalue().splitlines(), [
            "herdr fixture: pane scopes unavailable, panes stay in the server unit: Failed to connect to bus second line"])
        self.assertFalse((self.root / "home/.local/state/herdr").exists())

    def test_a_slow_start_on_a_loaded_machine_still_gets_its_opening_prompt(self) -> None:
        # Claude is ready for input 200 s after the start: past the 90 s limit, inside it scaled by load.
        for factor, typed_at, limit in ((4.0, 200.0, "300000"), (1.0, None, "90000")):
            with self.subTest(factor=factor):
                clock: list[float] = [0.0]
                started: list[list[str]] = []
                typed: list[float] = []

                def sleep(seconds: float) -> None:
                    clock[0] += seconds

                def start(_client: herdr.Herdr, argv: list[str], _kind: str, _pane: str,
                          _timeout: float) -> subprocess.CompletedProcess[str]:
                    started.append(argv)
                    return subprocess.CompletedProcess(argv, 0, "", "")

                def state(_client: herdr.Herdr, _pane: str, _transcript: object, _launched_at: datetime, *,
                          cursor: object = None) -> herdr.PromptInputReady | herdr.PromptInputNotReady:
                    del cursor
                    return (herdr.PromptInputReady("composer", True) if clock[0] >= 200
                            else herdr.PromptInputNotReady("destination session is still loading"))

                def prompt(_client: herdr.Herdr, _pane: str, _text: str) -> None:
                    typed.append(clock[0])

                with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)),
                      patch.object(herdr, "load_factor", return_value=factor),
                      patch.object(herdr.Herdr, "submit_opening_prompt", REAL_SUBMIT_OPENING_PROMPT),
                      patch.object(herdr.Herdr, "start_command", autospec=True, side_effect=start),
                      patch.object(herdr.Herdr, "prompt_input_state", autospec=True, side_effect=state),
                      patch.object(herdr.Herdr, "prompt", autospec=True, side_effect=prompt)):
                    outcome = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.OpeningPrompt("go"), 90_000)
                self.assertEqual(started[0][started[0].index("--timeout") + 1], limit)
                assert isinstance(outcome, herdr.PromptNotSubmitted)
                if typed_at is None:
                    self.assertEqual((outcome.reason, outcome.never_typed, typed),
                                     ("destination never became ready for input", True, []))
                else:
                    self.assertEqual(len(typed), 1)
                    self.assertGreaterEqual(typed[0], typed_at)
                    self.assertFalse(outcome.never_typed)

    def test_load_factor_is_the_load_per_cpu_from_one_to_six(self) -> None:
        for load, factor in ((4.0, 1.0), (64.0, 2.0), (400.0, 6.0)):
            with (self.subTest(load=load), patch.object(os, "getloadavg", return_value=(load, 0.0, 0.0)),
                  patch.object(os, "cpu_count", return_value=32)):
                self.assertEqual(REAL_LOAD_FACTOR(), factor)

    def test_agent_not_ready_reads_screen_without_input(self) -> None:
        arguments = ["agent", "start", "worker", "--kind", "claude", "--pane", "pane-1", "--timeout", "100", "--", "--model", "fixture-model"]
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "agent_not_ready", "message": "trust dialog"}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible"], "Trust this folder?")
        with self.assertRaises(herdr.AgentNotReady) as raised:
            _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--model", "fixture-model"], herdr.OpeningPrompt("opening\nprompt"), 100)
        self.assertEqual(raised.exception.screen, "Trust this folder?")
        self.assertEqual(self.start_commands(), [["--session", "fixture", *arguments], ["--session", "fixture", "pane", "read", "pane-1", "--source", "visible"]])

    def startup_process(self, pid: int, arguments: list[str]) -> None:
        process = self.process(pid, 10 if pid != 10 else 1, arguments, {})
        fields = ["S", str(10 if pid != 10 else 1), str(pid), "10", "1", "10",
                  *(["0"] * 13), str(pid * 100)]
        _ = (process / "stat").write_text(f"{pid} (pane process) {' '.join(fields)}\n")
        task = process / "task" / str(pid)
        task.mkdir(parents=True)
        _ = (task / "children").write_text("")

    def startup_observation(self, arguments: list[str]) -> str:
        return json.dumps({"result": {"process_info": {
            "shell_pid": 10, "foreground_process_group_id": 10 if arguments == ["zsh"] else 11,
            "foreground_processes": [{"pid": 10 if arguments == ["zsh"] else 11,
                                      "name": arguments[0], "argv": arguments}],
        }}})

    def startup_watch(self, clock: StartupClock, ready_after: float, on_wait: Callable[[], None],
                      observation: Callable[[], str]) -> tuple[WaitingAgentCLI, list[tuple[float, float]]]:
        process = WaitingAgentCLI(clock, ready_after, on_wait)
        calls: list[tuple[float, float]] = []

        def run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
            calls.append((clock.elapsed, timeout))
            payload = observation()
            if argv[3:5] == ["agent", "get"]:
                payload = json.dumps({"result": {"agent": {
                    "name": "worker", "agent_status": "unknown", "launch_pending": True,
                }}})
            return subprocess.CompletedProcess(argv, 0, payload, "")

        _ = self.enterContext(patch.object(herdr, "run_program", run))
        _ = self.enterContext(patch.object(subprocess, "Popen", return_value=process))
        _ = self.enterContext(patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)))
        return process, calls

    def test_start_detects_a_brief_agent_exit_while_registration_stays_pending(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--not-a-real-flag"])
        clock = StartupClock()
        children = self.proc / "10/task/10/children"

        def advance() -> None:
            _ = children.write_text("11" if 0.2 <= clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)

        process, calls = self.startup_watch(clock, 90, advance, lambda: self.startup_observation(["zsh"]))
        with self.assertRaisesRegex(herdr.AgentExited, "fixture:pane-1.*exited"):
            _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 1)
        self.assertTrue(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_keeps_an_identified_pid_alive_after_exec_and_parent_change(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--resume", "missing-id"])
        children = self.proc / "10/task/10/children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                # An exec changes argv/comm but preserves PID and start time.
                _ = (self.proc / "11/cmdline").write_bytes(b"/nix/store/runtime/bin/runtime\0")
                stat = (self.proc / "11/stat").read_text().replace("pane process", "runtime").replace("S 10 ", "S 1 ")
                _ = (self.proc / "11/stat").write_text(stat)

        process, calls = self.startup_watch(clock, 4.3, advance, lambda: self.startup_observation(["zsh"]))
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_keeps_a_wrapper_child_alive_after_its_parent_exits(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude"])
        self.startup_process(12, ["/nix/store/runtime/bin/runtime"])
        stat = self.proc / "12/stat"
        _ = stat.write_text(stat.read_text().replace("S 10 ", "S 11 "))
        _ = (self.proc / "11/task/11/children").write_text("12")
        children = self.proc / "10/task/10/children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)
                _ = stat.write_text(stat.read_text().replace("S 11 ", "S 1 "))

        process, calls = self.startup_watch(clock, 4.3, advance, lambda: self.startup_observation(["zsh"]))
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_detects_exit_of_an_agent_forked_by_another_shell_thread(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--not-a-real-flag"])
        task = self.proc / "10/task/20"
        task.mkdir()
        children = task / "children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if 0.2 <= clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)

        process, calls = self.startup_watch(clock, 90, advance, lambda: self.startup_observation(["zsh"]))
        with self.assertRaises(herdr.AgentExited):
            _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 1)
        self.assertTrue(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_ignores_prompt_helpers_before_a_healthy_agent(self) -> None:
        for arguments in (["git", "status", "claude"], ["node", "prompt-helper.js", "claude"]):
            with self.subTest(helper=arguments):
                for pid in (10, 11):
                    shutil.rmtree(self.proc / str(pid), ignore_errors=True)
                self.startup_process(10, ["zsh"])
                self.startup_process(11, arguments)
                clock = StartupClock()
                children = self.proc / "10/task/10/children"

                def advance() -> None:
                    _ = children.write_text("11" if clock.elapsed < 0.2 or clock.elapsed >= 0.4 else "")
                    if clock.elapsed >= 0.4:
                        _ = (self.proc / "11/cmdline").write_bytes(b"claude\0")

                process, calls = self.startup_watch(clock, 1.5, advance, lambda: self.startup_observation(["zsh"]))
                _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
                self.assertFalse(process.killed)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(len(calls), 1)

    def test_start_uses_slow_herdr_observations_when_proc_is_unreadable(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            return self.startup_observation(["claude"] if clock.elapsed >= 0.4 else ["zsh"])

        process, calls = self.startup_watch(clock, 4.3, lambda: None, observation)
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertFalse(process.killed)
        self.assertLessEqual(len(calls), 1 + int(clock.elapsed * 2))
        self.assertTrue(all(later[0] - earlier[0] >= 0.5 for earlier, later in zip(calls, calls[1:])))

    def test_start_falls_back_to_process_info_to_confirm_shell_return(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            return self.startup_observation(["claude"] if 0.4 <= clock.elapsed < 0.9 else ["zsh"])

        process, calls = self.startup_watch(clock, 90, lambda: None, observation)
        with self.assertRaises(herdr.AgentExited):
            _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 2)
        self.assertTrue(process.killed)
        self.assertGreaterEqual(len(calls), 3)

    def test_start_collects_cli_completion_after_a_slow_observation_crosses_deadline(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            if clock.elapsed > 0:
                clock.elapsed += 1.0
            return self.startup_observation(["zsh"])

        process, calls = self.startup_watch(clock, 0.7, lambda: None, observation)
        result = self.herdr.start_command(["fake-herdr", "agent", "start"], "claude", "pane-1", 1)
        self.assertEqual(result.stdout, "CLI completed")
        self.assertFalse(process.killed)
        self.assertGreater(clock.elapsed, 1)
        self.assertTrue(all(timeout <= 1 - called_at for called_at, timeout in calls))
        self.assertLess(calls[-1][1], 0.5)

    def test_start_does_not_treat_an_unreadable_observation_as_an_exit(self) -> None:
        source = self.fake.read_text().replace(
            '    responses = cast(',
            '    if argv[2:4] == ["agent", "start"]:\n'
            + '        import time\n'
            + '        time.sleep(0.3)\n'
            + '    responses = cast(',
        )
        _ = self.fake.write_text(source)
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], code=1, stderr="unreadable")
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.OpeningPrompt("continue"), 90_000)
        self.assertTrue(any(command[2:4] == ["pane", "process-info"] for command in self.commands()))
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "agent", "prompt", "pane-1", "continue"])

    def test_start_maps_an_explicit_process_exit_to_the_cleanup_error(self) -> None:
        self.configure_herdr(
            ["agent", "start", "worker", "--kind", "claude", "--pane", "pane-1", "--timeout", "90000", "--"],
            code=1, stderr=json.dumps({"error": {"code": "agent_start_failed",
                                              "message": "agent process exited before becoming interactive"}}))
        with self.assertRaises(herdr.AgentExited):
            _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(len(self.start_commands()), 1)

    def test_start_sends_exactly_one_multiline_prompt(self) -> None:
        _ = self.herdr.start_agent("worker", "pane-1", "codex", ["-m", "fixture-model"], herdr.OpeningPrompt("opening\nprompt"), 200)
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "codex", "--pane", "pane-1", "--timeout", "200", "--", "-m", "fixture-model"],
            ["--session", "fixture", "agent", "prompt", "pane-1", "opening\nprompt"],
        ])

    def start_responses(self, timeout_ms: int, responses: list[CommandResponse]) -> list[str]:
        arguments = ["--session", "fixture", "agent", "start", "worker", "--kind", "claude",
                     "--pane", "pane-1", "--timeout", str(timeout_ms), "--", "--resume", "conversation"]
        self.responses[" ".join(arguments)] = responses
        _ = self.responses_path.write_text(json.dumps(self.responses))
        return arguments

    def enforce_start_timeout_minimum(self) -> None:
        command = herdr.Herdr.start_command

        def constrained_command(client: herdr.Herdr, argv: list[str], kind: str, pane: str,
                                timeout: float) -> subprocess.CompletedProcess[str]:
            result = command(client, argv, kind, pane, timeout)
            if argv[3:5] == ["agent", "start"] and int(argv[argv.index("--timeout") + 1]) <= 3000:
                return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {
                    "code": "invalid_agent_timeout", "message": "timeout must exceed 3000 ms",
                }}))
            return result

        _ = cast(object, self.enterContext(patch.object(herdr.Herdr, "start_command", constrained_command)))

    def test_start_retries_busy_then_prompts_once(self) -> None:
        self.enforce_start_timeout_minimum()
        busy: CommandResponse = {"stdout": "", "code": 1, "stderr": json.dumps({"error": {
            "code": "agent_pane_busy", "message": "shell not yet available",
        }})}
        arguments = self.start_responses(10_000, [busy, busy, {"stdout": "", "code": 0}])
        pause = Mock()
        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
            _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue\nworking"), 10_000)
        self.assertEqual(self.start_commands(), [arguments, arguments, arguments,
                                          ["--session", "fixture", "agent", "prompt", "pane-1", "continue\nworking"]])
        self.assertEqual([call.args for call in pause.call_args_list], [(0.1,), (0.1,)])

    def test_start_busy_retries_share_deadline_and_raise_last_failure(self) -> None:
        self.enforce_start_timeout_minimum()
        attempts: list[list[str]] = []
        for timeout_ms, message in ((3200, "first busy"), (3100, "last busy")):
            attempts.append(self.start_responses(timeout_ms, [{"stdout": "", "code": 1, "stderr": json.dumps({
                "error": {"code": "agent_pane_busy", "message": message},
            })}]))
        elapsed = [0.0]

        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=advance)):
            with self.assertRaisesRegex(herdr.HerdrFailure, "last busy") as raised:
                _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), 3200)
        self.assertEqual(raised.exception.code, "agent_pane_busy")
        self.assertEqual(self.start_commands(), attempts)

    def test_start_initial_small_timeout_keeps_herdr_refusal_without_retry(self) -> None:
        self.enforce_start_timeout_minimum()
        for timeout_ms in (2999, 3000):
            with self.subTest(timeout_ms=timeout_ms):
                self.log.unlink(missing_ok=True)
                arguments = self.start_responses(timeout_ms, [{"stdout": "", "code": 0}])
                pause = Mock()
                with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
                    with self.assertRaises(herdr.HerdrFailure) as raised:
                        _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), timeout_ms)
                self.assertEqual(raised.exception.code, "invalid_agent_timeout")
                self.assertEqual(self.start_commands(), [arguments])
                pause.assert_not_called()

    def test_start_does_not_retry_other_failure_codes(self) -> None:
        for code in ("agent_blocked", "invalid_agent_name", "not_found"):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                arguments = self.start_responses(10_000, [{"stdout": "", "code": 1, "stderr": json.dumps({
                    "error": {"code": code, "message": "start refused"},
                })}, {"stdout": "", "code": 0}])
                pause = Mock()
                with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
                    with self.assertRaisesRegex(herdr.HerdrFailure, "start refused") as raised:
                        _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), 10_000)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.start_commands(), [arguments])
                pause.assert_not_called()

    def test_claude_start_passes_only_extra_arguments_after_separator(self) -> None:
        _ = self.herdr.start_agent(
            "worker", "pane-1", kind="claude",
            agent_arguments=["--resume", "conversation", "--model", "fixture-model"],
            opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
        )
        start, prompt = self.start_commands()
        self.assertEqual(start, [
            "--session", "fixture", "agent", "start", "worker", "--kind", "claude",
            "--pane", "pane-1", "--timeout", "10000", "--", "--resume", "conversation",
            "--model", "fixture-model",
        ])
        self.assertEqual(start.count("claude"), 1)
        self.assertEqual(start[start.index("--") + 1:], ["--resume", "conversation", "--model", "fixture-model"])
        self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "pane-1", "continue"])

    def test_agent_executable_argument_is_refused_before_any_herdr_call(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "agent_arguments must omit"):
                    _ = self.herdr.start_agent(
                        "worker", "pane-1", kind=kind, agent_arguments=[kind, "--resume", "conversation"],
                        opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
                    )
        self.assertEqual(self.commands(), [])

    def test_module_start_refuses_executable_before_pane_lookup(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent_arguments must omit"):
            _ = herdr.start_agent(
                "fixture", "pane-1", kind="claude", agent_arguments=["claude", "--resume", "conversation"],
                opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000, agent="worker",
            )
        self.assertEqual(self.commands(), [])

    def test_module_start_passes_extra_arguments_and_names_the_agent(self) -> None:
        _ = herdr.start_agent(
            "fixture", "pane-1", kind="claude", agent_arguments=["--resume", "conversation"],
            opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000, agent="worker",
        )
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "claude", "--pane", "pane-1",
             "--timeout", "10000", "--", "--resume", "conversation"],
            ["--session", "fixture", "agent", "prompt", "pane-1", "continue"],
        ])

    def test_agent_names_are_valid_for_method_and_module_launches(self) -> None:
        for label, expected in (("Kept Runner", "kept-runner"), ("7runner", "a-7runner"),
                                ("X" * 40, "x" * 32), ("", "a-7pane-1")):
            for module_launch in (False, True):
                with self.subTest(label=label, module_launch=module_launch):
                    self.log.unlink(missing_ok=True)
                    if module_launch:
                        _ = herdr.start_agent("fixture", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 200,
                                              agent=label or "7pane-1")
                    else:
                        _ = self.herdr.start_agent(label or "7pane-1", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 200)
                    start, prompt = self.start_commands()[-2:]
                    self.assertEqual(start, [
                        "--session", "fixture", "agent", "start", expected, "--kind", "claude",
                        "--pane", "7pane-1", "--timeout", "200", "--", "--model", "opus",
                    ])
                    self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "7pane-1", "continue"])

    def test_pane_activity_distinguishes_shell_from_foreground_commands(self) -> None:
        for foreground, names, expected in (
            (3392858, ("zsh",), herdr.PaneAtShell()),
            (3393000, ("claude", "node"), herdr.PaneRunning(("claude", "node"))),
            (3393001, ("zsh",), herdr.PaneRunning(("zsh",))),
        ):
            with self.subTest(foreground=foreground):
                self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({
                    "id": "cli:pane:process_info", "result": {"type": "pane_process_info", "process_info": {
                        "pane_id": "w1:p1", "shell_pid": 3392858,
                        "foreground_process_group_id": foreground,
                        "foreground_processes": [{"name": name, "pid": foreground + index,
                            "argv": [name], "cmdline": name, "cwd": "/tmp"} for index, name in enumerate(names)],
                    }},
                }))
                self.assertEqual(self.herdr.pane_activity("w1:p1"), expected)

    def test_pane_activity_rejects_malformed_process_info(self) -> None:
        valid: dict[str, object] = {"shell_pid": 10, "foreground_process_group_id": 10,
                                   "foreground_processes": [{"name": "zsh"}]}
        malformed: list[object] = [
            None, [], {}, {**valid, "shell_pid": "10"}, {**valid, "shell_pid": True},
            {**valid, "foreground_process_group_id": 0},
            {**valid, "foreground_process_group_id": None},
            {**valid, "foreground_processes": {}},
            {**valid, "foreground_processes": ["zsh"]},
            {**valid, "foreground_processes": [{}]},
            {**valid, "foreground_processes": [{"name": 10}]},
        ]
        for info in malformed:
            with self.subTest(info=info):
                self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({
                    "result": {"process_info": info},
                }))
                with self.assertRaises(herdr.HerdrFailure):
                    _ = self.herdr.pane_activity("w1:p1")

    def test_prompt_refusal_raises_blocked_without_retry(self) -> None:
        self.configure_herdr(["agent", "prompt", "pane-1", "work"], code=1, stderr=json.dumps({"error": {"code": "agent_blocked", "message": "permission dialog"}}))
        with self.assertRaises(herdr.AgentBlocked):
            self.herdr.prompt("pane-1", "work")
        self.assertEqual(len(self.commands()), 1)

    def test_readiness_and_wait_states(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        self.assertEqual(self.herdr.readiness("pane-1"), {"agent_status": "idle", "interactive_ready": True})
        arguments = ["agent", "wait", "pane-1", "--until", "done", "--timeout", "20"]
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "done"}}}))
        self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.ObservedAgentState("done"))
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "timeout", "message": "deadline"}}))
        self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.WaitTimedOut())
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 1)):
            self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.WaitTimedOut())
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        with self.assertRaises(herdr.AgentBlocked):
            _ = self.herdr.wait_until("pane-1", "done", 20)

    def test_read_sources_formats_keys_and_close(self) -> None:
        _ = self.herdr.read("pane-1", 40, joined=False, ansi=False)
        _ = self.herdr.read("pane-1", 80, joined=True, ansi=True)
        self.herdr.send_keys("pane-1", "ctrl-c")
        self.herdr.close_pane("pane-1")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent", "--lines", "40"],
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent-unwrapped", "--lines", "80", "--format", "ansi"],
            ["--session", "fixture", "pane", "send-keys", "pane-1", "ctrl-c"],
            ["--session", "fixture", "pane", "close", "pane-1"],
        ])

    def test_moved_pane_resolves_single_and_batched_process_places_from_retained_shell(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "STAFF_ID": "unit-id"})
        _ = self.process(13, 12, ["worker"], {"HERDR_PANE_ID": "w1:p1"})
        for pane in ("w2:p9", "w1:pA"):
            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                {"pane_id": pane, "workspace_id": pane.split(":")[0]}]}}))
            self.configure_herdr(["pane", "process-info", "--pane", pane],
                                 json.dumps({"result": {"process_info": {"shell_pid": 11}}}))
            expected = {pid: herdr.HerdrPlace("fixture", pane) for pid in (11, 12, 13)}
            with self.subTest(pane=pane):
                self.assertEqual(herdr.herdr_place_of(12), expected[12])
                self.log.unlink()
                self.assertEqual(herdr.herdr_places(), expected)
                self.assertEqual(self.commands().count(["--session", "fixture", "pane", "list"]), 1)
                self.assertEqual(self.commands().count(
                    ["--session", "fixture", "pane", "process-info", "--pane", pane]), 1)
                self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
                self.assertEqual(next(row.marks for row in herdr.herdr_processes() if row.pid == 12),
                                 {"STAFF_ID": "unit-id"})

    def test_missing_live_pane_never_returns_the_launch_environment_place(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": []}}))
        self.assertEqual(herdr.herdr_place_of(11), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_places(), {})
        self.assertEqual(herdr.herdr_processes(), [])

    def test_unreadable_live_panes_refuse_place_reads_instead_of_reporting_absence(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        readers: tuple[Callable[[], object], ...] = (
            lambda: herdr.herdr_place_of(11), herdr.herdr_places, herdr.herdr_processes)
        for reply in ("not JSON", json.dumps({"result": {"panes": None}})):
            self.configure_herdr(["pane", "list"], reply)
            for read in readers:
                with self.subTest(reply=reply, reader=read), self.assertRaises(herdr.HerdrFailure):
                    _ = read()
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}):
            for read in readers:
                with self.subTest(reader=read), self.assertRaises(herdr.HerdrFailure):
                    _ = read()

    def test_darwin_unreadable_live_panes_refuse_place_reads(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 70 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.darwin_environment(71, "claude", "HERDR_PANE_ID=w1:p1")
        self.configure(["--session", "writing", "pane", "list"], "not JSON")
        readers: tuple[Callable[[], object], ...] = (
            lambda: herdr.herdr_place_of(71), herdr.herdr_places, herdr.herdr_processes)
        for read in readers:
            with self.subTest(reader=read), self.assertRaises(herdr.HerdrFailure):
                _ = read()

    def test_darwin_moved_pane_resolves_processes_from_the_retained_shell(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 1 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w2:p9", "workspace_id": "w2"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w2:p9"],
                       json.dumps({"result": {"process_info": {"shell_pid": 71}}}))
        for pid, command in ((71, "zsh"), (72, "claude")):
            self.darwin_environment(pid, command, "HERDR_PANE_ID=w1:p1")
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn{self.root}\n")
        expected = {pid: herdr.HerdrPlace("writing", "w2:p9") for pid in (71, 72)}
        self.assertEqual(herdr.herdr_places(), expected)
        self.assertEqual(self.commands().count(["--session", "writing", "pane", "list"]), 1)
        self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
        self.assertEqual(herdr.herdr_place_of(72), expected[72])

    def test_linux_user_tmux_is_never_attributed_to_its_outer_herdr_pane(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/tmux"], {"HERDR_PANE_ID": "outer"})
        _ = self.process(12, 11, ["/bin/claude"], {"HERDR_PANE_ID": "outer", "TMUX_PANE": "%1"})
        _ = self.process(13, 10, ["/bin/claude"], {"HERDR_PANE_ID": "native", "TMUX_PANE": ""})
        self.assertEqual(herdr.herdr_place_of(12), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(13), herdr.HerdrPlace("fixture", "native"))
        self.assertEqual([row.pid for row in herdr.herdr_processes() if row.command == "claude"], [13])

    def test_linux_deleted_cwd_preserves_the_process_marks_and_place(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        marks = {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}
        process = self.process(12, 10, ["/bin/claude"], {**marks, "HERDR_PANE_ID": "pane-1"})
        (process / "cwd").unlink()
        (process / "cwd").symlink_to(self.root / "deleted-directory")
        rows = herdr.herdr_processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].pid, rows[0].command, rows[0].place, rows[0].marks),
                         (12, "claude", herdr.HerdrPlace("fixture", "pane-1"), marks))
        self.assertIsInstance(rows[0].working_directory, herdr.UnreadableCwd)

    def test_process_place_named_default_and_nonherdr(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/shell"], {})
        _ = self.process(12, 11, ["/bin/sleep", "600"], {"HERDR_PANE_ID": "pane-1"})
        self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("fixture", "pane-1"))
        server = self.process(20, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/herdr.sock", 200)
        self.socket_fd(server, 200)
        _ = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-default"})
        self.assertEqual(herdr.herdr_place_of(21), herdr.HerdrPlace("default", "pane-default"))
        _ = self.process(30, 1, ["/bin/program", "server"], {"HERDR_PANE_ID": "pane-foreign"})
        self.assertEqual(herdr.herdr_place_of(30), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(11), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(99), herdr.NotInHerdr())

    def test_server_environment_finds_attach_and_handoff_servers_by_socket(self) -> None:
        commands = (["/fixture/herdr", "server"], ["/fixture/herdr", "--handoff", "replacement"])
        for offset, arguments in enumerate(commands):
            name = f"fixture{offset}"
            with self.subTest(arguments=arguments):
                environment = {"HERDR_SESSION": name, "CLAUDE_CONFIG_DIR": "/account",
                               "CLAUDE_CODE_REMOTE_MEMORY_DIR": "/memory", "VALUE": "one=two"}
                path = self.root / "config" / "herdr" / "sessions" / name / "herdr.sock"
                inode = 813310542 + offset * 2
                self.unix_socket(path, inode)
                self.unix_socket(path, inode + 1, listening=False)
                server = self.process(10 + offset, 1, arguments, environment)
                self.socket_fd(server, inode)
                self.socket_fd(server, inode, fd=4)
                # A connected client shares the path, but never the listener inode.
                client = self.process(20 + offset, 1, ["/fixture/herdr", "--session", name,
                    "session", "attach", name], {"CLAUDE_CONFIG_DIR": "/client-account"})
                self.socket_fd(client, inode + 1)
                # An argv match without the listening fd must not win discovery.
                _ = self.process(30 + offset, 1, ["/fixture/herdr", "--session", name, "server"],
                                 {"CLAUDE_CONFIG_DIR": "/old-server-account"})
                self.assertEqual(herdr.server_environment(name), environment)

    def test_server_environment_uses_root_socket_for_default_session(self) -> None:
        self.unix_socket(self.root / "config/herdr/herdr.sock", 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "default-server"})
        self.socket_fd(server, 100)
        self.unix_socket(self.root / "config/herdr/sessions/default/herdr.sock", 200)
        decoy = self.process(20, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "nested-decoy"})
        self.socket_fd(decoy, 200)
        self.assertEqual(herdr.server_environment("default"), {"ACCOUNT": "default-server"})

    def test_server_environment_uses_config_directory_with_spaces(self) -> None:
        path = self.root / "configuration with spaces/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "configured"})
        self.socket_fd(server, 100)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "configuration with spaces")}):
            self.assertEqual(herdr.server_environment("fixture"), {"ACCOUNT": "configured"})

    def test_server_environment_defaults_to_home_config_directory(self) -> None:
        path = self.root / "home/.config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "home"})
        self.socket_fd(server, 100)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(herdr.server_environment("fixture"), {"ACCOUNT": "home"})

    def test_server_environment_requires_a_listener_for_the_exact_session(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100, listening=False)
        self.unix_socket(self.root / "config/herdr/sessions/other/herdr.sock", 200)
        process = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.socket_fd(process, 100)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket, found 0"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_never_uses_a_connected_client_as_holder(self) -> None:
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        self.unix_socket(path, 101, listening=False)
        client = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "session", "attach", "fixture"], {})
        self.socket_fd(client, 101)
        _ = self.process(20, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket holder, found 0"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_refuses_two_listener_holders(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        first = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "first"})
        second = self.process(20, 1, ["/fixture/herdr", "--handoff"], {"ACCOUNT": "second"})
        self.socket_fd(first, 100)
        self.socket_fd(second, 100)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket holder, found 2"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_refuses_ambiguous_listeners(self) -> None:
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        self.unix_socket(path, 200)
        for pid, inode in ((10, 100), (20, 200)):
            process = self.process(pid, 1, ["/fixture/herdr", "server"], {})
            self.socket_fd(process, inode)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket, found 2"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_reports_unreadable_environment(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        process = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.socket_fd(process, 100)
        process.joinpath("environ").unlink()
        process.joinpath("environ").mkdir()
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*cannot read process environment"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_reports_unreadable_socket_table(self) -> None:
        self.proc.joinpath("net", "unix").mkdir(parents=True)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*cannot read listening sockets"):
            _ = herdr.server_environment("fixture")

    def test_process_snapshot_serves_roster_units_inventory_and_codex_from_one_scan(self) -> None:
        from scripts.message import roster
        from scripts.shutdown import inventory
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        unit = unit_membership("production", "unit")

        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(12, 10, ["/bin/claude"], {
            "HERDR_PANE_ID": "pane-unit", "SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "unit",
            "STAFF_ID": unit.id,
        })
        _ = self.process(13, 10, ["/bin/codex"], {"HERDR_PANE_ID": "pane-codex"})
        import unit_lookup as lookup

        def current_slug(slug: str) -> str:
            return slug
        live = lookup.LiveClaude("Unit", "session-unit", "/fixture/socket", 12)
        with (patch.object(herdr, "live_sessions", return_value=[{"name": "fixture"}]),
              patch.object(herdr.Herdr, "agents", return_value=[{
                  "agent": "codex", "pane_id": "pane-codex", "agent_status": "idle",
              }]),
              patch.object(lookup, "_claudes", return_value=(
                  {12: live}, lookup.UnreadAt({}, lookup.RecordsRead()))),
              patch.object(lookup, "_lookup_slug", side_effect=current_slug),
              patch.object(roster, "_process_account", return_value=roster.AccountLabel("codex2")),
              patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans):
            def read_callers() -> tuple[object, object, object]:
                return (roster._every_marked_unit(), inventory._unit_sessions((  # pyright: ignore[reportPrivateUsage]
                    inventory.ShowrunnerRegistration("runner", "production", str(self.root / "production.md")),
                )), roster._herdr_codex_panes("fixture"))  # pyright: ignore[reportPrivateUsage]

            expected = read_callers()
            self.assertEqual(scans.call_count, 3)
            scans.reset_mock()
            with herdr.process_snapshot():
                observed = read_callers()
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(observed, expected)
        self.assertIn("session-unit", cast(dict[str, object], observed[1]))
        self.assertEqual(cast(roster.Roster, observed[2]).entries[0].account, roster.AccountLabel("codex2"))

    def test_process_reads_outside_scope_and_after_scope_exit_are_fresh(self) -> None:
        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 2)
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                with herdr.process_snapshot():
                    self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 3)
            self.assertEqual(herdr.herdr_processes(), [])
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 5)

    def test_process_snapshot_isolates_mutable_results_and_next_scope_sees_changes(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        process = self.process(12, 10, ["/bin/claude"], {"HERDR_PANE_ID": "pane-unit", "SHOWRUNNER_UNIT_ID": "first"})
        with herdr.process_snapshot():
            rows = herdr.herdr_processes()
            expected = herdr.herdr_processes()
            rows[0].marks["SHOWRUNNER_UNIT_ID"] = "caller change"
            rows.clear()
            _ = process.joinpath("environ").write_bytes(b"HERDR_PANE_ID=pane-unit\0SHOWRUNNER_UNIT_ID=second\0")
            self.assertEqual(herdr.herdr_processes(), expected)
        with herdr.process_snapshot():
            self.assertEqual(herdr.herdr_processes()[0].marks["SHOWRUNNER_UNIT_ID"], "second")

    def test_process_snapshot_does_not_change_scan_failure_or_retry(self) -> None:
        with (patch.object(herdr, "_process_sessions", side_effect=[herdr.HerdrFailure("fixture scan failed"), {}]) as scans,
              herdr.process_snapshot()):
            with self.assertRaisesRegex(herdr.HerdrFailure, "fixture scan failed"):
                _ = herdr.herdr_processes()
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 2)
        with patch.object(herdr, "_proc_root", return_value=self.root / "missing") as roots:
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(roots.call_count, 1)

    def test_action_reads_retire_report_snapshots_and_ignore_nested_report_scopes(self) -> None:
        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                _ = herdr.herdr_processes()
                _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 1)
                with herdr.process_action():
                    with herdr.process_snapshot():
                        _ = herdr.herdr_processes()
                        _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 3)
                _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 4)

    def test_action_retires_nested_snapshot_in_a_copied_context(self) -> None:
        from contextvars import copy_context

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                _ = herdr.herdr_processes()
                copied = copy_context()
                nested = herdr.process_snapshot()
                copied.run(nested.__enter__)
                try:
                    _ = copied.run(herdr.herdr_processes)
                    self.assertEqual(scans.call_count, 1)
                    with herdr.process_action():
                        _ = herdr.herdr_processes()
                    _ = copied.run(herdr.herdr_processes)
                    self.assertEqual(scans.call_count, 3)
                finally:
                    _ = copied.run(nested.__exit__, None, None, None)

    def test_copied_context_scans_fresh_after_snapshot_scope_closes(self) -> None:
        from contextvars import copy_context

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                copied = copy_context()
                self.assertEqual(copied.run(herdr.herdr_processes), [])
                self.assertEqual(scans.call_count, 1)
            self.assertEqual(copied.run(herdr.herdr_processes), [])
            self.assertEqual(scans.call_count, 2)

    def test_process_snapshot_is_released_on_error_and_not_shared_with_another_thread(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with self.assertRaisesRegex(ValueError, "fixture scope failed"):
                with herdr.process_snapshot():
                    _ = herdr.herdr_processes()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        self.assertEqual(pool.submit(herdr.herdr_processes).result(), [])
                    _ = herdr.herdr_processes()
                    self.assertEqual(scans.call_count, 2)
                    raise ValueError("fixture scope failed")
            _ = herdr.herdr_processes()
            self.assertEqual(scans.call_count, 3)

    def test_processes_skip_unreadable_and_limit_marks(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(12, 10, ["/bin/claude", "--model", "fixture"], {"HERDR_PANE_ID": "pane-1", "SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit", "SECRET": "never returned"})
        unreadable = self.process(13, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        unreadable.joinpath("environ").unlink()
        unreadable.joinpath("environ").mkdir()
        self.assertEqual(herdr.herdr_processes(), [herdr.HerdrProcess(12, herdr.HerdrPlace("fixture", "pane-1"), {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.root))])

    def test_ancestry_loop_is_skipped_and_vanished_cwd_keeps_placement(self) -> None:
        _ = self.process(10, 11, ["/bin/sh"], {"HERDR_PANE_ID": "pane-1"})
        _ = self.process(11, 10, ["/bin/sh"], {})
        self.assertEqual(herdr.herdr_place_of(10), herdr.NotInHerdr())
        server = self.process(20, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/herdr.sock", 100)
        self.socket_fd(server, 100)
        process = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        process.joinpath("cwd").unlink()
        rows = herdr.herdr_processes()
        self.assertEqual([(row.pid, row.place) for row in rows],
                         [(21, herdr.HerdrPlace("default", "pane-2"))])
        self.assertIsInstance(rows[0].working_directory, herdr.UnreadableCwd)

    def test_trust_ignores_inherited_git_repository_and_configuration(self) -> None:
        requested = self.root / "requested"
        foreign = self.root / "foreign"
        requested.mkdir()
        foreign.mkdir()
        clean_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        clean_environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        for directory in (requested, foreign):
            result = subprocess.run([GIT_BINARY, "init", str(directory)], env=clean_environment,
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {
            str(foreign): {"hasTrustDialogAccepted": True},
            str(requested): {"hasTrustDialogAccepted": False},
        }}))
        hostile_environment = {
            "PATH": str(Path(GIT_BINARY).parent), "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign), "GIT_COMMON_DIR": str(foreign / ".git"),
            "GIT_CONFIG_GLOBAL": "/fixture/foreign-global", "GIT_CONFIG_SYSTEM": "/fixture/foreign-system",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
        }
        with patch.dict(os.environ, hostile_environment), patch.object(subprocess, "run", wraps=subprocess.run) as invoked:
            self.assertEqual(herdr.claude_trust(requested, config), herdr.Untrusted(requested, config, (requested,)))
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)
            environment = cast(dict[str, str], invoked.call_args.kwargs["env"])
        self.assertEqual({key: value for key, value in environment.items() if key.startswith("GIT_")},
                         {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def test_herdr_clients_and_their_children_keep_the_owning_session(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "owner", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/owner/herdr.sock", 100)
        self.socket_fd(server, 100)
        commands = [
            ["--session", "target", "agent", "prompt", "w2:p1", "server"],
            ["--session", "target", "server", "stop"],
            ["--session=target", "server", "reload-config"],
            ["--session", "target", "status", "server"],
        ]
        for offset, arguments in enumerate(commands):
            pid = 20 + offset * 2
            _ = self.process(pid, 10, ["/fixture/herdr", *arguments], {"HERDR_PANE_ID": "pane-owner"})
            _ = self.process(pid + 1, pid, ["/bin/child"], {
                "HERDR_PANE_ID": "pane-owner", "SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "unit",
            })
            with self.subTest(arguments=arguments):
                self.assertEqual(herdr.herdr_place_of(pid), herdr.HerdrPlace("owner", "pane-owner"))
                self.assertEqual(herdr.herdr_place_of(pid + 1), herdr.HerdrPlace("owner", "pane-owner"))
        processes = herdr.herdr_processes()
        self.assertEqual(len(processes), len(commands) * 2)
        self.assertTrue(all(process.place.session == "owner" for process in processes))
        self.assertEqual([process.marks for process in processes if process.command == "child"],
                         [{"SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "unit"}] * len(commands))

    def test_socket_ownership_decides_place_despite_conflicting_argv(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--handoff", "--session=other", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/owner/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-owner"})
        self.assertEqual(herdr.herdr_place_of(11), herdr.HerdrPlace("owner", "pane-owner"))

    def test_plain_calls_and_session_listing_translate_timeouts(self) -> None:
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 10)):
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.run("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.result("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr session list: no answer after 10 s"):
                _ = herdr.live_sessions()

    def test_trust_uses_explicit_config_and_common_repository(self) -> None:
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {"/repository": {"hasTrustDialogAccepted": True}}}))
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 0, "/repository/.git\n", "")):
            self.assertEqual(herdr.claude_trust(Path("/repository/worktree"), config),
                             herdr.Trusted(Path("/repository/worktree"), config, Path("/repository")))
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 1, "", "")):
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)

    def trust_git(self, folder: Path, root: Path, main: Path) -> None:
        def result(argv: list[str], timeout: float = 10.0, *,
                   environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = timeout, environment
            self.assertEqual(argv[2], str(folder))
            path = root if argv[-1] == "--show-toplevel" else main / ".git"
            return subprocess.CompletedProcess(argv, 0, str(path) + "\n", "")

        _ = cast(object, self.enterContext(patch.object(herdr, "run_program", side_effect=result)))

    def test_each_exact_folder_repository_root_and_main_key_trusts(self) -> None:
        folder = self.root / "worktree" / "nested"
        root = folder.parent
        main = self.root / "main"
        config = self.root / "claude.json"
        self.trust_git(folder, root, main)
        for key in (folder, root, main):
            with self.subTest(key=key):
                _ = config.write_text(json.dumps({"projects": {str(key): {"hasTrustDialogAccepted": True}}}))
                self.assertEqual(herdr.claude_trust(folder, config), herdr.Trusted(folder, config, key))

    def test_trust_copy_adds_only_the_source_grant_and_keeps_the_rest_of_the_target(self) -> None:
        folder = self.root / "worktree" / "nested"
        main = self.root / "main"
        self.trust_git(folder, folder.parent, main)
        source, target = self.root / "source.json", self.root / "target.json"
        _ = source.write_text(json.dumps({"projects": {str(main): {"hasTrustDialogAccepted": True},
                                                       str(self.root / "other"): {"hasTrustDialogAccepted": True}}}))
        _ = target.write_text(json.dumps({"theme": "dark", "projects": {str(main): {"history": ["kept"]}}}))
        self.assertEqual(herdr.copy_claude_trust(folder, source, target), herdr.Trusted(folder, target, main))
        self.assertEqual(json.loads(target.read_text()),
                         {"theme": "dark", "projects": {str(main): {"history": ["kept"], "hasTrustDialogAccepted": True}}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_trust_copy_never_adds_trust_the_source_lacks(self) -> None:
        folder = self.root / "worktree"
        main = self.root / "main"
        self.trust_git(folder, folder, main)
        source, target = self.root / "source.json", self.root / "target.json"
        _ = source.write_text(json.dumps({"projects": {}}))
        _ = target.write_text(json.dumps({"projects": {}}))
        self.assertIsInstance(herdr.copy_claude_trust(folder, source, target), herdr.Untrusted)
        self.assertEqual(json.loads(target.read_text()), {"projects": {}})

    def test_ancestor_trust_does_not_authorize_child_repository(self) -> None:
        folder = self.root / "worktree" / "nested"
        main = self.root / "main"
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        self.trust_git(folder, folder.parent, main)
        self.assertEqual(herdr.claude_trust(folder, config),
                         herdr.Untrusted(folder, config, (folder, folder.parent, main)))

    def test_only_boolean_true_accepts_project_trust(self) -> None:
        config = self.root / "claude.json"
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 1, "", "")):
            refused_values: tuple[object, ...] = (False, 1, "true", None, {}, [])
            for value in refused_values:
                with self.subTest(value=value):
                    _ = config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": value}}}))
                    self.assertEqual(herdr.claude_trust(self.root, config), herdr.Untrusted(self.root, config, (self.root,)))

    def test_missing_or_malformed_account_file_refuses_without_contents(self) -> None:
        config = self.root / "claude.json"
        self.assertEqual(herdr.claude_trust(self.root, config),
                         herdr.UnreadableTrustFile(self.root, config, "FileNotFoundError"))
        for contents, error_class in (("private malformed contents", "JSONDecodeError"),
                                      ("[]", "ValueError"), ('{"projects": []}', "ValueError")):
            with self.subTest(error_class=error_class, contents=contents):
                _ = config.write_text(contents)
                result = herdr.claude_trust(self.root, config)
                self.assertEqual(result, herdr.UnreadableTrustFile(self.root, config, error_class))
                self.assertEqual(str(result), f"cannot read {config} to check Claude trust ({error_class})")
                self.assertNotIn(contents, repr(result))
        with patch.object(Path, "read_text", side_effect=PermissionError("private exception detail")):
            result = herdr.claude_trust(self.root, config)
            self.assertEqual(str(result), f"cannot read {config} to check Claude trust (PermissionError)")
            self.assertNotIn("private exception detail", repr(result))
        _ = config.write_bytes(b"\xffprivate content")
        self.assertEqual(str(herdr.claude_trust(self.root, config)),
                         f"cannot read {config} to check Claude trust (UnicodeDecodeError)")

    def test_unreadable_account_file_identifies_the_claude_trust_check(self) -> None:
        config = self.root / "account file.json"
        self.assertEqual(str(herdr.UnreadableTrustFile(self.root, config, "PermissionError")),
                         f"cannot read {config} to check Claude trust (PermissionError)")

    def test_new_worktree_checks_main_checkout_not_trusted_showrunner_worktree(self) -> None:
        showrunner = self.root / "showrunner"
        main = self.root / "main"
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(showrunner): {"hasTrustDialogAccepted": True}}}))

        def result(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            path = Path(argv[2]) if argv[-1] == "--show-toplevel" else main / ".git"
            return subprocess.CompletedProcess(argv, 0, str(path) + "\n", "")

        with patch.object(herdr, "run_program", side_effect=result):
            checked_folder = herdr.repository_trust_key(showrunner)
            self.assertEqual(checked_folder, main)
            self.assertEqual(herdr.claude_trust(checked_folder, config), herdr.Untrusted(main, config, (main,)))

    def test_survey_counts_nested_repositories_and_submodules_but_skips_generated_trees(self) -> None:
        checkout = self.root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "nested" / ".git").mkdir(parents=True)
        submodule = checkout / "submodule"
        submodule.mkdir()
        _ = (submodule / ".git").write_text("gitdir: ../.git/modules/submodule\n")
        for generated in (".git", "target", "node_modules"):
            (checkout / generated / "ignored" / ".git").mkdir(parents=True)
        self.assertEqual(trust_survey.descendant_repositories(self.root), 3)
        self.assertEqual(trust_survey.descendant_repositories(checkout), 2)
        self.assertEqual(trust_survey.project_kind(self.root, self.root, self.root), "ancestor of 3 repos")


class LayoutHerdr(herdr.Herdr):
    """An in-memory herdr session: workspaces and tabs in order, one pane per tab."""

    def __init__(self) -> None:
        super().__init__("fixture")
        self.spaces: list[tuple[str, str]] = []
        self.tabs: list[dict[str, str]] = []
        self.count: int = 0

    def new_id(self, kind: str) -> str:
        self.count += 1
        return f"{kind}{self.count}"

    def add_tab(self, workspace: str, label: str) -> dict[str, object]:
        tab = {"tab_id": self.new_id("t"), "pane_id": self.new_id("p"), "workspace_id": workspace, "label": label}
        self.tabs.append(tab)
        return {"tab": {"tab_id": tab["tab_id"]}, "root_pane": {"pane_id": tab["pane_id"]}}

    def layout(self) -> list[tuple[str, list[str]]]:
        return [(label, [tab["label"] for tab in self.tabs if tab["workspace_id"] == ident]) for ident, label in self.spaces]

    @override
    def result(self, *arguments: str) -> dict[str, object]:
        words = list(arguments)
        match words[:2]:
            case ["workspace", "list"]:
                return {"workspaces": [{"workspace_id": ident, "label": label, "focused": False} for ident, label in self.spaces]}
            case ["workspace", "create"]:
                ident = self.new_id("w")
                self.spaces.append((ident, words[words.index("--label") + 1]))
                return self.add_tab(ident, words[words.index("--label") + 1])
            case ["tab", "create"]:
                return self.add_tab(words[words.index("--workspace") + 1], words[words.index("--label") + 1])
            case ["tab", "list"]:
                shown = [tab for tab in self.tabs if "--workspace" not in words or tab["workspace_id"] == words[-1]]
                return {"tabs": [{**tab, "pane_count": 1} for tab in shown]}
            case ["pane", "list"]:
                return {"panes": [{"pane_id": tab["pane_id"], "tab_id": tab["tab_id"], "workspace_id": tab["workspace_id"],
                                   "label": tab["label"]} for tab in self.tabs]}
            case ["pane", "move"]:
                tab = next(tab for tab in self.tabs if tab["pane_id"] == words[2])
                self.tabs.remove(tab)
                moved = {**tab, "tab_id": self.new_id("t"), "workspace_id": words[words.index("--workspace") + 1]}
                self.tabs.append(moved)
                return {"move_result": {"created_tab": {"tab_id": moved["tab_id"]}, "pane": {"pane_id": moved["pane_id"]}}}
            case _:
                raise AssertionError(f"unexpected herdr call {words}")

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        return herdr.PaneAtShell()

    @override
    def run(self, *arguments: str) -> str:
        words = list(arguments)
        match words[:2]:
            case ["tab", "rename"]:
                next(tab for tab in self.tabs if tab["tab_id"] == words[2])["label"] = words[3]
            case ["workspace", "close"]:
                self.spaces = [space for space in self.spaces if space[0] != words[2]]
                self.tabs = [tab for tab in self.tabs if tab["workspace_id"] != words[2]]
            case ["tab", "close"]:
                self.tabs = [tab for tab in self.tabs if tab["tab_id"] != words[2]]
            case ["pane", "rename"] | ["tab", "focus"] | ["workspace", "focus"]:
                pass
            case _:
                raise AssertionError(f"unexpected herdr call {words}")
        return "{}"


class TitledLayoutHerdr(LayoutHerdr):
    """Record tab and pane renames through the real layout code."""

    @override
    def result(self, *arguments: str) -> dict[str, object]:
        if arguments[:2] == ("agent", "list"):
            return {"agents": []}
        if arguments[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": tab["pane_id"], "tab_id": tab["tab_id"],
                               "workspace_id": tab["workspace_id"],
                               "label": tab.get("pane_label", tab["label"])} for tab in self.tabs]}
        return super().result(*arguments)

    @override
    def run(self, *arguments: str) -> str:
        if arguments[:2] == ("pane", "rename"):
            next(tab for tab in self.tabs if tab["pane_id"] == arguments[2])["pane_label"] = arguments[3]
            return "{}"
        return super().run(*arguments)


@final
class SavedLayoutTests(unittest.TestCase):
    """A restart puts every session back in its saved workspace and tab, in the saved order (user, 2026-10-21)."""

    BEFORE: list[tuple[str, list[str]]] = [
        ("ad-hoc", ["postmaster", "ups"]),
        ("cargo-liner", ["cargo-liner", "berth", "tile"]),
        ("hana", ["hana", "assembly", "widget"]),
        ("fps", ["performance", "benchmark", "frame-cost", "metrics-gathering"]),
        ("natedev", ["natedev", "accounts", "herdr", "unit-board"]),
        ("sdlc", ["sdlc", "roles"]),
        ("cargo-liner-style", ["cargo-liner-style", "names"]),
    ]

    def __init__(self, method: str = "runTest") -> None:
        super().__init__(method)
        self.root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = self.enterContext(patch.object(herdr, "herdr_processes", return_value=[]))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                                                    "HOME": str(self.root)})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))

    def running(self) -> LayoutHerdr:
        client = LayoutHerdr()
        for space, titles in self.BEFORE:
            for title in titles:
                _ = client.open_tab(space, title, "/repo", {})
        return client

    def test_session_names_restore_saved_tab_order_and_resolve_each_pane(self) -> None:
        old = TitledLayoutHerdr()
        for key in ("sdlc", "zulu", "alpha"):
            _ = old.open_tab("sdlc", key, "/repo", {})
        order = [tab["pane_label"] for tab in old.tabs]
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]], order)

        fresh = TitledLayoutHerdr()
        for key in reversed(order):
            _ = fresh.open_tab("sdlc", key, "/repo", {})
        self.assertEqual([tab["pane_label"] for tab in fresh.tabs], order)
        for tab in fresh.tabs:
            key = tab["pane_label"]
            self.assertEqual(fresh.lookup(key), herdr.PaneFound(tab["pane_id"]))
            self.assertEqual(fresh.lookup(tab["label"]), herdr.PaneFound(tab["pane_id"]))
            self.assertEqual(tab["label"], key)
            self.assertTrue(herdr.attach_hint("sdlc", key).endswith("tab " + key))
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]], order)

    def test_showrunner_unit_director_and_plain_tabs_and_panes_take_the_session_name(self) -> None:
        client = TitledLayoutHerdr()
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("control", "control", {}),
            ("control", "roles", {"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "roles-unit"}),
            ("ad-hoc", "scratch", {}))
        for space, name, environment in cases:
            with self.subTest(session=name):
                pane = client.open_tab(space, name, "/repo", environment)
                tab = next(tab for tab in client.tabs if tab["pane_id"] == pane)
                self.assertEqual(tab["label"], name)
                self.assertEqual(tab["pane_label"], name)
                for added in (" · ", "sdlc", "showrunner", "unit director"):
                    self.assertNotIn(added, tab["label"])

    def test_a_reboot_restart_in_any_order_brings_back_every_workspace_and_tab_in_place(self) -> None:
        self.assertEqual(self.running().layout(), self.BEFORE)
        launches = [(space, title) for space, titles in self.BEFORE for title in titles]
        for seed in range(5):
            with self.subTest(seed=seed):
                import random
                random.Random(seed).shuffle(launches)
                fresh = LayoutHerdr()
                fresh.prepare_restore_workspaces(frozenset(space for space, _ in self.BEFORE))
                for space, title in launches:
                    _ = fresh.open_tab(space, title, "/repo", {})
                self.assertEqual(fresh.layout(), self.BEFORE)

    def test_a_partial_restart_keeps_the_place_of_the_sessions_still_to_come(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"natedev", "hana"}))
        for space, title in (("natedev", "herdr"), ("hana", "widget"), ("natedev", "natedev")):
            _ = fresh.open_tab(space, title, "/repo", {})
        self.assertEqual([space for space, _ in fresh.layout()], ["hana", "natedev"])
        again = LayoutHerdr()
        again.prepare_restore_workspaces(frozenset(space for space, _ in self.BEFORE))
        for space, titles in reversed(self.BEFORE):
            for title in reversed(titles):
                _ = again.open_tab(space, title, "/repo", {})
        self.assertEqual(again.layout(), self.BEFORE)

    def test_failed_restore_removes_only_unfilled_shell_placeholders(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana", "natedev"}))
        _ = fresh.open_tab("natedev", "herdr", "/repo", {})
        fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("natedev", ["herdr"])])

    def test_placeholder_with_a_running_program_survives_cleanup(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana"}))
        with patch.object(fresh, "pane_activity", return_value=herdr.PaneRunning(("editor",))):
            fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("hana", [herdr.LAYOUT_PLACEHOLDER])])

    def test_late_session_renaming_a_placeholder_survives_cleanup(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana"}))
        def landed(_pane: str) -> herdr.PaneAtShell:
            fresh.tabs[0]["label"] = "late-session"
            return herdr.PaneAtShell()
        with patch.object(fresh, "pane_activity", side_effect=landed):
            fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("hana", ["late-session"])])

    def test_a_new_tab_takes_its_a_to_z_place_and_never_passes_the_first_tab(self) -> None:
        client = self.running()
        _ = client.open_tab("natedev", "builds", "/repo", {})
        _ = client.open_tab("fps", "alpha", "/repo", {})
        _ = client.open_tab("natedev", "zebra", "/repo", {})
        shown = dict(client.layout())
        self.assertEqual(shown["natedev"], ["natedev", "accounts", "builds", "herdr", "unit-board", "zebra"])
        self.assertEqual(shown["fps"], ["performance", "alpha", "benchmark", "frame-cost", "metrics-gathering"])

    def test_move_pane_to_workspace_orders_the_unit_tab_and_saves_its_destination(self) -> None:
        _ = unit_membership("target", "models")
        client = LayoutHerdr()
        _ = client.open_tab("source", "source", "/repo", {})
        pane = client.open_tab("source", "models", "/unit", {})
        _ = client.open_tab("target", "target", "/repo", {})
        _ = client.open_tab("target", "apple-terminal", "/repo", {})
        moved = client.move_pane_to_workspace(pane, "target", "models")
        self.assertEqual(client.layout(), [("source", ["source"]), ("target", ["target", "models", "apple-terminal"])])
        self.assertEqual(next(tab["workspace_id"] for tab in client.tabs if tab["pane_id"] == moved),
                         next(ident for ident, label in client.spaces if label == "target"))
        self.assertEqual([tab["name"] for space in herdr.saved_layout("fixture")["workspaces"]
                          if space["name"] == "target" for tab in space["tabs"]], ["target", "models", "apple-terminal"])

    def test_move_pane_to_workspace_does_not_create_a_missing_destination(self) -> None:
        client = LayoutHerdr()
        pane = client.open_tab("source", "source", "/repo", {})
        saved = herdr.layout_path("fixture").read_bytes()
        before = client.layout()
        with self.assertRaisesRegex(herdr.HerdrFailure, "no workspace named 'absent'"):
            _ = client.move_pane_to_workspace(pane, "absent", "models")
        self.assertEqual(client.layout(), before)
        self.assertEqual(herdr.layout_path("fixture").read_bytes(), saved)

    def test_the_snapshot_pass_saves_a_tab_renamed_in_herdr_only_for_a_running_shared_session(self) -> None:
        client = self.running()
        next(tab for tab in client.tabs if tab["label"] == "roles")["label"] = "design"
        with patch.object(herdr, "shared_session", return_value="fixture"), patch.object(herdr, "Herdr", return_value=client):
            herdr.save_shared_layout({"other"})
            self.assertNotIn("design", [tab["name"] for space in herdr.saved_layout("fixture")["workspaces"] for tab in space["tabs"]])
            herdr.save_shared_layout({"fixture", "other"})
        saved = {space["name"]: [tab["name"] for tab in space["tabs"]] for space in herdr.saved_layout("fixture")["workspaces"]}
        self.assertEqual(saved["sdlc"], ["sdlc", "roles", "design"])

    def test_without_a_saved_layout_the_showrunner_comes_first_then_a_to_z(self) -> None:
        self.assertEqual(herdr.tab_order("boss", ["delta", "boss", "alpha"], [], set()), [1, 2, 0])

    def test_unit_directors_sort_before_other_tabs_from_their_panes_marks(self) -> None:
        for name in ("mike", "zulu"):
            _ = unit_membership("boss", name)
        client = LayoutHerdr()
        client.spaces.append(("w1", "boss"))
        for title in ("apple", "zulu", "boss", "beta", "mike", "able", "aardvark"):
            _ = client.add_tab("w1", title)
        marks = {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "one"}
        processes = [herdr.HerdrProcess(index, herdr.HerdrPlace(session, tab["pane_id"]), environment, "claude", "/repo")
                     for index, (tab, session, environment) in enumerate((
                         (client.tabs[1], "fixture", marks), (client.tabs[4], "fixture", marks),
                         (client.tabs[5], "fixture", {"SHOWRUNNER_UNIT": "prod"}),
                         (client.tabs[6], "other", marks)))]
        with patch.object(herdr, "herdr_processes", return_value=processes):
            _ = client.order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[]))
        self.assertEqual(client.layout(), [("boss", ["boss", "mike", "zulu", "aardvark", "able", "apple", "beta"])])

    def test_new_unit_environment_sorts_before_terminals_before_its_process_starts(self) -> None:
        _ = unit_membership("boss", "zulu")
        client = LayoutHerdr()
        _ = client.open_tab("boss", "boss", "/repo", {})
        _ = client.open_tab("boss", "apple", "/repo", {})
        pane = client.open_tab("boss", "zulu", "/repo", {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "one"})
        self.assertEqual(client.layout(), [("boss", ["boss", "zulu", "apple"])])
        self.assertEqual(next(tab["label"] for tab in client.tabs if tab["pane_id"] == pane), "zulu")
        processes = [herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", pane),
                                       {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "one"}, "claude", "/repo")]
        with patch.object(herdr, "herdr_processes", return_value=processes):
            _ = client.open_tab("boss", "banana", "/repo", {})
            _ = client.open_tab("boss", "aardvark", "/repo", {"SHOWRUNNER_UNIT": "prod"})
        self.assertEqual(client.layout(), [("boss", ["boss", "zulu", "aardvark", "apple", "banana"])])

    def test_sort_tabs_uses_staff_identity_and_membership_after_hand_resume_or_transfer(self) -> None:
        client = LayoutHerdr()
        client.spaces.extend([("w1", "sdlc"), ("w2", "natedev")])
        for title in ("sdlc", "devtools", "roles", "style", "shutdown", "herdr", "a-terminal", "renamed-tab"):
            _ = client.add_tab("w1", title)
        _ = client.add_tab("w2", "natedev")
        for name in ("devtools", "roles", "style", "shutdown", "herdr", "pinned"):
            _ = unit_membership("sdlc", name)
        pinned = next(row for row in staff.snapshot().find(kind="unit") if row.name == "pinned")
        # Hand resumes have no marks; a transferred process can retain the old production mark.
        processes = [
            herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", client.tabs[7]["pane_id"]),
                               {"STAFF_ID": pinned.id, "SHOWRUNNER_UNIT": "natedev"}, "claude", "/repo"),
            herdr.HerdrProcess(2, herdr.HerdrPlace("fixture", client.tabs[6]["pane_id"]),
                               {"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "gone-unit"}, "claude", "/repo"),
        ]
        with patch.object(herdr, "herdr_processes", return_value=processes), \
                patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            _ = client.sort_tabs()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(client.layout(), [
            ("sdlc", ["sdlc", "devtools", "herdr", "renamed-tab", "roles", "shutdown", "style", "a-terminal"]),
            ("natedev", ["natedev"]),
        ])
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]],
                         client.layout()[0][1])

    def test_saved_tab_order_wins_over_unit_groups_and_alphabetical_order(self) -> None:
        titles = ["boss", "apple", "zulu", "mike"]
        saved = [herdr.SavedName(name=title, seen=100.0) for title in ("boss", "zulu", "apple", "mike")]
        order = herdr.tab_order("boss", titles, saved, {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["boss", "zulu", "apple", "mike"])

    def test_new_units_and_terminals_take_their_places_after_the_first_saved_tab(self) -> None:
        titles = ["performance", "zulu", "apple", "mike", "banana"]
        saved = [herdr.SavedName(name=title, seen=100.0) for title in titles[:3]]
        order = herdr.tab_order("fps", titles, saved, {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["performance", "mike", "zulu", "apple", "banana"])

    def test_sort_without_saved_order_keeps_a_first_tab_whose_title_differs_from_its_workspace(self) -> None:
        titles = ["performance", "apple", "zulu", "mike"]
        order = herdr.tab_order("fps", titles, [], {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["performance", "mike", "zulu", "apple"])

    def test_a_gone_entry_keeps_its_place_until_it_is_old(self) -> None:
        saved = [herdr.SavedName(name="a", seen=100.0), herdr.SavedName(name="b", seen=100.0),
                 herdr.SavedName(name="c", seen=100.0)]
        self.assertEqual([entry["name"] for entry in herdr.merged_order(saved, ["c", "d"], 200.0)], ["a", "b", "c", "d"])
        late = 100.0 + herdr.LAYOUT_KEEP_SECONDS + 1
        self.assertEqual([entry["name"] for entry in herdr.merged_order(saved, ["c"], late)], ["c"])

    def test_an_unreadable_layout_file_is_an_empty_layout(self) -> None:
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True)
        _ = path.write_text("{not json")
        self.assertEqual(herdr.saved_layout("fixture"), herdr.SavedLayout(at=0.0, workspaces=[]))


if __name__ == "__main__":
    _ = unittest.main()
