"""Background Claude publishes its observed identity before the opening work."""
from __future__ import annotations

from functools import cached_property
from dataclasses import dataclass

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest.mock import patch

from scripts.direct.assign_launcher_test_support import backend_fixture_command
from scripts.message import sessions
from scripts.production import staff
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase, fixture_runtime_id, fixture_worker, transcript_fixture, stopped_worker_module
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


BACKEND = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["FIXTURE_CHECKOUT"])
log = Path(os.environ["FIXTURE_BACKEND_LOG"])
with log.open("a") as output:
    output.write(json.dumps({"arguments": sys.argv[1:], "STAFF_ID": os.environ.get("STAFF_ID", ""),
        "row_locks": [key for key in os.environ if key.startswith("STAFF_ROW_FD_")],
        "lock_fds": [str(path.resolve()) for path in Path("/proc/self/fd").glob("*")
                     if "/rows/" in str(path.resolve()) and str(path.resolve()).endswith(".lock")]}) + "\\n")
if sys.argv[1] == "stop":
    raise SystemExit(int(os.environ.get("FIXTURE_STOP_EXIT", "0")))
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

SEND = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["FIXTURE_CHECKOUT"])
from scripts.production import staff
row = staff.by_current_session(os.environ["FIXTURE_NATIVE_ID"])
if row is None:
    raise SystemExit("opening prompt preceded staff publication")
prompt = Path(sys.argv[sys.argv.index("--file") + 1]).read_text()
Path(os.environ["FIXTURE_SENT"]).write_text(json.dumps({"arguments": sys.argv[1:], "row": row.to_json(), "prompt": prompt}))
print("SENT: fixture")
'''


@dataclass
class ClaudeWorkerFixture:
    unit: staff.StaffRow
    identity: str
    work: Path
    run_dir: Path
    prompt: Path
    backend: Path
    sender: Path
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
        environment = {**os.environ, "CODEX_THREAD_ID": unit.id, "UNIT_RUN_DIR": str(run_dir),
            "CONF_DIR": str(CHECKOUT / "config"),
            "CLAUDE_BIN": str(backend), "AGENT_BG_ROLE": "impl", "AGENT_BG_DETACH": "1", "AGENT_BG_SEND_SCRIPT": str(sender),
            "FIXTURE_CHECKOUT": str(CHECKOUT), "FIXTURE_NATIVE_ID": identity,
            "FIXTURE_BACKEND_LOG": str(backend_log), "FIXTURE_SENT": str(sent),
            "FIXTURE_REGISTRY_PATH": str(registry), "FIXTURE_REGISTRY_RECORD": json.dumps(record)}
        _ = environment.pop("CLAUDE_CONFIG_DIR", None)

        return ClaudeWorkerFixture(unit, identity, work, run_dir, prompt, backend, sender, backend_log, sent, registry, environment)

    def launch(self, **environment: str) -> subprocess.CompletedProcess[str]:
        self.fixture.sent.unlink(missing_ok=True)
        command, launch_environment = backend_fixture_command(
            self.root / "assign-parent",
            ["bash", str(CHECKOUT / "scripts/agents/agent_bg.sh"), "--run", str(self.fixture.run_dir),
             "fresh-impl", str(self.fixture.work), str(self.fixture.prompt), str(self.root / "summary"), str(self.root / "log")],
            {**self.fixture.environment, **environment})
        return subprocess.run(command, cwd=self.fixture.work, env=launch_environment, capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def backend_calls(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.fixture.backend_log.read_text().splitlines()]

    def test_fresh_claude_worker_publishes_its_registry_id_before_opening_work(self) -> None:
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = staff.by_id(staff.StaffId(self.fixture.identity))
        assert row is not None
        self.assertEqual((row.name, row.parent), ("fresh-impl", staff.ReportsTo(self.fixture.unit.id)))
        sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
        self.assertEqual(sent["row"], row.to_json())
        self.assertEqual(cast(list[str], sent["arguments"])[:2], ["--to", "session:" + self.fixture.identity])
        calls = self.backend_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--session-id", cast(list[str], calls[0]["arguments"]))
        self.assertEqual(calls[0]["STAFF_ID"], "")

    def test_codex_to_claude_stops_the_source_and_publishes_before_the_transcript_opening(self) -> None:
        old = fixture_worker("fresh-impl", self.fixture.unit.id,
                             backend=staff.CodexBackend(self.root / "source-codex"))
        source, evidence = transcript_fixture(old, staff.CodexBackend(self.root / "source-codex"))
        before = self.staff_file.read_bytes()
        modules = stopped_worker_module(self.root / "switch-modules")
        stopped = self.root / "stopped.json"
        parent = self.root / "parent-session"
        parent.mkdir()
        roster = parent / "mesh_roster.json"
        _ = roster.write_text('{"parent": {"staff_id": "parent-worker"}}\n')
        saved = roster.read_bytes()
        result = self.launch(UNIT_RUN_DIR=str(parent), PLAN_DELEGATE_BOARD_DIR=str(parent),
                             PYTHONPATH=os.pathsep.join((str(modules), str(CHECKOUT))),
                             FIXTURE_STOPPED=str(stopped), CODEX_HOME=str(self.root / "source-codex"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = staff.by_id(old.id)
        self.assertEqual(json.loads(stopped.read_text()), old.to_json())
        self.assertEqual({path.name: path.read_bytes() for path in parent.iterdir()}, {"mesh_roster.json": saved})
        private = cast(dict[str, dict[str, object]], json.loads((self.fixture.run_dir / "mesh_roster.json").read_text()))
        self.assertEqual(private["fresh-impl"]["dispatch_state"], "accepted")
        sent = cast(dict[str, object], json.loads(self.fixture.sent.read_text()))
        assert current is not None
        self.assertEqual(sent["row"], current.to_json())
        prompt = str(sent["prompt"])
        extract = self.fixture.run_dir / f"{old.id}.handoff.md"
        self.assertLess(prompt.index(str(extract)), prompt.index(str(source)))
        self.assertIn(self.fixture.prompt.read_text(), prompt)
        self.assertIn(evidence[0][0], extract.read_text())
        self.assertEqual(self.backend_calls()[0]["row_locks"], [])
        self.assertEqual(self.backend_calls()[0]["lock_fds"], [])
        self.assert_only_current_session_changed(before, old, self.fixture.identity)

    def test_worker_waits_for_its_registry_record_after_the_banner_then_publishes(self) -> None:
        fake_modules = self.root / "fake-modules"
        fake_modules.mkdir()
        reads = self.root / "registry-reads"
        _ = (fake_modules / "sitecustomize.py").write_text('''import os
