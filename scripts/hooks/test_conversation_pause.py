"""Conversation pauses isolate reports until a quiet session chooses to return them."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conversation_pause


LIBRARY = Path(__file__).with_name("conversation_pause.py")
PROMPT_HOOK = Path(__file__).with_name("user-prompt-submit-conversation-pause.py")
STOP_HOOK = Path(__file__).with_name("stop-conversation-pause.py")
SETTINGS = LIBRARY.parent.parent.parent / "settings.json"
SESSION = "session-1"
NOW = 10_000
QUESTION = (
    "conversation-pause: the user has been quiet here for 15 minutes. Ask them this, "
    "word for word, and nothing else: Return to automatic updates? (yes / no) They "
    "return on their own in 5 minutes. Their yes or no is handled when they type it."
)
RESUME = (
    '"$HOME/.claude/scripts/lib/py" '
    '"$HOME/.claude/scripts/hooks/conversation_pause.py" resume'
)
RETURN_SCHEDULE = "They return 20 minutes after my last reply; I ask you first at 15."
ASK_AGAIN = "If you write again, I ask about them 15 minutes after my reply."
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


class ConversationPauseRegistrationTests(unittest.TestCase):
    def test_settings_registers_hooks_and_permission_once(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])

        def registered_commands(groups: list[dict[str, object]]) -> list[str]:
            commands: list[str] = []
            for group in groups:
                registered = cast(list[dict[str, object]], group["hooks"])
                commands.extend(
                    command
                    for hook in registered
                    if isinstance(command := hook.get("command"), str)
                )
            return commands

        prompt_command = (
            '"$HOME/.claude/scripts/lib/py" '
            '"$HOME/.claude/scripts/hooks/user-prompt-submit-conversation-pause.py"'
        )
        self.assertEqual(
            registered_commands(hooks["UserPromptSubmit"]).count(prompt_command), 1
        )

        stop_command = (
            '"$HOME/.claude/scripts/lib/py" '
            '"$HOME/.claude/scripts/hooks/stop-conversation-pause.py"'
        )
        self.assertEqual(registered_commands(hooks["Stop"]).count(stop_command), 1)

        resume_prefix = conversation_pause.RESUME_COMMAND.rsplit(" ", 1)[0]
        self.assertEqual(
            conversation_pause.KEEP_COMMAND.rsplit(" ", 1)[0], resume_prefix
        )
        permissions = cast(dict[str, object], settings["permissions"])
        allow = cast(list[str], permissions["allow"])
        self.assertEqual(allow.count(f"Bash({resume_prefix} *)"), 1)


def cross_session(sender: str, text: str) -> str:
    return (
        f'<cross-session-message from="uds:/tmp/source.sock" from-name="{sender}" '
        f'from-mode="default">\n{text}\n</cross-session-message>'
    )


def asked_not_read(asked_at: int) -> dict[str, object]:
    return {
        "kind": "asked",
        "asked_at": asked_at,
        "reading": {"kind": "not_read"},
    }


def asked_read(asked_at: int, replies_ended: int) -> dict[str, object]:
    return {
        "kind": "asked",
        "asked_at": asked_at,
        "reading": {"kind": "read", "replies_ended": replies_ended},
    }


def scheduled_wakeup(prompt: str) -> dict[str, object]:
    return {
        "id": "scheduled-1",
        "schedule": "0 * * * *",
        "recurring": True,
        "prompt": prompt,
    }


class ConversationPauseTests(unittest.TestCase):
    root: Path = Path()
    notifier_root: Path = Path()
    pause_root: Path = Path()
    showrunner_root: Path = Path()
    escalate_root: Path = Path()
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
        self.escalate_root = self.root / "escalate"
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
import time

args = sys.argv[1:]
with Path(os.environ["NOTIFIER_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
action, name = args[:2]
root = Path(os.environ["NOTIFIER_STATE_DIR"])
instance = root / name
if action == "new" and os.environ.get("FAIL_NEW") == "1":
    print("notifier new refused", file=sys.stderr)
    sys.exit(9)
if action == "stop":
    delay = float(os.environ.get("STOP_DELAY", "0"))
    if delay:
        time.sleep(delay)
    if name in os.environ.get("FAIL_STOP_FOR", "").split(","):
        print(f"notifier stop refused for {name}", file=sys.stderr)
        sys.exit(8)
if action == "resume" and name in os.environ.get("FAIL_RESUME_FOR", "").split(","):
    print(f"notifier resume refused for {name}", file=sys.stderr)
    sys.exit(8)
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
if os.environ.get("SEND_RESULT") == "QUEUED":
    print("QUEUED: delivery pending")
    sys.exit(1)
print("SENT: delivered")
"""
        )
        self.send_stub.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "CLAUDE_CODE_SESSION_ID": SESSION,
            "NOTIFIER_STATE_DIR": str(self.notifier_root),
            "SHOWRUNNER_STATE_DIR": str(self.showrunner_root),
            "ESCALATE_STATE_DIR": str(self.escalate_root),
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
        scheduled: bool = True,
    ) -> Path:
        # A paused item is one whose schedule still exists; `scheduled=False` writes a record
        # naming schedules removed since, as switching a production to on demand does.
        for name in (*instances, *(f"showrunner-{slug}" for slug in footers)) if scheduled else ():
            (self.notifier_root / name).mkdir(exist_ok=True)
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

    def scheduled_prompts_path(self, session_id: str = SESSION) -> Path:
        return self.pause_root / "scheduled-prompts" / f"{session_id}.json"

    def scheduled_prompts(self, session_id: str = SESSION) -> list[str]:
        return cast(
            list[str], json.loads(self.scheduled_prompts_path(session_id).read_text())
        )

    def calls(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()]

    def wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"timed out waiting for {path}")

    def run_prompt(
        self,
        prompt: str,
        *,
        now: int = NOW,
        extra: dict[str, object] | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION, "prompt": prompt}
        if extra is not None:
            payload.update(extra)
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_NOW_EPOCH": str(now),
        }
        if extra_env is not None:
            environment.update(extra_env)
        return subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=timeout,
        )

    def run_stop(
        self,
        *,
        now: int = NOW,
        extra: dict[str, object] | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {"session_id": SESSION}
        if extra is not None:
            payload.update(extra)
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_NOW_EPOCH": str(now),
        }
        if extra_env is not None:
            environment.update(extra_env)
        return subprocess.run(
            [sys.executable, str(STOP_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=timeout,
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

    def run_tick_with_prompt_during_send(
        self,
        prompt: str,
        *,
        now: int,
    ) -> subprocess.CompletedProcess[str]:
        send = self.root / "send-through-prompt-hook.py"
        _ = send.write_text(
            """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

result = subprocess.run(
    [sys.executable, os.environ["PROMPT_HOOK"]],
    input=json.dumps({
        "session_id": os.environ["CLAUDE_CODE_SESSION_ID"],
        "prompt": os.environ["DELIVERED_PROMPT"],
    }),
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode != 0 or result.stdout or result.stderr:
    print("prompt hook delivery failed", file=sys.stderr)
    sys.exit(8)
print("SENT: delivered")
"""
        )
        send.chmod(0o755)
        return self.run_cli(
            "tick",
            now=now,
            extra_env={
                "CONVERSATION_PAUSE_SEND": str(send),
                "DELIVERED_PROMPT": prompt,
                "PROMPT_HOOK": str(PROMPT_HOOK),
            },
        )

    def parsed_reply(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return cast(dict[str, object], json.loads(result.stdout))

    def test_prompt_source_rows(self) -> None:
        def unexpected_sender_read() -> frozenset[str]:
            self.fail("sender files were read for a non-cross-session prompt")

        def unexpected_scheduled_prompt_read() -> tuple[str, ...]:
            self.fail("scheduled prompts were read for a notice or cross-session prompt")

        def no_scheduled_prompts() -> tuple[str, ...]:
            return ()

        typed = (
            "hello",
            "   hello",
            "<3 thanks",
            "/unit:report",
            "/compact",
            "From the user (via the showrunner): go on",
            '<pasted_content id="1">code</pasted_content> why?',
            "<div> why",
        )
        for prompt in typed:
            with self.subTest(prompt=prompt):
                self.assertIs(
                    conversation_pause.prompt_source(
                        prompt, unexpected_sender_read, no_scheduled_prompts
                    ),
                    conversation_pause.PromptSource.TYPED,
                )
        self.assertIs(
            conversation_pause.prompt_source(
                cross_session("natedev", "go"),
                frozenset,
                unexpected_scheduled_prompt_read,
            ),
            conversation_pause.PromptSource.PEER,
        )
        for sender in ("stall-watch", "mac-test", "disk_floor", "shutdown"):
            with self.subTest(sender=sender):
                self.assertIs(
                    conversation_pause.prompt_source(
                        cross_session(sender, "go"),
                        frozenset,
                        unexpected_scheduled_prompt_read,
                    ),
                    conversation_pause.PromptSource.SCHEDULED,
                )
        self.assertIs(
            conversation_pause.prompt_source(
                cross_session("delegate-abc", "go"),
                lambda: frozenset({"delegate-abc"}),
                unexpected_scheduled_prompt_read,
            ),
            conversation_pause.PromptSource.SCHEDULED,
        )
        for prompt in (
            "<task-notification>done</task-notification>",
            "<system-reminder>remember</system-reminder>",
            '<agent-message from="a1">done</agent-message>',
            "",
        ):
            with self.subTest(prompt=prompt):
                self.assertIs(
                    conversation_pause.prompt_source(
                        prompt,
                        unexpected_sender_read,
                        unexpected_scheduled_prompt_read,
                    ),
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

    def test_typed_prompts_record_alert_stamp_with_and_without_reports(self) -> None:
        stamp = self.escalate_root / "typed"
        without_reports = self.run_prompt("hello")
        self.assertEqual(
            (without_reports.returncode, without_reports.stdout, without_reports.stderr),
            (0, "", ""),
        )
        self.assertTrue(stamp.exists())

        stamp.unlink()
        _ = self.create_instance("delegate-abc")
        with_reports = self.run_prompt("hello")
        self.assertEqual(with_reports.returncode, 0, with_reports.stderr)
        self.assertNotEqual(with_reports.stdout, "")
        self.assertTrue(stamp.exists())

    def test_non_typed_prompts_do_not_record_alert_stamp(self) -> None:
        stamp = self.escalate_root / "typed"
        peer = self.run_prompt(cross_session("natedev", "continue"))
        self.assertEqual((peer.returncode, peer.stderr), (0, ""))
        notice = self.run_prompt("<task-notification>done</task-notification>")
        self.assertEqual((notice.returncode, notice.stderr), (0, ""))
        _ = self.create_instance("delegate-abc", sender="delegate-abc")
        scheduled = self.run_prompt(cross_session("delegate-abc", "update"))
        self.assertEqual((scheduled.returncode, scheduled.stderr), (0, ""))
        self.assertFalse(stamp.exists())

    def test_typed_stamp_failure_does_not_skip_report_pause(self) -> None:
        _ = self.escalate_root.write_text("not a directory")
        instance = self.create_instance("delegate-abc")
        result = self.run_prompt("hello")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("conversation-pause: typed stamp:"))
        reply = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(
            reply["systemMessage"],
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_typed_showrunner_prompt_pauses_dailies_and_footer_once(self) -> None:
        instance = self.create_instance("showrunner-demo")
        first = self.parsed_reply(self.run_prompt("hello", now=100))
        self.assertEqual(first, {
            "systemMessage": (
                f"Automatic updates paused while we talk: dailies, footer. {RETURN_SCHEDULE}"
            ),
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
            "phase": {"kind": "replying", "user_wrote_at": 100},
        })
        calls_after_first = self.calls(self.notifier_log)

        second = self.run_prompt("another thought", now=120)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")
        self.assertEqual(self.calls(self.notifier_log), calls_after_first)
        phase = cast(dict[str, object], self.record()["phase"])
        self.assertEqual(
            phase,
            {"kind": "replying", "user_wrote_at": 120},
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
                    f"Automatic updates paused while we talk: {words}. {RETURN_SCHEDULE}",
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
            f"Automatic updates paused while we talk: build report. {RETURN_SCHEDULE}",
        )
        self.assertEqual(
            self.record()["instances"], ["delegate-abc", "report-builds"]
        )
        self.assertTrue((first / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((build / "state").read_text().startswith("ENABLED=0\n"))

    def test_failed_watcher_creation_stops_nothing_and_writes_no_record(self) -> None:
        instance = self.create_instance("delegate-abc")
        result = self.run_prompt("hello", extra_env={"FAIL_NEW": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertEqual(
            [call[:2] for call in self.calls(self.notifier_log)],
            [["new", "conversation-pause"]],
        )

    def test_half_created_watcher_is_replaced_before_report_stop(self) -> None:
        watcher = self.notifier_root / "conversation-pause"
        watcher.mkdir()
        _ = (watcher / "conf").write_text("EVERY=1\nRUN=tick\n")
        instance = self.create_instance("delegate-abc")
        result = self.run_prompt("hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call[:2] for call in self.calls(self.notifier_log)[:3]],
            [
                ["remove", "conversation-pause"],
                ["new", "conversation-pause"],
                ["stop", "delegate-abc"],
            ],
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((self.pause_root / f"{SESSION}.json").exists())

    def test_half_created_watcher_replacement_failure_stops_nothing(self) -> None:
        watcher = self.notifier_root / "conversation-pause"
        watcher.mkdir()
        _ = (watcher / "conf").write_text("EVERY=1\nRUN=tick\n")
        instance = self.create_instance("delegate-abc")
        result = self.run_prompt("hello", extra_env={"FAIL_NEW": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(
            [call[:2] for call in self.calls(self.notifier_log)],
            [["remove", "conversation-pause"], ["new", "conversation-pause"]],
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_disabled_watcher_is_resumed_before_report_stop(self) -> None:
        watcher = self.create_instance("conversation-pause", run="tick", enabled=False)
        instance = self.create_instance("delegate-abc")
        result = self.run_prompt("hello")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [call[:2] for call in self.calls(self.notifier_log)[:2]],
            [["resume", "conversation-pause"], ["stop", "delegate-abc"]],
        )
        self.assertTrue((watcher / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_failed_report_stop_is_removed_from_record_and_retried(self) -> None:
        failed = self.create_instance("delegate-alpha")
        stopped = self.create_instance("delegate-beta")
        first = self.run_prompt(
            "hello", extra_env={"FAIL_STOP_FOR": "delegate-alpha"}
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, "")
        self.assertEqual(len(first.stderr.splitlines()), 1)
        self.assertIn("delegate-alpha", first.stderr)
        self.assertEqual(self.record()["instances"], ["delegate-beta"])
        self.assertTrue((failed / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((stopped / "state").read_text().startswith("ENABLED=0\n"))

        second = self.run_prompt("again", now=NOW + 1)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.record()["instances"], ["delegate-beta", "delegate-alpha"]
        )
        self.assertTrue((failed / "state").read_text().startswith("ENABLED=0\n"))

    def test_listed_enabled_report_is_stopped_again(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=True)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 100},
            instances=("delegate-abc",),
        )
        result = self.run_prompt("again", now=120)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertIn(["stop", "delegate-abc"], self.calls(self.notifier_log))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertEqual(self.record()["instances"], ["delegate-abc"])

    def test_resume_after_failed_stop_restores_only_recorded_report(self) -> None:
        failed = self.create_instance("delegate-alpha")
        stopped = self.create_instance("delegate-beta")
        first = self.run_prompt(
            "hello", extra_env={"FAIL_STOP_FOR": "delegate-alpha"}
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(self.record()["instances"], ["delegate-beta"])
        self.notifier_log.unlink()

        resumed = self.run_cli("resume")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.calls(self.notifier_log), [["resume", "delegate-beta"]])
        self.assertTrue((failed / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((stopped / "state").read_text().startswith("ENABLED=1\n"))

    def test_peer_prompt_does_not_pause_a_showrunner_session(self) -> None:
        instance = self.create_instance("showrunner-demo")
        result = self.run_prompt(cross_session("natedev", "continue"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_pasted_content_prompt_pauses_reports(self) -> None:
        instance = self.create_instance("delegate-abc")
        prompt = (
            '<pasted_content id="one">display: flex</pasted_content id="one">\n'
            "why does this not center?"
        )

        reply = self.parsed_reply(self.run_prompt(prompt))

        self.assertEqual(
            reply["systemMessage"],
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_arbitrary_tag_opened_prompt_pauses_reports(self) -> None:
        instance = self.create_instance("delegate-abc")

        reply = self.parsed_reply(self.run_prompt("<div> why"))

        self.assertEqual(
            reply["systemMessage"],
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_task_notification_and_system_reminder_do_not_pause_reports(self) -> None:
        instance = self.create_instance("delegate-abc")
        for prompt in (
            "<task-notification>done</task-notification>",
            "<system-reminder>remember</system-reminder>",
        ):
            with self.subTest(prompt=prompt):
                result = self.run_prompt(prompt)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr), (0, "", "")
                )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertFalse((self.escalate_root / "typed").exists())

    def test_subagent_hand_back_does_not_pause_reports(self) -> None:
        instance = self.create_instance("showrunner-demo")
        hand_back = (
            '<agent-message from="a715bbb613f1bc07c">\n'
            "[Subagent hand-back] fixed the recorder</agent-message>"
        )
        for prompt in (hand_back, f"Another Claude session sent a message:\n{hand_back}"):
            with self.subTest(prompt=prompt):
                result = self.run_prompt(prompt)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr), (0, "", "")
                )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertFalse((self.escalate_root / "typed").exists())

    def test_recorded_scheduled_prompt_does_not_pause_but_different_prompt_does(self) -> None:
        instance = self.create_instance("delegate-abc")
        recorded = self.run_stop(
            extra={"session_crons": [scheduled_wakeup("  recurring status  ")]}
        )
        self.assertEqual(
            (recorded.returncode, recorded.stdout, recorded.stderr), (0, "", "")
        )

        scheduled = self.run_prompt("recurring status")

        self.assertEqual(
            (scheduled.returncode, scheduled.stdout, scheduled.stderr), (0, "", "")
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.escalate_root / "typed").exists())
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

        typed = self.parsed_reply(self.run_prompt("a different thought"))
        self.assertEqual(
            typed["systemMessage"],
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertTrue((self.escalate_root / "typed").exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_clipped_scheduled_prompt_matches_by_prefix(self) -> None:
        instance = self.create_instance("delegate-abc")
        recorded = self.run_stop(extra={
            "session_crons": [
                scheduled_wakeup("summarize this long plan… [+240 chars]")
            ]
        })
        self.assertEqual(recorded.returncode, 0, recorded.stderr)

        scheduled = self.run_prompt(
            "summarize this long plan and include the unresolved decisions"
        )

        self.assertEqual(
            (scheduled.returncode, scheduled.stdout, scheduled.stderr), (0, "", "")
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse((self.escalate_root / "typed").exists())
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
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
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

    def test_prompt_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.create_instance("delegate-abc")
        self.pause_root.mkdir(parents=True)
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            result = self.run_prompt(
                "hello",
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
                timeout=3,
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_stop_hook_budget_expires_while_record_lock_is_held(self) -> None:
        _ = self.write_record({"kind": "replying", "user_wrote_at": 50})
        with (self.pause_root / ".lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            result = self.run_stop(
                extra_env={"CONVERSATION_PAUSE_HOOK_BUDGET": "1"},
                timeout=3,
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)

    def test_prompt_hook_budget_expires_during_notifier_stop(self) -> None:
        instance = self.create_instance("delegate-abc")
        started = time.monotonic()
        result = self.run_prompt(
            "hello",
            extra_env={
                "CONVERSATION_PAUSE_HOOK_BUDGET": "1",
                "STOP_DELAY": "30",
            },
            timeout=3,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("HookBudgetSpent", result.stderr)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_tick_releases_record_lock_during_session_lookup(self) -> None:
        marker = self.root / "lookup-started"
        sessions = self.root / "sessions-slow.py"
        _ = sessions.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os, time\n",
                "from pathlib import Path\n",
                "Path(os.environ['LOOKUP_MARKER']).touch()\n",
                "time.sleep(2)\n",
                "print(os.environ['SESSION_SOCKET'])\n",
            ))
        )
        sessions.chmod(0o755)
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100}
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SESSIONS": str(sessions),
            "CONVERSATION_PAUSE_NOW_EPOCH": "200",
            "LOOKUP_MARKER": str(marker),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            with (self.pause_root / ".lock").open("w") as lock_file:
                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            _, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)

    def test_message_arrived_uses_named_reply_variants(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            no_reply = conversation_pause.message_arrived(
                SESSION, conversation_pause.PromptSource.TYPED, "hello", 100
            )
            self.assertIsInstance(no_reply, conversation_pause.NoReply)

            _ = self.create_instance("delegate-abc")
            shown = conversation_pause.message_arrived(
                SESSION, conversation_pause.PromptSource.TYPED, "hello", 110
            )
            self.assertIsInstance(shown, conversation_pause.ShownToUser)

            _ = self.write_record(
                asked_read(100, 1),
                instances=("delegate-abc",),
            )
            context = conversation_pause.message_arrived(
                SESSION, conversation_pause.PromptSource.TYPED, "later", 120
            )
            self.assertIsInstance(context, conversation_pause.ContextOnly)

    def test_cli_resume_skips_missing_instance_restores_footer_and_deletes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        switch = self.footer_off("demo")
        record_path = self.write_record(
            {"kind": "replying", "user_wrote_at": 1},
            instances=("showrunner-demo", "delegate-gone"),
            footers=("demo",), scheduled=False,
        )
        result = self.run_cli("resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "automatic updates on: dailies, footer\n")
        self.assertEqual(self.calls(self.notifier_log), [["resume", "showrunner-demo"]])
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())
        self.assertFalse(record_path.exists())

    def test_resume_verb_keeps_only_failed_items_for_retry(self) -> None:
        failed = self.create_instance("delegate-alpha", enabled=False)
        restored = self.create_instance("delegate-beta", enabled=False)
        switch = self.footer_off("demo")
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 100},
            instances=(failed.name, restored.name),
            footers=("demo",),
        )
        first = self.run_cli(
            "resume", extra_env={"FAIL_RESUME_FOR": failed.name}
        )
        self.assertEqual((first.returncode, first.stdout), (1, ""))
        self.assertEqual(len(first.stderr.splitlines()), 1)
        self.assertIn(f"failed to resume: {failed.name}", first.stderr)
        self.assertEqual(self.record(), {
            "session_id": SESSION,
            "instances": [failed.name],
            "footers": [],
            "phase": {"kind": "replying", "user_wrote_at": 100},
        })
        self.assertTrue((failed / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((restored / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())

        second = self.run_cli("resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "automatic updates on: status reports\n")
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        calls = self.calls(self.notifier_log)
        self.assertEqual(calls.count(["resume", failed.name]), 2)
        self.assertEqual(calls.count(["resume", restored.name]), 1)

    def test_cli_status_and_keep(self) -> None:
        self.assertEqual(self.run_cli("status").stdout, "not paused\n")
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 1},
            instances=("delegate-abc", "showrunner-demo"),
            footers=("demo",),
        )
        self.assertEqual(
            self.run_cli("status").stdout,
            "paused: dailies, footer, status reports (replying)\n",
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

    def test_stop_hook_sets_quiet_time_once(self) -> None:
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 50},
            instances=("delegate-abc",),
        )
        first = self.run_stop(now=100)
        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "quiet", "user_wrote_at": 50, "reply_ended_at": 100},
        )
        second = self.run_stop(now=200)
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "quiet", "user_wrote_at": 50, "reply_ended_at": 100},
        )

    def test_stop_hook_skips_absent_record_and_agent_payload(self) -> None:
        absent = self.run_stop()
        self.assertEqual((absent.returncode, absent.stdout, absent.stderr), (0, "", ""))
        self.assertFalse(self.pause_root.exists())
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 50}
        )
        before = self.record()
        agent = self.run_stop(extra={"agent_id": "a1"})
        self.assertEqual((agent.returncode, agent.stdout, agent.stderr), (0, "", ""))
        self.assertEqual(self.record(), before)

    def test_stop_hook_records_scheduled_prompts_without_pause_record(self) -> None:
        result = self.run_stop(extra={
            "session_crons": [
                scheduled_wakeup("first scheduled prompt"),
                scheduled_wakeup("second scheduled prompt"),
            ]
        })

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(
            self.scheduled_prompts(),
            ["first scheduled prompt", "second scheduled prompt"],
        )
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_stop_hook_active_event_still_records_scheduled_prompts(self) -> None:
        result = self.run_stop(extra={
            "stop_hook_active": True,
            "session_crons": [scheduled_wakeup("scheduled while stop hook is active")],
        })

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(
            self.scheduled_prompts(), ["scheduled while stop hook is active"]
        )
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())

    def test_stop_hook_empty_scheduled_list_removes_recorded_prompts(self) -> None:
        created = self.run_stop(extra={
            "session_crons": [scheduled_wakeup("scheduled prompt")]
        })
        self.assertEqual(created.returncode, 0, created.stderr)
        path = self.scheduled_prompts_path()
        self.assertTrue(path.exists())

        removed = self.run_stop(extra={"session_crons": []})

        self.assertEqual((removed.returncode, removed.stdout, removed.stderr), (0, "", ""))
        self.assertFalse(path.exists())

    def test_stop_payload_without_scheduled_list_leaves_recorded_prompts(self) -> None:
        created = self.run_stop(extra={
            "session_crons": [scheduled_wakeup("scheduled prompt")]
        })
        self.assertEqual(created.returncode, 0, created.stderr)

        unchanged = self.run_stop()

        self.assertEqual(
            (unchanged.returncode, unchanged.stdout, unchanged.stderr), (0, "", "")
        )
        self.assertEqual(self.scheduled_prompts(), ["scheduled prompt"])

    def test_tick_and_status_ignore_scheduled_prompt_files(self) -> None:
        recorded = self.run_stop(extra={
            "session_crons": [scheduled_wakeup("scheduled prompt")]
        })
        self.assertEqual(recorded.returncode, 0, recorded.stderr)

        status = self.run_cli("status")
        tick = self.run_cli("tick")

        self.assertEqual((status.returncode, status.stdout, status.stderr), (0, "not paused\n", ""))
        self.assertEqual((tick.returncode, tick.stdout, tick.stderr), (0, "", ""))
        self.assertEqual(self.calls(self.sessions_log), [])
        self.assertTrue(self.scheduled_prompts_path().exists())

    def test_tick_removes_scheduled_prompts_older_than_eight_days(self) -> None:
        recorded = self.run_stop(extra={
            "session_crons": [scheduled_wakeup("expired scheduled prompt")]
        })
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        path = self.scheduled_prompts_path()
        expired_at = NOW - conversation_pause.SCHEDULED_PROMPT_RETENTION_SECONDS - 1
        os.utime(path, (expired_at, expired_at))

        tick = self.run_cli("tick", now=NOW)

        self.assertEqual((tick.returncode, tick.stdout, tick.stderr), (0, "", ""))
        self.assertFalse(path.exists())

    def test_stop_hook_active_event_does_not_count_reply_twice(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.write_record(
            asked_read(100, 0),
            instances=(instance.name,),
        )

        first = self.run_stop(now=110)
        repeated = self.run_stop(now=120, extra={"stop_hook_active": True})

        self.assertEqual((first.returncode, first.stdout, first.stderr), (0, "", ""))
        self.assertEqual(
            (repeated.returncode, repeated.stdout, repeated.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked_read(100, 1))
        reply = self.parsed_reply(self.run_prompt("yes", now=130))
        self.assertEqual(
            reply["systemMessage"],
            "Automatic updates are back on: status reports.",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_tick_asks_nothing_when_no_paused_schedule_still_exists(self) -> None:
        path = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=("showrunner-example",), footers=("example",), scheduled=False,
        )
        result = self.run_cli("tick", now=1_000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.calls(self.send_log), [])

    def test_tick_waits_for_fifteen_quiet_minutes_and_sends_question_once(self) -> None:
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=("delegate-abc",),
        )
        before = self.run_cli("tick", now=999)
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(self.calls(self.send_log), [])
        self.assertEqual(
            self.record()["phase"],
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
        )

        at_fifteen = self.run_cli("tick", now=1_000)
        self.assertEqual(at_fifteen.returncode, 0, at_fifteen.stderr)
        self.assertEqual(self.record()["phase"], asked_not_read(1_000))
        self.assertEqual(self.calls(self.sessions_log)[-1], ["socket", f"session:{SESSION}"])
        self.assertEqual(self.calls(self.send_log), [[
            "--to", f"uds:{self.socket_path}",
            "--from", "conversation-pause",
            "--key", f"conversation-pause-{SESSION}",
            "--text", QUESTION,
        ]])
        after = self.run_cli("tick", now=1_050)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_tick_waits_thirty_minutes_for_a_reply_that_never_ends(self) -> None:
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 10},
            instances=("delegate-abc",),
        )
        result = self.run_cli("tick", now=1_809)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(self.send_log), [])
        self.assertEqual(
            self.record()["phase"],
            {"kind": "replying", "user_wrote_at": 10},
        )

        result = self.run_cli("tick", now=1_810)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls(self.send_log)), 1)
        self.assertEqual(self.record()["phase"], asked_not_read(1_810))

    def test_queued_question_stays_pending_and_yes_restarts_replying(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=("delegate-abc",),
        )
        queued = self.run_cli("tick", now=150, extra_env={"SEND_RESULT": "QUEUED"})
        self.assertEqual(queued.returncode, 0)
        self.assertEqual(
            self.record()["phase"], {"kind": "question_pending", "due_at": 100}
        )
        self.assertEqual(len(self.calls(self.send_log)), 1)

        typed = self.run_prompt("yes", now=160)
        self.assertEqual((typed.returncode, typed.stdout, typed.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"], {"kind": "replying", "user_wrote_at": 160}
        )
        self.assertNotIn(["resume", "delegate-abc"], self.calls(self.notifier_log))
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_delivered_question_records_delivery_time(self) -> None:
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record({"kind": "question_pending", "due_at": 100}, instances=("delegate-abc",))
        result = self.run_cli("tick", now=170)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record()["phase"], asked_not_read(170))
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_prompt_before_send_return_stays_read_and_yes_resumes(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )

        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )

        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(self.record()["phase"], asked_read(150, 0))
        stopped = self.run_stop(now=160)
        self.assertEqual(
            (stopped.returncode, stopped.stdout, stopped.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked_read(150, 1))
        answer = self.parsed_reply(self.run_prompt("yes", now=170))
        self.assertEqual(
            answer["systemMessage"],
            "Automatic updates are back on: status reports.",
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_prompt_before_send_return_reserves_yes_after_newer_reply(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=(instance.name,),
        )
        tick = self.run_tick_with_prompt_during_send(
            cross_session("conversation-pause", QUESTION),
            now=150,
        )
        self.assertEqual(tick.returncode, 0, tick.stderr)

        for ended_at in (160, 170):
            stopped = self.run_stop(now=ended_at)
            self.assertEqual(
                (stopped.returncode, stopped.stdout, stopped.stderr),
                (0, "", ""),
            )
        asked = asked_read(150, 2)
        self.assertEqual(self.record()["phase"], asked)

        answer = self.run_prompt("yes", now=180)
        self.assertEqual(
            (answer.returncode, answer.stdout, answer.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_return_question_sender_is_recognized_when_text_changes(self) -> None:
        _ = self.write_record({"kind": "question_pending", "due_at": 100})
        changed_question = QUESTION.replace("15 minutes", "six minutes", 1)

        delivered = self.run_prompt(
            cross_session("conversation-pause", changed_question),
            now=150,
        )

        self.assertEqual(
            (delivered.returncode, delivered.stdout, delivered.stderr),
            (0, "", ""),
        )
        self.assertEqual(self.record()["phase"], asked_read(150, 0))

    def test_undelivered_question_returns_updates_after_five_minutes(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=("delegate-abc",),
        )
        before = self.run_cli("tick", now=399, extra_env={"SEND_RESULT": "QUEUED"})
        self.assertEqual(before.returncode, 0)
        self.assertEqual(
            self.record()["phase"], {"kind": "question_pending", "due_at": 100}
        )

        due = self.run_cli("tick", now=400, extra_env={"SEND_RESULT": "QUEUED"})
        self.assertEqual(due.returncode, 0, due.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "returned", "returned_at": 400}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue(watcher.exists())
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_delivery_does_not_overwrite_newer_replying_phase(self) -> None:
        marker = self.root / "send-started"
        send = self.root / "send-slow.py"
        _ = send.write_text(
            "".join((
                "#!/usr/bin/env python3\n",
                "import os, time\n",
                "from pathlib import Path\n",
                "Path(os.environ['SEND_MARKER']).touch()\n",
                "time.sleep(2)\n",
                "print('SENT: delivered')\n",
            ))
        )
        send.chmod(0o755)
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100},
            instances=("delegate-abc",),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_SEND": str(send),
            "CONVERSATION_PAUSE_NOW_EPOCH": "150",
            "SEND_MARKER": str(marker),
        }
        process = subprocess.Popen(
            [sys.executable, str(LIBRARY), "tick"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            self.wait_for_path(marker)
            typed = self.run_prompt("new topic", now=160)
            self.assertEqual(
                (typed.returncode, typed.stdout, typed.stderr), (0, "", "")
            )
            _, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=3)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "replying", "user_wrote_at": 160}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_question_changed_before_its_send_is_skipped(self) -> None:
        send = self.root / "send-rewrite.py"
        _ = send.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

with Path(os.environ["SEND_LOG"]).open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
path = Path(os.environ["SECOND_RECORD"])
record = json.loads(path.read_text())
record["phase"] = {"kind": "replying", "user_wrote_at": 160}
path.write_text(json.dumps(record))
print("SENT: delivered")
"""
        )
        send.chmod(0o755)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "question_pending", "due_at": 100}, session_id="a-session",
            instances=("delegate-abc",),
        )
        second = self.write_record(
            {"kind": "question_pending", "due_at": 100}, session_id="b-session",
            instances=("delegate-abc",),
        )
        result = self.run_cli(
            "tick",
            now=150,
            extra_env={
                "CONVERSATION_PAUSE_SEND": str(send),
                "SECOND_RECORD": str(second),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls(self.send_log)), 1)
        self.assertEqual(
            self.record("a-session")["phase"],
            asked_not_read(150),
        )
        self.assertEqual(
            self.record("b-session")["phase"],
            {"kind": "replying", "user_wrote_at": 160},
        )

    def test_asked_yes_variants_resume(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("Yes.", " y "):
            with self.subTest(prompt=prompt):
                _ = self.write_record(
                    asked_not_read(100),
                    instances=("delegate-abc",),
                )
                self.set_enabled(instance, False)
                self.notifier_log.unlink(missing_ok=True)
                question = self.run_prompt(
                    cross_session("conversation-pause", QUESTION),
                    now=105,
                )
                self.assertEqual(
                    (question.returncode, question.stdout, question.stderr),
                    (0, "", ""),
                )
                stopped = self.run_stop(now=110)
                self.assertEqual(
                    (stopped.returncode, stopped.stdout, stopped.stderr),
                    (0, "", ""),
                )
                self.assertEqual(self.record()["phase"], asked_read(100, 1))
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
            asked_read(100, 1),
            instances=("delegate-abc",),
        )
        reply = self.parsed_reply(self.run_prompt("NO!"))
        self.assertEqual(reply, {
            "systemMessage": f"Automatic updates stay off. {ASK_AGAIN}",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": NO_CONTEXT,
            },
        })
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_reply_running_before_question_does_not_take_bare_answer(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("yes", "no"):
            with self.subTest(prompt=prompt):
                self.set_enabled(instance, False)
                _ = self.write_record(
                    asked_not_read(100),
                    instances=(instance.name,),
                )

                older_reply = self.run_stop(now=110)
                self.assertEqual(
                    (older_reply.returncode, older_reply.stdout, older_reply.stderr),
                    (0, "", ""),
                )
                self.assertEqual(self.record()["phase"], asked_not_read(100))

                question = self.run_prompt(
                    cross_session("conversation-pause", QUESTION),
                    now=120,
                )
                self.assertEqual(
                    (question.returncode, question.stdout, question.stderr),
                    (0, "", ""),
                )
                self.assertEqual(self.record()["phase"], asked_read(100, 0))

                question_reply = self.run_stop(now=130)
                self.assertEqual(
                    (
                        question_reply.returncode,
                        question_reply.stdout,
                        question_reply.stderr,
                    ),
                    (0, "", ""),
                )
                self.assertEqual(self.record()["phase"], asked_read(100, 1))

                answer = self.parsed_reply(self.run_prompt(prompt, now=140))
                if prompt == "yes":
                    self.assertEqual(
                        answer["systemMessage"],
                        "Automatic updates are back on: status reports.",
                    )
                    self.assertFalse(
                        (self.pause_root / f"{SESSION}.json").exists()
                    )
                    self.assertTrue(
                        (instance / "state").read_text().startswith("ENABLED=1\n")
                    )
                else:
                    self.assertEqual(
                        answer["systemMessage"],
                        f"Automatic updates stay off. {ASK_AGAIN}",
                    )
                    self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
                    self.assertTrue(
                        (instance / "state").read_text().startswith("ENABLED=0\n")
                    )

    def test_newer_reply_reserves_bare_answers_for_session_until_timeout(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("yes", "no"):
            with self.subTest(prompt=prompt):
                self.set_enabled(instance, False)
                _ = self.write_record(
                    asked_not_read(100),
                    instances=(instance.name,),
                )
                question = self.run_prompt(
                    cross_session("conversation-pause", QUESTION),
                    now=105,
                )
                self.assertEqual(
                    (question.returncode, question.stdout, question.stderr),
                    (0, "", ""),
                )
                for ended_at in (110, 120):
                    stopped = self.run_stop(now=ended_at)
                    self.assertEqual(
                        (stopped.returncode, stopped.stdout, stopped.stderr),
                        (0, "", ""),
                    )
                asked = asked_read(100, 2)
                self.assertEqual(self.record()["phase"], asked)

                typed = self.run_prompt(prompt, now=200)
                self.assertEqual(
                    (typed.returncode, typed.stdout, typed.stderr),
                    (0, "", ""),
                )
                self.assertEqual(self.record()["phase"], asked)
                self.assertEqual(
                    self.run_cli("status").stdout,
                    "paused: status reports (asked; a newer reply ended; "
                    + "bare yes/no belongs to the session)\n",
                )

                before_timeout = self.run_cli("tick", now=399)
                self.assertEqual(before_timeout.returncode, 0, before_timeout.stderr)
                self.assertEqual(self.record()["phase"], asked)
                self.assertTrue(
                    (instance / "state").read_text().startswith("ENABLED=0\n")
                )

                timed_out = self.run_cli("tick", now=400)
                self.assertEqual(timed_out.returncode, 0, timed_out.stderr)
                self.assertEqual(
                    self.record()["phase"],
                    {"kind": "returned", "returned_at": 400},
                )
                self.assertTrue(
                    (instance / "state").read_text().startswith("ENABLED=1\n")
                )

    def test_asked_records_without_reading_use_reply_count_or_default(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        legacy_records: tuple[tuple[dict[str, object], int], ...] = (
            ({"kind": "asked", "asked_at": 100}, 1),
            (
                {
                    "kind": "asked",
                    "asked_at": 100,
                    "replies_ended_since_question": 2,
                },
                2,
            ),
        )
        for phase, replies_ended in legacy_records:
            with self.subTest(replies_ended=replies_ended):
                path = self.write_record(phase, instances=(instance.name,))
                with patch.dict(os.environ, self.environment, clear=True):
                    record = conversation_pause.read_record(path)
                    self.assertEqual(
                        record.phase,
                        conversation_pause.Asked(
                            100,
                            conversation_pause.QuestionRead(replies_ended),
                        ),
                    )
                    conversation_pause.write_record(record)
                self.assertEqual(
                    self.record()["phase"],
                    asked_read(100, replies_ended),
                )

        path = self.write_record(
            {"kind": "asked", "asked_at": 100},
            instances=(instance.name,),
        )
        reply = self.parsed_reply(self.run_prompt("yes", now=200))
        self.assertEqual(
            reply["systemMessage"],
            "Automatic updates are back on: status reports.",
        )
        self.assertFalse(path.exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_session_answer_commands_apply_after_newer_reply(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt, action in (
            ("Please return the automatic updates", "resume"),
            ("Keep the automatic updates off", "keep"),
        ):
            with self.subTest(action=action):
                self.set_enabled(instance, False)
                _ = self.write_record(
                    asked_read(100, 2),
                    instances=(instance.name,),
                )
                reply = self.parsed_reply(self.run_prompt(prompt, now=200))
                output = cast(dict[str, object], reply["hookSpecificOutput"])
                self.assertEqual(output["additionalContext"], OTHER_ANSWER_CONTEXT)

                command = self.run_cli(action, now=210)
                self.assertEqual(command.returncode, 0, command.stderr)
                if action == "resume":
                    self.assertEqual(
                        command.stdout,
                        "automatic updates on: status reports\n",
                    )
                    self.assertFalse(
                        (self.pause_root / f"{SESSION}.json").exists()
                    )
                    self.assertTrue(
                        (instance / "state").read_text().startswith("ENABLED=1\n")
                    )
                else:
                    self.assertEqual(command.stdout, "automatic updates stay off\n")
                    self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
                    self.assertTrue(
                        (instance / "state").read_text().startswith("ENABLED=0\n")
                    )

    def test_asked_other_and_peer_yes_restart_replying(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        for prompt in ("later", cross_session("natedev", "yes")):
            with self.subTest(prompt=prompt):
                _ = self.write_record(
                    asked_read(100, 1),
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
                    {"kind": "replying", "user_wrote_at": 200},
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
            "systemMessage": f"Automatic updates are off again: status reports. {ASK_AGAIN}",
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
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertEqual(
            self.record()["phase"],
            {"kind": "replying", "user_wrote_at": 200},
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_question_timeout_returns_updates_and_late_yes_removes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        unit_report = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        switch = self.footer_off("demo")
        _ = self.write_record(
            asked_read(100, 1),
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
        self.assertNotIn(["remove", "conversation-pause"], self.calls(self.notifier_log))
        self.assertTrue(watcher.exists())

        late = self.parsed_reply(self.run_prompt("yes", now=410))
        self.assertNotIn("systemMessage", late)
        self.assertFalse((self.pause_root / f"{SESSION}.json").exists())
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertTrue((unit_report / "state").read_text().startswith("ENABLED=1\n"))

    def test_timeout_restore_keeps_only_failed_items_for_retry(self) -> None:
        failed = self.create_instance("delegate-alpha", enabled=False)
        restored = self.create_instance("delegate-beta", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        switch = self.footer_off("demo")
        _ = self.write_record(
            asked_read(100, 1),
            instances=(failed.name, restored.name),
            footers=("demo",),
        )
        first = self.run_cli(
            "tick", now=400, extra_env={"FAIL_RESUME_FOR": failed.name}
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(first.stderr.splitlines()), 1)
        self.assertIn(f"failed to resume: {failed.name}", first.stderr)
        self.assertEqual(self.record(), {
            "session_id": SESSION,
            "instances": [failed.name],
            "footers": [],
            "phase": asked_read(100, 1),
        })
        self.assertTrue((failed / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue((restored / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())
        self.assertTrue(watcher.exists())

        second = self.run_cli("tick", now=401)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "returned", "returned_at": 401}
        )
        calls = self.calls(self.notifier_log)
        self.assertEqual(calls.count(["resume", failed.name]), 2)
        self.assertEqual(calls.count(["resume", restored.name]), 1)

    def test_late_no_after_timeout_pauses_and_marks_kept_off(self) -> None:
        instance = self.create_instance("delegate-abc")
        _ = self.write_record({"kind": "returned", "returned_at": 400})
        reply = self.parsed_reply(self.run_prompt("no", now=410))
        self.assertEqual(
            reply["systemMessage"],
            f"Automatic updates are off again: status reports. {ASK_AGAIN}",
        )
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_kept_off_survives_tick_then_typed_message_starts_replying(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "kept_off"}, instances=("delegate-abc",)
        )
        tick = self.run_cli("tick", now=100_000)
        self.assertEqual(tick.returncode, 0, tick.stderr)
        self.assertEqual(self.record()["phase"], {"kind": "kept_off"})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertTrue(watcher.exists())

        prompt = self.run_prompt("another topic", now=100_001)
        self.assertEqual((prompt.returncode, prompt.stdout, prompt.stderr), (0, "", ""))
        self.assertEqual(
            self.record()["phase"],
            {"kind": "replying", "user_wrote_at": 100_001},
        )

    def test_kept_off_session_end_restores_items_and_removes_watcher(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        switch = self.footer_off("demo")
        watcher = self.create_instance("conversation-pause", run="tick")
        record_path = self.write_record(
            {"kind": "kept_off"},
            instances=("showrunner-demo",),
            footers=("demo",),
        )
        running = self.run_cli("tick", now=100)
        self.assertEqual(running.returncode, 0, running.stderr)
        self.assertTrue(record_path.exists())
        self.assertTrue(watcher.exists())

        gone = self.run_cli("tick", now=101, extra_env={"SESSION_RUNNING": "0"})
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())
        self.assertFalse(record_path.exists())
        self.assertFalse(watcher.exists())

    def test_ended_session_restore_keeps_only_failed_items_for_retry(self) -> None:
        failed = self.create_instance("delegate-alpha", enabled=False)
        restored = self.create_instance("delegate-beta", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        switch = self.footer_off("demo")
        path = self.write_record(
            {"kind": "kept_off"},
            instances=(failed.name, restored.name),
            footers=("demo",),
        )
        first = self.run_cli(
            "tick",
            now=100,
            extra_env={"SESSION_RUNNING": "0", "FAIL_RESUME_FOR": failed.name},
        )
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(first.stderr.splitlines()), 1)
        self.assertIn(f"failed to resume: {failed.name}", first.stderr)
        self.assertEqual(self.record(), {
            "session_id": SESSION,
            "instances": [failed.name],
            "footers": [],
            "phase": {"kind": "kept_off"},
        })
        self.assertTrue(path.exists())
        self.assertTrue((restored / "state").read_text().startswith("ENABLED=1\n"))
        self.assertFalse(switch.exists())

        second = self.run_cli("tick", now=101, extra_env={"SESSION_RUNNING": "0"})
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(watcher.exists())
        calls = self.calls(self.notifier_log)
        self.assertEqual(calls.count(["resume", failed.name]), 2)
        self.assertEqual(calls.count(["resume", restored.name]), 1)

    def test_missing_session_returns_updates_and_deletes_record(self) -> None:
        instance = self.create_instance("showrunner-demo", enabled=False)
        switch = self.footer_off("demo")
        _ = self.write_record(
            {"kind": "replying", "user_wrote_at": 10},
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

    def test_tick_removes_watcher_when_no_record_exists(self) -> None:
        watcher = self.create_instance("conversation-pause", run="tick")
        result = self.run_cli("tick")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            ["remove", "conversation-pause"], self.calls(self.notifier_log)
        )
        self.assertFalse(watcher.exists())

    def test_returned_record_keeps_watcher_until_late_window_ends(self) -> None:
        watcher = self.create_instance("conversation-pause", run="tick")
        path = self.write_record({"kind": "returned", "returned_at": 100})
        result = self.run_cli("tick", now=399)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(watcher.exists())
        self.assertNotIn(["remove", "conversation-pause"], self.calls(self.notifier_log))

        result = self.run_cli("tick", now=400)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(watcher.exists())
        self.assertIn(["remove", "conversation-pause"], self.calls(self.notifier_log))

    def test_unknown_session_still_advances_timed_phases(self) -> None:
        failing = self.root / "sessions-fail.py"
        _ = failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
        failing.chmod(0o755)
        quiet_instance = self.create_instance(
            "delegate-quiet", session_id="quiet-session", enabled=False
        )
        asked_instance = self.create_instance(
            "delegate-asked", session_id="asked-session", enabled=False
        )
        kept_instance = self.create_instance(
            "delegate-kept", session_id="kept-session", enabled=False
        )
        watcher = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=("delegate-quiet",),
            session_id="quiet-session",
        )
        _ = self.write_record(
            asked_read(100, 1),
            instances=("delegate-asked",),
            session_id="asked-session",
        )
        _ = self.write_record(
            {"kind": "kept_off"},
            instances=("delegate-kept",),
            session_id="kept-session",
        )
        result = self.run_cli(
            "tick", now=1_000, extra_env={"CONVERSATION_PAUSE_SESSIONS": str(failing)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record("quiet-session")["phase"],
            {"kind": "question_pending", "due_at": 1_000},
        )
        self.assertEqual(
            self.record("asked-session")["phase"],
            {"kind": "returned", "returned_at": 1_000},
        )
        self.assertEqual(
            self.record("kept-session")["phase"], {"kind": "kept_off"}
        )
        self.assertTrue(
            (quiet_instance / "state").read_text().startswith("ENABLED=0\n")
        )
        self.assertTrue(
            (asked_instance / "state").read_text().startswith("ENABLED=1\n")
        )
        self.assertTrue(
            (kept_instance / "state").read_text().startswith("ENABLED=0\n")
        )
        self.assertEqual(self.calls(self.send_log), [])
        self.assertTrue(watcher.exists())

        result = self.run_cli(
            "tick", now=1_300, extra_env={"CONVERSATION_PAUSE_SESSIONS": str(failing)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record("quiet-session")["phase"],
            {"kind": "returned", "returned_at": 1_300},
        )
        self.assertTrue(
            (quiet_instance / "state").read_text().startswith("ENABLED=1\n")
        )
        self.assertEqual(
            self.record("kept-session")["phase"], {"kind": "kept_off"}
        )

    def test_missing_session_registry_keeps_pause_record_and_report_off(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
            instances=(instance.name,),
        )
        environment = {
            **self.environment,
            "CONVERSATION_PAUSE_NOW_EPOCH": "1000",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "missing-sessions"),
        }
        _ = environment.pop("CONVERSATION_PAUSE_SESSIONS")
        result = subprocess.run(
            [sys.executable, str(LIBRARY), "tick"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "question_pending", "due_at": 1_000}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_open_reviews_leave_all_phases_byte_identical(self) -> None:
        asked_instance = self.create_instance(
            "showrunner-asked", session_id="asked-session", enabled=False
        )
        quiet_instance = self.create_instance(
            "showrunner-quiet", session_id="quiet-session", enabled=False
        )
        returned_instance = self.create_instance(
            "showrunner-returned", session_id="returned-session", enabled=False
        )
        _ = self.footer_off("asked")
        _ = self.footer_off("quiet")
        _ = self.footer_off("returned")
        _ = self.create_instance("conversation-pause", run="tick")
        paths = {
            "asked": self.write_record(
                asked_read(100, 1),
                instances=(asked_instance.name,),
                footers=("asked",),
                session_id="asked-session",
            ),
            "quiet": self.write_record(
                {"kind": "quiet", "user_wrote_at": 10, "reply_ended_at": 100},
                instances=(quiet_instance.name,),
                footers=("quiet",),
                session_id="quiet-session",
            ),
            "returned": self.write_record(
                {"kind": "returned", "returned_at": 100},
                instances=(returned_instance.name,),
                footers=("returned",),
                session_id="returned-session",
            ),
        }
        review_root = self.showrunner_root / "review-paused"
        review_root.mkdir(parents=True)
        review_paths = [review_root / f"{slug}.json" for slug in paths]
        for review_path in review_paths:
            _ = review_path.write_text("{}\n")
        before = {slug: path.read_bytes() for slug, path in paths.items()}

        held = self.run_cli("tick", now=1_000)
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertEqual(
            {slug: path.read_bytes() for slug, path in paths.items()}, before
        )
        self.assertEqual(self.calls(self.send_log), [])
        self.assertEqual(
            [call for call in self.calls(self.notifier_log) if call[0] == "resume"],
            [],
        )

        for review_path in review_paths:
            review_path.unlink()
        advanced = self.run_cli("tick", now=1_000)
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.assertEqual(
            self.record("asked-session")["phase"],
            {"kind": "returned", "returned_at": 1_000},
        )
        self.assertEqual(
            self.record("quiet-session")["phase"],
            asked_not_read(1_000),
        )
        self.assertFalse(paths["returned"].exists())
        self.assertEqual(len(self.calls(self.send_log)), 1)

    def test_review_file_is_ignored_without_a_showrunner_item(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        _ = self.create_instance("conversation-pause", run="tick")
        _ = self.write_record(
            asked_read(100, 1),
            instances=("delegate-abc",),
        )
        review_path = self.showrunner_root / "review-paused/demo.json"
        review_path.parent.mkdir(parents=True)
        _ = review_path.write_text("{}\n")

        result = self.run_cli("tick", now=400)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.record()["phase"], {"kind": "returned", "returned_at": 400}
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_unreadable_record_does_not_block_another_session(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        self.pause_root.mkdir(parents=True, exist_ok=True)
        _ = (self.pause_root / "a-broken.json").write_text("{not json")
        _ = self.write_record(
            asked_read(100, 1),
            instances=("delegate-abc",),
        )
        result = self.run_cli("tick", now=400)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conversation-pause: a-broken.json:", result.stderr)
        self.assertEqual(self.record()["phase"], {"kind": "returned", "returned_at": 400})
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=1\n"))

    def test_yes_with_nothing_left_to_turn_on_reads_as_a_full_sentence(self) -> None:
        _ = self.write_record(
            asked_read(100, 1),
            instances=("delegate-gone",), scheduled=False,
        )
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
            reply["systemMessage"],
            f"Automatic updates paused while we talk: status reports. {RETURN_SCHEDULE}",
        )
        self.assertEqual(
            self.record()["phase"],
            {"kind": "replying", "user_wrote_at": 400},
        )
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))

    def test_status_after_an_automatic_return_reads_not_paused(self) -> None:
        _ = self.write_record({"kind": "returned", "returned_at": 100})
        result = self.run_cli("status")
        self.assertEqual((result.returncode, result.stdout), (0, "not paused (returned)\n"))

    def test_release_returns_record_and_later_tick_resumes_nothing(self) -> None:
        instance = self.create_instance("delegate-abc", enabled=False)
        watcher = self.create_instance("conversation-pause", run="tick")
        path = self.write_record(
            {"kind": "kept_off"},
            instances=(instance.name,),
            footers=("demo",),
        )

        with patch.dict(os.environ, self.environment, clear=True):
            released = conversation_pause.release(SESSION)

        self.assertEqual(
            released,
            conversation_pause.PauseRecord(
                SESSION,
                (instance.name,),
                ("demo",),
                conversation_pause.KeptOff(),
            ),
        )
        self.assertFalse(path.exists())
        self.assertEqual(self.calls(self.notifier_log), [])

        result = self.run_cli("tick", now=NOW + 1)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((instance / "state").read_text().startswith("ENABLED=0\n"))
        self.assertFalse(watcher.exists())
        self.assertNotIn(
            ["resume", instance.name],
            self.calls(self.notifier_log),
        )

    def test_release_without_record_reports_absent(self) -> None:
        with patch.dict(os.environ, self.environment, clear=True):
            released = conversation_pause.release("never-paused")

        self.assertIs(released, conversation_pause.NoPauseRecord.ABSENT)

    def test_release_is_not_a_command_line_verb(self) -> None:
        result = self.run_cli("release", SESSION)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, conversation_pause.USAGE + "\n")


if __name__ == "__main__":
    _ = unittest.main()
