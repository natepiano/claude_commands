"""Private conversation-pause fixtures and real CLI calls without interpreter startup."""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from types import FrameType, ModuleType
from typing import ClassVar, NoReturn, cast, override
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))

import conversation_pause as conversation_pause
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.cli_fixture import invoke_cli

LIBRARY = Path(__file__).with_name("conversation_pause.py")
PROMPT_HOOK = Path(__file__).with_name("user-prompt-submit-conversation-pause.py")
STOP_HOOK = Path(__file__).with_name("stop-conversation-pause.py")
SETTINGS = LIBRARY.parent.parent.parent / "settings.json"
SESSION = "session-1"
NOW = 10_000
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300
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
    "dailies. Leave them off; the session asks the user before they return. "
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


# The prompt that ended auto silence at 04:34 AST on 2026-10-22: Claude Code typed it in a showrunner.
IDLE_NOTICE = (
    '[Cross-session idle notice] "cargo-liner", which you asked to be notified about, is idle now '
    "— it finished a turn at 04:34."
)


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


def retain_children[**Arguments](
    start: Callable[Arguments, subprocess.Popen[str] | subprocess.Popen[bytes]],
    children: list[subprocess.Popen[str] | subprocess.Popen[bytes]],
) -> Callable[Arguments, subprocess.Popen[str] | subprocess.Popen[bytes]]:
    """Keep detached handles alive until the hook has returned and can be reaped."""
    def launch(*args: Arguments.args, **kwargs: Arguments.kwargs) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
        child = start(*args, **kwargs)
        children.append(child)
        return child

    return launch


