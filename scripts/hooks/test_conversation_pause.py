"""Conversation pauses isolate reports until a quiet session chooses to return them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

import conversation_pause


LIBRARY = Path(__file__).with_name("conversation_pause.py")
PROMPT_HOOK = Path(__file__).with_name("user-prompt-submit-conversation-pause.py")
STOP_HOOK = Path(__file__).with_name("stop-conversation-pause.py")
SESSION = "session-1"
NOW = 10_000
QUESTION = (
    "conversation-pause: the user has been quiet here for 5 minutes. Ask them this, "
    "word for word, and nothing else: Return to automatic updates? (yes / no) They "
    "return on their own in 5 minutes. Their yes or no is handled when they type it."
)
RESUME = (
    '"$HOME/.claude/scripts/lib/py" '
    '"$HOME/.claude/scripts/hooks/conversation_pause.py" resume'
)
PAUSE_CONTEXT = (
    "Automatic updates for this session are paused while the user talks to you: "
    "dailies, footer. Leave them off; the session asks the user before they return. "
    f"If the user asks for them back sooner, run: {RESUME}"
)
OTHER_ANSWER_CONTEXT = (
    'You asked the user "Return to automatic updates? (yes / no)" and they wrote '
    "something else. If their message answers that question, run "
    f"{RESUME} for yes or "
    '"$HOME/.claude/scripts/lib/py" '
    '"$HOME/.claude/scripts/hooks/conversation_pause.py" keep for no. Otherwise '
    "answer them and say nothing of the question; it comes back when they go quiet."
)
NO_CONTEXT = (
    'The user answered no to "Return to automatic updates?". They stay off until '
    f"the user asks for them; then run: {RESUME}. Confirm it in one line. That no "
    "answers only this question."
)


def cross_session(sender: str, text: str) -> str:
    return (
        f'<cross-session-message from="uds:/tmp/source.sock" from-name="{sender}" '
        f'from-mode="default">\n{text}\n</cross-session-message>'
    )


class ConversationPauseTests(unittest.TestCase):
    root: Path = Path()
    notifier_root: Path = Path()
    pause_root: Path = Path()
    showrunner_root: Path = Path()
    notifier_log: Path = Path()
    sessions_log: Path = Path()
    send_log: Path = Path()
    notifier_stub: Path = Path()
    sessions_stub: Path = Path()
    send_stub: Path = Path()
    socket_path: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.notifier_root = self.root / "notifier"
        self.notifier_root.mkdir()
        self.pause_root = self.root / "conversation-pause"
        self.showrunner_root = self.root / "showrunner"
        self.notifier_log = self.root / "notifier.args"
        self.sessions_log = self.root / "sessions.args"
        self.send_log = self.root / "send.args"
        self.socket_path = self.root / "session.sock"
        self.notifier_stub = self.root / "notifier.py"
        _ = self.notifier_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
with Path(os.environ["NOTIFIER_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
action, name = args[:2]
root = Path(os.environ["NOTIFIER_STATE_DIR"])
instance = root / name
if action in {"stop", "resume"} and instance.exists():
    state = instance / "state"
    enabled = "0" if action == "stop" else "1"
    lines = state.read_text().splitlines()
    state.write_text("\\n".join(
        f"ENABLED={enabled}" if line.startswith("ENABLED=") else line
        for line in lines
    ) + "\\n")
elif action == "new":
    instance.mkdir(parents=True, exist_ok=True)
    command = args[args.index("--run") + 1]
    (instance / "conf").write_text(f"EVERY=1\\nRUN={command}\\n")
    (instance / "state").write_text("ENABLED=1\\nNEXT_DUE=1\\n")
elif action == "remove":
    shutil.rmtree(instance, ignore_errors=True)
"""
        )
        self.notifier_stub.chmod(0o755)
        self.sessions_stub = self.root / "sessions.py"
        _ = self.sessions_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with Path(os.environ["SESSIONS_LOG"]).open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("SESSION_RUNNING", "1") != "0":
    print(os.environ["SESSION_SOCKET"])
