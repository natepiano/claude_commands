"""Default Claude roles are recorded once without blocking hook delivery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.usage import agents_log


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

REPOSITORY = Path(__file__).resolve().parents[2]
HOOK = REPOSITORY / "scripts/hooks/session-start-agents-log.py"
REGISTERED_COMMAND = ('"$HOME/.claude/scripts/lib/py" '
                      '"$HOME/.claude/scripts/hooks/session-start-agents-log.py"')
SESSION = "a1b2c3d4-1234-4567-89ab-123456789abc"
SUBAGENT = "f00dbabe"
AT = datetime(2026, 10, 11, 17, 30, tzinfo=timezone.utc)


class SessionStartHookTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    log_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "usage"
        self.log_path = self.state / "agents.jsonl"
        self.enterContext(patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.state)}))

    def run_hook(self, payload: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload, cwd=self.root, env=os.environ.copy(),
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False,
        )

    def deliver(self, **payload: object) -> subprocess.CompletedProcess[str]:
        return self.run_hook(json.dumps(payload))

    def registered_usage_commands(self) -> dict[str, list[str]]:
        settings = cast(dict[str, object], json.loads(
            (REPOSITORY / "settings.json").read_text(encoding="utf-8")))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        commands: dict[str, list[str]] = {}
        for event, entries in hooks.items():
            for entry in entries:
                for hook in cast(list[dict[str, object]], entry["hooks"]):
                    command = hook.get("command")
                    if not isinstance(command, str) or "scripts/hooks/session-start-agents-log.py" not in command:
                        continue
                    self.assertEqual(hook["type"], "command", event)
                    self.assertNotIn("matcher", entry, event)
                    commands.setdefault(event, []).append(command)
        return commands

    def run_registered_hook(self, event: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        commands = self.registered_usage_commands().get(event, [])
        self.assertEqual(len(commands), 1, f"Expected one usage hook for {event}")
        home = self.root / "hook-home"
        if not home.exists():
            home.mkdir()
            (home / ".claude").symlink_to(REPOSITORY, target_is_directory=True)
        working_directory = self.root / "unrelated-working-directory"
        working_directory.mkdir(exist_ok=True)
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home), "USAGE_STATE_DIR": str(self.state),
            "XDG_CACHE_HOME": str(self.root / "cache"), "PYTHONDONTWRITEBYTECODE": "1",
        })
        return subprocess.run(
            ["sh", "-c", commands[0]], input=json.dumps(payload), cwd=working_directory,
            env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False,
        )

    def records(self) -> list[dict[str, object]]:
        if not self.log_path.is_file():
            return []
        return [cast(dict[str, object], json.loads(line))
                for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def assert_failed_delivery_is_harmless(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.records(), [])

    def test_usage_hook_is_registered_once_for_each_start_event_and_no_other_event(self) -> None:
        self.assertEqual(self.registered_usage_commands(), {
            "SessionStart": [REGISTERED_COMMAND],
            "SubagentStart": [REGISTERED_COMMAND],
        })

    def test_registered_session_start_command_records_once_from_an_unrelated_directory(self) -> None:
        payload: dict[str, object] = {
            "hook_event_name": "SessionStart", "source": "startup", "session_id": SESSION,
            "transcript_path": str(self.root / "parent.jsonl"), "cwd": str(self.root),
        }

        result = self.run_registered_hook("SessionStart", payload)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"]), (SESSION, "interactive"))
        before = self.log_path.read_bytes()

        repeated = self.run_registered_hook("SessionStart", payload)

        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (0, "", ""))
        self.assertEqual(self.log_path.read_bytes(), before)
        self.assertEqual(agents_log.assignments(self.log_path).problems, ())

    def test_registered_subagent_start_command_records_once_from_an_unrelated_directory(self) -> None:
        agent_id = "a4008deac73c98365"
        payload: dict[str, object] = {
            "hook_event_name": "SubagentStart", "session_id": SESSION,
            "agent_id": agent_id, "agent_type": "Explore",
            "transcript_path": str(self.root / "parent.jsonl"),
        }

        result = self.run_registered_hook("SubagentStart", payload)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"]), (agent_id, "subagent"))
        before = self.log_path.read_bytes()

        repeated = self.run_registered_hook("SubagentStart", payload)

        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (0, "", ""))
        self.assertEqual(self.log_path.read_bytes(), before)
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.problems, ())
        self.assertEqual(set(log.assignments), {agent_id})

    def test_session_payload_records_one_interactive_assignment(self) -> None:
        result = self.deliver(
            hook_event_name="SessionStart", source="startup", session_id=SESSION,
            transcript_path=str(self.root / "parent.jsonl"), cwd=str(self.root),
        )

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"]), (SESSION, "interactive"))
        at = datetime.fromisoformat(cast(str, records[0]["at"]))
        self.assertEqual(at.utcoffset(), timedelta(0))
        self.assertEqual(at.microsecond, 0)
        self.assertLessEqual(at, datetime.now(timezone.utc))
        self.assertEqual(agents_log.assignments(self.log_path).problems, ())

    def test_agent_payload_records_only_the_subagent_without_parent_metadata(self) -> None:
        result = self.deliver(
            hook_event_name="SubagentStart", session_id=SESSION, agent_id=SUBAGENT,
            transcript_path=str(self.root / "parent.jsonl"), agent_type="Explore",
        )

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), {"id", "role", "at"})
        self.assertEqual((records[0]["id"], records[0]["role"]), (SUBAGENT, "subagent"))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(set(log.assignments), {SUBAGENT})
        self.assertEqual(log.assignments[SUBAGENT][0].role, "subagent")
        self.assertEqual(log.problems, ())

    def test_agent_id_registers_without_a_parent_session_id(self) -> None:
        result = self.deliver(agent_id=SUBAGENT)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual([(row["id"], row["role"]) for row in self.records()], [(SUBAGENT, "subagent")])

    def test_empty_agent_id_falls_back_to_the_interactive_session(self) -> None:
        result = self.deliver(agent_id="", session_id=SESSION)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual([(row["id"], row["role"]) for row in self.records()], [(SESSION, "interactive")])

    def test_hook_then_worker_writer_uses_the_worker_role_from_its_assignment(self) -> None:
        result = self.deliver(session_id=SESSION)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        initial = agents_log.assignments(self.log_path).assignments[SESSION][0]
        delegated_at = initial.at + timedelta(seconds=5)

        agents_log.append(SESSION, "director", None, delegated_at)
        repeated = self.deliver(session_id=SESSION, hook_event_name="SessionStart", source="resume")

        self.assertEqual((repeated.returncode, repeated.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.assignments[SESSION], (
            initial, agents_log.Assignment(delegated_at, "director", None, False),
        ))
        self.assertEqual(agents_log.role_at(log, SESSION, delegated_at - timedelta(seconds=1)), initial)
        for query in (delegated_at, delegated_at + timedelta(hours=1)):
            with self.subTest(query=query):
                self.assertEqual(agents_log.role_at(log, SESSION, query),
                                 agents_log.Assignment(delegated_at, "director", None, False))

    def test_worker_writer_then_hook_preserves_the_worker_role_and_lens(self) -> None:
        agents_log.append(SESSION, "review", "adversary", AT)
        before = self.log_path.read_bytes()

        result = self.deliver(session_id=SESSION, hook_event_name="SessionStart", source="startup")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)
        log = agents_log.assignments(self.log_path)
        for query in (AT, AT + timedelta(days=1)):
            with self.subTest(query=query):
                self.assertEqual(agents_log.role_at(log, SESSION, query),
                                 agents_log.Assignment(AT, "review", "adversary", False))

    def test_hook_then_worker_writer_uses_the_worker_role_from_its_prefix_assignment(self) -> None:
        result = self.deliver(session_id=SESSION, hook_event_name="SessionStart", source="startup")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        initial = agents_log.assignments(self.log_path).assignments[SESSION][0]
        worker_at = initial.at + timedelta(seconds=1)

        agents_log.append(SESSION[:8], "worker", None, worker_at)
        repeated = self.deliver(session_id=SESSION, hook_event_name="SessionStart", source="resume")

        self.assertEqual((repeated.returncode, repeated.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(agents_log.role_at(log, SESSION, worker_at - timedelta(seconds=1)), initial)
        for query in (worker_at, worker_at + timedelta(seconds=1)):
            with self.subTest(query=query):
                self.assertEqual(agents_log.role_at(log, SESSION, query),
                                 agents_log.Assignment(worker_at, "worker", None))

    def test_eight_hex_worker_prefix_suppresses_registration_for_its_full_uuid(self) -> None:
        agents_log.append(SESSION[:8], "worker", None, AT)
        before = self.log_path.read_bytes()

        result = self.deliver(session_id=SESSION)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)
        self.assertEqual([(row["id"], row["role"]) for row in self.records()], [(SESSION[:8], "worker")])

    def test_unrelated_eight_hex_worker_does_not_suppress_the_session(self) -> None:
        agents_log.append("deadbeef", "worker", None, AT)

        result = self.deliver(session_id=SESSION)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual([(row["id"], row["role"]) for row in self.records()],
                         [("deadbeef", "worker"), (SESSION, "interactive")])

    def test_startup_resume_compact_and_clear_deliveries_write_one_assignment(self) -> None:
        before: bytes = b""
        for event, source in (
            ("SessionStart", "startup"), ("SessionStart", "resume"),
            ("SessionStart", "compact"), ("SessionStart", "clear"), ("PreCompact", "compact"),
        ):
            with self.subTest(event=event, source=source):
                result = self.deliver(session_id=SESSION, hook_event_name=event, source=source)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                if before:
                    self.assertEqual(self.log_path.read_bytes(), before)
                else:
                    before = self.log_path.read_bytes()

        self.assertEqual([(row["id"], row["role"]) for row in self.records()], [(SESSION, "interactive")])

    def test_repeated_subagent_deliveries_register_each_agent_once(self) -> None:
        for agent_id in (SUBAGENT, "cafebabe"):
            for event in ("SubagentStart", "SubagentStop", "SessionStart"):
                with self.subTest(agent_id=agent_id, event=event):
                    result = self.deliver(session_id=SESSION, agent_id=agent_id, hook_event_name=event)
                    self.assertEqual((result.returncode, result.stderr), (0, ""))

        self.assertEqual([(row["id"], row["role"]) for row in self.records()],
                         [(SUBAGENT, "subagent"), ("cafebabe", "subagent")])
        self.assertNotIn(SESSION, agents_log.assignments(self.log_path).assignments)

    def test_bad_json_returns_success_with_one_error_and_no_assignment(self) -> None:
        for payload in ("", "not JSON", '{"session_id":', "[" * 10_000 + "]" * 10_000):
            with self.subTest(payload=payload[:30]):
                self.assert_failed_delivery_is_harmless(self.run_hook(payload))

    def test_missing_or_empty_ids_return_success_with_one_error_and_no_assignment(self) -> None:
        for payload in ({}, {"hook_event_name": "SessionStart"}, {"session_id": ""},
                        {"session_id": "", "agent_id": ""}, {"session_id": "   "}):
            with self.subTest(payload=payload):
                self.assert_failed_delivery_is_harmless(self.run_hook(json.dumps(payload)))

    def test_nonobject_json_returns_success_with_one_error_and_no_assignment(self) -> None:
        for payload in ("null", "[]", "42", '"session-text"'):
            with self.subTest(payload=payload):
                self.assert_failed_delivery_is_harmless(self.run_hook(payload))

    def test_ledger_write_failure_returns_success_with_one_error(self) -> None:
        self.log_path.mkdir(parents=True)

        self.assert_failed_delivery_is_harmless(self.deliver(session_id=SESSION))

        self.assertTrue(self.log_path.is_dir())

    def test_invalid_utf8_tail_does_not_block_registration_or_repeat_delivery(self) -> None:
        agents_log.append("existing-session", "director", None, AT)
        before = self.log_path.read_bytes()
        with self.log_path.open("ab") as handle:
            _ = handle.write(b'{"id":"broken-\xff')

        result = self.deliver(session_id=SESSION, hook_event_name="SessionStart")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(set(log.assignments), {"existing-session", SESSION})
        self.assertEqual(log.assignments[SESSION][0].role, "interactive")
        self.assertEqual(len(log.problems), 1)
        self.assertIn(f"{self.log_path}:2:", log.problems[0])
        self.assertIn("utf-8", log.problems[0])
        self.assertTrue(self.log_path.read_bytes().startswith(before + b'{"id":"broken-\xff\n'))
        recorded = self.log_path.read_bytes()

        repeated = self.deliver(session_id=SESSION, source="resume")

        self.assertEqual((repeated.returncode, repeated.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), recorded)

    def test_subagent_state_directory_failure_returns_success_with_one_error(self) -> None:
        _ = self.state.write_text("cannot create a ledger inside this file", encoding="utf-8")

        self.assert_failed_delivery_is_harmless(self.deliver(session_id=SESSION, agent_id=SUBAGENT))

        self.assertEqual(self.state.read_text(encoding="utf-8"), "cannot create a ledger inside this file")


if __name__ == "__main__":
    _ = unittest.main()