from pathlib import Path
from scripts.message import sessions

if os.environ.get("BG_ID"):
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
        result = self.launch(PYTHONPATH=os.pathsep.join((str(fake_modules), str(CHECKOUT))),
                             FIXTURE_REGISTRY_READS=str(reads), CLAUDE_CODE_CHILD_SESSION="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = cast(list[str], self.backend_calls()[0]["arguments"])
        settings = cast(dict[str, object], json.loads(arguments[arguments.index("--settings") + 1]))
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", cast(dict[str, str], settings["env"]))
        self.assertEqual(reads.read_text().splitlines(), ["empty", "empty", "ready"])
        row = staff.by_id(staff.StaffId(self.fixture.identity))
        assert row is not None
        self.assertEqual((row.name, row.parent), ("fresh-impl", staff.ReportsTo(self.fixture.unit.id)))
        self.assertTrue(self.fixture.sent.exists())
        self.assertEqual(len(self.backend_calls()), 1)

    def test_refused_claude_permission_starts_nothing_and_writes_no_worker_row(self) -> None:
        _ = staff.set_worktree(self.fixture.unit.id, staff.RemovedWorktree("natedev", self.fixture.work), expected=self.fixture.unit.worktree)
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = staff.by_id(staff.StaffId(self.fixture.identity))
        assert row is not None
        self.assertEqual(row.parent, staff.ReportsTo(self.fixture.unit.id))
        self.assertEqual(len(self.backend_calls()), 1)
        self.assertEqual(json.loads(self.fixture.sent.read_text())["row"], row.to_json())
        parent = staff.by_id(self.fixture.unit.id)
        assert parent is not None
        self.assertIsInstance(parent.worktree, staff.RemovedWorktree)

    def test_each_claude_launch_delivers_work_with_a_missing_or_invalid_task_role(self) -> None:
        for count, role in enumerate(("", "invented-role"), 1):
            with self.subTest(role=role):
                result = self.launch(AGENT_BG_ROLE=role, UNIT_TEAM_ROLE="")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = staff.by_id(staff.StaffId(self.fixture.identity))
                assert row is not None
                self.assertEqual(row.parent, staff.ReportsTo(self.fixture.unit.id))
                self.assertEqual(json.loads(self.fixture.sent.read_text())["row"], row.to_json())
                self.assertEqual(len(self.backend_calls()), count)

    def test_claude_publication_refusal_names_and_stops_only_the_created_backend_without_work(self) -> None:
        result = self.launch(FIXTURE_REFUSE_PUBLICATION="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(self.fixture.identity, result.stderr)
        calls = self.backend_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["arguments"], ["stop", self.fixture.identity[:8]])
        self.assertFalse(self.fixture.sent.exists())
        self.assertIsNone(staff.by_id(staff.StaffId(self.fixture.identity)))

    def test_failed_claude_cleanup_keeps_the_unpublished_session_observable_and_dispatches_nothing(self) -> None:
        result = self.launch(FIXTURE_REFUSE_PUBLICATION="1", FIXTURE_STOP_EXIT="1")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("cleanup unproved", result.stderr)
        self.assertEqual(json.loads(self.fixture.registry.read_text())["sessionId"], self.fixture.identity)
        self.assertFalse(self.fixture.sent.exists())
        self.assertIsNone(staff.by_id(staff.StaffId(self.fixture.identity)))
