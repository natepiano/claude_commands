"""A full-context director stops only for a real wait or a later CLI wake-up."""

from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast


HOOK = Path(__file__).with_name("stop-direct-continue.py")
SESSION = "fixture-director"
# A 200k window reserves 33k for compaction and 25k for the handoff.
THRESHOLD = 142_000


@dataclass(frozen=True)
class DirectorStopFixture:
    root: Path
    run: Path
    marker: Path
    transcript: Path
    environment: dict[str, str]

    def write_usage(self, tokens: int) -> None:
        _ = self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {
                "model": "claude-sonnet-4", "usage": {"input_tokens": tokens},
            },
        }) + "\n", encoding="utf-8")

    def call(self, **fields: object) -> str:
        payload: dict[str, object] = {
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "background_tasks": [], **fields,
        }
        result = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, check=True, timeout=10,
            env=self.environment, cwd=self.root,
        )
        if result.stderr:
            raise AssertionError(result.stderr)
        return result.stdout


@contextmanager
def director_stop_fixture() -> Generator[DirectorStopFixture]:
    with tempfile.TemporaryDirectory(prefix="stop-hook-", dir="/tmp/claude") as temporary:
        root = Path(temporary)
        run = root / "run"
        run.mkdir()
        history = root / "history" / "run.jsonl"
        history.parent.mkdir()
        _ = history.write_text("", encoding="utf-8")
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "run_id": "fixture-run", "history_file": str(history),
            "working_dir": str(root), "status": "active",
            "main_agent": {"session_id": SESSION},
            "phase": {"id": "1", "instance_id": "fixture-phase", "status": "active"},
            "pass": {}, "open_phases": [],
        }), encoding="utf-8")
        active = root / "active"
        active.mkdir()
        marker = active / SESSION
        _ = marker.write_text(str(run), encoding="utf-8")
        config = root / "agents.conf"
        _ = write_registry(config, {'claude.context': {'default': '200k'}}, replace_sections=('claude.context',))
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1", "HERDR_BIN": "/nonexistent/herdr",
            "XDG_STATE_HOME": str(root / "state"), "UNIT_ACTIVE_DIR": str(active),
            "UNIT_HISTORY_DIR": str(history.parent), "UNIT_SYNC_WRITES": "0",
            "CLAUDE_PROBE_SESSION": "0", "AGENTS_CONFIG_FILE": str(config),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k",
        }
        fixture = DirectorStopFixture(root, run, marker, root / "transcript.jsonl", environment)
        fixture.write_usage(160_000)
        yield fixture


class DirectorStopTests(unittest.TestCase):
    def assert_blocked(self, output: str) -> None:
        self.assertTrue(output, "the hook allowed a stop with no live wake source")
        response = cast(dict[str, object], json.loads(output))
        self.assertEqual(response["decision"], "block")
        self.assertIn("compaction fires on the next request", str(response["reason"]))

    def test_finished_worker_heartbeat_does_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            _ = (fixture.run / "heartbeat.log").write_text(
                "[wrapper] fix:impl agent finished\n", encoding="utf-8")
            self.assert_blocked(fixture.call())

    def test_running_background_task_allows_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(background_tasks=[{"id": "launcher", "status": "running"}]), "")

    def test_finished_background_tasks_do_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            for status in ("completed", "failed", "cancelled"):
                with self.subTest(status=status):
                    self.assert_blocked(fixture.call(background_tasks=[{"status": status}]))

    def test_unreported_background_tasks_do_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(background_tasks=None))

    def test_handoff_threshold_blocks_without_a_wake_source(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.write_usage(THRESHOLD)
            self.assert_blocked(fixture.call())

    def test_context_below_handoff_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.write_usage(THRESHOLD - 1)
            self.assertEqual(fixture.call(), "")

    def test_unmeasurable_transcript_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.transcript.unlink()
            self.assertEqual(fixture.call(), "")

    def test_unconfigured_context_window_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            del fixture.environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
            self.assertEqual(fixture.call(), "")

    def test_subagent_allows_stop_even_with_full_context(self) -> None:
        with director_stop_fixture() as fixture:
            own = fixture.transcript.with_suffix("") / "subagents" / "agent-worker.jsonl"
            own.parent.mkdir(parents=True)
            _ = own.write_bytes(fixture.transcript.read_bytes())
            self.assertEqual(fixture.call(agent_id="worker"), "")

    def test_missing_session_id_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(session_id=""), "")

    def test_no_active_run_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.marker.unlink()
            self.assertEqual(fixture.call(), "")

    def test_declared_user_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(last_assistant_message="decision: approve the checkpoint"), "")
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForUser"', (fixture.root / "history/run.jsonl").read_text())

    def test_declared_supplier_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(last_assistant_message="blocked: waiting on session:supplier"), "")
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForSupplier"', (fixture.root / "history/run.jsonl").read_text())

    def test_continuation_after_a_block_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call())
            self.assertEqual(fixture.call(stop_hook_active=True), "")


if __name__ == "__main__":
    _ = unittest.main()
