"""Check reviewer identity, reuse, compaction and interpreter resolution."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
from scripts.direct import review_launcher_test_support as support
from contextlib import ExitStack
from scripts.direct.local_release_wait import IntervalReleasePolling
from pathlib import Path
from typing import cast
import json
import os
from unittest.mock import patch
import subprocess
import sys
import tempfile
from scripts.direct import wait_for_test_state
from scripts.direct.wait_for_test_state import wait_until


class ReviewLauncherPassTests(support.ReviewLauncherFixture):
    def test_rerun_lens_uses_review_slot_and_its_own_findings_file(self) -> None:
        session = self.start_implementation("rerun-slot")
        self.finish_implementation(session)
        result = subprocess.run(self.review_command(session, "rerun", ""),
                                env=self.environment(), capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "review_findings_1_rerun.txt").read_text(), "No findings.\n")
        events = [event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]
        self.assertEqual([event.get("team_slot") for event in events], ["review", "review"])

    def test_named_review_phase_reaches_verification_environment(self) -> None:
        session = self.start_implementation("review-phase-environment")
        self.finish_implementation(session)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the diff.\n")
        stub = self.root / "scripts" / "agents" / "agent_exec.sh"
        with stub.open("a") as output:
            _ = output.write('printf "%s|%s" "$UNIT_RUN_DIR" "$UNIT_TEAM_ROLE" > "$UNIT_RUN_DIR/wait-context"\n')
        result = self.checked_review(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((session / "wait-context").read_text(), f"{session}|")

    def test_interpreter_cache_reuses_the_resolved_python_without_a_version_probe(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "binaries"
        binaries.mkdir()
        interpreter = binaries / "python3"
        calls = self.root / "python-calls"
        _ = interpreter.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$2" >> "$CACHE_TEST_CALLS"\nexec "$CACHE_TEST_REAL_PYTHON" "$@"\n'
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(self.root / "cold-cache"),
                            "CACHE_TEST_CALLS": str(calls), "CACHE_TEST_REAL_PYTHON": sys.executable})
        for _ in range(2):
            result = subprocess.run(["bash", str(launcher), "-c", "print('ready')"],
                                    env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual((result.returncode, result.stdout), (0, "ready\n"), result.stderr)
        commands = calls.read_text().splitlines()
        self.assertEqual(len(commands), 3)
        self.assertIn("sys.version_info", commands[0])
        self.assertEqual(commands[1:], ["print('ready')"] * 2)
        self.assertEqual((self.root / "cold-cache" / "claude" / "python3-3.11").read_text().strip(), str(interpreter))

    def test_interpreter_resolver_rejects_python310_and_its_old_cache(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "python310-bin"
        binaries.mkdir()
        interpreter = binaries / "python3"
        _ = interpreter.write_text(
            f"#!{sys.executable}\n"
            + "import datetime, sys\n"
            + "sys.version_info = (3, 10, 0, 'final', 0)\n"
            + "del datetime.UTC\n"
            + "exec(sys.argv[2])\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        cache_root = self.root / "python310-cache"
        old_cache = cache_root / "claude" / "python3-3.10"
        old_cache.parent.mkdir(parents=True)
        _ = old_cache.write_text(str(interpreter) + "\n")
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(cache_root)})
        result = subprocess.run(
            ["bash", str(launcher), "-c", "import datetime; print(datetime.UTC)"],
            env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual((result.returncode, result.stdout), (0, "UTC\n"), result.stderr)

    def test_a_named_open_worker_receives_the_review_and_writes_findings(self) -> None:
        session_dir = self.start_implementation("assigned")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "follow_message.txt").read_text(encoding="utf-8"),
            "Review the diff.\n",
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        reviews = [
            event for event in self.pass_events("assigned")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "impl"), ("pass_finished", "impl")],
        )
        self.assertEqual(reviews[0]["worker_name"], worker)

    def test_a_review_opened_worker_has_durable_identity_for_an_implementation_claim(self) -> None:
        session_dir = self.start_implementation("new-worker")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review2"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["start"],
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        worker = "project-review2"
        self.assertEqual(roster[worker]["status"], "done")
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        # The seat's log is its own, so a second seat on pass 1 cannot truncate it.
        self.assertTrue((session_dir / "review_agent_1-review2.log").is_file())
        self.assertEqual(os.readlink(session_dir / "review_agent.log"), "review_agent_1-review2.log")
        reviews = [
            event for event in self.pass_events("new-worker")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "review2"), ("pass_finished", "review2")],
        )
        identity = cast(
            "dict[str, object]",
            json.loads((session_dir / "impl_worker_review2.json").read_text()),
        )
        self.assertEqual(identity, {
            "name": worker,
            "slot": "review2",
            "family": "codex",
            "model": "gpt-called",
            "worker_id": f"thread-{worker}",
        })

        implementation = subprocess.run(
            [
                "bash", str(self.review_script.with_name("implement.sh")),
                "--to", worker, str(session_dir), str(self.working_dir),
                str(self.prompt_file), "impl", "write the repair",
                "impl", "writing the repair", "0", "review2",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(implementation.returncode, 0, implementation.stderr)
        self.assertEqual(
            (session_dir / "impl_status_review2").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_a_test_worker_keeps_one_identity_when_assigned_a_review(self) -> None:
        run = self.start_implementation("test-becomes-review")
        self.finish_implementation(run)
        worker = "project-test"
        identity = {"name": worker, "slot": "test", "role": "test", "family": "codex",
                    "model": "gpt-called", "worker_id": "thread-test"}
        path = run / "impl_worker_test.json"
        _ = path.write_text(json.dumps(identity))
        _ = (run / "mesh_roster.json").write_text(json.dumps({worker: {
            "thread_id": "thread-test", "status": "done", "role": "test", "context_tokens": 0}}))
        _ = (run / "workers").write_text(f"thread-test\t{worker}\n")

        reviewed = self.run_assigned_review(run, worker)

        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        records = [cast(dict[str, object], json.loads(item.read_text()))
                   for item in run.glob("impl_worker_*.json")]
        identities = [record for record in records if record.get("name") == worker]
        self.assertEqual(len(identities), 1, records)
        self.assertEqual(identities[0]["worker_id"], "thread-test")
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["name"], worker)
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["role"], "review")

    def test_hyphenated_new_slot_is_refused_to_keep_followup_identity_stable(self) -> None:
        session_dir = self.start_implementation("hyphenated-slot")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review-extra"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("alphanumeric/_ characters", result.stderr)
        self.assertFalse((session_dir / "mesh_roster.json").exists())

    def test_without_a_named_worker_the_fresh_read_only_path_is_unchanged(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session_dir / "mesh_events.txt").exists())
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_without_a_named_worker_the_unit_reviewer_runs_and_keeps_its_thread(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            ["bash", str(self.review_script), "--pool", "code", "--lease", "builders:3:broad:1",
             *self.review_command(session_dir, "", "")[2:]],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        events = (session_dir / "mesh_events.txt").read_text().splitlines()
        self.assertIn("follow", events)
        records = cast(dict[str, dict[str, object]], json.loads((session_dir / "mesh_roster.json").read_text()))
        self.assertEqual(len(records), 1)
        self.assertEqual(next(iter(records.values()))["role"], "review")
        self.assertEqual(self.turn_records(session_dir)[0]["arm"], "own")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_paused_reviewer_receives_followup_on_its_recorded_thread(self) -> None:
        session_dir = self.start_implementation("paused-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="paused")
        result = self.run_assigned_review(session_dir, worker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text().splitlines(), ["can-follow", "follow"])
        roster = cast(dict[str, dict[str, object]], json.loads((session_dir / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["thread_id"], "thread-fixture")
        self.assertEqual((session_dir / "follow_message.txt").read_text(), "Review the diff.\n")
        self.assertEqual((session_dir / "review_status").read_text(), "reviewed\n")

    def test_a_busy_named_worker_is_refused_before_review_artifacts_change(self) -> None:
        session_dir = self.start_implementation("busy")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="running")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"codex_mesh: {worker}: worker is busy or not followable", result.stderr)
        self.assertFalse((session_dir / "review_status").exists())
        self.assertFalse((session_dir / "review_findings_1.txt").exists())

    def test_a_large_named_worker_is_compacted_before_the_review_claim(self) -> None:
        session_dir = self.start_implementation("compact")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact", "can-follow", "follow"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"compacted {worker}: 100001 -> 12000 tokens", board)

    def test_review_of_an_unmeasured_worker_does_not_compact_again(self) -> None:
        session_dir = self.start_implementation("unmeasured-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        path = session_dir / "mesh_roster.json"
        roster = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        roster[worker]["context_tokens"] = "not_measured_since_compaction"
        _ = path.write_text(json.dumps(roster), encoding="utf-8")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["can-follow", "follow"])
        stored = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(stored[worker]["context_tokens"], "not_measured_since_compaction")

    def test_review_posts_the_unmeasured_compaction_line_unchanged(self) -> None:
        session_dir = self.start_implementation("compact-unmeasured-line")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_reports_zero").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher: compacted {worker}: 100001 tokens, size not measured until its next turn", board)

    def test_a_worker_left_with_a_live_turn_by_compaction_gets_no_review_follow_up(self) -> None:
        session_dir = self.start_implementation("compact-still-live")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_still_live").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the worker", result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)


class FixtureReadinessTests(unittest.TestCase):
    def assert_exit_wakes_wait(self, *, portable: bool = False, polling: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "watched"
            directory.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                assert child.stdout is not None and child.stdin is not None
                self.assertEqual(child.stdout.readline(), "ready\n")
                checks = 0

                def exited() -> bool:
                    nonlocal checks
                    checks += 1
                    if checks == 1:
                        assert child.stdin is not None
                        child.stdin.close()
                        return False  # Exit races the wait, after registration.
                    return child.poll() is not None

                def polling_waiter(_a: Path, _b: Path) -> IntervalReleasePolling:
                    return IntervalReleasePolling()

                with ExitStack() as patches:
                    if portable:
                        _ = patches.enter_context(patch.object(os, "pidfd_open",
                            side_effect=AttributeError("pidfd_open unavailable"), create=True))
                    if polling:
                        _ = patches.enter_context(patch.object(wait_for_test_state,
                            "local_release_waiter", side_effect=polling_waiter))
                    # The exit predicate proves the wakeup; the deadline only guards a hang.
                    wait_until(exited, directory, timeout=TEST_HANG_GUARD_SECS, processes=(child,))
                self.assertGreaterEqual(checks, 2)
                self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                self.assertEqual(list(directory.iterdir()), [])
            finally:
                if child.poll() is None:
                    child.kill()
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
                if child.stdout is not None:
                    child.stdout.close()

    def test_child_exit_wakes_a_wait_without_a_directory_write(self) -> None:
        self.assert_exit_wakes_wait()

    def test_child_exit_wakes_a_wait_without_pidfd_support(self) -> None:
        self.assert_exit_wakes_wait(portable=True)

    def test_child_exit_wakes_a_wait_without_directory_notifications(self) -> None:
        self.assert_exit_wakes_wait(polling=True)


if __name__ == "__main__":
    _ = unittest.main()
