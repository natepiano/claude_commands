"""Exercise the usage collector's CLI against small invented source files."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypedDict, cast, override
from collections.abc import Iterable, Iterator
from types import ModuleType
from unittest.mock import patch

from scripts.usage import collect as collector
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

if TYPE_CHECKING:
    from ..production.cli_fixture import invoke_cli
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
    from cli_fixture import invoke_cli


REPOSITORY = Path(__file__).resolve().parents[2]
COLLECTOR = Path(__file__).with_name("collect.py")
FAILING_WATCH = """import sys
print("waste_watch: fixture watch failure", file=sys.stderr)
print("second line", file=sys.stderr)
raise SystemExit(1)
"""
FIXTURES = Path(__file__).with_name("fixtures") / "collect"
SESSION = "a1b2c3d4-1111-4111-8111-111111111111"
THREAD = "a1b2c3d4-2222-4222-8222-222222222222"
CHILD = "a0123456789abcdef"
COLLISION = "facefeed-4444-4444-8444-444444444444"
BOUNDARY = datetime(2026, 10, 10, 21, 35, tzinfo=timezone.utc)
MAC_PARENT = "00000001-1111-4111-8111-111111111111"
MAC_SECOND = "00000002-2222-4222-8222-222222222222"
MAC_THREAD = "00000003-3333-4333-8333-333333333333"
MAC_CHILD = "a0000000000000001"
READABLE_SESSION = "11223344-3333-4333-8333-333333333333"
ACTIVITY_THREAD = "a1b2c3d4-5555-4555-8555-555555555555"
ACTIVITY_SESSION = "a1b2c3d4-6666-4666-8666-666666666666"
BOARD = "bash ~/.claude/scripts/direct/board.sh"
# One request per entry, in time order: (activity, wait_action).
CODEX_ACTIVITIES: list[tuple[object, ...]] = [
    ("wait", "empty write_stdin"), ("status", None), ("wait", "sleep tool"),
    ("work", None), ("work", None), ("work", None), ("wait", "empty write_stdin"),
]
CLAUDE_ACTIVITIES: list[tuple[object, ...]] = [
    ("wait", "sleep"), ("wait", "Read *.output"), ("status", None), ("work", None), ("work", None),
]
BLOCKED_SESSION = "55667788-4444-4444-8444-444444444444"
SshBehavior = Literal["complete", "cut_after_first_cursor", "cut_inside_second_file",
                      "cut_inside_first_file", "cut_before_status", "cut_inside_record",
                      "unreachable", "early_disconnect", "wait_for_release", "python_error", "malformed_record", "unknown_measurement",
                      "missing_output", "oversized_output", "missing_evidence_field", "malformed_evidence_field",
                      "cut_inside_history", "cut_after_history_cursor", "missing_outcome_field",
                      "missing_opening_time", "invalid_edit_time", "missing_folder", "invalid_folder",
                      "invalid_activity", "wait_without_action", "missing_activity"]
SourceState = Literal["ready", "missing", "unreadable", "partial", "skipped", "unreachable"]


SNAPSHOT_COMMIT_SCRIPT = """
import sqlite3
import sys
from contextlib import closing
from unittest.mock import patch
original_connect = sqlite3.connect
database_path = sys.argv[sys.argv.index('--db') + 1]
with closing(original_connect(database_path)) as writer:
    assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
