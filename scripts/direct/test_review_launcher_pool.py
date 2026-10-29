"""Check ignored pool flags, own claims, release and canceled turns."""

from __future__ import annotations

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.review_launcher_test_support import DIRECT_DIR as DIRECT_DIR
from pathlib import Path
from scripts.direct.review_launcher_test_support import ProgressState as ProgressState
from scripts.direct.review_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast, override
import json
import signal
import subprocess
from scripts.direct.wait_for_test_state import wait_until


def own_review_with_legacy_flags(
    fixture: support.ReviewLauncherFixture, session_dir: Path, *, lens: str = "contract", phase: str = "3",
    early: str = "", reviewer_type: str = "code", pass_index: int = 1,
) -> list[str]:
    """Keep real ignored flags while directing the fixture at its unit worker."""
    roster = session_dir / "mesh_roster.json"
    if not roster.exists():
        old = fixture.root / "pool-state/production/mesh/7/mesh_roster.json"
        entries = cast(dict[str, dict[str, object]], json.loads(old.read_text())) if old.exists() else {}
        record = dict(entries.get("code1", {"status": "done", "context_tokens": 0}))
        record.update({"thread_id": "thread-fixture", "role": "review"})
        _ = roster.write_text(json.dumps({"fixture-review": record}))
    command = support.ReviewLauncherFixture.pool_command(
        fixture, session_dir, lens=lens, phase=phase, early=early,
        reviewer_type=reviewer_type, pass_index=pass_index)
    command[2:2] = ["--to", "fixture-review"]
    return command


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    @override
    def pool_command(self, session_dir: Path, *, lens: str = "contract", phase: str = "3",
                     early: str = "", reviewer_type: str = "code", pass_index: int = 1) -> list[str]:
        return own_review_with_legacy_flags(self, session_dir, lens=lens, phase=phase, early=early,
                                           reviewer_type=reviewer_type, pass_index=pass_index)

    def test_ignored_code_flags_use_own_claim_compaction_and_records(self) -> None:
        session = self.start_implementation("pool-routing")
        self.finish_implementation(session)
        environment = self.pool_fixture(context_tokens=100001)
        environment["STUB_POOL_RETIRED"] = "1"
        environment["STUB_FINDINGS"] = "1. First finding\n2) Second finding\n"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [cast("list[str]", json.loads(line)) for line in
                 (self.root / "mesh-calls.jsonl").read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ["compact", "can-follow", "follow", "release-follow"])
        for call in calls:
            self.assertEqual(call[call.index("--session-dir") + 1], str(session))
        follow = calls[2]
        self.assertEqual(follow[follow.index("--cwd") + 1], str(self.working_dir))
        self.assertIn("--read-only", follow)
        self.assertNotIn("--image", follow)
        for flag in ("--summary-file", "--reply-file", "--log-file"):
            self.assertEqual(Path(follow[follow.index(flag) + 1]).parent, session)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        board = (session / "board.log").read_text()
        self.assertIn("[review] register:", board)
        self.assertIn("compacted fixture-review", board)
        self.assertFalse((Path(environment["STUB_POOL_MESH_DIR"]) / "board.log").exists())
        self.assertEqual(result.stderr, "review.sh: --pool code and --lease are ignored; using the unit reviewer.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        events = [event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]
        self.assertEqual([event.get("team_slot") for event in events], ["review", "review"])
        record, = self.turn_records(session)
        self.assertEqual({key: record[key] for key in ("arm", "reviewer", "thread_id", "unit", "phase", "kind", "lens", "pass", "status", "raised")},
                         {"arm": "own", "reviewer": "fixture-review", "thread_id": "thread-fixture", "unit": "", "phase": "3", "kind": "broad", "lens": "contract", "pass": 1, "status": "reviewed", "raised": 2})
        state = cast("dict[str, object]", json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(record["phase_instance_id"], cast("dict[str, object]", state["phase"])["instance_id"])
        self.assertEqual(record["history_file"], state["history_file"])
        self.assertLessEqual(cast(float, record["started_at"]), cast(float, record["finished_at"]))

    def test_ignored_first_lease_delivers_the_original_prompt_to_the_own_reviewer(self) -> None:
        session = self.start_implementation("pool-first-lease")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_LEASE_ORDINAL"] = "1"
        command = self.pool_command(session)
        original = Path(command[10]).read_text()
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "follow_message.txt").read_text(), original)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_own_compaction_with_a_live_turn_refuses_without_follow(self) -> None:
        session = self.start_implementation("pool-live")
        self.finish_implementation(session)
        environment = self.pool_fixture(context_tokens=100001)
        (session / "compact_still_live").touch()
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((session / "mesh_events.txt").read_text().splitlines(), ["compact"])
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((session / "review_status_contract").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ignored_code_flags_keep_failed_turn_findings_and_release_the_claim(self) -> None:
        session = self.start_implementation("pool-failed-turn")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_TURN_FAILURE"] = "1"
        environment["STUB_FINDINGS"] = "1. Partial finding\n"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 1)
        self.assertEqual(self.turn_records(session)[0]["status"], "error")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertIn("release-follow", (self.root / "mesh-calls.jsonl").read_text())

    def test_ignored_code_flags_still_refuse_reviewer_tree_changes(self) -> None:
        session = self.start_implementation("pool-edit")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_REVIEW_EDIT"] = str(self.working_dir / "reviewed.txt")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree", result.stderr)
        self.assertEqual(self.turn_records(session)[0]["status"], "error")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_ignored_code_flags_keep_the_explicit_own_recipient(self) -> None:
        session = self.start_implementation("ignored-code-recipient")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("ignored", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["reviewer"], "fixture-review")
        self.assertEqual(self.turn_records(session)[0]["arm"], "own")

    def test_ignored_code_flags_open_the_explicit_role_named_slot_and_record_an_own_review(self) -> None:
        session = self.start_implementation("ignored-code-slot")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        command = support.ReviewLauncherFixture.pool_command(self, session)
        command[2:2] = ["--slot", "review"]

        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "review.sh: --pool code and --lease are ignored; using the unit reviewer.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        roster = cast(dict[str, dict[str, object]], json.loads((session / "mesh_roster.json").read_text()))
        self.assertEqual(set(roster), {"project-review"})
        self.assertEqual(roster["project-review"]["role"], "review")
        record, = self.turn_records(session)
        self.assertEqual(record["arm"], "own")
        self.assertEqual(record["reviewer"], "project-review")
        self.assertEqual((session / "review_status_contract").read_text(), "reviewed\n")

    def test_refused_unit_resolution_records_error_and_names_the_refusal_once(self) -> None:
        session = self.start_implementation("refused-unit-reviewer")
        self.finish_implementation(session)
        script = self.root / "scripts/direct/workers.py"
        _ = script.write_text("import sys\nprint('reviewer conflict: keep the existing thread', file=sys.stderr)\nsys.exit(1)\n")
        command = support.ReviewLauncherFixture.pool_command(self, session)

        result = subprocess.run(command, env=self.environment(), capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([line for line in result.stderr.splitlines() if line.startswith("ERROR:")],
                         ["ERROR: no unit reviewer: reviewer conflict: keep the existing thread"])
        self.assertEqual((session / "review_status_contract").read_text(), "error\n")
        self.assertFalse((session / "mesh_events.txt").exists())
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_pool_requires_codex_mesh_before_allocation(self) -> None:
        session = self.start_implementation("pool-mesh-off")
        environment = self.pool_fixture()
        environment["UNIT_CODEX_MESH"] = "0"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("requires the Codex mesh", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_ignored_code_flags_record_an_own_claim_refusal(self) -> None:
        session = self.start_implementation("pool-busy")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        mesh = session
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"fixture-review": {
            "thread_id": "thread-fixture", "status": "running",
        }}), encoding="utf-8")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("worker is busy or not followable", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ignored_code_flags_write_the_own_slot_with_its_status_suffix(self) -> None:
        for lens in ("contract", ""):
            with self.subTest(lens=lens):
                session = self.start_implementation(f"pool-slot-{lens or 'closure'}")
                self.finish_implementation(session)
                result = subprocess.run(self.pool_command(session, lens=lens), env=self.pool_fixture(),
                                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                suffix = f"_{lens}" if lens else ""
                self.assertEqual((session / f"review_slot{suffix}").read_text(), "review\n")

    def test_own_claim_refusal_removes_a_stale_review_slot(self) -> None:
        session = self.start_implementation("pool-stale-slot")
        self.finish_implementation(session)
        slot = session / "review_slot_contract"
        _ = slot.write_text("pool_code99\n", encoding="utf-8")
        environment = self.pool_fixture()
        _ = (session / "mesh_roster.json").write_text(json.dumps({"fixture-review": {
            "thread_id": "thread-fixture", "status": "running", "role": "review"}}))
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(slot.exists())

    def test_legacy_and_own_flags_both_record_the_replacement_thread(self) -> None:
        for arm in ("pool", "own"):
            with self.subTest(arm=arm):
                session = self.start_implementation(f"replacement-{arm}")
                self.finish_implementation(session)
                if arm == "pool":
                    environment = self.pool_fixture()
                    command = self.pool_command(session)
                else:
                    environment = self.environment()
                    command = self.review_command(session, "", "", self.open_worker(session))
                environment["STUB_REPLACEMENT_THREAD"] = "thread-replacement"
                result = subprocess.run(command, env=environment, capture_output=True,
                                        text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.turn_records(session)[0]["thread_id"], "thread-replacement")
                self.assertEqual(self.turn_records(session)[0]["arm"], "own")
                self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_ignored_code_flags_keep_early_delivery_and_turn_records(self) -> None:
        session = self.start_implementation("pool-early")
        environment = self.pool_fixture()
        ready = session / "ready"
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session, early=str(ready)), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = session
            wait_until(lambda: (mesh / "follow_message.txt").exists() or process.poll() is not None, mesh, processes=(process,))
            self.assertIsNone(process.poll(), "review must remain active before delivery")
            self.assertTrue((mesh / "follow_message.txt").exists())
            self.assertFalse(any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)))
            self.finish_implementation(session)
            ready.touch()
            finish.touch()
            _, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, errors)
        finally:
            finish.touch()
            if process.poll() is None:
                process.terminate()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.turn_records(session)[0]["status"], "reviewed")
        self.assertEqual(len([event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]), 2)

    def test_ignored_code_flags_keep_signal_cancellation_and_claim_release(self) -> None:
        session = self.start_implementation("pool-signal")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = session
            wait_until(lambda: (mesh / "follow_message.txt").exists() or process.poll() is not None, mesh, processes=(process,))
            self.assertIsNone(process.poll(), "review must remain active until signal")
            self.assertTrue((mesh / "follow_message.txt").exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 143)
        finally:
            finish.touch()
            if process.poll() is None:
                process.terminate()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        state = cast(ProgressState, json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"]["review"]["status"], "active")
        slot = (session / "review_slot_contract").read_text().strip()
        self.assertEqual(slot, "review")
        environment["UNIT_TEAM_ROLE"] = slot
        _ = environment.pop("UNIT_PASS_OWNER", None)
        canceled = subprocess.run(
            ["python3", str(DIRECT_DIR / "progress_history.py"), "finish-pass",
             "--session-dir", str(session), "--status", "canceled", "--orphaned-launcher"],
            env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        state = cast(ProgressState, json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"][slot]["status"], "canceled")
        self.assertEqual(self.turn_records(session)[0]["status"], "canceled")
        self.assertIn("release-follow", (self.root / "mesh-calls.jsonl").read_text())

    def test_a_lease_without_a_pool_flag_is_ignored_and_keeps_the_own_turn(self) -> None:
        session = self.start_implementation("lease-only")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        command = self.pool_command(session)
        index = command.index("--pool")
        del command[index:index + 2]
        result = subprocess.run(command, env=environment, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("ignored", result.stderr)
        self.assertEqual(self.turn_records(session)[0]["arm"], "own")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_one_launcher_keeps_two_turn_records_with_the_same_start_second(self) -> None:
        session = self.start_implementation("two-turn-records")
        self.finish_implementation(session)
        # Exercise both writes in one launcher's exit path. The start time and
        # shell pid stay the same, while the finding counts distinguish them.
        source = self.review_script.read_text(encoding="utf-8")
        _ = self.review_script.write_text(source.replace(
            "  if ! write_turn_record; then\n",
            "  TURN_RAISED=3\n"
            + "  write_turn_record || exit 1\n"
            + "  TURN_RAISED=7\n"
            + "  if ! write_turn_record; then\n",
            1,
        ), encoding="utf-8")
        result = subprocess.run(self.pool_command(session), env=self.pool_fixture(),
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        records = self.turn_records(session)
        self.assertEqual(len(records), 2)
        self.assertCountEqual([record["raised"] for record in records], [3, 7])
        self.assertEqual(records[0]["started_at"], records[1]["started_at"])
        for record in records:
            self.assertEqual(record["status"], "reviewed")
            self.assertEqual(record["thread_id"], "thread-fixture")


if __name__ == "__main__":
    _ = unittest.main()
