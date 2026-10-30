"""Two real detached verify callers receive only their own result and verdict."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.agents import codex_mesh, mesh_runs
from scripts.direct.test_verify_tree_cancel import CANCEL_LINE, VerifyTreeCancelFixture
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.wait_for_test_state import wait_until


class MeshTreeCancelTests(VerifyTreeCancelFixture):
    def start_detached(self, fixture: VerifyTreeCancelFixture, identity: str, *, done_message: bool = False) -> Path:
        session = fixture.board
        session.mkdir(parents=True, exist_ok=True)
        _ = (session / codex_mesh.ROSTER_FILE).write_text(json.dumps({identity: {
            "thread_id": identity + "-thread", "status": "running", "launcher_pid": os.getpid(),
            "turn_id": identity + "-turn", "port": 4100,
        }}))
        environment = {**fixture.environment, "CODEX_THREAD_ID": identity + "-thread",
                       "TEST_FINISH": "1", "GUILD_ROLES_STATE": str(fixture.root / "roles"),
                       "REVIEW_POOL_STATE": str(fixture.root / "review-pool")}
        done_flags: list[str] = []
        if done_message:
            done = fixture.root / "done-message.txt"
            _ = done.write_text("pass-only completion message")
            done_flags = ["--done-message-file", str(done)]
        # Start through the public entry point. The real detached supervisor,
        # verify, git, watcher and durable caller inbox all remain unmocked.
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(mesh_runs, "RUN_ROOTS", (fixture.root,)), \
                contextlib.chdir(fixture.workspace), contextlib.redirect_stdout(io.StringIO()) as output:
            status = mesh_runs.main(["start", "--label", identity + " verify test", *done_flags, "--",
                                     "bash", str(VERIFY), "test", "sample"])
        self.assertEqual(status, 0, output.getvalue())
        records = list((session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = cast(mesh_runs.RunRecord, json.loads(records[0].read_text()))
        fixture.started_groups.add(record["pid"])
        self.assertEqual(record["thread_id"], identity + "-thread")
        return records[0]

    def finished_record(self, path: Path) -> mesh_runs.RunRecord:
        def finished() -> bool:
            return cast(dict[str, object], json.loads(path.read_text())).get("status") == "finished"

        # Directory events, not a status-file polling loop: completion replaces
        # the run record atomically after it has delivered the message.
        wait_until(finished, path.parent, timeout=30)
        return cast(mesh_runs.RunRecord, json.loads(path.read_text()))

    def inbox(self, fixture: VerifyTreeCancelFixture, identity: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads((fixture.board / (identity + ".pending.json")).read_text()))

    def test_two_simultaneous_callers_receive_only_their_own_cancellation_or_pass(self) -> None:
        VerifyTreeCancelFixture.setUpClass()
        self.addCleanup(VerifyTreeCancelFixture.doClassCleanups)
        other = VerifyTreeCancelFixture()
        other.setUp()
        self.addCleanup(other.tearDown)
        first_identity, second_identity = "edited-worker", "stable-worker"
        first_path = self.start_detached(self, first_identity, done_message=True)
        second_path = self.start_detached(other, second_identity)
        # Both must reach Cargo before either tree changes; a sequential run
        # would not prove simultaneous callers are isolated.
        try:
            wait_until((self.control / "running").exists, self.control, timeout=30)
            wait_until((other.control / "running").exists, other.control, timeout=30)
        except TimeoutError:
            logs = [path.with_suffix(".log").read_text(errors="replace") for path in (first_path, second_path)]
            self.fail("detached verify did not reach Cargo: " + repr(logs))
        self.assert_resources_held()
        other.assert_resources_held()
        self.edit_source()
        first = self.finished_record(first_path)
        self.assertEqual(first.get("exit_code"), 77, Path(first["log"]).read_text())
        self.assert_no_cache_verdict()
        self.assert_resources_released()
        self.assertFalse((other.board / (second_identity + ".pending.json")).exists(),
                         "stable caller received the other caller's cancellation")
        second_running = cast(dict[str, object], json.loads(second_path.read_text()))
        self.assertEqual(second_running.get("status"), "running", "stable caller stopped with edited caller")
        other.assert_resources_held()
        _ = (other.control / "release").touch()
        second = self.finished_record(second_path)
        self.assertEqual(second.get("exit_code"), 0, Path(second["log"]).read_text())
        other.assert_resources_released()
        self.assertEqual(len(list((other.board / "verify_cache").glob("*.pass"))), 1)
        cancelled = self.inbox(self, first_identity)
        passed = self.inbox(other, second_identity)
        self.assertEqual(cancelled.get("thread_id"), first_identity + "-thread")
        self.assertEqual(passed.get("thread_id"), second_identity + "-thread")
        cancelled_messages = cast(list[str], cancelled["messages"])
        passed_messages = cast(list[str], passed["messages"])
        self.assertEqual(len(cancelled_messages), 1, cancelled)
        self.assertEqual(len(passed_messages), 1, passed)
        cancel_text, pass_text = cancelled_messages[0], passed_messages[0]
        self.assertIn("exit 77", cancel_text)
        self.assertIn("cancelled", cancel_text.lower())
        self.assertIn("resubmit", cancel_text.lower())
        self.assertIn(CANCEL_LINE.format(cmd="test"), cancel_text)
        self.assertNotIn("pass-only completion message", cancel_text)
        self.assertNotIn("FAIL", cancel_text)
        self.assertIn("PASS (exit 0)", pass_text)
        self.assertNotIn("cancelled", pass_text.lower())
        self.assertNotIn("exit 77", pass_text)
        self.assertFalse((self.board / (second_identity + ".pending.json")).exists())
        self.assertFalse((other.board / (first_identity + ".pending.json")).exists())
        self.assertEqual([(record.get("outcome"), record.get("status")) for record in self.records()],
                         [("cancelled", 77)])
        self.assertEqual([(record.get("outcome"), record.get("status")) for record in other.records()],
                         [("ran", 0)])
        for fixture in (self, other):
            self.assertEqual(len([args for args in fixture.cargo_calls() if args[:2] == ["nextest", "run"]]), 1)
        # Keep only small failed-run evidence in temporary fixtures; successful
        # detached logs have now been read and can be removed.
        for record in (first, second):
            _ = Path(record["log"]).read_text()
            Path(record["log"]).unlink()


if __name__ == "__main__":
    _ = unittest.main()
