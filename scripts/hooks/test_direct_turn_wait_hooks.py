"""The registered turn hooks record declared waits against fixture run history."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast


HOOKS = Path(__file__).resolve().parent
SESSION = "wait-director"
STOP = "stop-direct-continue.py"
PROMPT = "user-prompt-submit-conversation-pause.py"


@dataclass(frozen=True)
class DirectorWaitFixture:
    root: Path
    run: Path
    history: Path
    environment: dict[str, str]

    def call(self, hook: str, fields: dict[str, object], at: int) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOKS / hook)],
            input=json.dumps({"session_id": SESSION, **fields}), text=True,
            capture_output=True, check=True, timeout=30,
            env={**self.environment, "UNIT_NOW_EPOCH": str(at)}, cwd=self.root,
        )
        if result.stderr or result.stdout:
            raise AssertionError(f"unexpected hook output: {result.stdout} {result.stderr}")

    def events(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.history.read_text().splitlines()]

    def wait_events(self) -> list[dict[str, object]]:
        return [event for event in self.events() if str(event.get("event_type")).startswith("wait_")]


@contextmanager
def director_wait_fixture() -> Generator[DirectorWaitFixture]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run = root / "run"
        run.mkdir()
        history = root / "history" / "runs" / "run.jsonl"
        history.parent.mkdir(parents=True)
        phase: dict[str, object] = {"id": "6", "instance_id": "original-six", "title": "Wait hooks",
                                    "status": "active", "started_at": 1000}
        state: dict[str, object] = {"run_id": "run", "history_file": str(history), "working_dir": str(root),
                                    "worktree": "fixture-unit", "status": "active", "phase": phase,
                                    "main_agent": {"session_id": SESSION}, "pass": {}, "open_phases": []}
        _ = (run / "progress_history_state.json").write_text(json.dumps(state))
        _ = history.write_text(json.dumps({"schema_version": 1, "event_type": "phase_started", "run_id": "run",
                                          "phase_id": "6", "phase_instance_id": "original-six",
                                          "timestamp_epoch": 1000, "wait_coverage": {"state": "Recorded"}}) + "\n")
        active = root / "active"
        active.mkdir()
        _ = (active / SESSION).write_text(str(run))
        true_command = shutil.which("true")
        false_command = shutil.which("false")
        if true_command is None or false_command is None:
            raise RuntimeError("wait-hook fixtures need true and false on PATH")
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root), "XDG_STATE_HOME": str(root / "state"),
            "UNIT_ACTIVE_DIR": str(active), "UNIT_HISTORY_DIR": str(root / "history"), "UNIT_SYNC_WRITES": "0",
            "CLAUDE_PROBE_SESSION": "0", "CLAUDE_CODE_SESSION_ID": SESSION,
            "CONVERSATION_PAUSE_STATE_DIR": str(root / "pause"), "NOTIFIER_STATE_DIR": str(root / "notifier"),
            "SHOWRUNNER_STATE_DIR": str(root / "showrunner"), "ESCALATE_STATE_DIR": str(root / "escalate"),
            "CONVERSATION_PAUSE_SESSIONS": false_command, "CONVERSATION_PAUSE_SEND": false_command,
            "CONVERSATION_PAUSE_NOTIFIER": false_command, "AUTO_SILENCE_COMMAND": true_command,
            "CONVERSATION_PAUSE_HOOK_BUDGET": "25",
        }
        yield DirectorWaitFixture(root, run, history, environment)


class DirectorTurnWaitHookTests(unittest.TestCase):
    def test_final_decision_and_gate_lines_start_one_user_wait_and_allow_the_stop(self) -> None:
        for final_line in ("decision: approve the checkpoint", "— gate: phase ready"):
            with self.subTest(final_line=final_line), director_wait_fixture() as fixture:
                fields: dict[str, object] = {"last_assistant_message": f"Checks complete.\n\n{final_line}\n",
                                            "stop_hook_active": True,
                                            "background_tasks": [{"status": "running"}]}
                fixture.call(STOP, fields, 1010)
                fixture.call(STOP, fields, 1020)
                waits = fixture.wait_events()
                self.assertEqual(len(waits), 1)
                wait = waits[0]
                self.assertEqual((wait["wait_kind"], wait["wait_reason"], wait["phase_instance_id"]),
                                 ("WaitingForUser", final_line.removeprefix("— "), "original-six"))
                marker = cast(dict[str, object], json.loads((fixture.run / "turn_wait.json").read_text()))
                self.assertEqual(marker["wait_id"], wait["wait_id"])

    def test_showrunner_and_peer_waits_finish_on_the_next_prompt(self) -> None:
        for final_line in ("blocked: waiting on the showrunner to merge", "— blocked: waiting on a peer review",
                           "blocked: waiting on claude-build-followups-other"):
            with self.subTest(final_line=final_line), director_wait_fixture() as fixture:
                fixture.call(STOP, {"last_assistant_message": final_line}, 1010)
                fixture.call(PROMPT, {"prompt": '<cross-session-message from-name="peer">Review complete.</cross-session-message>'}, 1040)
                waits = fixture.wait_events()
                self.assertEqual([wait["event_type"] for wait in waits], ["wait_started", "wait_finished"])
                self.assertEqual(waits[0]["wait_kind"], "WaitingForSupplier")
                self.assertEqual(waits[0]["wait_id"], waits[1]["wait_id"])
                self.assertEqual(cast(dict[str, object], waits[1]["wait_end"])["epoch"], 1040)
                self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_typed_user_prompt_finishes_the_user_wait_before_conversation_pause(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "decision: continue?"}, 1010)
            fixture.call(PROMPT, {"prompt": "Continue."}, 1050)
            fixture.call(PROMPT, {"prompt": "Another message."}, 1060)
            waits = fixture.wait_events()
            self.assertEqual([wait["event_type"] for wait in waits], ["wait_started", "wait_finished"])
            self.assertEqual(waits[1]["wait_started_epoch"], 1010)
            self.assertEqual(cast(dict[str, object], waits[1]["wait_end"])["epoch"], 1050)

    def test_only_the_final_line_of_a_director_stop_starts_a_wait(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "decision: example\nImplementation complete."}, 1010)
            fixture.call(STOP, {"last_assistant_message": "gate: a subagent result", "agent_id": "worker"}, 1020)
            fixture.call(STOP, {"session_id": "other-session", "last_assistant_message": "gate: unrelated"}, 1030)
            self.assertEqual(fixture.wait_events(), [])
            self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_probe_hooks_neither_start_nor_finish_a_wait(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.environment["CLAUDE_PROBE_SESSION"] = "1"
            fixture.call(STOP, {"last_assistant_message": "gate: probe"}, 1010)
            self.assertEqual(fixture.wait_events(), [])
            fixture.environment["CLAUDE_PROBE_SESSION"] = "0"
            fixture.call(STOP, {"last_assistant_message": "gate: real wait"}, 1020)
            fixture.environment["CLAUDE_PROBE_SESSION"] = "1"
            fixture.call(PROMPT, {"prompt": '<cross-session-message from-name="peer">Probe.</cross-session-message>'}, 1030)
            self.assertEqual(len(fixture.wait_events()), 1)
            self.assertTrue((fixture.run / "turn_wait.json").exists())

    def test_next_prompt_closes_the_saved_instance_after_the_current_phase_changes(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "decision: approve?"}, 1010)
            path = fixture.run / "progress_history_state.json"
            state = cast(dict[str, object], json.loads(path.read_text()))
            state["phase"] = {"id": "7", "instance_id": "new-seven", "status": "active"}
            _ = path.write_text(json.dumps(state))
            # Even a marker older than active_run's liveness window retains
            # the exact wait to close when the user finally answers.
            for file in fixture.run.iterdir():
                os.utime(file, (1, 1))
            fixture.call(PROMPT, {"prompt": '<cross-session-message from-name="showrunner">Approved.</cross-session-message>'}, 1100)
            waits = fixture.wait_events()
            self.assertEqual([wait["phase_instance_id"] for wait in waits], ["original-six", "original-six"])
            self.assertEqual(waits[1]["event_type"], "wait_finished")

    def test_a_subagent_prompt_does_not_finish_the_directors_wait(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "decision: user approval"}, 1010)
            fixture.call(PROMPT, {"prompt": "Worker input", "agent_id": "worker"}, 1030)
            self.assertEqual(len(fixture.wait_events()), 1)
            self.assertTrue((fixture.run / "turn_wait.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
