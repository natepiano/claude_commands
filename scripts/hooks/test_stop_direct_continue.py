"""A full-context director stops only for a real wait or a later CLI wake-up."""

from __future__ import annotations

from scripts.lib.test_models_registry import write_registry

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
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("stop-direct-continue.py")
SESSION = fixture_runtime_id("fixture-director")
# A 200k window reserves 33k for compaction and 25k for the handoff.
THRESHOLD = 142_000


@dataclass(frozen=True)
class DirectorStopFixture:
    root: Path
    run: Path
    staff_file: Path
    transcript: Path
    environment: dict[str, str]

    def write_usage(self, tokens: int) -> None:
        _ = self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {
                "model": "claude-sonnet-4", "usage": {"input_tokens": tokens},
            },
        }) + "\n", encoding="utf-8")

    def call(self, **fields: object) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "background_tasks": [], **fields,
        }
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            env=self.environment, cwd=self.root,
        )


@contextmanager
def director_stop_fixture() -> Generator[DirectorStopFixture]:
    with tempfile.TemporaryDirectory(prefix="stop-hook-") as temporary:
        root = Path(temporary)
        run = root / "unit" / "run"
        run.mkdir(parents=True)
        history = root / "history" / "run.jsonl"
        history.parent.mkdir()
        _ = history.write_text("", encoding="utf-8")
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "run_id": "fixture-run", "history_file": str(history),
            "working_dir": str(root),
            "main_agent": {"session_id": SESSION},
            "phase": {"id": "1", "instance_id": "fixture-phase", "status": "active"},
            "pass": {}, "open_phases": [],
        }), encoding="utf-8")
        config = root / "models.conf"
        _ = write_registry(config, {'claude.context': {'default': '200k'}}, replace_sections=('claude.context',))
        from scripts.lib.atomic_file import write_text_atomic

        row = staff.StaffRow(staff.StaffId(SESSION), "showrunner", "director", staff.StaffRoot(),
                             current_session=staff.StaffId(SESSION), run=staff.OpenRun(run))
        write_text_atomic(root / "staff.json", json.dumps({"schema": 2, "rows": [row.to_json()]}))
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1", "HERDR_BIN": "/nonexistent/herdr",
            "XDG_STATE_HOME": str(root / "state"), 
            "UNIT_HISTORY_DIR": str(history.parent), "UNIT_SYNC_WRITES": "0",
            "SCRATCH_ROOT": str(root / "scratch"),
            "CLAUDE_PROBE_SESSION": "0", "MODELS_CONFIG_FILE": str(config),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k", "STAFF_FILE": str(root / "staff.json"),
        }
        fixture = DirectorStopFixture(root, run, root / "staff.json", root / "transcript.jsonl", environment)
        fixture.write_usage(160_000)
        yield fixture


class DirectorStopTests(unittest.TestCase):
    def assert_blocked(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn(
            "Compaction runs when the context reaches 167,000 tokens. It does not run while the session is idle.\n"
            + "Take the next workflow action now. Do not wait for compaction.\n"
            + "After compaction, read ~/.claude/commands/unit/direct.md in full.\n"
            + "Then read each stub file whose moment is live.\n"
            + "If you need a user decision, state it in one line and end the turn.\n", result.stderr)

    def assert_allowed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_finished_worker_heartbeat_does_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            _ = (fixture.run / "heartbeat.log").write_text(
                "[wrapper] fix:impl agent finished\n", encoding="utf-8")
            self.assert_blocked(fixture.call())

    def test_running_background_task_allows_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(background_tasks=[{"id": "launcher", "status": "running"}]))

    def test_finished_background_tasks_do_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            for status in ("completed", "failed", "cancelled"):
                with self.subTest(status=status):
                    self.assert_blocked(fixture.call(
                        background_tasks=[{"status": status}], last_assistant_message=status))

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
            self.assert_allowed(fixture.call())

    def test_unmeasurable_transcript_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.transcript.unlink()
            self.assert_allowed(fixture.call())

    def test_unconfigured_context_window_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            del fixture.environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
            self.assert_allowed(fixture.call())

    def test_subagent_allows_stop_even_with_full_context(self) -> None:
        with director_stop_fixture() as fixture:
            own = fixture.transcript.with_suffix("") / "subagents" / "agent-worker.jsonl"
            own.parent.mkdir(parents=True)
            _ = own.write_bytes(fixture.transcript.read_bytes())
            self.assert_allowed(fixture.call(agent_id="worker"))

    def test_missing_session_id_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(session_id=""))

    def test_no_active_run_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            _ = fixture.staff_file.write_text(json.dumps({"schema": 2, "rows": []}))
            self.assert_allowed(fixture.call())

    def test_unreadable_staff_allows_stop_without_output(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.environment["STAFF_FILE"] = str(fixture.root)
            self.assert_allowed(fixture.call())

    def test_declared_user_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="decision: approve the checkpoint"))
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForUser"', (fixture.root / "history/run.jsonl").read_text())

    def test_declared_supplier_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="blocked: waiting on session:supplier"))
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForSupplier"', (fixture.root / "history/run.jsonl").read_text())

    def test_same_final_line_above_the_threshold_does_not_wake_again(self) -> None:
        with director_stop_fixture() as fixture:
            status = "holding: waiting on the showrunner's restart order"
            self.assert_blocked(fixture.call(last_assistant_message=f"First turn.\n— {status}"))
            self.assert_allowed(fixture.call(last_assistant_message=f"Second turn.\n  — {status}  \n"))
            self.assertEqual((fixture.run / "compaction_wake_line").read_text(encoding="utf-8"), status)

    def test_continuation_with_a_different_final_line_above_the_threshold_wakes_again(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="The first action is complete."))
            status = "The next action is complete."
            self.assert_blocked(fixture.call(stop_hook_active=True, last_assistant_message=status))
            self.assertEqual((fixture.run / "compaction_wake_line").read_text(encoding="utf-8"), status)
            self.assert_allowed(fixture.call(stop_hook_active=True, last_assistant_message=status))

    def test_holding_status_without_pause_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="Work is ready.\n  — holding: the next workflow action  \n"))

    def test_blocked_status_without_pause_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="blocked: the next test needs a fixture"))
            self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_blocked_pause_order_above_the_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message=(
                "blocked: pause order from the user through the showrunner; only the showrunner restarts this work"
            )))
            self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_paused_status_without_a_prefix_above_the_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="The production is PAUSED by the user."))

    def test_blocked_within_a_sentence_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="The first test was blocked by a missing fixture."))


if __name__ == "__main__":
    _ = unittest.main()