changed = False
class CommitBetweenQueries(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        global changed
        cursor = super().execute(sql, parameters)
        if not changed and sql.lstrip().upper().startswith('SELECT'):
            changed = True
            with closing(original_connect(database_path)) as writer:
                with writer:
                    writer.execute("DELETE FROM turns WHERE family = 'codex'")
                    writer.execute("DELETE FROM compactions WHERE family = 'codex'")
                    writer.execute("UPDATE turn_sources SET state = 'unreachable', "
                                   "last_complete_at = '2026-10-12T18:41:59+00:00' WHERE family = 'codex'")
        return cursor
def snapshot_connect(*args, **kwargs):
    kwargs['factory'] = CommitBetweenQueries
    return original_connect(*args, **kwargs)
"""


class CommittedRoleSnapshot(TypedDict):
    problems: int
    state: str
    read_at: str


class MacStreamReport(TypedDict):
    measurement_records: int
    cursor_records: int
    forwarded_bytes: int
    stdin_bytes: int
    options: list[str]
    turn_records: int
    compaction_records: int
    opening_records: int
    edit_records: int
    folder_records: int
    outcome_records: int
    history_cursor_records: int
    altered_outcome_records: int
    altered_lifecycle_records: int
    role_snapshots: list[CommittedRoleSnapshot]
    director_records: int
    history_file_records: int
    history_chunks: int
    metadata_chunks: int
    altered_evidence_records: int


@dataclass(frozen=True)
class MacRun:
    output: str
    stream: MacStreamReport


class WeightReader(Protocol):
    def weighted(self, turn_row: sqlite3.Row, at: datetime) -> float: ...


class CollectorFixture(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    claude_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    codex_root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    agents_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    db_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory(prefix="collect-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture_root = Path(temporary.name) / "case"
        cls.fixture_seed = Path(temporary.name) / "seed"
        cls.fixture_root.mkdir()
        fixture = cls("runTest")
        fixture.root = cls.fixture_root
        fixture.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_seed)

    @override
    def setUp(self) -> None:
        shutil.rmtree(self.fixture_root)
        _ = shutil.copytree(self.fixture_seed, self.fixture_root)
        self.root = self.fixture_root
        self.state = self.root / "state"
        self.db_path = self.state / "usage.db"
        self.bind_sources()

    def bind_sources(self) -> None:
        destination = self.root / "sources"
        self.claude_root = destination / "claude"
        self.codex_root = destination / "codex"
        self.agents_path = destination / "agents.jsonl"

    def build_fixture(self) -> None:
        self.state = self.root / "state"
        self.state.mkdir()
        self.db_path = self.state / "usage.db"
        (self.root / "fixture-repository").mkdir()
        (self.root / "production-docs").mkdir()
        # Ordinary source/ledger cases do not exercise the waste watcher.
        # Its CLI handoff and failure behavior retain the explicit watch test.
        _ = (self.root / "waste_watch_noop.py").write_text("")
        self.use_fixture("basic")

    def use_fixture(self, name: str) -> None:
        destination = self.root / "sources"
        if destination.exists():
            shutil.rmtree(destination)
        _ = shutil.copytree(FIXTURES / name, destination)
        self.claude_root = destination / "claude"
        self.codex_root = destination / "codex"
        self.agents_path = destination / "agents.jsonl"

    def collector_invocation(self, *, db: Path, machine: str = "natedev",
            history_override: Literal["configured", "empty"] = "configured",
            mac_pass: Literal["skipped", "fixture"] = "skipped") -> tuple[list[str], dict[str, str]]:
        environment = os.environ.copy()
        environment["USAGE_STATE_DIR"] = str(self.state)
        environment["HOME"] = str(self.root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["USAGE_WASTE_WATCH_SCRIPT"] = str(self.root / "waste_watch_noop.py")
        environment["PATH"] = f"{self.root / 'bin'}:{environment.get('PATH', '')}"
        environment["UNIT_HISTORY_DIR"] = (
            "" if history_override == "empty" else str(self.root / ".local/state/plan-delegate"))
        command = [sys.executable, str(COLLECTOR), "--db", str(db), "--durability", "off",
                   "--machine", machine, "--claude-root", str(self.claude_root),
                   "--codex-root", str(self.codex_root), "--agents-log", str(self.agents_path),
                   "--readings", str(self.root / "readings.jsonl"),
                   "--baseline-readings", str(self.root / "baseline-readings.jsonl"),
                   "--account-roles", str(self.root / "account-roles.jsonl"),
                   "--claude-repo", str(self.root / "fixture-repository"),
                   "--production-docs", str(self.root / "production-docs")]
        if mac_pass == "skipped":
            command.append("--no-mac")
        else:
            configuration = self.root / "ssh-configuration.json"
            self.assertTrue(configuration.is_file(), "Mac tests require the fake SSH configuration")
            self.assertTrue((self.root / "bin/ssh").is_file(), "Mac tests require the fake SSH executable")
            environment["USAGE_TEST_SSH_CONFIG"] = str(configuration)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(REPOSITORY), environment.get("PYTHONPATH", ""))
            )
        return command, environment

    def cli_with_watch(self, watch_program: str) -> subprocess.CompletedProcess[str]:
        """Run the collector with `USAGE_WASTE_WATCH_SCRIPT` naming a stub watch that runs `watch_program`."""
        stub = self.root / "waste_watch_stub.py"
        _ = stub.write_text(watch_program)
        command, environment = self.collector_invocation(db=self.db_path)
        environment["USAGE_WASTE_WATCH_SCRIPT"] = str(stub)
        return subprocess.run(command, cwd=REPOSITORY, env=environment, capture_output=True, text=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def cli(self, *, db: Path, machine: str = "natedev",
            history_override: Literal["configured", "empty"] = "configured",
            mac_pass: Literal["skipped", "fixture"] = "skipped") -> subprocess.CompletedProcess[str]:
        command, environment = self.collector_invocation(db=db, machine=machine,
                                                        history_override=history_override, mac_pass=mac_pass)
        real_run = subprocess.run
        noop = self.root / "waste_watch_noop.py"
        fake = ModuleType("collect_noop_watch")
        fake.__file__ = str(noop)

        def run(arguments: list[str], *, stdin: int, capture_output: bool, text: bool,
                timeout: float, check: bool) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == [sys.executable, str(noop)]:
                def main() -> int:
                    exec(compile(noop.read_text(), str(noop), "exec"), {**vars(fake), "__name__": "__main__"})
                    return 0
                return invoke_cli(fake, main, arguments[2:], os.environ.copy(), Path.cwd())
            return real_run(arguments, stdin=stdin, capture_output=capture_output, text=text,
                            timeout=timeout, check=check)

        # The iterator resumes after consume_remote has processed its previous
        # record. Signal the fixture after the real attribution transaction,
        # then let its independent connection inspect the committed snapshot.
        records = collector.remote_records
        event = self.root / "role-commit-event"
        os.mkfifo(event)
        descriptor = os.open(event, os.O_RDWR | os.O_NONBLOCK)
        environment["USAGE_TEST_ROLE_COMMIT_EVENT"] = str(event)

        def remote_records(stream: Iterable[str]) -> Iterator[dict[str, object]]:
            for record in records(stream):
                yield record
                if record.get("type") == "attribution_ready":
                    _ = os.write(descriptor, b"1")

        processes = ModuleType("collect_process_fixture")
        vars(processes).update(vars(subprocess))
        setattr(processes, "run", run)
        try:
            with patch.object(collector, "subprocess", processes), patch.object(collector, "remote_records", remote_records):
                return invoke_cli(collector, lambda: collector.main(command[2:]), command[2:], environment, REPOSITORY)
        finally:
            os.close(descriptor)
            event.unlink()

    def assert_cli_durability(self, selection: list[str], expected: str) -> None:
        command, environment = self.collector_invocation(db=self.db_path)
        # Remove the fixture's disposable default to exercise the actual CLI default too.
        position = command.index("--durability")
        del command[position:position + 2]
        trace = self.root / "durability-sql.jsonl"
        program = f'''import json, runpy, sqlite3, sys
from pathlib import Path
trace = Path({str(trace)!r})
original_connect = sqlite3.connect
def record(statement):
    with trace.open("a") as output:
        output.write(json.dumps(statement) + "\\n")
def connect(*args, **kwargs):
    database = original_connect(*args, **kwargs)
    database.set_trace_callback(record)
    return database
sqlite3.connect = connect
sys.argv.pop(0)
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-c", program, *command[1:], *selection],
                                env=environment, cwd=REPOSITORY, capture_output=True,
                                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        statements = [cast(str, json.loads(line)) for line in trace.read_text().splitlines()]
        self.assertEqual([sql for sql in statements if sql.startswith("PRAGMA synchronous=")], [expected])
        self.assertGreater(len(self.rows("SELECT * FROM turns")), 0)

    def collect(self, *, machine: str = "natedev",
                history_override: Literal["configured", "empty"] = "configured",
                mac_pass: Literal["skipped", "fixture"] = "skipped") -> str:
        result = self.cli(db=self.db_path, machine=machine, history_override=history_override, mac_pass=mac_pass)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.db_path.is_file(), result.stdout + result.stderr)
        return result.stdout

    def rows(self, sql: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)) as database:
            database.row_factory = sqlite3.Row
            return cast(list[sqlite3.Row], database.execute(sql, parameters).fetchall())

    def turns(self, family: str, agent_id: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM turns WHERE family = ? AND agent_id = ? ORDER BY at",
                         (family, agent_id))

    def values(self, rows: list[sqlite3.Row], *columns: str) -> list[tuple[object, ...]]:
        return [tuple(cast(object, row[column]) for column in columns) for row in rows]

    def all_values(self, rows: list[sqlite3.Row]) -> list[tuple[object, ...]]:
        return [tuple(cast(object, row[index]) for index in range(len(row))) for row in rows]

    def source_path(self, family: str) -> Path:
        if family == "claude":
            return self.claude_root / "project" / f"{SESSION}.jsonl"
        return self.codex_root / "2026/10/10/rollout-example.jsonl"

    def write_codex_session(self, path: Path, thread: str = THREAD) -> tuple[int, int]:
        records = (
            {"timestamp": "2026-10-10T21:34:57Z", "type": "session_meta",
             "payload": {"id": thread, "cwd": "/invented/π"}},
            {"timestamp": "2026-10-10T21:34:58Z", "type": "turn_context",
             "payload": {"model": "gpt-5.6-sol", "effort": "medium"}},
            {"timestamp": "2026-10-10T21:34:59Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 2}}}},
            {"timestamp": "2026-10-10T21:35:01Z", "type": "response_item",
             "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "invented edit"}},
        )
        lines = [(json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8") for record in records]
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(lines))
        return sum(map(len, lines[:2])), sum(map(len, lines[:3]))

    def write_activity_rollout(self, path: Path, thread: str = ACTIVITY_THREAD) -> None:
        """Requests whose items match CODEX_ACTIVITIES; each token count ends one request."""
        def item(payload: dict[str, object]) -> dict[str, object]:
            return {"timestamp": "2026-10-10T21:35:00Z", "type": "response_item", "payload": payload}

        def exec_cell(source: str) -> dict[str, object]:
            return item({"type": "custom_tool_call", "name": "exec", "input": source})

        requests: list[list[dict[str, object]]] = [
            [item({"type": "reasoning", "summary": []}),
             exec_cell('await tools.write_stdin({session_id: 3, chars: "", yield_time_ms: 30000});')],
            [exec_cell('await tools.exec_command({cmd: "' + BOARD.replace('"', '\\"')
                       + ' post /tmp/board impl status \\"build started\\""});')],
            [item({"type": "function_call", "name": "sleep", "arguments": '{"duration_ms":45000}'}),
             item({"type": "function_call_output", "output": "slept"})],
            [exec_cell('await tools.exec_command({cmd: "cargo build 2>&1 | tail -5"});')],
            [item({"type": "function_call", "name": "wait", "arguments": '{"cell_id":"7"}'}),
             item({"type": "message", "role": "assistant", "phase": "final_answer", "content": []})],
            [],
            [exec_cell('await tools.exec_command({cmd: "sleep 30; tail -3 /tmp/build.log"});\n'
                       + 'await tools.write_stdin({session_id: 3, chars: ""});')],
        ]
        records: list[dict[str, object]] = [
            {"timestamp": "2026-10-10T21:34:57Z", "type": "session_meta", "payload": {"id": thread, "cwd": "/invented"}},
            {"timestamp": "2026-10-10T21:34:58Z", "type": "turn_context",
             "payload": {"model": "gpt-6.1-sol", "effort": "medium"}},
        ]
        for second, items in enumerate(requests):
            records.extend(items)
            records.append({"timestamp": f"2026-10-10T21:36:{second:02d}Z", "type": "event_msg",
                            "payload": {"type": "token_count", "info": {"last_token_usage": {
                                "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 1}}}})
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def claude_line(self, message_id: str, second: int, *blocks: dict[str, object]) -> bytes:
        record = {"type": "assistant", "timestamp": f"2026-10-10T21:37:{second:02d}Z", "requestId": f"request-{message_id}",
                  "message": {"id": message_id, "model": "claude-opus-4-6", "content": list(blocks),
                              "usage": {"input_tokens": 1, "output_tokens": second + 1}}}
        return (json.dumps(record) + "\n").encode("utf-8")

    def bash(self, command: str) -> dict[str, object]:
        return {"type": "tool_use", "name": "Bash", "input": {"command": command}}

    def write_activity_transcript(self, path: Path) -> None:
        """Messages whose blocks match CLAUDE_ACTIVITIES."""
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join((
            self.claude_line("activity-one", 0, {"type": "thinking", "thinking": "build runs"},
                             self.bash("sleep 45; tail -2 /tmp/claude-1000/project/session/tasks/build3.log")),
            self.claude_line("activity-two", 1, {"type": "tool_use", "name": "Read", "input": {
                "file_path": "/tmp/claude-1000/project/session/tasks/b37sx2o7k.output"}}),
            self.claude_line("activity-three", 2, self.bash(f'{BOARD} post /tmp/board impl status "tests running"')),
            self.claude_line("activity-four", 3, self.bash("sleep 5"),
                             {"type": "tool_use", "name": "Edit", "input": {"file_path": "invented.py"}}),
            self.claude_line("activity-five", 4, {"type": "text", "text": "Done."}),
        )))

    def activities(self, machine: str, family: str, agent_id: str) -> list[tuple[object, ...]]:
        return self.values(self.rows("SELECT activity, wait_action FROM turns WHERE machine = ? AND family = ? "
                                     + "AND agent_id = ? ORDER BY at", (machine, family, agent_id)),
                           "activity", "wait_action")

    def cursor(self, path: Path) -> int:
        matches = [row for row in self.rows("SELECT path, offset FROM cursors WHERE machine = 'natedev'")
                   if cast(str, row["path"]).endswith(path.name)]
        self.assertEqual(len(matches), 1, str(path))
        return cast(int, matches[0]["offset"])

    def append_bytes(self, path: Path, content: bytes) -> None:
        with path.open("ab") as source:
            _ = source.write(content)

    def assert_source_additions(self, output: str, family: str, added: int,
                                machine: str = "natedev") -> None:
        lines = [line for line in output.splitlines() if line.startswith(f"{machine} {family}:")]
        self.assertEqual(len(lines), 1, output)
        self.assertRegex(lines[0], rf"\b{added} turns added\b")

    def assert_family_state(self, output: str, machine: str, family: str, expected: SourceState) -> None:
        sources = self.rows("SELECT state FROM turn_sources WHERE machine = ? AND family = ?", (machine, family))
        self.assertEqual(self.values(sources, "state"), [(expected,)])
        lines = [line for line in output.splitlines() if line.startswith(f"{machine} {family}:")]
        self.assertEqual(len(lines), 1, output)
        self.assertRegex(lines[0], rf"; {expected}(?:\b|$)")

    def deny_listing(self, path: Path) -> None:
        original_mode = stat.S_IMODE(path.stat().st_mode)
        self.addCleanup(path.chmod, original_mode)
        path.chmod(0)
        try:
            with os.scandir(path) as entries:
                _ = list(entries)
        except PermissionError:
            return
        self.skipTest("This process can list a directory at mode 000")

    def add_listing_projects(self, root: Path) -> Path:
        readable = root / "readable-project"
        blocked = root / "blocked-project"
        readable.mkdir()
        blocked.mkdir()
        _ = shutil.copyfile(FIXTURES / "listing/readable.jsonl", readable / f"{READABLE_SESSION}.jsonl")
        _ = shutil.copyfile(FIXTURES / "listing/blocked.jsonl", blocked / f"{BLOCKED_SESSION}.jsonl")
        return blocked

    def weights(self) -> WeightReader:
        return cast(WeightReader, cast(object, importlib.import_module("scripts.usage.weights")))

    def assert_unweighted(self, row: sqlite3.Row, at: datetime) -> None:
        try:
            result = self.weights().weighted(row, at)
        except (ValueError, LookupError) as exc:
            self.assertTrue(str(exc), "An unavailable weight needs an explanation")
        else:
            self.assertNotIsInstance(result, (int, float), "An unavailable weight cannot use a fallback")

    def two_codex_sessions(self) -> tuple[Path, Path, int, int]:
        shutil.rmtree(self.codex_root)
        first = self.codex_root / "2026/10/10/rollout-first.jsonl"
        second = self.codex_root / "2026/10/11/rollout-second.jsonl"
        turn_offset, edit_offset = self.write_codex_session(first)
        self.assertEqual(self.write_codex_session(second), (turn_offset, edit_offset))
        return first, second, turn_offset, edit_offset



class MacCollectorFixture(CollectorFixture):
    mac_home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    execution_directory: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        self.mac_home = self.root / "fixture-mac-home"
        _ = shutil.copytree(FIXTURES / "mac/home", self.mac_home)
        self.execution_directory = self.root / "outside-checkout"
        self.execution_directory.mkdir()
        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        ssh = binary_directory / "ssh"
        _ = shutil.copyfile(FIXTURES / "mac/fake_ssh.py", ssh)
        ssh.chmod(0o700)
        self.claude_root: Path = self.root / "local/claude"
        self.codex_root: Path = self.root / "local/codex"
        self.claude_root.mkdir(parents=True)
        self.codex_root.mkdir()
        self.agents_path: Path = self.root / "local/agents.jsonl"
        _ = self.agents_path.write_bytes(b"")


    @override
    def setUp(self) -> None:
        super().setUp()
        self.mac_home = self.root / "fixture-mac-home"
        self.execution_directory = self.root / "outside-checkout"
        self.claude_root = self.root / "local/claude"
        self.codex_root = self.root / "local/codex"
        self.agents_path = self.root / "local/agents.jsonl"

    def mac_collect(self, behavior: SshBehavior = "complete") -> MacRun:
        report_path = self.root / "ssh-report.json"
        self.assertFalse(report_path.exists(), "Read and remove each SSH report before another pass")
        previous = self.mac_rows("role_log_sources") if self.db_path.is_file() else []
        configuration = {
            "home": str(self.mac_home), "execution_directory": str(self.execution_directory),
            "report": str(report_path), "behavior": behavior,
            "database": str(self.db_path),
            "previous_role_read_at": cast(str, previous[0]["read_at"]) if previous else "",
        }
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        output = self.collect(mac_pass="fixture")
        self.assertTrue(report_path.is_file(), "The configured fake SSH must receive every Mac read")
        report = cast(MacStreamReport, json.loads(report_path.read_text(encoding="utf-8")))
        report_path.unlink()
        return MacRun(output, report)


    def mac_transcript(self, agent_id: str) -> Path:
        return self.mac_home / ".claude/projects/project" / f"{agent_id}.jsonl"


    def mac_rollout(self) -> Path:
        return self.mac_home / ".codex/sessions/2026/10/10/rollout-mac.jsonl"


    def mac_ledger(self) -> Path:
        return self.mac_home / ".local/state/usage-model/agents.jsonl"


    def mac_history(self) -> Path:
        return self.mac_home / ".local/state/plan-delegate/runs"


    def mac_rows(self, table: str) -> list[sqlite3.Row]:
        order = {"turns": "family, parent_id, agent_id, turn_key", "compactions": "family, parent_id, agent_id, at",
                 "cursors": "path", "role_log_sources": "machine"}[table]
        scope = " AND path NOT LIKE '%/plan-delegate/runs/%'" if table == "cursors" else ""
        return self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'{scope} ORDER BY {order}")


    def source_rows(self, machine: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM turn_sources WHERE machine = ? AND family IN ('claude', 'codex') ORDER BY family",
                         (machine,))


    def assert_source_states(self, output: str, machine: str, expected: SourceState) -> None:
        sources = self.source_rows(machine)
        self.assertEqual(self.values(sources, "family", "state"), [("claude", expected), ("codex", expected)])
        for family in ("claude", "codex"):
            lines = [line for line in output.splitlines() if line.startswith(f"{machine} {family}:")]
            self.assertEqual(len(lines), 1, output)
            self.assertRegex(lines[0], rf"; {expected}(?:\b|$)")
        for source in sources:
            stamp = datetime.fromisoformat(cast(str, source["read_at"]))
            self.assertEqual(stamp.utcoffset(), timedelta(0))
            if expected == "ready":
                complete = datetime.fromisoformat(cast(str, source["last_complete_at"]))
                self.assertEqual(complete.utcoffset(), timedelta(0))


    def complete_times(self, machine: str) -> list[tuple[object, ...]]:
        return self.values(self.source_rows(machine), "family", "last_complete_at")


    def assert_committed_role_snapshot_is_preserved(self, run: MacRun) -> None:
        observed = run.stream["role_snapshots"]
        self.assertEqual(len(observed), 1, "Observe the snapshot before the stream is cut")
        expected = observed[0]
        self.assertEqual((expected["problems"], expected["state"]), (2, "ready"))
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state", "read_at"),
                         [(expected["problems"], expected["state"], expected["read_at"])])
        shared = self.rows("SELECT role, lens, attribution FROM turns WHERE machine = 'mac' AND output = 73")
        self.assertEqual(self.values(shared, "role", "lens", "attribution"), [("review", "craft", "recorded")])
        agents = [line for line in run.output.splitlines() if line.startswith("mac agents:")]
        self.assertEqual(agents, ["mac agents: 0 turns added; ready; 2 problems"])


    def assert_first_file_is_committed(self) -> None:
        turns = self.mac_rows("turns")
        self.assertEqual(self.values(turns, "agent_id", "output", "before_first_edit"), [
            (MAC_PARENT, 2, 1), (MAC_PARENT, 1, 1),
        ])
        cursors = self.mac_rows("cursors")
        self.assertEqual(self.values(cursors, "path", "offset"), [
            (str(self.mac_transcript(MAC_PARENT)), self.mac_transcript(MAC_PARENT).stat().st_size),
        ])
        compactions = self.mac_rows("compactions")
        self.assertEqual(self.values(compactions, "agent_id"), [(MAC_PARENT,)])


    def assert_mac_fixture_totals(self) -> None:
        totals = self.rows("SELECT family, COUNT(*) AS turns, SUM(input) AS input, "
                           + "SUM(cache_write_5m) AS cache_write_5m, SUM(cache_write_1h) AS cache_write_1h, "
                           + "SUM(cache_read) AS cache_read, SUM(output) AS output FROM turns "
                           + "WHERE machine = 'mac' GROUP BY family ORDER BY family")
        self.assertEqual(self.values(totals, "family", "turns", "input", "cache_write_5m", "cache_write_1h",
                                     "cache_read", "output"), [
            ("claude", 4, 32, 2, 4, 7, 87), ("codex", 2, 45, 0, 0, 25, 5),
        ])
        shared = self.rows("SELECT * FROM turns WHERE machine = 'mac' AND output = 73")
        self.assertEqual(self.values(shared, "kind", "parent_id", "agent_id", "model", "effort",
                                     "before_first_edit", "role", "attribution"), [
            ("session", "", MAC_PARENT, "claude-opus-4-6", "high", 0, "interactive", "recorded"),
        ])
