"""Background Claude publishes its observed identity before the opening work."""
from __future__ import annotations

from functools import cached_property
from contextlib import suppress
from dataclasses import dataclass

import json
import os
from pathlib import Path
import subprocess
import sys
import signal
from typing import cast, override
from unittest.mock import patch

from scripts.direct import worker_state
from scripts.lib import models_registry
from scripts.direct import claude_worker
from scripts.lib.test_models_registry import write_registry
from scripts.lib.process_group import send_process_group
from scripts.message import sessions
from scripts.production import staff, worker_admission
from scripts.usage import agents_log
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase, fixture_runtime_id, fixture_worker, transcript_fixture, stopped_worker_module
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.socket_test_support import ShortUnixSocket


BACKEND = '''import json, os, sys, socket
from pathlib import Path
with open(Path(os.environ["FIXTURE_CHECKOUT"]) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
log = Path(os.environ["FIXTURE_BACKEND_LOG"])
with log.open("a") as output:
    output.write(json.dumps({"arguments": sys.argv[1:], "STAFF_ID": os.environ.get("STAFF_ID", ""),
        "operation_environment": {key: os.environ[key] for key in ("MODELS_LAUNCH_CONFIGURATION", "MODELS_REGISTRY_EDIT_BASE") if key in os.environ},
        "row_locks": [key for key in os.environ if key.startswith("STAFF_ROW_FD_")],
        "lock_fds": [str(path.resolve()) for path in (Path("/proc/self/fd").glob("*") if sys.platform == "linux" else ())
                     if "/rows/" in str(path.resolve()) and str(path.resolve()).endswith(".lock")]}) + "\\n")
if sys.argv[1] == "stop":
    raise SystemExit(int(os.environ.get("FIXTURE_STOP_EXIT", "0")))
if sys.argv[1] == "agents":
    with socket.socket(socket.AF_UNIX) as gate:
        gate.connect(os.environ["FIXTURE_GATE"])
        gate.sendall(b"1")
        status = gate.recv(16).decode()
    print(json.dumps([{"id": os.environ["FIXTURE_NATIVE_ID"][:8], "status": status}]))
    raise SystemExit
if sys.argv[1] == "logs":
    print("the worker completed the assignment")
    raise SystemExit
Path(os.environ["FIXTURE_REGISTRY_PATH"]).write_text(os.environ["FIXTURE_REGISTRY_RECORD"])
transcript = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects/fixture" / (os.environ["FIXTURE_NATIVE_ID"] + ".jsonl")
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.touch()
if os.environ.get("FIXTURE_REFUSE_PUBLICATION"):
    path = Path(os.environ["STAFF_FILE"])
    lock = path.with_name(path.name + ".lock")
    lock.unlink(missing_ok=True)
    lock.mkdir()
print("backgrounded · " + os.environ["FIXTURE_NATIVE_ID"][:8] + " · fresh-impl")
'''

SEND = '''import json, os, sys, socket
from pathlib import Path
with open(Path(os.environ["FIXTURE_CHECKOUT"]) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.production import staff
from scripts.usage import agents_log
try:
    history = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
except OSError:
    history = agents_log.RoleLog({}, ())
row = staff.by_current_session(os.environ["FIXTURE_NATIVE_ID"])
if row is None:
    raise SystemExit("opening prompt preceded staff publication")
prompt = Path(sys.argv[sys.argv.index("--file") + 1]).read_text()
roles = history.assignments.get(row.current_session, ())
Path(os.environ["FIXTURE_SENT"]).write_text(json.dumps({"arguments": sys.argv[1:], "row": row.to_json(), "prompt": prompt,
    "role": roles[-1].role if roles else ""}))
if os.environ.get("FIXTURE_GATE"):
    with socket.socket(socket.AF_UNIX) as gate:
        gate.connect(os.environ["FIXTURE_GATE"])
        gate.sendall(str(os.getppid()).encode())
        gate.recv(1)
print("SENT: fixture")
'''


