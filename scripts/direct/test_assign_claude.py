"""Test Claude launcher validation and artifact placement."""
from __future__ import annotations

import json
import os
import shutil
import shlex
import signal
import socket
from contextlib import ExitStack, suppress
import subprocess
from pathlib import Path
from unittest.mock import patch
from scripts.whoami.account_test_support import write_codex_auth
from typing import ClassVar, cast

from scripts.direct import assign_claude_test_support as support
from scripts.lib.test_models_registry import RegistryReadProbe
from scripts.lib import models_registry, process_group, process_start, unit_runs
from scripts.lib.socket_test_support import short_socket_directory
from scripts.direct import worker_state
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.production.cli_fixture import process_boundary


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



GROUP_STOP_READY = """import json, os, socket
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect(os.environ['TEST_STOP_SOCKET'])
    connection.sendall((json.dumps([os.getpid(), os.getpgrp(), 'stop']) + '\\n').encode())
    connection.recv(1)
"""


def stop_assignment(command: list[str], environment: dict[str, str], number: int, *, private_worker: bool = False) -> tuple[subprocess.CompletedProcess[str], int]:
    """Stop a private assignment group from the external test controller."""
    with short_socket_directory() as directory, socket.socket(socket.AF_UNIX) as listener:
        address = str(Path(directory) / "ready")
        listener.bind(address)
        listener.listen()
        listener.settimeout(TEST_HANG_GUARD_SECS)
        with subprocess.Popen(command[3:], env={**environment, "TEST_STOP_SOCKET": address},
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as launcher:
            try:
                with ExitStack() as connections:
                    peers: list[socket.socket] = []
                    child = 0
                    for _ in range(2 if private_worker else 1):
                        connection = connections.enter_context(listener.accept()[0])
                        connection.settimeout(TEST_HANG_GUARD_SECS)
                        with connection.makefile() as incoming:
                            pid, group, kind = cast(tuple[int, int, str], json.loads(incoming.readline()))
                        assert group == launcher.pid, (group, launcher.pid)
                        if kind == "stop":
                            child = pid
                        peers.append(connection)
                    assert child > 0
                    process_group.send_process_group(launcher.pid, number)
                    for connection in peers:
                        assert connection.recv(1) == b""
                    output, errors = launcher.communicate(timeout=TEST_HANG_GUARD_SECS)
                    assert isinstance(process_start.process_start(child), (process_start.Gone, process_start.Zombie))
                    if private_worker:
                        pid = int((Path(environment["STUB_CLAUDE_ROOT"]) / "private-worker-pid").read_text())
                        assert isinstance(process_start.process_start(pid), (process_start.Gone, process_start.Zombie))
                return subprocess.CompletedProcess(command, launcher.returncode, output, errors), child
            finally:
                with suppress(ProcessLookupError):
                    process_group.send_process_group(launcher.pid, signal.SIGKILL)
                _ = launcher.communicate(timeout=TEST_HANG_GUARD_SECS)


def private_worker_backend(root: Path, source: str) -> str:
    """Register a private worker that inherits the launcher's process group."""
    _ = (root / "private-worker.py").write_text(GROUP_STOP_READY.replace("'stop'", "'worker'"))
    return source.replace("if sys.argv[1] == '--bg':", """if sys.argv[1] == '--bg':
    import subprocess
    from scripts.message import sessions
    worker = subprocess.Popen([sys.executable, str(root / 'private-worker.py')],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / 'private-worker-pid').write_text(str(worker.pid))
    os.environ['FIXTURE_BACKEND_PID'] = str(worker.pid)
    os.environ['FIXTURE_BACKEND_START'] = sessions.process_start(worker.pid) or ''""")


class AssignClaudeLauncherWorkerTests(support.AssignClaudeLauncherFixture):
    direct_cli_calls: ClassVar[bool] = True

    @process_boundary
    def test_group_stop_after_the_start_banner_ends_the_private_worker(self) -> None:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(number=number):
                session = self.start_phase(f"banner-{number}")
                backend = self.root / "claude-bin"
                original = backend.read_text()
                text = private_worker_backend(self.root, original).replace(
                    "    print(f'backgrounded · abc12345 · {name}')",
                    "    print(f'backgrounded · abc12345 · {name}', flush=True)\n"
                    + "\n".join("    " + line for line in GROUP_STOP_READY.splitlines()))
                _ = backend.write_text(text)
                try:
                    result, pid = stop_assignment(self.assign_command(session), self.launch_environment(session), number, private_worker=True)
                finally:
                    _ = backend.write_text(original)
                self.assertEqual(result.returncode, -number,
                    result.stdout + result.stderr)
                self.assertIsInstance(process_start.process_start(pid), (process_start.Gone, process_start.Zombie))
                worker_pid = int((self.root / "private-worker-pid").read_text())
                self.assertIsInstance(process_start.process_start(worker_pid), (process_start.Gone, process_start.Zombie))
                _ = worker_state.worker_status_paths(session)
                self.assertEqual((session / "impl_status_impl").read_text(), "error\n")
                self.assertIn("this worker is down", (session / "board.log").read_text())
                for record in (self.root / "sessions").glob("*.json"):
                    record.unlink()

    def test_an_invalid_registry_wait_records_a_failed_pass_and_status(self) -> None:
        session = self.start_phase("invalid-registry-wait")
        _ = self.config_file.write_text(self.config_file.read_text().replace(
            "CLAUDE_WORKER_REGISTER_WAIT_SECS=30", "CLAUDE_WORKER_REGISTER_WAIT_SECS=invalid"))
        result = self.worker_launch(session)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        finished, = self.launch_records(session, "pass_finished")
        self.assertEqual(finished["status"], "error")
        self.assertEqual((session / "impl_status_impl").read_text(), "error\n")

    @process_boundary
    def test_group_term_during_the_worker_wait_records_a_dead_launcher(self) -> None:
        session = self.start_phase("term-during-wait")
        stub = self.root / "claude-bin"
        _ = stub.write_text(private_worker_backend(self.root, stub.read_text()).replace(
            "(root / 'worker_state').write_text('idle')",
            "(root / 'worker-wait-ready').touch()"))
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$3" > "$STUB_CLAUDE_ROOT/agent-pid"
python3 "$TEST_WAIT_HELPER" "$STUB_CLAUDE_ROOT/worker-wait-ready"
exec python3 -c ''' + shlex.quote(GROUP_STOP_READY) + "\n")
        result, watcher_pid = stop_assignment(self.assign_command(session), self.launch_environment(session), signal.SIGTERM, private_worker=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        agent_pid = int((self.root / "agent-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(agent_pid, 0)
        self.assertIsInstance(process_start.process_start(watcher_pid), (process_start.Gone, process_start.Zombie))
        worker_pid = int((self.root / "private-worker-pid").read_text())
        self.assertIsInstance(process_start.process_start(worker_pid), (process_start.Gone, process_start.Zombie))
        _ = worker_state.worker_status_paths(session)
        self.assertEqual((session / "impl_status_impl").read_text(), "error\n")
        self.assertEqual(self.open_passes(session)["impl"]["status"], "active")
        self.assertEqual(self.launch_records(session, "pass_finished"), [])
        board = (session / "board.log").read_text()
        self.assertIn("blocked:", board)
        self.assertIn("this worker is down", board)
        self.assertNotIn("done:", board)

    def test_a_custom_launch_configuration_reaches_the_worker_as_a_file_path(self) -> None:
        session = self.start_phase("custom-models-path")
        environment = self.launch_environment(session)
        custom = self.root / "custom-models.conf"
        _ = custom.write_text(Path(environment.pop("MODELS_CONFIG_FILE")).read_text())
        configuration = models_registry.launch_configuration({
            "MODELS_LAUNCH_CONFIGURATION": json.dumps([str(custom), custom.read_text()]),
        })
        stub = self.root / "claude-bin"
        _ = stub.write_text(stub.read_text().replace(
            "    baseline = session / 'phases/3/phase_baseline'",
            "    (root / 'worker-models-path').write_text(settings['MODELS_CONFIG_FILE'])\n"
            + "    baseline = session / 'phases/3/phase_baseline'"))
        result = self.run_assignment(self.assign_command(session), configuration.environment(environment))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "worker-models-path").read_text(), str(custom.resolve()))

    def test_a_failed_claude_launch_ends_its_run_and_keeps_its_worker_row(self) -> None:
        session = self.start_phase("failed-worker")
        environment = self.launch_environment(session)
        environment.update({
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "visible-scratch"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
            "DESIGN_ROLES_STATE": str(self.root / "designs"),
        })
        # This backend has no native worker panes to remove.
        _ = (self.assign_script.parent / "remove_workers.py").write_text("")
        copies = self.root / "scripts/copies"
        copies.mkdir(exist_ok=True)
        _ = shutil.copy2(support.DIRECT_DIR.parent / "copies/copies.py", copies / "copies.py")
        environment["COPIES_CONFIG"] = str(support.DIRECT_DIR.parents[1] / "config/copies.conf")
        with patch.dict(os.environ, environment):
            row = staff.local_snapshot().by_current_session(environment["CODEX_THREAD_ID"])
            assert row is not None
            _ = staff.set_run(row.id, staff.OpenRun(session))
        registry = Path(environment["NOTIFIER_SESSIONS_DIR"])
        registry.mkdir(parents=True)
        from scripts.message import sessions
        _ = (registry / "director.json").write_text(json.dumps({
            "sessionId": environment["CODEX_THREAD_ID"], "pid": os.getpid(),
            "name": "fixture director", "procStart": sessions.process_start(os.getpid()),
            "updatedAt": 1, "messagingSocketPath": "",
        }))
        sender = self.root / "opening-send.py"
        _ = sender.write_text(sender.read_text().replace("print('SENT: fixture')", "raise SystemExit(2)"))
        result = self.run_assignment(self.assign_command(session), environment)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        with patch.dict(os.environ, environment):
            self.assertIsNone(unit_runs.open_run(environment["CODEX_THREAD_ID"]), result.stdout + result.stderr)
        state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["status"], "error")
        self.assertEqual([event["status"] for event in self.launch_records(session, "run_finished")], ["error"])
        self.assertIn("run ended", result.stderr)
        worker = self.recorded_worker(session)
        self.assertEqual(worker.parent, staff.ReportsTo(staff.native_id(self.staff_parent)))
        self.assertTrue(worker.current_session)

    @process_boundary
    def test_delivery_has_assign_as_its_only_launcher_parent(self) -> None:
        session = self.start_phase("one-launcher")
        sender = self.root / "opening-send.py"
        _ = sender.write_text(sender.read_text().replace("print('SENT: fixture')", "\n".join((
            "import subprocess",
            "rows = subprocess.run(['ps', '-eo', 'pid=,ppid=,args='], check=True, capture_output=True, text=True).stdout",
            "(root / 'launch-processes').write_text(rows)",
            "print('SENT: fixture')",
        ))))
        result = self.worker_launch(session)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        processes = {int(pid): (int(parent), command) for line in (self.root / "launch-processes").read_text().splitlines()
                     for pid, parent, command in [line.split(None, 2)]}
        sender_pid, (launcher_pid, command) = next(
            (pid, row) for pid, row in processes.items()
            if str(sender) in row[1])
        self.assertIn(str(sender), command)
        parent_pid, launcher_command = processes[launcher_pid]
        self.assertIn(str(self.assign_script.with_suffix(".py")), launcher_command)
        self.assertNotEqual(sender_pid, launcher_pid)
        self.assertNotIn("agent_bg.sh", processes[parent_pid][1])

    @process_boundary
    def test_implementation_reads_one_registry_snapshot_across_its_child_processes(self) -> None:
        session = self.start_phase("registry-snapshot")
        environment = self.environment()
        probe = RegistryReadProbe(self.root, Path(environment["MODELS_CONFIG_FILE"]))
        result = self.run_assignment(self.assign_command(session), probe.environment(environment))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(probe.reads()), 1, probe.reads())

    def test_a_dispatch_names_its_director_in_the_run_folder(self) -> None:
        # Staff records the director for each worker.
        session_dir = self.start_phase("director")
        environment = self.environment()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = self.run_assignment(self.assign_command(session_dir), environment)
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

    @process_boundary
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

    def test_a_launch_naming_its_phase_writes_in_that_phase_directory(self) -> None:
        # Each phase keeps its own artifacts and the same worker address.
        session_dir = self.start_phase("phase-directory")
        result = self.run_assignment(self.assign_command(session_dir, "--phase", "3"), self.environment())
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session_dir / "phases" / "3"
        self.assertEqual((phase_dir / "impl_status_impl").read_text(encoding="utf-8").strip(), "implemented")
        self.assertTrue((phase_dir / "impl_agent_impl.log").is_file())
        self.assertEqual(sorted(path.name for path in session_dir.glob("impl_status*")), [])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertNotIn("3-impl", board)

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

    def test_launcher_stops_on_a_stale_prompt(self) -> None:
        session = self.start_phase("refused-stale-prompt")
        _ = self.prompt_file.write_text("Phase 9 — Other work\n")
        result = self.checked_launch(session, "--phase", "3")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((session / "worker-started").exists())
        self.assertEqual(self.launch_records(session, "pass_started"), [])
        refusal, = self.launch_records(session, "launch_refused")
        self.assertEqual(refusal["prompt_file"], str(self.prompt_file))
        self.assertTrue(refusal["reason"])
        self.assertIn("prompt", result.stderr.lower())

    def test_an_unopened_phase_records_a_refusal_without_starting_a_worker(self) -> None:
        session = self.start_phase("unopened-phase")
        _ = self.prompt_file.write_text("Phase 9 — Other work\n")
        result = self.checked_launch(session, "--phase", "9")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((session / "worker-started").exists())
        self.assertFalse((self.root / "launch_args.json").exists())
        self.assertEqual(self.launch_records(session, "pass_started"), [])
        refusal, = self.launch_records(session, "launch_refused")
        self.assertEqual(refusal["prompt_file"], str(self.prompt_file))
        self.assertIn("Phase 9 is not open", str(refusal["reason"]))
        self.assertIn("phase", result.stderr.lower())

    def test_phase_claim_precedes_the_claude_worker_open(self) -> None:
        session = self.start_phase("claim-before-open")
        environment, calls = self.claim_fixture(session, "clear")
        result = self.run_assignment(self.assign_command(session, "--phase", "3"), environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["sid-director.phase-3 check reviewed.txt --json"])
        state = (session / "phases/3/delegated_phase_reservation_state.json").read_text()
        self.assertIn('"r1"', state)
        self.assertEqual((session / "dispatch_events.txt").read_text().splitlines(), ["claim", "open"])

    def test_refused_phase_claim_prevents_the_claude_worker_open(self) -> None:
        session = self.start_phase("claim-refused")
        environment, calls = self.claim_fixture(session, "blocked")
        result = self.run_assignment(self.assign_command(session, "--phase", "3"), environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("phase 3 could not claim its files", result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertEqual((session / "dispatch_events.txt").read_text().splitlines(), ["claim"])
        self.assertEqual(self.launch_records(session, "pass_started"), [])

    def test_unreadable_ledger_allows_the_worker_with_the_claim_pending(self) -> None:
        session = self.start_phase("claim-unreadable")
        environment, calls = self.claim_fixture(session, "ledger_unreadable")
        result = self.run_assignment(self.assign_command(session, "--phase", "3"), environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        state = (session / "phases/3/delegated_phase_reservation_state.json").read_text()
        self.assertIn("enrolled_awaiting_first_touch", state)
        self.assertEqual((session / "dispatch_events.txt").read_text().splitlines(), ["claim", "open"])

    @process_boundary
    def test_new_worker_identity_precedes_the_heartbeat_watcher(self) -> None:
        session = self.start_phase("identity-before-heartbeat")
        watcher = self.root / "scripts/agents/heartbeat_watch.sh"
        _ = watcher.write_text('#!/bin/bash\ncat "$UNIT_RUN_DIR/impl_worker_impl.json" > "$UNIT_RUN_DIR/watcher-identity"\n')
        backend = self.root / "claude-bin"
        _ = backend.write_text(backend.read_text().replace(
            "elif sys.argv[1:3] == ['agents', '--json']:",
            "elif sys.argv[1:3] == ['agents', '--json']:\n"
            + "    import subprocess\n"
            + f"    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], {str(session / 'watcher-identity')!r}], check=True)"))
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
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((session / "progress_history_state.json").read_bytes(), before)
        self.assertEqual(self.launch_records(session, "pass_started"), [])
