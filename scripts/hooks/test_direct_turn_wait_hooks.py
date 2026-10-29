"""The registered turn hooks record declared waits against fixture run history."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
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

    def message(self, body: str, *, address: str = "uds:/fixture/showrunner/socket", name: str = "another-name") -> str:
        return f'<cross-session-message from="{address}" from-name="{name}">{body}</cross-session-message>'


@contextmanager
def director_wait_fixture() -> Generator[DirectorWaitFixture]:
    with tempfile.TemporaryDirectory(dir="/tmp/claude") as temporary, ExitStack() as resources:
        root = Path(temporary)
        run = root / "run"
        run.mkdir()
        history = root / "history" / "runs" / "run.jsonl"
        history.parent.mkdir(parents=True)
        phase: dict[str, object] = {"id": "6", "instance_id": "original-six", "title": "Wait hooks",
                                    "status": "active", "started_at": 1000}
        state: dict[str, object] = {"run_id": "run", "history_file": str(history), "working_dir": str(root),
                                    "worktree": "fixture-unit", "status": "active", "phase": phase,
                                    "main_agent": {"session_id": SESSION}, "pass": {}, "open_phases": [],
                                    "unit": "fixture-unit", "project_plan_doc": "plan.md"}
        _ = (root / "plan.md").write_text('> **Production: wait-production** — unit `fixture-unit`; production doc `production.md`\n')
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
        notifier = root / "notifier" / "showrunner-wait-production"
        notifier.mkdir(parents=True)
        document = root / "wait-production.md"
        _ = document.write_text("Fixture production\n")
        _ = (notifier / "conf").write_text(
            f"TARGET=session:showrunner-id\nCHECK=zsh /scripts/production_check.sh {document}\n")
        sessions = root / "sessions"
        sessions.mkdir()
        for index, name in enumerate(("expected-showrunner", "claude-build-followups-other", "claude-one", "claude-two", "postmaster")):
            connection = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            address = str(root / f"socket-{index}")
            connection.bind(address)
            _ = (sessions / (f"{os.getpid()}.json" if index == 0 else f"peer-{index}.json")).write_text(json.dumps(
                {"pid": os.getpid(), "sessionId": "showrunner-id" if index == 0 else f"peer-{index}",
                 "name": name, "messagingSocketPath": address, "updatedAt": 1, "tmux": "", "formerNames": []}))
        _ = (root / "current_socket").write_text("/fixture/showrunner/socket")
        session_lookup = root / "session_lookup.py"
        _ = session_lookup.write_text(f"from pathlib import Path\nprint(Path({str(root / 'current_socket')!r}).read_text())\n")
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root), "XDG_STATE_HOME": str(root / "state"),
            "UNIT_ACTIVE_DIR": str(active), "UNIT_HISTORY_DIR": str(root / "history"), "UNIT_SYNC_WRITES": "0",
            "CLAUDE_PROBE_SESSION": "0", "CLAUDE_CODE_SESSION_ID": SESSION,
            "CONVERSATION_PAUSE_STATE_DIR": str(root / "pause"), "NOTIFIER_STATE_DIR": str(root / "notifier"),
            "SHOWRUNNER_STATE_DIR": str(root / "showrunner"), "ESCALATE_STATE_DIR": str(root / "escalate"),
            "CONVERSATION_PAUSE_SESSIONS": false_command, "CONVERSATION_PAUSE_SEND": false_command,
            "CONVERSATION_PAUSE_NOTIFIER": false_command, "AUTO_SILENCE_COMMAND": true_command,
            "CONVERSATION_PAUSE_HOOK_BUDGET": "25",
            "SHOWRUNNERS_SESSIONS": str(session_lookup), "NOTIFIER_SESSIONS_DIR": str(sessions),
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

    def test_supplier_wait_only_finishes_on_its_current_address(self) -> None:
        for final_line, sender, address in (
            ("blocked: waiting on the showrunner to merge", "showrunner:wait-production", "/fixture/showrunner/socket"),
            ("blocked: waiting on claude-build-followups-other", "claude-build-followups-other", "socket-1"),
            ("blocked: waiting on session:peer-1", "session:peer-1", "socket-1"),
        ):
            with self.subTest(final_line=final_line), director_wait_fixture() as fixture:
                fixture.call(STOP, {"last_assistant_message": final_line}, 1010)
                fixture.call(PROMPT, {"prompt": fixture.message("Review complete.", address="uds:/another/socket", name="expected-showrunner")}, 1020)
                self.assertEqual(len(fixture.wait_events()), 1)
                origin = "uds:" + (address if address.startswith("/") else str(fixture.root / address))
                fixture.call(PROMPT, {"prompt": fixture.message("Complete.", address=origin)}, 1040)
                fixture.call(PROMPT, {"prompt": fixture.message("Again.", address=origin)}, 1050)
                waits = fixture.wait_events()
                self.assertEqual([wait["event_type"] for wait in waits], ["wait_started", "wait_finished"])
                self.assertEqual(waits[0]["expected_supplier"], {"state": "NamedSupplier", "address": sender})
                self.assertEqual(waits[0]["wait_id"], waits[1]["wait_id"])
                self.assertEqual(cast(dict[str, object], waits[1]["wait_end"])["epoch"], 1040)
                self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_user_wait_ignores_notices_and_peers_until_typed_input(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "decision: approve?"}, 1010)
            for prompt in ('<cross-session-message from-name="timer">Tick.</cross-session-message>',
                           '<task-notification>done</task-notification>',
                           '<cross-session-message from-name="peer">Complete.</cross-session-message>'):
                fixture.call(PROMPT, {"prompt": prompt}, 1020)
                self.assertEqual(len(fixture.wait_events()), 1)
            fixture.call(PROMPT, {"prompt": "Approved."}, 1040)
            self.assertEqual(fixture.wait_events()[-1]["event_type"], "wait_finished")

    def test_only_expected_showrunner_can_relay_the_users_decision(self) -> None:
        for prefix in ("", "Message from expected-showrunner via postmaster: Decision [abcdef12]\n\n"):
            with self.subTest(prefix=prefix), director_wait_fixture() as fixture:
                fixture.call(STOP, {"last_assistant_message": "decision: approve?"}, 1010)
                text = prefix + "From the user (via the showrunner): Approved."
                fixture.call(PROMPT, {"prompt": fixture.message(text, address="uds:/another/socket", name="expected-showrunner")}, 1020)
                self.assertEqual(len(fixture.wait_events()), 1)
                fixture.call(PROMPT, {"prompt": fixture.message(text)}, 1040)
                self.assertEqual(fixture.wait_events()[-1]["event_type"], "wait_finished")

    def test_showrunner_restart_uses_its_new_address_from_the_plan_production(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on the showrunner to merge"}, 1010)
            _ = (fixture.root / "current_socket").write_text("/fixture/restarted/socket")
            fixture.call(PROMPT, {"prompt": fixture.message("Old socket")}, 1020)
            self.assertEqual(len(fixture.wait_events()), 1)
            fixture.call(PROMPT, {"prompt": fixture.message("Merged", address="uds:/fixture/restarted/socket", name="new-showrunner-name")}, 1040)
            self.assertEqual(fixture.wait_events()[-1]["event_type"], "wait_finished")

    def test_postmaster_forwarded_reply_leaves_the_wait_open_with_or_without_its_log(self) -> None:
        for declaration in ("decision: approve?", "blocked: waiting on the showrunner"):
            for log_exists in (False, True):
                with self.subTest(declaration=declaration, log_exists=log_exists), director_wait_fixture() as fixture:
                    supplier_socket = str(fixture.root / "socket-0")
                    _ = (fixture.root / "current_socket").write_text(supplier_socket)
                    fixture.call(STOP, {"last_assistant_message": declaration}, 1010)
                    started, = fixture.wait_events()
                    reply = "From the user (via the showrunner): Approved."
                    text = "Message from renamed-showrunner (their answer to director, passed on by postmaster) [1020000-1]:\n\n" + reply
                    postmaster = fixture.root / "state/message/postmaster"
                    (postmaster / "replies").mkdir(parents=True)
                    _ = (postmaster / "heartbeat.json").write_text(json.dumps({"session_id": "peer-4"}))
                    if log_exists:
                        _ = (postmaster / "replies/1020000-1.json").write_text(json.dumps({
                            "socket": "uds:" + supplier_socket, "at_ms": 1020000,
                            "body": reply, "forwarded": [SESSION]}))
                    message = fixture.message(text, address="uds:" + str(fixture.root / "socket-4"), name="postmaster")
                    fixture.call(PROMPT, {"prompt": message}, 1020)
                    self.assertEqual(fixture.wait_events(), [started])
                    self.assertTrue((fixture.run / "turn_wait.json").exists())
                    fixture.call(PROMPT, {"prompt": fixture.message(reply, address="uds:" + supplier_socket)}, 1030)
                    finished = fixture.wait_events()[-1]
                    self.assertEqual((finished["event_type"], finished["wait_id"]), ("wait_finished", started["wait_id"]))

    def test_separate_relay_with_the_suppliers_name_has_no_verified_origin(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on the showrunner"}, 1010)
            fixture.call(PROMPT, {"prompt": fixture.message("Merged", address="uds:/relay/socket", name="expected-showrunner")}, 1020)
            self.assertEqual(len(fixture.wait_events()), 1)

    def test_worker_address_alias_matches_its_roster_thread_and_not_its_name(self) -> None:
        with director_wait_fixture() as fixture:
            runs = fixture.root / "unit-runs"
            worker_run = runs / "supplier-run"
            worker_run.mkdir(parents=True)
            _ = (worker_run / "mesh_roster.json").write_text(json.dumps({"supplier-fix": {"thread_id": "supplier-thread"}}))
            fixture.environment["UNIT_RUN_ROOT"] = str(runs)
            supplier = "worker:supplier-run/supplier-fix"
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on " + supplier}, 1010)
            fixture.call(PROMPT, {"prompt": fixture.message("Ready", address="codex:another-thread", name=supplier)}, 1020)
            self.assertEqual(len(fixture.wait_events()), 1)
            fixture.call(PROMPT, {"prompt": fixture.message("Ready", address="codex:supplier-thread")}, 1040)
            self.assertEqual(fixture.wait_events()[-1]["event_type"], "wait_finished")

    def test_unnamed_supplier_remains_unresolved_and_different_supplier_replaces_wait(self) -> None:
        with director_wait_fixture() as fixture:
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on a peer review"}, 1010)
            fixture.call(PROMPT, {"prompt": '<cross-session-message from-name="peer">Complete.</cross-session-message>'}, 1020)
            self.assertEqual(len(fixture.wait_events()), 1)
            self.assertEqual(cast(dict[str, object], fixture.wait_events()[0]["expected_supplier"])["state"], "NoNamedSupplier")
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on claude-one"}, 1030)
            fixture.call(STOP, {"last_assistant_message": "blocked: waiting on claude-two"}, 1040)
            starts = [event for event in fixture.wait_events() if event["event_type"] == "wait_started"]
            self.assertEqual(len(starts), 3)
            self.assertEqual(starts[-1]["expected_supplier"], {"state": "NamedSupplier", "address": "claude-two"})
            self.assertEqual([event["event_type"] for event in fixture.wait_events()].count("wait_unclosed"), 2)

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
            fixture.call(PROMPT, {"prompt": "Approved."}, 1100)
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