else:
    sys.exit(1)
"""
        )
        self.sessions_stub.chmod(0o755)
        self.send_stub = self.root / "send.py"
        _ = self.send_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with Path(os.environ["SEND_LOG"]).open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAIL_SEND") == "1":
    print("delivery failed", file=sys.stderr)
    sys.exit(7)
"""
        )
        self.send_stub.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "CLAUDE_CODE_SESSION_ID": SESSION,
            "NOTIFIER_STATE_DIR": str(self.notifier_root),
            "SHOWRUNNER_STATE_DIR": str(self.showrunner_root),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.pause_root),
            "CONVERSATION_PAUSE_NOTIFIER": str(self.notifier_stub),
            "CONVERSATION_PAUSE_SESSIONS": str(self.sessions_stub),
            "CONVERSATION_PAUSE_SEND": str(self.send_stub),
            "CONVERSATION_PAUSE_NOW_EPOCH": str(NOW),
            "NOTIFIER_LOG": str(self.notifier_log),
            "SESSIONS_LOG": str(self.sessions_log),
            "SEND_LOG": str(self.send_log),
            "SESSION_SOCKET": str(self.socket_path),
            "SESSION_RUNNING": "1",
        }

    def create_instance(
        self,
        name: str,
        *,
        session_id: str = SESSION,
        enabled: bool = True,
        sender: str = "",
        run: str = "",
    ) -> Path:
        instance = self.notifier_root / name
        instance.mkdir()
        if run:
            conf = f"EVERY=1\nRUN={run}\n"
        else:
            conf = f"TARGET=session:{session_id}\nEVERY=5\nCOMMAND=report\n"
            if sender:
                conf += f"FROM={sender}\n"
        _ = (instance / "conf").write_text(conf)
        _ = (instance / "state").write_text(
            f"ENABLED={int(enabled)}\nNEXT_DUE=12000\n"
        )
        return instance

    def set_enabled(self, instance: Path, enabled: bool) -> None:
        _ = (instance / "state").write_text(
            f"ENABLED={int(enabled)}\nNEXT_DUE=12000\n"
        )

    def footer_off(self, slug: str) -> Path:
        switch = self.showrunner_root / "footers-off" / slug
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.touch()
        return switch

    def write_record(
        self,
        phase: dict[str, object],
        *,
        instances: tuple[str, ...] = (),
        footers: tuple[str, ...] = (),
        session_id: str = SESSION,
    ) -> Path:
        self.pause_root.mkdir(parents=True, exist_ok=True)
        path = self.pause_root / f"{session_id}.json"
        _ = path.write_text(json.dumps({
            "session_id": session_id,
            "instances": list(instances),
            "footers": list(footers),
            "phase": phase,
        }))
        return path

    def record(self, session_id: str = SESSION) -> dict[str, object]:
        return cast(
            dict[str, object],
            json.loads((self.pause_root / f"{session_id}.json").read_text()),
        )

    def calls(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()]

    def run_prompt(
        self,
        prompt: str,
        *,
        now: int = NOW,
        extra: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION, "prompt": prompt}
        if extra is not None:
            payload.update(extra)
        return subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            env={**self.environment, "CONVERSATION_PAUSE_NOW_EPOCH": str(now)},
            timeout=10,
        )

    def run_stop(
        self,
        *,
        now: int = NOW,
        extra: dict[str, object] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION}
        if extra is not None:
            payload.update(extra)
        return subprocess.run(
            [sys.executable, str(STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            env={**self.environment, "CONVERSATION_PAUSE_NOW_EPOCH": str(now)},
            timeout=10,
        )

    def run_cli(
        self,
        *args: str,
        now: int = NOW,
        session_id: str = SESSION,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **self.environment,
            "CLAUDE_CODE_SESSION_ID": session_id,
            "CONVERSATION_PAUSE_NOW_EPOCH": str(now),
        }
        if extra_env is not None:
            environment.update(extra_env)
        return subprocess.run(
            [sys.executable, str(LIBRARY), *args],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=10,
        )

    def parsed_reply(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return cast(dict[str, object], json.loads(result.stdout))

    def test_prompt_source_rows(self) -> None:
        def unexpected_sender_read() -> frozenset[str]:
            self.fail("sender files were read for a non-cross-session prompt")

        typed = (
            "hello",
            "   hello",
            "<3 thanks",
            "/unit:report",
            "/compact",
            "From the user (via the showrunner): go on",
        )
        for prompt in typed:
            with self.subTest(prompt=prompt):
                self.assertIs(
                    conversation_pause.prompt_source(prompt, unexpected_sender_read),
                    conversation_pause.PromptSource.TYPED,
                )
        self.assertIs(
            conversation_pause.prompt_source(cross_session("natedev", "go"), frozenset),
            conversation_pause.PromptSource.PEER,
        )
        for sender in ("stall-watch", "mac-test", "disk_floor"):
            with self.subTest(sender=sender):
                self.assertIs(
                    conversation_pause.prompt_source(cross_session(sender, "go"), frozenset),
                    conversation_pause.PromptSource.SCHEDULED,
                )
        self.assertIs(
            conversation_pause.prompt_source(
                cross_session("delegate-abc", "go"),
                lambda: frozenset({"delegate-abc"}),
            ),
            conversation_pause.PromptSource.SCHEDULED,
        )
        for prompt in (
            "<task-notification>done</task-notification>",
            '<agent-message from="a1">done</agent-message>',
            "",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(
                    conversation_pause.prompt_source(prompt, unexpected_sender_read),
                    conversation_pause.PromptSource.NOTICE,
                )

    def test_pause_policy_for_each_source_and_session_kind(self) -> None:
        source = conversation_pause.PromptSource
        self.assertTrue(conversation_pause.pauses(source.PEER, False))
        self.assertFalse(conversation_pause.pauses(source.PEER, True))
        self.assertTrue(conversation_pause.pauses(source.TYPED, False))
        self.assertTrue(conversation_pause.pauses(source.TYPED, True))
        for showrunner_session in (False, True):
            self.assertFalse(
                conversation_pause.pauses(source.SCHEDULED, showrunner_session)
            )
            self.assertFalse(
                conversation_pause.pauses(source.NOTICE, showrunner_session)
            )

    def test_typed_showrunner_prompt_pauses_dailies_and_footer_once(self) -> None:
        instance = self.create_instance("showrunner-demo")
        first = self.parsed_reply(self.run_prompt("hello", now=100))
        self.assertEqual(first, {
            "systemMessage": "Automatic updates paused while we talk: dailies, footer.",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": PAUSE_CONTEXT,
            },
        })
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((self.showrunner_root / "footers-off" / "demo").exists())
        self.assertEqual(self.record(), {
            "session_id": SESSION,
            "instances": ["showrunner-demo"],
            "footers": ["demo"],
            "phase": {"kind": "talking", "user_wrote_at": 100, "answered_at": None},
        })
        calls_after_first = self.calls(self.notifier_log)

        second = self.run_prompt("another thought", now=120)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")
        self.assertEqual(self.calls(self.notifier_log), calls_after_first)
        phase = cast(dict[str, object], self.record()["phase"])
        self.assertEqual(
            phase,
            {"kind": "talking", "user_wrote_at": 120, "answered_at": None},
        )

    def test_report_names_for_unit_and_build_instances(self) -> None:
        for name, words in (
            ("delegate-abc", "status reports"),
            ("report-builds", "build report"),
        ):
            with self.subTest(name=name):
                instance = self.create_instance(name)
                reply = self.parsed_reply(self.run_prompt("hello"))
                self.assertEqual(
                    reply["systemMessage"],
                    f"Automatic updates paused while we talk: {words}.",
                )
                (self.pause_root / f"{SESSION}.json").unlink()
                (instance / "conf").unlink()
                (instance / "state").unlink()
                instance.rmdir()
                watcher = self.notifier_root / "conversation-pause"
                if watcher.exists():
                    for child in watcher.iterdir():
                        child.unlink()
                    watcher.rmdir()

    def test_stopped_other_session_and_run_instances_are_untouched(self) -> None:
        stopped = self.create_instance("delegate-stopped", enabled=False)
        other = self.create_instance("delegate-other", session_id="other")
        run_only = self.create_instance("job", run="/bin/true")
        result = self.run_prompt("hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertTrue((stopped / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((other / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((run_only / "state").read_text().startswith("ENABLED=1\n"))
        resumed = self.run_cli("resume")
        self.assertEqual(resumed.stdout, "nothing was paused\n")
        self.assertTrue((stopped / "state").read_text().startswith("ENABLED=0\n"))

    def test_existing_pause_adds_a_report_that_was_turned_on_later(self) -> None:
        first = self.create_instance("delegate-abc")
        _ = self.parsed_reply(self.run_prompt("first", now=100))
        build = self.create_instance("report-builds")
        reply = self.parsed_reply(self.run_prompt("second", now=120))
        self.assertEqual(
            reply["systemMessage"],
            "Automatic updates paused while we talk: build report.",
        )
        self.assertEqual(
            self.record()["instances"], ["delegate-abc", "report-builds"]
        )
        self.assertTrue((first / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((build / "state").read_text().startswith("ENABLED=0\n"))

    def test_peer_prompt_does_not_pause_a_showrunner_session(self) -> None:
        instance = self.create_instance("showrunner-demo")
        result = self.run_prompt(cross_session("natedev", "continue"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_scheduled_notice_agent_and_typed_slash_prompts(self) -> None:
        instance = self.create_instance(
            "delegate-abc", sender="delegate-abc"
        )
        scheduled = self.run_prompt(cross_session("delegate-abc", "/unit:report"))
        self.assertEqual((scheduled.returncode, scheduled.stdout, scheduled.stderr), (0, "", ""))
        notice = self.run_prompt("<task-notification>done</task-notification>")
        self.assertEqual((notice.returncode, notice.stdout, notice.stderr), (0, "", ""))
        agent = self.run_prompt("hello", extra={"agent_id": "a1"})
        self.assertEqual((agent.returncode, agent.stdout, agent.stderr), (0, "", ""))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

        slash = self.parsed_reply(self.run_prompt("/unit:report"))
        self.assertEqual(
            slash["systemMessage"],
            "Automatic updates paused while we talk: status reports.",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_session_without_reports_has_no_record_or_output(self) -> None:
        result = self.run_prompt("hello")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_prompt_hook_invalid_json_reports_one_line_and_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("conversation-pause: JSONDecodeError:"))

    def test_cli_resume_skips_missing_instance_restores_footer_and_deletes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        switch = self.footer_off("demo")
        record_path = self.write_record(
            {"kind": "talking", "user_wrote_at": 1, "answered_at": None},
            instances=("showrunner-demo", "delegate-gone"),
            footers=("demo",),
        )
        result = self.run_cli("resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "automatic updates on: dailies, footer\n")
        self.assertEqual(self.calls(self.notifier_log), [["resume", "showrunner-demo"]])
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())
        self.assertFalse(record_path.exists())

    def test_cli_status_and_keep(self) -> None:
        self.assertEqual(self.run_cli("status").stdout, "not paused\n")
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 1, "answered_at": None},
            instances=("delegate-abc", "showrunner-demo"),
            footers=("demo",),
        )
        self.assertEqual(
            self.run_cli("status").stdout,
            "paused: dailies, footer, status reports (talking)\n",
        )
        kept = self.run_cli("keep")
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertEqual(kept.stdout, "automatic updates stay off\n")
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})

    def test_cli_usage_and_session_requirements(self) -> None:
        bad = self.run_cli("other")
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(
            bad.stderr,
            "usage: conversation_pause.py status|resume|keep|tick\n",
        )
        for action in ("status", "resume", "keep"):
            with self.subTest(action=action):
                result = self.run_cli(action, session_id="")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "no session\n")

    def test_stop_hook_sets_answered_time_once(self) -> None:
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 50, "answered_at": None},
            instances=("delegate-abc",),
        )
        first = self.run_stop(now=100)
        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 50, "answered_at": 100},
        )
        second = self.run_stop(now=200)
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 50, "answered_at": 100},
        )

    def test_stop_hook_skips_absent_record_and_agent_payload(self) -> None:
        absent = self.run_stop()
        self.assertEqual((absent.returncode, absent.stdout, absent.stderr), (0, "", ""))
        self.assertFalse(self.pause_root.exists())
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 50, "answered_at": None}
        )
        before = self.record()
        agent = self.run_stop(extra={"agent_id": "a1"})
        self.assertEqual((agent.returncode, agent.stdout, agent.stderr), (0, "", ""))
        self.assertEqual(self.record(), before)

    def test_tick_waits_for_five_quiet_minutes_and_sends_question_once(self) -> None:
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 10, "answered_at": 100},
            instances=("delegate-abc",),
        )
        before = self.run_cli("tick", now=399)
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(self.calls(self.send_log), [])
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 10, "answered_at": 100},
        )

        at_five = self.run_cli("tick", now=400)
        self.assertEqual(at_five.returncode, 0, at_five.stderr)
        self.assertEqual(self.record()["phase"], {"kind": "asked", "asked_at": 400})
        self.assertEqual(self.calls(self.sessions_log)[-1], ["socket", f"session:{SESSION}"])
        self.assertEqual(self.calls(self.send_log), [[
            "--to", f"uds:{self.socket_path}",
            "--from", "conversation-pause",
            "--key", f"conversation-pause-{SESSION}",
            "--text", QUESTION,
        ]])
        after = self.run_cli("tick", now=450)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_tick_waits_thirty_minutes_for_a_reply_that_never_ends(self) -> None:
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 10, "answered_at": None},
            instances=("delegate-abc",),
        )
        result = self.run_cli("tick", now=1_809)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(self.send_log), [])
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 10, "answered_at": None},
        )

        result = self.run_cli("tick", now=1_810)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls(self.send_log)), 1)
        self.assertEqual(self.record()["phase"], {"kind": "asked", "asked_at": 1_810})

    def test_asked_yes_variants_resume(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("Yes.", " y "):
            with self.subTest(prompt=prompt):
                _ = self.write_record(
                    {"kind": "asked", "asked_at": 100},
                    instances=("delegate-abc",),
                )
                self.set_enabled(instance, False)
                self.notifier_log.unlink(missing_ok=True)
                reply = self.parsed_reply(self.run_prompt(prompt))
                self.assertEqual(reply, {
                    "systemMessage": "Automatic updates are back on: status reports.",
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": (
                            'The user answered yes to "Return to automatic updates?". '
                            "They are back on: status reports. Confirm it in one line. "
                            "That yes answers only this question."
                        ),
                    },
                })
                self.assertEqual(
                    self.calls(self.notifier_log), [["resume", "delegate-abc"]]
                )
                self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_asked_no_keeps_updates_off(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.write_record(
            {"kind": "asked", "asked_at": 100}, instances=("delegate-abc",)
        )
        reply = self.parsed_reply(self.run_prompt("NO!"))
        self.assertEqual(reply, {
            "systemMessage": "Automatic updates stay off.",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": NO_CONTEXT,
            },
        })
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_asked_other_and_peer_yes_restart_talking(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("later", cross_session("natedev", "yes")):
            with self.subTest(prompt=prompt):
                _ = self.write_record(
                    {"kind": "asked", "asked_at": 100},
                    instances=("delegate-abc",),
                )
                self.set_enabled(instance, False)
                result = self.run_prompt(prompt, now=200)
                if prompt == "later":
                    reply = self.parsed_reply(result)
                    self.assertNotIn("systemMessage", reply)
                    output = cast(dict[str, object], reply["hookSpecificOutput"])
                    self.assertEqual(output["additionalContext"], OTHER_ANSWER_CONTEXT)
                else:
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                self.assertEqual(
                    self.record()["phase"],
                    {"kind": "talking", "user_wrote_at": 200, "answered_at": None},
                )
                self.assertTrue(
                    (instance / "state").read_text().startswith("ENABLED=0\n")
                )

    def test_returned_yes_deletes_record_without_pausing_again(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": NOW - 60})
        reply = self.parsed_reply(self.run_prompt("yes"))
        self.assertNotIn("systemMessage", reply)
        output = cast(dict[str, object], reply["hookSpecificOutput"])
        self.assertEqual(
            output["additionalContext"],
            "Automatic updates already returned on their own. Tell the user that in one line.",
        )
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertEqual(self.calls(self.notifier_log), [])

    def test_returned_no_pauses_again_and_keeps_updates_off(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": NOW - 60})
        reply = self.parsed_reply(self.run_prompt("no"))
        self.assertEqual(reply, {
            "systemMessage": "Automatic updates are off again: status reports.",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": NO_CONTEXT,
            },
        })
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_returned_other_starts_a_new_pause(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": 100})
        reply = self.parsed_reply(self.run_prompt("new topic", now=200))
        self.assertEqual(
            reply["systemMessage"],
            "Automatic updates paused while we talk: status reports.",
        )
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 200, "answered_at": None},
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_question_timeout_returns_updates_and_late_yes_removes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        unit_report = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        switch = self.footer_off("demo")
        _ = self.write_record(
            {"kind": "asked", "asked_at": 100},
            instances=("showrunner-demo", "delegate-abc"),
            footers=("demo",),
        )
        tick = self.run_cli("tick", now=400)
        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(
            self.record(),
            {
                "session_id": SESSION,
                "instances": [],
                "footers": [],
                "phase": {"kind": "returned", "returned_at": 400},
            },
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((unit_report / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())
        self.assertIn(["resume", "showrunner-demo"], self.calls(self.notifier_log))
        self.assertIn(["resume", "delegate-abc"], self.calls(self.notifier_log))
        self.assertIn(["remove", "conversation-pause"], self.calls(self.notifier_log))
        self.assertFalse(watcher.exists())

        late = self.parsed_reply(self.run_prompt("yes", now=410))
        self.assertNotIn("systemMessage", late)
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((unit_report / "state").read_text().startswith("ENABLED=1\n"))

    def test_late_no_after_timeout_pauses_and_marks_kept_off(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": 400})
        reply = self.parsed_reply(self.run_prompt("no", now=410))
        self.assertEqual(
            reply["systemMessage"],
            "Automatic updates are off again: status reports.",
        )
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_kept_off_survives_tick_then_typed_message_starts_talking(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.write_record(
            {"kind": "kept_off"}, instances=("delegate-abc",)
        )
        tick = self.run_cli("tick", now=100_000)
        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

        prompt = self.run_prompt("another topic", now=100_001)
        self.assertEqual((prompt.returncode, prompt.stdout, prompt.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 100_001, "answered_at": None},
        )

    def test_missing_session_returns_updates_and_deletes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        switch = self.footer_off("demo")
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 10, "answered_at": None},
            instances=("showrunner-demo",),
            footers=("demo",),
        )
        result = self.run_cli(
            "tick", now=20, extra_env={"SESSION_RUNNING": "0"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())

    def test_first_pause_creates_one_watcher_with_exact_arguments(self) -> None:
        _ = self.create_instance("delegate-abc")
        first = self.run_prompt("hello")
        self.assertEqual(first.returncode, 0, first.stderr)
        command = (
            f"{self.root}/.claude/scripts/lib/py "
            f"{self.root}/.claude/scripts/hooks/conversation_pause.py tick"
        )
        self.assertIn(
            ["new", "conversation-pause", "--every", "1", "--run", command],
            self.calls(self.notifier_log),
        )
        second = self.run_prompt("again", now=NOW + 1)
        self.assertEqual(second.returncode, 0, second.stderr)
        watcher_creates = [
            call for call in self.calls(self.notifier_log)
            if call[:2] == ["new", "conversation-pause"]
        ]
        self.assertEqual(len(watcher_creates), 1)

    def test_tick_removes_watcher_without_talking_or_asked_record(self) -> None:
        watcher = self.create_instance("conversation-pause", run="tick")
        result = self.run_cli("tick")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            ["remove", "conversation-pause"], self.calls(self.notifier_log)
        )
        self.assertFalse(watcher.exists())

    def test_failed_question_send_leaves_asked_and_exits_zero(self) -> None:
        _ = self.write_record(
            {"kind": "talking", "user_wrote_at": 10, "answered_at": 100}
        )
        result = self.run_cli("tick", now=400, extra_env={"FAIL_SEND": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.record()["phase"], {"kind": "asked", "asked_at": 400})
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_returned_record_expires_after_five_minutes(self) -> None:
        path = self.write_record({"kind": "returned", "returned_at": 100})
        result = self.run_cli("tick", now=399)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.exists())
        result = self.run_cli("tick", now=400)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())

    def test_failed_session_lookup_leaves_the_pause_alone(self) -> None:
        failing = self.root / "sessions-fail.py"
        _ = failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
        failing.chmod(0o755)
        instance = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        phase: dict[str, object] = {"kind": "talking", "user_wrote_at": 10, "answered_at": 20}
        _ = self.write_record(phase, instances=("delegate-abc",))
        result = self.run_cli(
            "tick", now=10_000, extra_env={"CONVERSATION_PAUSE_SESSIONS": str(failing)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record()["phase"], phase)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue(watcher.exists())
        self.assertEqual(self.calls(self.notifier_log), [])

    def test_unreadable_record_does_not_block_another_session(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        self.pause_root.mkdir(parents=True, exist_ok=True)
        _ = (self.pause_root / "a-broken.json").write_text("{not json")
        _ = self.write_record({"kind": "asked", "asked_at": 100}, instances=("delegate-abc",))
        result = self.run_cli("tick", now=400)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conversation-pause: a-broken.json:", result.stderr)
        self.assertEqual(self.record()["phase"], {"kind": "returned", "returned_at": 400})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_yes_with_nothing_left_to_turn_on_reads_as_a_full_sentence(self) -> None:
        _ = self.write_record({"kind": "asked", "asked_at": 100}, instances=("delegate-gone",))
        reply = self.parsed_reply(self.run_prompt("yes", now=200))
        self.assertEqual(reply["systemMessage"], "Automatic updates are back on.")
        output = cast(dict[str, object], reply["hookSpecificOutput"])
        self.assertIn(
            "They are back on. Confirm it in one line.", cast(str, output["additionalContext"])
        )
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_bare_yes_after_the_late_window_is_plain_typing(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": 100})
        reply = self.parsed_reply(self.run_prompt("yes", now=400))
        self.assertEqual(
            reply["systemMessage"], "Automatic updates paused while we talk: status reports."
        )
        self.assertEqual(
            self.record()["phase"],
            {"kind": "talking", "user_wrote_at": 400, "answered_at": None},
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_status_after_an_automatic_return_reads_not_paused(self) -> None:
        _ = self.write_record({"kind": "returned", "returned_at": 100})
        result = self.run_cli("status")
        self.assertEqual((result.returncode, result.stdout), (0, "not paused (returned)\n"))


if __name__ == "__main__":
    _ = unittest.main()
