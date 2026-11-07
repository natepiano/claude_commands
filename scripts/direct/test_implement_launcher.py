"""Test Claude launcher validation and artifact placement."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from scripts.whoami.account_test_support import write_codex_auth
from typing import cast

from scripts.direct import implement_launcher_test_support as support
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import RegistryReadProbe
from scripts.production import staff


def configure_pane_account(home: Path, notes: Path) -> None:
    """Give the child command a signed-in private worker account."""
    configuration = home / ".claude/config"
    configuration.mkdir(parents=True, exist_ok=True)
    _ = (configuration / "accounts.json").write_text(json.dumps({"workers": "codex1"}))
    notes.mkdir(parents=True, exist_ok=True)
    _ = (notes / "codex1.md").write_text("---\nlogin: worker@example.invalid\n---\n")
    account = home / ".codex"
    account.mkdir(parents=True, exist_ok=True)
    write_codex_auth(account, "worker@example.invalid")



class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_implementation_reads_one_registry_snapshot_across_its_child_processes(self) -> None:
        session = self.start_phase("registry-snapshot")
        environment = self.environment()
        probe = RegistryReadProbe(self.root, Path(environment["MODELS_CONFIG_FILE"]))
        result = subprocess.run(
            ["bash", str(self.implement_script), str(session), str(self.working_dir), str(self.prompt_file),
             "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            env=probe.environment(environment), capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(probe.reads()), 1, probe.reads())

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
        registry = self.root / ".claude/config/models.conf"
        _ = registry.write_text(registry.read_text().replace("direct=claude", "direct=codex"))
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

    def test_phase_claim_precedes_the_claude_worker_open(self) -> None:
        session = self.start_phase("claim-before-open")
        environment, calls = self.claim_fixture(session, "clear")
        result = subprocess.run(self.implement_command(session, "--phase", "3"),
                                capture_output=True, text=True, check=False, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["sid-director.phase-3 check reviewed.txt --json"])
        state = (session / "phases/3/delegated_phase_reservation_state.json").read_text()
        self.assertIn('"r1"', state)
        self.assertEqual((session / "mesh_events.txt").read_text().splitlines(), ["claim", "open"])

    def test_refused_phase_claim_prevents_the_claude_worker_open(self) -> None:
        session = self.start_phase("claim-refused")
        environment, calls = self.claim_fixture(session, "blocked")
        result = subprocess.run(self.implement_command(session, "--phase", "3"),
                                capture_output=True, text=True, check=False, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("phase 3 could not claim its files", result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertEqual((session / "mesh_events.txt").read_text().splitlines(), ["claim"])
        self.assertEqual(self.launch_records(session, "pass_started"), [])

    def test_unreadable_ledger_allows_the_worker_with_the_claim_pending(self) -> None:
        session = self.start_phase("claim-unreadable")
        environment, calls = self.claim_fixture(session, "ledger_unreadable")
        result = subprocess.run(self.implement_command(session, "--phase", "3"),
                                capture_output=True, text=True, check=False, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        state = (session / "phases/3/delegated_phase_reservation_state.json").read_text()
        self.assertIn("enrolled_awaiting_first_touch", state)
        self.assertEqual((session / "mesh_events.txt").read_text().splitlines(), ["claim", "open"])

    def test_new_worker_identity_precedes_the_heartbeat_watcher(self) -> None:
        session = self.start_phase("identity-before-heartbeat")
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('#!/bin/bash\ncat "$UNIT_RUN_DIR/impl_worker_impl.json" > "$UNIT_RUN_DIR/watcher-identity"\n')
        sender = self.root / "opening-send.py"
        _ = sender.write_text(sender.read_text().replace(
            "print('SENT: fixture')",
            "import subprocess\n"
            + f"subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], {str(session / 'watcher-identity')!r}], check=True)\n"
            + "print('SENT: fixture')"))
        result = self.worker_launch(session)
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = cast(dict[str, object], json.loads((session / "watcher-identity").read_text()))
        self.assertEqual(identity["staff_id"], self.recorded_worker(session).id)

    def test_model_resolution_failure_preserves_the_previous_pass(self) -> None:
        session = self.start_phase("resolve-failed")
        registry = self.root / ".claude/config/models.conf"
        _ = registry.write_text("invalid configuration\n")
        before = (session / "progress_history_state.json").read_bytes()
        result = self.checked_launch(session)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((session / "progress_history_state.json").read_bytes(), before)
        self.assertEqual(self.launch_records(session, "pass_started"), [])