class ConversationPauseFixture(unittest.TestCase):
    run_in_subprocess: ClassVar[bool] = False
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
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
    activity_log: Path = Path()
    activity_stub: Path = Path()
    unit_active_root: Path = Path()
    socket_path: Path = Path()
    environment: dict[str, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="conversation-pause-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        builder = cls()
        builder.root = cls.fixture_root
        builder.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.root = self.fixture_root
        self.bind_environment()

    def build_fixture(self) -> None:
        self.root.mkdir()
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
import threading

TEST_HANG_GUARD_SECS = 300

args = sys.argv[1:]
with Path(os.environ["NOTIFIER_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
action, name = args[:2]
root = Path(os.environ["NOTIFIER_STATE_DIR"])
instance = root / name
if action == "new" and os.environ.get("FAIL_NEW") == "1":
    print("notifier new refused", file=sys.stderr)
    sys.exit(9)
if action in {"stop", "pause"}:
    if os.environ.get("STOP_HOLD") == "1":
        threading.Event().wait(TEST_HANG_GUARD_SECS)
    if name in os.environ.get("FAIL_STOP_FOR", "").split(","):
        print(f"notifier stop refused for {name}", file=sys.stderr)
        sys.exit(8)
if action in {"resume", "unpause"} and name in os.environ.get("FAIL_RESUME_FOR", "").split(","):
    print(f"notifier resume refused for {name}", file=sys.stderr)
    sys.exit(8)
if action in {"stop", "resume", "pause", "unpause"} and instance.exists():
    # Mirrors notifier.sh: `pause` marks a running timer, `unpause` brings back only a marked one,
    # and `stop` or `resume` clears the mark.
    state = instance / "state"
    lines = state.read_text().splitlines()
    enabled = "ENABLED=1" in lines
    paused = "PAUSED=1" in lines
    if action == "pause":
        paused, enabled = enabled, False
    elif action == "unpause":
        if paused:
            print("next_due=1")
        enabled = enabled or paused
        paused = False
    else:
        paused, enabled = False, action == "resume"
    kept = [line for line in lines if not line.startswith(("ENABLED=", "PAUSED="))]
    marks = [f"ENABLED={int(enabled)}"] + (["PAUSED=1"] if paused else [])
    state.write_text("\\n".join(marks + kept) + "\\n")
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
        self.activity_log = self.root / "auto-silence.args"
        self.activity_stub = self.root / "auto_silence.py"
        _ = self.activity_stub.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["AUTO_SILENCE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""
        )
        self.activity_stub.chmod(0o755)
        self.unit_active_root = self.root / "unit-active"
        self.unit_active_root.mkdir()
    def bind_environment(self) -> None:
        self.notifier_root = self.root / "notifier"
        self.pause_root = self.root / "conversation-pause"
        self.showrunner_root = self.root / "showrunner"
        self.escalate_root = self.root / "escalate"
        self.notifier_log = self.root / "notifier.args"
        self.sessions_log = self.root / "sessions.args"
        self.send_log = self.root / "send.args"
        self.socket_path = self.root / "session.sock"
        self.notifier_stub = self.root / "notifier.py"
        self.sessions_stub = self.root / "sessions.py"
        self.send_stub = self.root / "send.py"
        self.activity_log = self.root / "auto-silence.args"
        self.activity_stub = self.root / "auto_silence.py"
        self.unit_active_root = self.root / "unit-active"
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
            "CONVERSATION_PAUSE_HOOK_BUDGET": str(TEST_HANG_GUARD_SECS),
            "NOTIFIER_LOG": str(self.notifier_log),
            "SESSIONS_LOG": str(self.sessions_log),
            "SEND_LOG": str(self.send_log),
            "SESSION_SOCKET": str(self.socket_path),
            "SESSION_RUNNING": "1",
            # The detached auto silence call outlives the hook, so a stub that writes could still
            # be writing in the temp dir as it is removed. Only a test that waits for the log
            # writes one (`record_activity`).
            "AUTO_SILENCE_COMMAND": shutil.which("true") or "/bin/true",
            "AUTO_SILENCE_LOG": str(self.activity_log),
            "UNIT_ACTIVE_DIR": str(self.unit_active_root),
        }

    def create_instance(
        self,
        name: str,
        *,
        session_id: str = SESSION,
        enabled: bool = True,
        paused: bool = False,
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
        self.set_enabled(instance, enabled, paused=paused)
        return instance

    def set_enabled(self, instance: Path, enabled: bool, *, paused: bool = False) -> None:
        """`paused` marks the timer as one a conversation pause stopped, as `notifier.sh pause` does."""
        mark = "PAUSED=1\n" if paused else ""
        _ = (instance / "state").write_text(
            f"ENABLED={int(enabled)}\n{mark}NEXT_DUE=12000\n"
        )

    def write_record(
        self,
        phase: dict[str, object],
        *,
        instances: tuple[str, ...] = (),
        session_id: str = SESSION,
        scheduled: bool = True,
    ) -> Path:
        # A paused item is one whose schedule still exists; `scheduled=False` writes a record
        # naming schedules removed since, as switching a production to on demand does. An item
        # the test has not set up yet is set up as the pause leaves it: stopped and marked.
        for name in instances if scheduled else ():
            (self.notifier_root / name).mkdir(exist_ok=True)
        for name in instances if scheduled else ():
            if not (self.notifier_root / name / "state").exists():
                self.set_enabled(self.notifier_root / name, False, paused=True)
        self.pause_root.mkdir(parents=True, exist_ok=True)
        path = self.pause_root / f"{session_id}.json"
        _ = path.write_text(json.dumps({
            "session_id": session_id,
            "instances": list(instances),
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
        wait_until(path.exists, path.parent, TEST_HANG_GUARD_SECS)
        self.assertTrue(path.exists(), f"timed out waiting for {path}")

    def run_prompt(
        self,
        prompt: str,
        *,
        now: int = NOW,
        extra: dict[str, object] | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: float = TEST_HANG_GUARD_SECS,
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
        return self.invoke(
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
        timeout: float = TEST_HANG_GUARD_SECS,
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
        return self.invoke(
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
        return self.invoke(
            [sys.executable, str(LIBRARY), *args],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=TEST_HANG_GUARD_SECS,
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

    def record_activity(self) -> None:
        """Make the detached auto silence call write its arguments; every write then needs a wait."""
        self.environment["AUTO_SILENCE_COMMAND"] = str(self.activity_stub)

    def activity_calls(self, expected: int) -> list[list[str]]:
        """The detached call lands after the hook exits: wait for `expected` lines by file event."""
        def recorded() -> bool:
            return self.activity_log.exists() and len(self.activity_log.read_text().splitlines()) >= expected

        try:
            wait_until(recorded, self.root, TEST_HANG_GUARD_SECS)
        except TimeoutError:
            return []
        return [cast(list[str], json.loads(line)) for line in self.activity_log.read_text().splitlines()]

    def invoke(self, command: list[str], *, input: str = "", capture_output: bool = True,
               text: bool = True, check: bool = False, env: dict[str, str],
               timeout: float = TEST_HANG_GUARD_SECS) -> subprocess.CompletedProcess[str]:
        # Alarm expiry, blocked flock, re-entrant delivery and executable diagnostics
        # keep their actual process boundary. Other cases exercise the same scripts
        # and stand-ins in fresh namespaces, including every write and error path.
        if self.run_in_subprocess:
            return subprocess.run(command, input=input, capture_output=capture_output,
                                  text=text, check=check, env=env, timeout=timeout)

        real_run = subprocess.run

        def run_child(command: list[str], *, capture_output: bool = False,
                      text: bool = False, check: bool = False,
                      timeout: float | None = None) -> subprocess.CompletedProcess[str]:
            script = Path(command[0])
            if script.is_relative_to(self.root) and script.suffix == ".py":
                result = self.execute_script(script, command[1:], dict(os.environ))
                if check:
                    result.check_returncode()
                return result
            return real_run(command, capture_output=capture_output, text=text,
                            check=check, timeout=timeout)

        def hang_guard(_signal: int, _frame: FrameType | None) -> NoReturn:
            raise TimeoutError("conversation-pause fixture exceeded its hang guard")

        children: list[subprocess.Popen[str] | subprocess.Popen[bytes]] = []
        alarm_handler = signal.getsignal(signal.SIGALRM)
        alarm_timer = signal.getitimer(signal.ITIMER_REAL)
        try:
            _ = signal.signal(signal.SIGALRM, hang_guard)
            _ = signal.alarm(int(timeout))
            with patch.object(subprocess, "run", run_child), patch.object(
                subprocess, "Popen", retain_children(subprocess.Popen, children),
            ):
                return self.execute_script(Path(command[1]), command[2:], env, input)
        finally:
            _ = signal.alarm(0)
            _ = signal.signal(signal.SIGALRM, alarm_handler)
            _ = signal.setitimer(signal.ITIMER_REAL, *alarm_timer)
            for child in children:
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def execute_script(self, script: Path, arguments: list[str], environment: dict[str, str],
                       stdin: str = "") -> subprocess.CompletedProcess[str]:
        module = ModuleType("__main__")
        module.__file__ = str(script)
        code = compile(script.read_text(), str(script), "exec")

        def main() -> int:
            exec(code, vars(module))
            return 0

        # The hook's imported alert module has one import-time path. Rebind it
        # under the temporary home, and restore the caller's alarm and stdin.
        with patch.dict(os.environ, environment, clear=True), patch.object(sys, "path", sys.path.copy()):
            sys.path.insert(0, str(LIBRARY.parents[1] / "message"))
            import escalate
            import direct_run

        with patch.object(sys, "stdin", io.StringIO(stdin)), patch.object(
            escalate, "STATE", Path(environment["ESCALATE_STATE_DIR"]),
        ), patch.object(
            direct_run, "ACTIVE_OVERRIDE", environment["UNIT_ACTIVE_DIR"],
        ), patch.dict(sys.modules, {"__main__": module}):
            return invoke_cli(module, main, arguments, environment, self.root)
