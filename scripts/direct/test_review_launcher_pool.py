"""Check pool leases, claims, release and canceled turns."""

from __future__ import annotations

import unittest
from scripts.direct import review_launcher_test_support as support
from scripts.direct.review_launcher_test_support import DIRECT_DIR as DIRECT_DIR
from pathlib import Path
from scripts.direct.review_launcher_test_support import ProgressState as ProgressState
from scripts.direct.review_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast
import json
import signal
import subprocess
from scripts.direct.wait_for_test_state import wait_until


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def test_pool_mesh_claim_compaction_release_use_allocated_dir_and_unit_records_stay_local(self) -> None:
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
            self.assertEqual(call[call.index("--session-dir") + 1], environment["STUB_POOL_MESH_DIR"])
        follow = calls[2]
        self.assertEqual(follow[follow.index("--cwd") + 1], str(self.working_dir))
        self.assertIn("--read-only", follow)
        self.assertNotIn("--image", follow)
        for flag in ("--summary-file", "--reply-file", "--log-file"):
            self.assertEqual(Path(follow[follow.index(flag) + 1]).parent, session)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        board = (session / "board.log").read_text()
        self.assertIn("[pool_code1] register:", board)
        self.assertIn("compacted code1", board)
        self.assertFalse((Path(environment["STUB_POOL_MESH_DIR"]) / "board.log").exists())
        self.assertIn("review_pool: retired code0: reset failed", (session / "review_agent_1_contract-pool.log").read_text())
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])
        self.assertFalse(self.call_records("pool-calls.jsonl")[0]["fresh"])
        events = [event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]
        self.assertEqual([event.get("team_slot") for event in events], ["pool_code1", "pool_code1"])
        record, = self.turn_records(session)
        self.assertEqual({key: record[key] for key in ("arm", "pool", "reviewer", "thread_id", "unit", "phase", "kind", "lens", "pass", "lease_ordinal", "prime_hash", "status", "raised")},
                         {"arm": "pool", "pool": "production", "reviewer": "code1", "thread_id": "thread-pool", "unit": "unit", "phase": "3", "kind": "broad", "lens": "contract", "pass": 1, "lease_ordinal": 4, "prime_hash": "contract-hash", "status": "reviewed", "raised": 2})
        state = cast("dict[str, object]", json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(record["phase_instance_id"], cast("dict[str, object]", state["phase"])["instance_id"])
        self.assertEqual(record["history_file"], state["history_file"])
        self.assertLessEqual(cast(float, record["started_at"]), cast(float, record["finished_at"]))

    def test_a_first_pool_lease_delivers_the_original_prompt(self) -> None:
        session = self.start_implementation("pool-first-lease")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_LEASE_ORDINAL"] = "1"
        command = self.pool_command(session)
        original = Path(command[8]).read_text()
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "follow_message.txt").read_text(), original)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])

    def test_pool_compaction_with_live_turn_refuses_and_releases_without_follow(self) -> None:
        session = self.start_implementation("pool-live")
        self.finish_implementation(session)
        environment = self.pool_fixture(context_tokens=100001)
        (Path(environment["STUB_POOL_MESH_DIR"]) / "compact_still_live").touch()
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "mesh_events.txt").read_text().splitlines(), ["compact"])
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])
        self.assertEqual((session / "review_status_contract").read_text().strip(), "error")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_pool_failed_turn_releases_claim_and_pool_and_records_findings(self) -> None:
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
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        self.assertIn("release-follow", (self.root / "mesh-calls.jsonl").read_text())

    def test_pool_tree_change_is_refused_and_releases_allocation(self) -> None:
        session = self.start_implementation("pool-edit")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_REVIEW_EDIT"] = str(self.working_dir / "reviewed.txt")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree", result.stderr)
        self.assertEqual(self.turn_records(session)[0]["status"], "error")
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")

    def test_pool_excludes_to_and_slot_before_any_allocation(self) -> None:
        session = self.start_implementation("pool-exclusive")
        environment = self.pool_fixture()
        for option in ("--to", "--slot"):
            command = self.pool_command(session)
            command[2:2] = [option, "fixture-impl"]
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("excludes --to and --slot", result.stderr)
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

    def test_pool_claim_refusal_releases_allocation_and_records_error(self) -> None:
        session = self.start_implementation("pool-busy")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        mesh = Path(environment["STUB_POOL_MESH_DIR"])
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"code1": {
            "thread_id": "thread-pool", "status": "running",
        }}), encoding="utf-8")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("worker is busy or not followable", result.stderr)
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_pool_writes_allocated_slot_with_the_status_suffix(self) -> None:
        for lens in ("contract", ""):
            with self.subTest(lens=lens):
                session = self.start_implementation(f"pool-slot-{lens or 'closure'}")
                self.finish_implementation(session)
                result = subprocess.run(self.pool_command(session, lens=lens), env=self.pool_fixture(),
                                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                suffix = f"_{lens}" if lens else ""
                self.assertEqual((session / f"review_slot{suffix}").read_text(), "pool_code1\n")

    def test_failed_pool_allocation_removes_stale_slot(self) -> None:
        session = self.start_implementation("pool-stale-slot")
        self.finish_implementation(session)
        slot = session / "review_slot_contract"
        _ = slot.write_text("pool_code99\n", encoding="utf-8")
        environment = self.pool_fixture()
        environment["STUB_POOL_FAILURE"] = "1"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(slot.exists())

    def test_replaced_thread_is_recorded_from_the_turn_roster_and_released_to_pool(self) -> None:
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
                if arm == "pool":
                    release = self.call_records("pool-calls.jsonl")[-1]
                    self.assertEqual(release["verb"], "release")
                    self.assertEqual(release["thread_id"], "thread-replacement")

    def test_pool_early_launch_defers_pass_until_delivery_and_keeps_turn_record(self) -> None:
        session = self.start_implementation("pool-early")
        environment = self.pool_fixture()
        ready = session / "ready"
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session, early=str(ready)), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = Path(environment["STUB_POOL_MESH_DIR"])
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

    def test_pool_signal_leaves_pass_open_for_director_cancel_and_releases_reviewer(self) -> None:
        session = self.start_implementation("pool-signal")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = Path(environment["STUB_POOL_MESH_DIR"])
            wait_until(lambda: (mesh / "follow_message.txt").exists() or process.poll() is not None, mesh, processes=(process,))
            self.assertIsNone(process.poll(), "review must remain active until signal")
            self.assertTrue((mesh / "follow_message.txt").exists())
            process.send_signal(signal.SIGTERM)
            _, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 143, errors)
        finally:
            finish.touch()
            if process.poll() is None:
                process.terminate()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        state = cast(ProgressState, json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"]["pool_code1"]["status"], "active")
        slot = (session / "review_slot_contract").read_text().strip()
        self.assertEqual(slot, "pool_code1")
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

    def test_code_pool_refuses_ux_lens_before_allocation_or_turn(self) -> None:
        session = self.start_implementation("code-wrong-lens")
        self.finish_implementation(session)
        result = subprocess.run(self.pool_command(session, lens="ux"), env=self.pool_fixture(),
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "ERROR: --pool code does not accept lens ux.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())

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
            self.assertEqual(record["thread_id"], "thread-pool")


if __name__ == "__main__":
    _ = unittest.main()