OPEN_WORKER = """from pathlib import Path
import os, signal, sys
from scripts.direct import claude_worker

def interrupted(signum, frame):
    raise SystemExit(128 + signum)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, interrupted)
name, run, cwd, prompt, summary, log, model, role, workers, native_id = sys.argv[1:]
turn = claude_worker.ClaudeTurn(name, Path(run), Path(cwd), Path(prompt), Path(summary), Path(log),
    model, os.environ.get("TEST_WORKER_EFFORT", ""), os.environ.get("TEST_WORKER_ROLE", role),
    os.environ.get("TEST_WORKER_STAFF_ID", ""), workers == "1", dict(os.environ))
result = claude_worker.follow_up(turn, native_id) if native_id else claude_worker.open_worker(turn)
if isinstance(result, claude_worker.NotDelivered):
    raise SystemExit(result.code)
"""
WAIT_FOR_TURN = """
if isinstance(result, claude_worker.Delivered):
    raise SystemExit(claude_worker.wait_for_turn(turn, result))
"""


def worker_command(name: str, run: Path, cwd: Path, prompt: Path, summary: Path, log: Path,
                   model: str = "", role: str = "", workers: bool = False, native_id: str = "") -> list[str]:
    """Call the worker functions in a child with the fixture environment."""
    return [str(CHECKOUT / "scripts/lib/py"), "-c", OPEN_WORKER, name, str(run), str(cwd), str(prompt), str(summary),
            str(log), model, role, "1" if workers else "0", native_id]



@dataclass
class ClaudeWorkerFixture:
    unit: staff.StaffRow
    identity: str
    work: Path
    run_dir: Path
    prompt: Path
    backend_log: Path
    sent: Path
    registry: Path
    environment: dict[str, str]


class NativeClaudeWorkerTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PATH": os.environ.get("PATH", os.defpath),
            "HERDR_BIN": "/nonexistent/herdr"}, clear=True)))
        super().setUp()
        _ = self.fixture

    @cached_property
    def fixture(self) -> ClaudeWorkerFixture:
        _, unit, _ = self.tree()
        identity = fixture_runtime_id("background-Claude-native")
        home = Path.home()
        shim = home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(CHECKOUT / "scripts/lib/py")
        self.write_json(home / ".claude.json", {"oauthAccount": {"emailAddress": "fixture@example.invalid"}})
        work = self.root / "project"
        work.mkdir()
        run_dir = self.worker_run("sdlc", "models-unit", unit.id)
        prompt = self.root / "opening"
        _ = prompt.write_text("work begins only after publication")
        backend = self.root / "claude"
        _ = backend.write_text("#!" + sys.executable + "\n" + BACKEND)
        backend.chmod(0o755)
        sender = self.root / "send.py"
        _ = sender.write_text(SEND)
        backend_log = self.root / "backend.jsonl"
        sent = self.root / "sent.json"
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json"
        record: dict[str, object] = {"pid": os.getpid(), "procStart": sessions.process_start(os.getpid()),
                  "sessionId": identity, "name": "fresh-impl", "entrypoint": "cli",
                  "messagingSocketPath": "", "updatedAt": 1, "formerNames": [], "tmux": ""}
        models = write_registry(self.root / "models.conf")
        self.enterContext(patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(models)}))
        environment = {**os.environ, "CODEX_THREAD_ID": unit.id, "UNIT_RUN_DIR": str(run_dir),
            "CONF_DIR": str(CHECKOUT / "config"), "UNIT_WORKER_POLL_SECS": "0",
            "CLAUDE_BIN": str(backend), "CLAUDE_WORKER_SEND_SCRIPT": str(sender),
            "FIXTURE_CHECKOUT": str(CHECKOUT), "FIXTURE_NATIVE_ID": identity,
            "FIXTURE_BACKEND_LOG": str(backend_log), "FIXTURE_SENT": str(sent),
            "FIXTURE_REGISTRY_PATH": str(registry), "FIXTURE_REGISTRY_RECORD": json.dumps(record)}
        _ = environment.pop("CLAUDE_CONFIG_DIR", None)

        return ClaudeWorkerFixture(unit, identity, work, run_dir, prompt, backend_log, sent, registry, environment)

    def clear_worker(self, row: staff.StaffRow) -> None:
        self.fixture.registry.unlink()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.fixture.unit.current_session}):
            path = worker_state.worker_record_path(self.fixture.run_dir, row.name, row.name)
        _ = staff.retire(row.id)
        path.unlink(missing_ok=True)

    def command(self) -> list[str]:
        return worker_command("fresh-impl", self.fixture.run_dir, self.fixture.work,
                              self.fixture.prompt, self.root / "summary", self.root / "log")

    def launch(self, **environment: str) -> subprocess.CompletedProcess[str]:
        self.fixture.sent.unlink(missing_ok=True)
        return subprocess.run(self.command(), cwd=self.fixture.work,
                              env={**self.fixture.environment, **environment}, capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def backend_calls(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.fixture.backend_log.read_text().splitlines()]

    def test_a_log_directory_uses_the_fallback_summary(self) -> None:
        log = self.root / "log-directory"
        log.mkdir()
        summary = self.root / "summary"
        turn = claude_worker.ClaudeTurn("fresh-impl", self.fixture.run_dir, self.fixture.work,
            self.fixture.prompt, summary, log, "", "", "", "", False, self.fixture.environment)
        backend = self.root / "claude"
        _ = backend.write_text(backend.read_text().replace(
            'if sys.argv[1] == "agents":',
            'if sys.argv[1] == "agents":\n    print("[]")\n    raise SystemExit'))
        delivered = claude_worker.open_worker(turn)
        self.assertIsInstance(delivered, claude_worker.Delivered)
        assert isinstance(delivered, claude_worker.Delivered)
        self.assertEqual(claude_worker.wait_for_turn(turn, delivered), 0)
        self.assertEqual(summary.read_text(), "The background agent fresh-impl produced no summary.\n")

    def test_transcript_counts_ignore_null_messages_and_json_lists(self) -> None:
        turn = claude_worker.ClaudeTurn("fresh-impl", self.fixture.run_dir, self.fixture.work,
            self.fixture.prompt, self.root / "summary", self.root / "log", "", "", "", "", False,
            self.fixture.environment)
        self.assertIsInstance(claude_worker.open_worker(turn), claude_worker.Delivered)
        projects = Path.home() / ".claude/projects/fixture"
        projects.mkdir(parents=True, exist_ok=True)
        transcript = projects / f"{self.fixture.identity}.jsonl"
        _ = transcript.write_text('\n'.join((
            '{"type": "assistant", "message": null}',
            '[]',
            '{"type": "assistant", "message": {"stop_reason": "end_turn"}}',
            '{"type": "assistant", "message": {"stop_reason": "stop_sequence"}}',
        )) + '\n')
        delivered = claude_worker.follow_up(turn, self.fixture.identity)
        self.assertIsInstance(delivered, claude_worker.Delivered)
        assert isinstance(delivered, claude_worker.Delivered)
        self.assertEqual(delivered.turns_before, 2)

    def test_claude_dispatch_records_the_task_role_before_delivery(self) -> None:
        for switched in (False, True):
            with self.subTest(switched=switched):
                environment = {"TEST_WORKER_ROLE": "impl"}
                if switched:
                    backend = staff.CodexBackend(self.root / "source-codex")
                    source = fixture_worker("fresh-impl", self.fixture.unit.id, backend=backend)
                    _ = transcript_fixture(source, backend)
                    modules = stopped_worker_module(self.root / "source-stop-modules")
                    environment.update(CODEX_HOME=str(backend.codex_home),
                        FIXTURE_STOPPED=str(self.root / "source-stopped.json"),
                        PYTHONPATH=os.pathsep.join((str(modules), str(CHECKOUT))))
                result = self.launch(**environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
                self.assertEqual(sent["role"], "impl")
                history = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
                self.assertEqual(history.assignments[self.fixture.identity][-1].role, "impl")
                row = staff.by_current_session(self.fixture.identity)
                assert row is not None
                self.clear_worker(row)

    def test_claude_consultation_without_a_role_preserves_the_assignment_history(self) -> None:
        result = self.launch(TEST_WORKER_ROLE="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        history = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
        self.assertEqual(history.assignments, {})
        sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
        self.assertEqual(sent["prompt"], self.fixture.prompt.read_text())

    def test_claude_admission_completes_and_cancels_without_linux_process_files(self) -> None:
        read_text = Path.read_text

        def portable_read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            self.assertFalse(str(path).startswith("/proc/"), str(path))
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", portable_read):
            self.test_claude_admission_ends_at_dispatch_acceptance_or_cancellation()

    def test_claude_admission_ends_at_dispatch_acceptance_or_cancellation(self) -> None:
        for cancel in (False, True):
            with (self.subTest(cancel=cancel), ShortUnixSocket() as gate):
                backend = staff.CodexBackend(self.root / "source-codex")
                old = fixture_worker("fresh-impl", self.fixture.unit.id, backend=backend)
                source, evidence = transcript_fixture(old, backend)
                before = self.staff_file.read_bytes()
                parent = self.root / ("cancel-parent" if cancel else "complete-parent")
                parent.mkdir()
                record_path = parent / "progress_history_state.json"
                _ = record_path.write_text('{"parent": {"staff_id": "parent-worker"}}\n')
                saved = record_path.read_bytes()
                stopped = self.root / ("cancel-stopped.json" if cancel else "complete-stopped.json")
                self.fixture.backend_log.unlink(missing_ok=True)
                modules = stopped_worker_module(self.root / ("cancel-modules" if cancel else "complete-modules"))
                address = gate.bind_socket()
                gate.listen()
                gate.settimeout(TEST_HANG_GUARD_SECS)
                command = self.command()
                command[2] += WAIT_FOR_TURN
                environment = {**self.fixture.environment, "FIXTURE_GATE": str(address),
                    "CODEX_HOME": str(backend.codex_home), "FIXTURE_STOPPED": str(stopped),
                    "UNIT_RUN_DIR": str(parent), "PLAN_DELEGATE_BOARD_DIR": str(parent),
                    "PYTHONPATH": os.pathsep.join((str(modules), str(CHECKOUT)))}
                with subprocess.Popen(command, cwd=self.fixture.work, text=True, env=environment,
                        start_new_session=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) as launcher:
                    try:
                        with gate.accept()[0] as delivery:
                            delivery.settimeout(TEST_HANG_GUARD_SECS)
                            launcher_pid = int(delivery.recv(32))
                            if cancel:
                                os.kill(launcher_pid, signal.SIGTERM)
                                with suppress(BrokenPipeError, ConnectionResetError):
                                    delivery.sendall(b"1")
                            else:
                                delivery.sendall(b"1")
                        if not cancel:
                            for status in (b"busy", b"idle"):
                                with gate.accept()[0] as turn:
                                    turn.settimeout(TEST_HANG_GUARD_SECS)
                                    _ = turn.recv(512)
                                    turn.sendall(status)
                        output, errors = launcher.communicate(timeout=TEST_HANG_GUARD_SECS)
                    finally:
                        if launcher.poll() is None:
                            send_process_group(launcher.pid, signal.SIGKILL)
                            _ = launcher.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(launcher.returncode, 143 if cancel else 0, output + errors)
                path = worker_admission.switch_record_path(self.fixture.run_dir, "fresh-impl")
                record = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(record["dispatch_state"], "sending" if cancel else "accepted")
                row = staff.by_current_session(self.fixture.identity)
                assert row is not None
                self.assertEqual(json.loads(stopped.read_text()), old.to_json())
                self.assertEqual({path.name: path.read_bytes() for path in parent.iterdir()},
                                 {"progress_history_state.json": saved})
                self.assert_only_current_session_changed(before, old, self.fixture.identity)
                sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
                prompt = str(sent["prompt"])
                extract = worker_admission.handoff_path(self.fixture.run_dir, old)
                self.assertLess(prompt.index(str(extract)), prompt.index(str(source)))
                self.assertIn(self.fixture.prompt.read_text(), prompt)
                self.assertIn(evidence[0][0], extract.read_text())
                self.assertEqual(self.backend_calls()[0]["row_locks"], [])
                self.assertEqual(self.backend_calls()[0]["lock_fds"], [])
                self.assertEqual(sent["row"], row.to_json())
                self.assertFalse(any(call["arguments"] == ["stop", self.fixture.identity[:8]] for call in self.backend_calls()))
                if not cancel:
                    self.assertIn("the worker completed the assignment", (self.root / "summary").read_text())
                self.clear_worker(row)

    def test_fresh_claude_publication_precedes_work_for_each_parent_state(self) -> None:
        for count, removed in enumerate((False, True), 1):
            with self.subTest(removed=removed):
                if removed:
                    _ = staff.set_worktree(self.fixture.unit.id, staff.RemovedWorktree("natedev", self.fixture.work),
                                           expected=self.fixture.unit.worktree)
                result = self.launch(UNIT_TEAM_ROLE="")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = staff.by_id(staff.StaffId(self.fixture.identity))
                assert row is not None
                self.assertEqual((row.name, row.parent), ("fresh-impl", staff.ReportsTo(self.fixture.unit.id)))
                sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
                self.assertEqual(sent["row"], row.to_json())
                self.assertEqual(cast(list[str], sent["arguments"])[:2], ["--to", "session:" + self.fixture.identity])
                calls = self.backend_calls()
                self.assertEqual(len(calls), count)
                self.assertNotIn("--session-id", cast(list[str], calls[-1]["arguments"]))
                self.assertEqual(calls[-1]["STAFF_ID"], "")
                if removed:
                    parent = staff.by_id(self.fixture.unit.id)
                    assert parent is not None
                    self.assertIsInstance(parent.worktree, staff.RemovedWorktree)
                self.clear_worker(row)

    def test_worker_waits_for_its_registry_record_after_the_banner_then_publishes(self) -> None:
        fake_modules = self.root / "fake-modules"
        fake_modules.mkdir()
        reads = self.root / "registry-reads"
        _ = (fake_modules / "sitecustomize.py").write_text('''import os, sys
from pathlib import Path
from scripts.message import sessions

if len(sys.argv) > 1 and sys.argv[0] == "-c":
    read_registry = sessions.read_registry
    read_count = 0
    def delayed_registry():
        global read_count
        read_count += 1
        with Path(os.environ["FIXTURE_REGISTRY_READS"]).open("a") as output:
            output.write("empty\\n" if read_count <= 2 else "ready\\n")
        return sessions.RegistryRead((), ()) if read_count <= 2 else read_registry()
    sessions.read_registry = delayed_registry
''')
        environment = models_registry.launch_configuration(os.environ).environment(
            models_registry.edit_base(os.environ).environment(self.fixture.environment))
        result = self.launch(**{**environment, "PYTHONPATH": os.pathsep.join((str(fake_modules), str(CHECKOUT))),
                               "FIXTURE_REGISTRY_READS": str(reads), "CLAUDE_CODE_CHILD_SESSION": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = cast(list[str], self.backend_calls()[0]["arguments"])
        settings = cast(dict[str, object], json.loads(arguments[arguments.index("--settings") + 1]))
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", cast(dict[str, str], settings["env"]))
        for surface, environment in (("process", self.backend_calls()[0]["operation_environment"]),
                                     ("settings", settings["env"])):
            with self.subTest(surface=surface):
                self.assertNotIn("MODELS_LAUNCH_CONFIGURATION", cast(dict[str, str], environment))
                self.assertNotIn("MODELS_REGISTRY_EDIT_BASE", cast(dict[str, str], environment))
        self.assertEqual(reads.read_text().splitlines(), ["empty", "empty", "ready"])
        row = staff.by_id(staff.StaffId(self.fixture.identity))
        assert row is not None
        self.assertEqual((row.name, row.parent), ("fresh-impl", staff.ReportsTo(self.fixture.unit.id)))
        self.assertTrue(self.fixture.sent.exists())
        self.assertEqual(len(self.backend_calls()), 1)

    def test_publication_refusal_stops_only_the_created_backend_and_reports_failed_cleanup(self) -> None:
        for stop_exit in (1, 0):
            with self.subTest(stop_exit=stop_exit):
                lock = self.staff_file.with_name(self.staff_file.name + ".lock")
                if lock.is_dir():
                    lock.rmdir()
                self.fixture.backend_log.unlink(missing_ok=True)
                result = self.launch(FIXTURE_REFUSE_PUBLICATION="1", FIXTURE_STOP_EXIT=str(stop_exit))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(self.fixture.identity, result.stderr)
                calls = self.backend_calls()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1]["arguments"], ["stop", self.fixture.identity[:8]])
                self.assertFalse(self.fixture.sent.exists())
                self.assertIsNone(staff.by_id(staff.StaffId(self.fixture.identity)))
                if stop_exit:
                    self.assertIn("cleanup unproved", result.stderr)
                else:
                    self.assertNotIn("cleanup unproved", result.stderr)
                self.assertEqual(json.loads(self.fixture.registry.read_text())["sessionId"], self.fixture.identity)
