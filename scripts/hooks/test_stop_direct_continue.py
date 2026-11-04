"""The direct Stop hook records waits and prints nothing."""

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
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


HOOK = Path(__file__).with_name("stop-direct-continue.py")
SESSION = "fixture-director"


@dataclass(frozen=True)
class DirectorStopFixture:
    root: Path
    run: Path
    transcript: Path
    environment: dict[str, str]

    def call(self, **fields: object) -> str:
        payload: dict[str, object] = {
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "background_tasks": [], **fields,
        }
        result = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS,
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
        config = root / "models.conf"
        _ = write_registry(config, {'claude.context': {'default': '200k'}}, replace_sections=('claude.context',))
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1", "HERDR_BIN": "/nonexistent/herdr",
            "XDG_STATE_HOME": str(root / "state"), "UNIT_ACTIVE_DIR": str(active),
            "UNIT_HISTORY_DIR": str(history.parent), "UNIT_SYNC_WRITES": "0",
            "CLAUDE_PROBE_SESSION": "0", "MODELS_CONFIG_FILE": str(config),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k",
        }
        transcript = root / "transcript.jsonl"
        _ = transcript.write_text(json.dumps({
            "type": "assistant", "message": {
                "model": "claude-sonnet-4", "usage": {"input_tokens": 160_000},
            },
        }) + "\n", encoding="utf-8")
        fixture = DirectorStopFixture(root, run, transcript, environment)
        yield fixture


class DirectorStopTests(unittest.TestCase):
    def test_stop_past_the_handoff_threshold_prints_nothing(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(), "")

    def test_declared_user_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assertEqual(fixture.call(last_assistant_message="decision: approve the checkpoint"), "")
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForUser"', (fixture.root / "history/run.jsonl").read_text())


if __name__ == "__main__":
    _ = unittest.main()
