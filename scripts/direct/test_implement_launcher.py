"""Test Claude launcher validation and artifact placement."""
from __future__ import annotations

import subprocess
from typing import cast

from scripts.direct import implement_launcher_test_support as support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_a_dispatch_names_its_director_in_the_run_folder(self) -> None:
        # Staff records the director for each worker.
        session_dir = self.start_phase("director")
        environment = self.environment()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = subprocess.run(
            ["bash", str(self.implement_script), str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded_worker(session_dir).parent, staff.ReportsTo(staff.native_id(self.staff_parent)))

    def test_named_phase_reaches_worker_verification_environment(self) -> None:
        stub = self.root / "claude-bin"
        _ = stub.write_text(stub.read_text().replace(
            "    baseline = session / 'phases/3/phase_baseline'",
            "    (session / 'wait-context').write_text(settings['UNIT_RUN_DIR'] + '|' + settings['UNIT_TEAM_ROLE'])\n"
            + "    baseline = session / 'phases/3/phase_baseline'"))
        session = self.start_phase("phase-environment")
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nWrite the retry path.\n")
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((session / "wait-context").read_text(), f"{session}|impl")

    def test_new_launch_keeps_heartbeat_watcher_output(self) -> None:
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text(
            '#!/usr/bin/env bash\nprintf "heartbeat watcher output\\n"\n: > "$TEST_HEARTBEAT_READY"\n')
        stub = self.root / "claude-bin"
        _ = stub.write_text(stub.read_text().replace(
            "    status = (root / 'worker_state').read_text().strip()",
            "    import subprocess\n"
            + "    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['TEST_HEARTBEAT_READY']], check=True)\n"
            + "    status = (root / 'worker_state').read_text().strip()"))
        session = self.start_phase("heartbeat-visible")
        result = self.launch(session, "impl", "impl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("heartbeat watcher output", result.stdout)

    def test_a_dispatch_with_no_pass_kind_is_refused(self) -> None:
        # What the incident actually looked like on the wire: a worker launched
        # with the sixth argument dropped. It used to run the agent and record
        # nothing; now nothing runs at all.
        session_dir = self.start_phase("dropped")
        result = subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), "test", "the retry tests", "",
                "writing the retry tests", "0", "test",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("pass_kind must be impl, test, fix, or review", result.stderr)
        # Refused before anything ran: no status file to be read as a live worker,
        # and no register line for peers to answer.
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertFalse((session_dir / "board.log").exists())

    def test_a_launch_naming_its_phase_writes_in_that_phase_directory(self) -> None:
        # Two phases running the same role each name their own, so neither overwrites the
        # other's status, summary or log, and the name stays off the worker's address.
        session_dir = self.start_phase("phase-directory")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--phase", "3", str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=self.environment(), timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session_dir / "phases" / "3"
        self.assertEqual((phase_dir / "impl_status_impl").read_text(encoding="utf-8").strip(), "implemented")
        self.assertTrue((phase_dir / "impl_agent_impl.log").is_file())
        self.assertEqual(sorted(path.name for path in session_dir.glob("impl_status*")), [])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertNotIn("3-impl", board)

    def test_an_unknown_pass_kind_is_refused_by_the_same_check(self) -> None:
        session_dir = self.start_phase("unknown")
        result = self.launch(session_dir, "implementation", "impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("got 'implementation'", result.stderr)

    def test_launcher_passes_its_prompt_to_the_launch_check(self) -> None:
        session = self.start_phase("checked-launch")
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nWrite the retry.\n")
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertTrue((session / "worker-started").exists())

    def test_launcher_stops_on_stale_prompt_and_missing_phase_refusals(self) -> None:
        for label, options, text in (
            ("stale-prompt", ("--phase", "3"), "Phase 9 — Other work\n"),
            ("missing-phase", (), "Write the retry.\n"),
        ):
            with self.subTest(label=label):
                session = self.start_phase(f"refused-{label}")
                _ = self.prompt_file.write_text(text)
                result = self.checked_launch(session, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((session / "worker-started").exists())
                self.assertEqual(self.launch_records(session, "pass_started"), [])
                refusal, = self.launch_records(session, "launch_refused")
                self.assertEqual(refusal["prompt_file"], str(self.prompt_file))
                self.assertTrue(refusal["reason"])
                self.assertIn("prompt" if label == "stale-prompt" else "phase", result.stderr.lower())


    def test_codex_workers_are_refused_before_artifacts_or_claims_change(self) -> None:
        session = self.start_phase("codex-refused")
        _ = write_registry(self.root / ".claude/config/models.conf", base_text=support.MODELS_REGISTRY)
        summary = session / "impl_summary_impl.txt"
        _ = summary.write_text("previous summary\n")
        before = (session / "progress_history_state.json").read_bytes()
        for options in ((), ("--to", "fixture-impl")):
            with self.subTest(options=options):
                result = self.checked_launch(session, *options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, "implement.sh: Codex workers must use assign.sh.\n")
                self.assertEqual(summary.read_text(), "previous summary\n")
                self.assertEqual((session / "progress_history_state.json").read_bytes(), before)
                self.assertFalse((session / "impl_claim_impl.json").exists())
                self.assertFalse((self.root / "launch_args.json").exists())
