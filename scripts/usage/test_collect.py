"""Exercise the usage collector's CLI against small invented source files."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast, final, override


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.db_path = self.state / "usage.db"
        (self.root / "fixture-repository").mkdir()
        (self.root / "production-docs").mkdir()
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
        return command, environment

    def cli_with_watch(self, watch_program: str) -> subprocess.CompletedProcess[str]:
        """Run the collector with `USAGE_WASTE_WATCH_SCRIPT` naming a stub watch that runs `watch_program`."""
        stub = self.root / "waste_watch_stub.py"
        _ = stub.write_text(watch_program)
        command, environment = self.collector_invocation(db=self.db_path)
        environment["USAGE_WASTE_WATCH_SCRIPT"] = str(stub)
        return subprocess.run(command, cwd=REPOSITORY, env=environment, capture_output=True, text=True,
                              check=False, timeout=60)

    def cli(self, *, db: Path, machine: str = "natedev",
            history_override: Literal["configured", "empty"] = "configured",
            mac_pass: Literal["skipped", "fixture"] = "skipped") -> subprocess.CompletedProcess[str]:
        command, environment = self.collector_invocation(db=db, machine=machine,
                                                        history_override=history_override, mac_pass=mac_pass)
        return subprocess.run(
            command,
            cwd=REPOSITORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=TEST_HANG_GUARD_SECS,
            check=False,
        )

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
                                text=True, check=False, timeout=300)
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


class CollectTests(CollectorFixture):
    def test_collector_cli_keeps_durable_commits_by_default(self) -> None:
        self.assert_cli_durability([], "PRAGMA synchronous=FULL")

    def test_collector_cli_can_disable_sync_for_disposable_ledgers(self) -> None:
        self.assert_cli_durability(["--durability", "off"], "PRAGMA synchronous=OFF")

    def two_codex_sessions(self) -> tuple[Path, Path, int, int]:
        shutil.rmtree(self.codex_root)
        first = self.codex_root / "2026/10/10/rollout-first.jsonl"
        second = self.codex_root / "2026/10/11/rollout-second.jsonl"
        turn_offset, edit_offset = self.write_codex_session(first)
        self.assertEqual(self.write_codex_session(second), (turn_offset, edit_offset))
        return first, second, turn_offset, edit_offset

    def test_two_session_files_keep_requests_at_the_same_byte_offset(self) -> None:
        first, second, offset, _ = self.two_codex_sessions()
        output = self.collect()
        turns = self.turns("codex", THREAD)
        self.assertEqual(len(turns), 2)
        self.assertCountEqual(self.values(turns, "turn_key"),
                              [(f"{first.name}:{offset}",), (f"{second.name}:{offset}",)])
        self.assertEqual(self.values(turns, "input", "cache_read", "output"), [(20, 10, 2), (20, 10, 2)])
        self.assert_source_additions(output, "codex", 2)

    def test_two_session_files_keep_edits_at_the_same_byte_offset(self) -> None:
        first, second, _, offset = self.two_codex_sessions()
        _ = self.collect()
        edits = self.rows("SELECT * FROM edits WHERE family = 'codex' AND agent_id = ?", (THREAD,))
        self.assertEqual(len(edits), 2)
        self.assertCountEqual(self.values(edits, "edit_key"),
                              [(f"{first.name}:{offset}",), (f"{second.name}:{offset}",)])

    def test_repeated_cursor_reads_replayed_files_and_relocation_keep_each_event_once(self) -> None:
        shutil.rmtree(self.codex_root)
        path = self.codex_root / "2026/10/10/rollout-movable.jsonl"
        turn_offset, edit_offset = self.write_codex_session(path)
        _ = self.collect()
        turns = self.all_values(self.turns("codex", THREAD))
        edits = self.all_values(self.rows("SELECT * FROM edits WHERE family = 'codex'"))
        self.assertEqual(self.values(self.turns("codex", THREAD), "turn_key"), [(f"{path.name}:{turn_offset}",)])
        self.assertEqual(self.values(self.rows("SELECT * FROM edits WHERE family = 'codex'"), "edit_key"),
                         [(f"{path.name}:{edit_offset}",)])
        for step in ("cursor", "replay", "relocate"):
            with self.subTest(step=step):
                if step == "replay":
                    with closing(sqlite3.connect(self.db_path)) as database, database:
                        _ = database.execute("DELETE FROM cursors WHERE path = ?", (str(path),))
                elif step == "relocate":
                    destination = self.codex_root / "2026/10/12" / path.name
                    destination.parent.mkdir(parents=True)
                    path = path.rename(destination)
                output = self.collect()
                self.assert_source_additions(output, "codex", 0)
                self.assertEqual(self.all_values(self.turns("codex", THREAD)), turns)
                self.assertEqual(self.all_values(self.rows("SELECT * FROM edits WHERE family = 'codex'")), edits)
                self.assertEqual(self.values(self.rows("SELECT sum(input), sum(cache_read), sum(output) "
                                                      + "FROM turns WHERE family = 'codex'"), "sum(input)",
                                             "sum(cache_read)", "sum(output)"), [(20, 10, 2)])
                self.assertEqual(self.rows("SELECT offset FROM cursors WHERE path = ?", (str(path),))[0][0],
                                 path.stat().st_size)

    def test_offset_key_store_versions_are_refused_without_changing_rows_or_cursors(self) -> None:
        _ = self.collect()
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("UPDATE turns SET turn_key = substr(turn_key, instr(turn_key, ':') + 1) "
                                 + "WHERE family = 'codex'")
            _ = database.execute("UPDATE edits SET edit_key = substr(edit_key, instr(edit_key, ':') + 1) "
                                 + "WHERE family = 'codex'")
        self.assertTrue(all(cast(str, row["turn_key"]).isdigit() for row in self.turns("codex", THREAD)))
        legacy_edits = self.rows("SELECT edit_key FROM edits WHERE family = 'codex'")
        self.assertTrue(legacy_edits)
        self.assertTrue(all(cast(str, row["edit_key"]).isdigit() for row in legacy_edits))
        for version in (7, 1):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.db_path)) as database, database:
                    _ = database.execute(f"PRAGMA user_version = {version}")
                before = {cast(str, row["name"]): self.all_values(self.rows(
                    'SELECT * FROM "' + cast(str, row["name"]) + '"'))
                    for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
                result = self.cli(db=self.db_path)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, f"collect: store version {version} predates file-named Codex keys; "
                                 + f"delete {self.db_path} and run the collector again to rebuild it\n")
                self.assertEqual(self.rows("PRAGMA user_version")[0][0], version)
                for table, rows in before.items():
                    self.assertEqual(self.all_values(self.rows('SELECT * FROM "' + table + '"')), rows, table)

    def test_legacy_store_refusal_preserves_bytes_journal_and_schema(self) -> None:
        _ = self.collect()
        with closing(sqlite3.connect(self.db_path)) as database:
            _ = database.execute("DROP TABLE folders")
            _ = database.execute("PRAGMA user_version = 6")
            self.assertEqual(database.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        schema_query = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        schema = self.all_values(self.rows(schema_query))
        tables = [cast(str, row["name"]) for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        self.assertNotIn("folders", tables)
        rows = {table: self.all_values(self.rows('SELECT * FROM "' + table + '"')) for table in tables}
        before = self.db_path.read_bytes()

        result = self.cli(db=self.db_path)

        self.assertEqual((result.returncode, result.stdout, result.stderr), (
            1, "", "collect: store version 6 predates file-named Codex keys; "
            + f"delete {self.db_path} and run the collector again to rebuild it\n",
        ))
        self.assertTrue(self.db_path.read_bytes() == before, "Refusal changed the store file's bytes")
        self.assertEqual(self.rows("PRAGMA journal_mode")[0][0], "delete")
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], 6)
        self.assertEqual(self.all_values(self.rows(schema_query)), schema)
        for table, before_rows in rows.items():
            self.assertEqual(self.all_values(self.rows('SELECT * FROM "' + table + '"')), before_rows, table)

    def test_new_store_is_stamped_with_the_request_activity_version(self) -> None:
        _ = self.collect()
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], 10)

    def test_codex_requests_record_wait_status_and_work(self) -> None:
        self.write_activity_rollout(self.codex_root / "2026/10/10/rollout-activity.jsonl")
        output = self.collect()
        self.assert_source_additions(output, "codex", 4 + len(CODEX_ACTIVITIES))
        self.assertEqual(self.activities("natedev", "codex", ACTIVITY_THREAD), CODEX_ACTIVITIES)
        self.assertEqual(self.activities("natedev", "codex", THREAD), [("work", None)] * 4)

    def test_claude_requests_record_wait_status_and_work(self) -> None:
        self.write_activity_transcript(self.claude_root / "project" / f"{ACTIVITY_SESSION}.jsonl")
        _ = self.collect()
        self.assertEqual(self.activities("natedev", "claude", ACTIVITY_SESSION), CLAUDE_ACTIVITIES)
        self.assertEqual(self.activities("natedev", "claude", SESSION), [("work", None)] * 4)

    def test_claude_message_lines_merge_into_one_request_activity(self) -> None:
        source = self.claude_root / "project" / f"{ACTIVITY_SESSION}.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        _ = source.write_bytes(self.claude_line("growing", 0, {"type": "text", "text": "Waiting on the build."}))
        _ = self.collect()
        self.assertEqual(self.activities("natedev", "claude", ACTIVITY_SESSION), [("work", None)])
        self.append_bytes(source, self.claude_line("growing", 1, self.bash("sleep 30")))
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        self.assertEqual(self.activities("natedev", "claude", ACTIVITY_SESSION), [("wait", "sleep")])
        tool_result = {"type": "user", "timestamp": "2026-10-10T21:37:02Z",
                       "message": {"content": [{"type": "tool_result", "content": "slept"}]}}
        self.append_bytes(source, (json.dumps(tool_result) + "\n").encode("utf-8")
                          + self.claude_line("growing", 3, {"type": "tool_use", "name": "Write",
                                                            "input": {"file_path": "invented.py"}})
                          + self.claude_line("next", 4, self.bash("ps -p 123")))
        output = self.collect()
        self.assert_source_additions(output, "claude", 1)
        self.assertEqual(self.activities("natedev", "claude", ACTIVITY_SESSION), [("work", None), ("wait", "ps")])
        self.assertEqual(self.values(self.turns("claude", ACTIVITY_SESSION), "output"), [(4,), (5,)])

    def test_version_nine_store_gains_activity_columns_and_reads_every_source_again(self) -> None:
        _ = self.collect()
        before = self.rows("SELECT machine, family, agent_id, turn_key, input, output FROM turns ORDER BY 1, 2, 3, 4")
        with closing(sqlite3.connect(self.db_path)) as database:
            _ = database.execute("ALTER TABLE turns DROP COLUMN wait_action")
            _ = database.execute("ALTER TABLE turns DROP COLUMN activity")
            with database:
                _ = database.execute("INSERT INTO turns VALUES (" + ",".join("?" * 19) + ")", (
                    "natedev", "codex", "thread", "", "gone-thread", "rollout-gone.jsonl:0",
                    "2026-10-09T12:00:00+00:00", "gpt-6.1-sol", "high", 5, 0, 0, 1, 1, 1, None, None,
                    "unassigned", "not_applicable"))
                _ = database.execute("PRAGMA user_version = 9")
        cursors = len(self.rows("SELECT * FROM cursors"))
        self.assertGreater(cursors, 0)
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        self.assert_source_additions(output, "codex", 0)
        self.assertEqual(self.rows("PRAGMA user_version")[0][0], 10)
        self.assertEqual([cast(str, row["name"]) for row in self.rows("PRAGMA table_info(turns)")][-2:],
                         ["activity", "wait_action"])
        after = self.rows("SELECT machine, family, agent_id, turn_key, input, output FROM turns "
                          + "WHERE agent_id != 'gone-thread' ORDER BY 1, 2, 3, 4")
        self.assertEqual(self.all_values(after), self.all_values(before))
        self.assertEqual(self.values(self.rows("SELECT activity, COUNT(*) AS turns FROM turns GROUP BY activity "
                                               + "ORDER BY activity"), "activity", "turns"),
                         [("unclassified", 1), ("work", len(before))])
        self.assertEqual(self.activities("natedev", "codex", "gone-thread"), [("unclassified", None)])
        self.assertEqual(len(self.rows("SELECT * FROM cursors")), cursors)
        for family in ("claude", "codex"):
            self.assertEqual(self.cursor(self.source_path(family)), self.source_path(family).stat().st_size)

    def test_waste_watch_failure_leaves_the_pass_successful(self) -> None:
        result = self.cli_with_watch(FAILING_WATCH)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "waste_watch: exit 1: fixture watch failure second line\n")
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 10)

    def test_claude_request_keys_keep_the_message_and_request_identity(self) -> None:
        _ = self.collect()
        self.assertEqual([json.loads(cast(str, row["turn_key"])) for row in self.turns("claude", SESSION)],
                         [["message-one", "request-one"], ["message-two", "request-two"],
                          ["message-three", "request-two"], ["message-one", "request-four"]])

    def test_held_pass_lock_exits_without_opening_or_changing_the_store(self) -> None:
        _ = self.collect()
        before = self.db_path.read_bytes()
        holder = """
import fcntl
import sys
with open(sys.argv[1], 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.read()
"""
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), USAGE_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        with subprocess.Popen([sys.executable, "-c", holder, str(self.db_path) + ".lock"],
                              env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), "locked\n")
                result = self.cli(db=self.db_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "usage collect: another pass is running\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.db_path.read_bytes(), before)
                _ = self.db_path.write_bytes(b"fixture store must not be opened")
                result = self.cli(db=self.db_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "usage collect: another pass is running\n")
                self.assertEqual(result.stderr, "")
                self.assertEqual(self.db_path.read_bytes(), b"fixture store must not be opened")
                _ = self.db_path.write_bytes(before)
                assert process.stdin is not None
                process.stdin.close()
                self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        _ = self.collect()

    def test_pass_uses_wal_and_finishes_while_a_reader_holds_its_snapshot(self) -> None:
        _ = self.collect()
        self.assertEqual(self.values(self.rows("PRAGMA journal_mode"), "journal_mode"), [("wal",)])
        with closing(sqlite3.connect(self.db_path)) as reader:
            _ = reader.execute("BEGIN")
            before = cast(int, reader.execute("SELECT count(*) FROM turns").fetchone()[0])
            path = self.source_path("claude")
            line = {"type": "assistant", "timestamp": "2026-10-12T00:00:00Z",
                    "message": {"id": "new-reader-held-turn", "model": "claude-opus-4-6",
                                "usage": {"input_tokens": 1, "output_tokens": 0}}}
            self.append_bytes(path, (json.dumps(line) + "\n").encode())
            _ = self.collect()
            self.assertEqual(reader.execute("SELECT count(*) FROM turns").fetchone()[0], before)
        self.assertEqual(self.rows("SELECT count(*) AS turns FROM turns")[0]["turns"], before + 1)

    def test_unlistable_local_project_marks_claude_partial_and_keeps_complete_time(self) -> None:
        _ = self.collect()
        previous = self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'claude'"),
            "last_complete_at")
        blocked = self.add_listing_projects(self.claude_root)
        self.deny_listing(blocked)
        output = self.collect()
        self.assert_family_state(output, "natedev", "claude", "partial")
        self.assert_family_state(output, "natedev", "codex", "ready")
        self.assert_source_additions(output, "claude", 1)
        self.assertEqual(self.values(self.turns("claude", READABLE_SESSION), "input", "output"), [(7, 9)])
        self.assertEqual(self.turns("claude", BLOCKED_SESSION), [])
        self.assertEqual(self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'claude'"),
            "last_complete_at"), previous)

    def test_unlistable_local_root_records_unreadable_and_keeps_complete_time(self) -> None:
        _ = self.collect()
        previous = self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'claude'"),
            "last_complete_at")
        rows = self.all_values(self.rows("SELECT * FROM turns WHERE family = 'claude' ORDER BY agent_id, turn_key"))
        self.deny_listing(self.claude_root)
        output = self.collect()
        self.assert_family_state(output, "natedev", "claude", "unreadable")
        self.assert_family_state(output, "natedev", "codex", "ready")
        self.assert_source_additions(output, "claude", 0)
        self.assertEqual(self.all_values(self.rows(
            "SELECT * FROM turns WHERE family = 'claude' ORDER BY agent_id, turn_key")), rows)
        self.assertEqual(self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'claude'"),
            "last_complete_at"), previous)

    def test_oversized_claude_count_skips_only_its_line_and_advances_cursor(self) -> None:
        path = self.source_path("claude")
        _ = shutil.copyfile(FIXTURES / "invalid-counts/claude.jsonl", path)
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "input", "output"), [(2, 3), (4, 5)])
        self.assertEqual(self.cursor(path), path.stat().st_size)

    def test_oversized_codex_count_skips_only_its_line_and_advances_cursor(self) -> None:
        path = self.source_path("codex")
        _ = shutil.copyfile(FIXTURES / "invalid-counts/codex.jsonl", path)
        _ = self.collect()
        self.assertEqual(self.values(self.turns("codex", THREAD), "input", "cache_read", "output"),
                         [(5, 2, 3), (6, 3, 5)])
        self.assertEqual(self.cursor(path), path.stat().st_size)

    def test_schema_preserves_all_raw_columns_and_identity_keys(self) -> None:
        _ = self.collect()
        expected = {
            "turns": (
                ("machine", "family", "kind", "parent_id", "agent_id", "turn_key", "at", "model",
                 "effort", "input", "cache_write_5m", "cache_write_1h", "cache_read", "output",
                 "before_first_edit", "role", "lens", "attribution", "lens_state", "activity", "wait_action"),
                ("machine", "family", "parent_id", "agent_id", "turn_key"),
            ),
            "compactions": (
                ("machine", "family", "parent_id", "agent_id", "at"),
                ("machine", "family", "parent_id", "agent_id", "at"),
            ),
            "cursors": (("machine", "path", "offset"), ("machine", "path")),
            "role_log_sources": (("machine", "problems", "state", "read_at"), ("machine",)),
            "turn_sources": (("machine", "family", "state", "read_at", "last_complete_at"), ("machine", "family")),
        }
        for table, (columns, primary_key) in expected.items():
            with self.subTest(table=table):
                information = self.rows(f"PRAGMA table_info({table})")
                self.assertEqual(tuple(cast(str, row["name"]) for row in information), columns)
                keys = sorted((cast(int, row["pk"]), cast(str, row["name"]))
                              for row in information if cast(int, row["pk"]) > 0)
                self.assertEqual(tuple(name for _, name in keys), primary_key)
        unique_columns: list[tuple[str, ...]] = []
        for index in self.rows("PRAGMA index_list(turns)"):
            if cast(int, index["unique"]) == 1 and cast(int, index["partial"]) == 1:
                name = cast(str, index["name"]).replace('"', '""')
                index_columns = self.rows(f'PRAGMA index_info("{name}")')
                unique_columns.append(tuple(cast(str, column["name"]) for column in index_columns))
        self.assertIn(("machine", "turn_key"), unique_columns)

    def test_fixture_raw_totals_keep_token_classes_apart(self) -> None:
        _ = self.collect()
        totals = self.rows(
            "SELECT family, COUNT(*) AS turns, SUM(input) AS input, "
            + "SUM(cache_write_5m) AS cache_write_5m, SUM(cache_write_1h) AS cache_write_1h, "
            + "SUM(cache_read) AS cache_read, SUM(output) AS output FROM turns GROUP BY family ORDER BY family")
        self.assertEqual(self.values(totals, "family", "turns", "input", "cache_write_5m",
                                     "cache_write_1h", "cache_read", "output"), [
            ("claude", 6, 53, 19, 9, 99, 17),
            ("codex", 4, 80, 0, 0, 47, 10),
        ])

    def test_claude_models_effort_and_cache_remainder_belong_to_each_turn(self) -> None:
        _ = self.collect()
        rows = self.turns("claude", SESSION)
        self.assertEqual(self.values(rows, "model", "effort", "input", "cache_write_5m", "cache_write_1h",
                                     "cache_read", "output"), [
            ("claude-opus-4-6", None, 10, 9, 3, 20, 2),
            ("claude-sonnet-4-6", "high", 11, 2, 0, 21, 3),
            ("claude-opus-4-6", "low", 12, 5, 4, 22, 4),
            ("claude-opus-4-6", "high", 13, 0, 0, 23, 5),
        ])
        self.assertEqual(len({cast(str, row["turn_key"]) for row in rows}), 4)

    def test_repeated_claude_message_keeps_each_token_class_maximum(self) -> None:
        source = self.source_path("claude")
        _ = shutil.copyfile(FIXTURES / "messages/growing.jsonl", source)
        output = self.collect()
        self.assert_source_additions(output, "claude", 3)
        rows = self.turns("claude", SESSION)
        self.assertEqual(self.values(rows, "input", "cache_write_5m", "cache_write_1h", "cache_read", "output"),
                         [(30, 8, 9, 15, 73)])
        first_line = source.read_bytes().splitlines(keepends=True)[0]
        self.append_bytes(source, first_line)
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        self.assertEqual(self.all_values(self.turns("claude", SESSION)), self.all_values(rows))
        snapshot = self.all_values(self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key"))
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key")), snapshot)

    def test_growing_claude_message_updates_existing_row_without_adding_a_turn(self) -> None:
        source = self.source_path("claude")
        lines = (FIXTURES / "messages/growing.jsonl").read_bytes().splitlines(keepends=True)
        _ = source.write_bytes(lines[0])
        _ = self.collect()
        before = self.turns("claude", SESSION)
        self.assertEqual(self.values(before, "output"), [(1,)])
        codex = self.all_values(self.turns("codex", THREAD))
        self.append_bytes(source, b"".join(lines[1:]))
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        after = self.turns("claude", SESSION)
        self.assertEqual(self.values(after, "input", "cache_write_5m", "cache_write_1h", "cache_read", "output"),
                         [(30, 8, 9, 15, 73)])
        self.assertEqual(self.values(after, "turn_key"), self.values(before, "turn_key"))
        self.assertEqual(self.all_values(self.turns("codex", THREAD)), codex)
        self.assertEqual(self.cursor(source), source.stat().st_size)
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        self.assertEqual(self.all_values(self.turns("claude", SESSION)), self.all_values(after))

    def test_claude_message_with_a_later_edit_line_is_not_before_first_edit(self) -> None:
        _ = shutil.copyfile(FIXTURES / "messages/first-edit.jsonl", self.source_path("claude"))
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "output", "before_first_edit"),
                         [(3, 1), (40, 0)])

    def test_appended_edit_line_updates_the_existing_message_boundary(self) -> None:
        source = self.source_path("claude")
        lines = (FIXTURES / "messages/first-edit.jsonl").read_bytes().splitlines(keepends=True)
        _ = source.write_bytes(b"".join(lines[:2]))
        _ = self.collect()
        before = self.turns("claude", SESSION)
        self.assertEqual(self.values(before, "output", "before_first_edit"), [(3, 1), (1, 1)])
        self.append_bytes(source, lines[2])
        output = self.collect()
        self.assert_source_additions(output, "claude", 0)
        after = self.turns("claude", SESSION)
        self.assertEqual(self.values(after, "output", "before_first_edit"), [(3, 1), (40, 0)])
        self.assertEqual(self.values(after, "turn_key"), self.values(before, "turn_key"))

    def test_codex_context_changes_model_effort_and_missing_effort(self) -> None:
        _ = self.collect()
        self.assertEqual(self.values(self.turns("codex", THREAD), "model", "effort", "input",
                                     "cache_write_5m", "cache_write_1h", "cache_read", "output"), [
            ("gpt-5.6-sol", "medium", 20, 0, 0, 10, 2),
            ("gpt-6.1-sol", "high", 25, 0, 0, 15, 3),
            ("gpt-6.1-sol", "low", 30, 0, 0, 20, 4),
            ("unknown-future-model", None, 5, 0, 0, 2, 1),
        ])

    def test_codex_turn_keys_use_the_file_name_and_byte_offset(self) -> None:
        _ = self.collect()
        expected_offsets: list[int] = []
        offset = 0
        for line in self.source_path("codex").read_bytes().splitlines(keepends=True):
            record = cast(dict[str, object], json.loads(line))
            payload = cast(dict[str, object], record["payload"])
            info = cast(dict[str, object], payload.get("info", {}))
            if record["type"] == "event_msg" and payload.get("type") == "token_count" and "last_token_usage" in info:
                expected_offsets.append(offset)
            offset += len(line)
        self.assertEqual(self.values(self.turns("codex", THREAD), "turn_key"),
                         [(f"{self.source_path('codex').name}:{offset}",) for offset in expected_offsets])

    def test_edit_boundaries_and_compactions_are_per_agent(self) -> None:
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "before_first_edit"), [(1,), (0,), (0,), (0,)])
        self.assertEqual(self.values(self.turns("claude", CHILD), "before_first_edit"), [(1,), (0,)])
        self.assertEqual(self.values(self.turns("codex", THREAD), "before_first_edit"), [(1,), (1,), (0,), (0,)])
        compactions = self.rows("SELECT * FROM compactions ORDER BY family, agent_id")
        self.assertEqual(len(compactions), 3)
        actual = {
            (cast(str, row["family"]), cast(str, row["parent_id"]), cast(str, row["agent_id"]),
             datetime.fromisoformat(cast(str, row["at"])))
            for row in compactions
        }
        self.assertEqual(actual, {
            ("claude", "", SESSION, BOUNDARY + timedelta(seconds=2)),
            ("claude", SESSION, CHILD, BOUNDARY + timedelta(seconds=7)),
            ("codex", "", THREAD, BOUNDARY + timedelta(seconds=4)),
        })

    def test_all_claude_edit_tools_mark_the_first_edit(self) -> None:
        path = self.source_path("claude")
        original = path.read_text(encoding="utf-8")
        for name in ("Edit", "Write", "NotebookEdit"):
            with self.subTest(tool=name):
                _ = path.write_text(original.replace('"name":"Edit"', f'"name":"{name}"'), encoding="utf-8")
                _ = self.collect(machine=name)
                rows = self.rows("SELECT before_first_edit FROM turns WHERE machine = ? AND agent_id = ? ORDER BY at",
                                 (name, SESSION))
                self.assertEqual(self.values(rows, "before_first_edit"), [(1,), (0,), (0,), (0,)])

    def test_hook_first_worker_merges_full_id_and_unique_prefix_history(self) -> None:
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution", "lens", "lens_state"), [
            ("interactive", "recorded", None, "not_applicable"),
            ("worker", "recorded", None, "not_applicable"),
            ("worker", "recorded", None, "not_applicable"),
            ("worker", "recorded", None, "not_applicable"),
        ])

    def test_review_lenses_and_estimated_roles_follow_assignment_time(self) -> None:
        _ = self.collect()
        self.assertEqual(self.values(self.turns("codex", THREAD), "role", "attribution", "lens", "lens_state"), [
            ("impl", "recorded", None, "not_applicable"),
            ("impl", "recorded", None, "not_applicable"),
            ("review", "recorded", "contract", "known"),
            ("review", "estimated", None, "unrecorded"),
        ])

    def test_a_nonworker_prefix_cannot_replace_an_exact_hook_assignment(self) -> None:
        ledger = self.agents_path.read_text(encoding="utf-8")
        _ = self.agents_path.write_text(ledger.replace('"role":"worker"', '"role":"review","lens":"contract"'),
                                        encoding="utf-8")
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"),
                         [("interactive", "recorded")] * 4)

    def test_a_subagent_does_not_borrow_a_session_worker_prefix(self) -> None:
        self.append_bytes(self.agents_path,
                          b'{"id":"a0123456","role":"worker","at":"2026-10-10T21:35:05Z"}\n')
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", CHILD), "role", "attribution"),
                         [("subagent", "recorded")] * 2)

    def test_parent_and_subagent_rows_keep_separate_usage(self) -> None:
        _ = self.collect()
        parent = self.turns("claude", SESSION)
        child = self.turns("claude", CHILD)
        self.assertEqual(self.values(parent, "kind", "parent_id"), [("session", "")] * 4)
        self.assertEqual(self.values(child, "kind", "parent_id", "role"), [("subagent", SESSION, "subagent")] * 2)
        self.assertEqual(sum(cast(int, row["input"]) for row in parent), 46)
        self.assertEqual(sum(cast(int, row["input"]) for row in child), 7)

    def test_subagent_copy_merges_into_parent_message_and_keeps_own_messages(self) -> None:
        parent = self.source_path("claude")
        child = parent.with_suffix("") / "subagents" / f"agent-{CHILD}.jsonl"
        _ = shutil.copyfile(FIXTURES / "messages/parent.jsonl", parent)
        _ = shutil.copyfile(FIXTURES / "messages/fork.jsonl", child)
        for machine in ("natedev", "fixture-machine"):
            with self.subTest(machine=machine):
                output = self.collect(machine=machine)
                self.assert_source_additions(output, "claude", 3, machine)
                rows = self.rows("SELECT * FROM turns WHERE machine = ? AND family = 'claude' ORDER BY at",
                                 (machine,))
                self.assertEqual(self.values(rows, "kind", "parent_id", "agent_id", "output"), [
                    ("session", "", SESSION, 73), ("session", "", SESSION, 3),
                    ("subagent", SESSION, CHILD, 4),
                ])
                self.assertEqual(self.values(rows[:1], "input", "cache_write_5m", "cache_write_1h", "cache_read"),
                                 [(20, 2, 3, 5)])
                snapshot = self.all_values(rows)
                output = self.collect(machine=machine)
                self.assert_source_additions(output, "claude", 0, machine)
                after = self.rows("SELECT * FROM turns WHERE machine = ? AND family = 'claude' ORDER BY at",
                                  (machine,))
                self.assertEqual(self.all_values(after), snapshot)

    def test_claude_message_copy_in_a_new_file_keeps_its_first_owner(self) -> None:
        parent = self.source_path("claude")
        child = parent.with_suffix("") / "subagents" / f"agent-{CHILD}.jsonl"
        _ = shutil.copyfile(FIXTURES / "messages/parent.jsonl", parent)
        child.unlink()
        output = self.collect()
        self.assert_source_additions(output, "claude", 2)
        before = self.turns("claude", SESSION)
        _ = shutil.copyfile(FIXTURES / "messages/fork.jsonl", child)
        output = self.collect()
        self.assert_source_additions(output, "claude", 1)
        after = self.turns("claude", SESSION)
        self.assertEqual(self.values(after, "turn_key", "kind", "parent_id", "agent_id"),
                         self.values(before, "turn_key", "kind", "parent_id", "agent_id"))
        self.assertEqual(self.values(after, "output"), [(73,), (3,)])
        self.assertEqual(self.values(self.turns("claude", CHILD), "output"), [(4,)])

    def test_two_claude_session_files_share_one_message_row(self) -> None:
        _ = shutil.copyfile(FIXTURES / "messages/parent.jsonl", self.source_path("claude"))
        _ = shutil.copyfile(FIXTURES / "messages/fork.jsonl", self.claude_root / "project" / f"{COLLISION}.jsonl")
        shutil.rmtree(self.source_path("claude").with_suffix(""))
        output = self.collect()
        self.assert_source_additions(output, "claude", 3)
        rows = self.rows("SELECT * FROM turns WHERE family = 'claude' ORDER BY at")
        self.assertEqual(self.values(rows, "kind", "parent_id", "agent_id", "output"), [
            ("session", "", SESSION, 73), ("session", "", SESSION, 3), ("session", "", COLLISION, 4),
        ])
        self.assertEqual(self.values(rows[:1], "input", "cache_write_5m", "cache_write_1h", "cache_read"),
                         [(20, 2, 3, 5)])

    def test_same_child_id_under_two_parents_is_counted_once_per_parent(self) -> None:
        other_parent = "11223344-3333-4333-8333-333333333333"
        source = self.claude_root / "project" / SESSION / "subagents"
        destination = self.claude_root / "project" / other_parent / "subagents"
        _ = shutil.copytree(source, destination)
        other_child = destination / f"agent-{CHILD}.jsonl"
        content = other_child.read_text(encoding="utf-8")
        _ = other_child.write_text(content.replace('"child-one"', '"other-child-one"')
                                  .replace('"child-two"', '"other-child-two"'), encoding="utf-8")
        _ = self.collect()
        groups = self.rows("SELECT parent_id, kind, COUNT(*) AS turns, SUM(input) AS input FROM turns "
                           + "WHERE agent_id = ? GROUP BY parent_id, kind ORDER BY parent_id", (CHILD,))
        self.assertEqual(self.values(groups, "parent_id", "kind", "turns", "input"), [
            (other_parent, "subagent", 2, 7), (SESSION, "subagent", 2, 7),
        ])
        _ = self.collect()
        self.assertEqual(len(self.turns("claude", CHILD)), 4)

    def test_shared_worker_prefixes_are_unassigned(self) -> None:
        self.use_fixture("identity")
        _ = self.collect()
        ambiguous = self.rows("SELECT * FROM turns WHERE family = 'claude' AND agent_id LIKE 'deadbeef%' ORDER BY agent_id")
        self.assertEqual(self.values(ambiguous, "role", "attribution", "lens", "lens_state"), [
            (None, "unassigned", None, "not_applicable"),
            (None, "unassigned", None, "not_applicable"),
        ])

    def test_ambiguous_prefix_still_allows_an_exact_assignment(self) -> None:
        self.use_fixture("identity")
        _ = self.collect()
        self.append_bytes(self.agents_path,
                          b'{"id":"deadbeef-1111-4111-8111-111111111111","role":"interactive","at":"2026-10-10T21:34:58Z"}\n')
        _ = self.collect()
        ambiguous = self.rows("SELECT * FROM turns WHERE family = 'claude' AND agent_id LIKE 'deadbeef%' ORDER BY agent_id")
        self.assertEqual(self.values(ambiguous, "role", "attribution"), [("interactive", "recorded"), (None, "unassigned")])

    def test_cross_family_id_collision_cannot_borrow_a_claude_worker_prefix(self) -> None:
        self.use_fixture("identity")
        _ = self.collect()
        claude = self.turns("claude", COLLISION)
        codex = self.turns("codex", COLLISION)
        self.assertEqual(self.values(claude, "kind", "role", "attribution"), [("session", "worker", "recorded")])
        self.assertEqual(self.values(codex, "kind", "role", "attribution"), [("thread", None, "unassigned")])
        self.assertEqual((cast(int, claude[0]["input"]), cast(int, codex[0]["input"])), (3, 5))

    def test_late_prefix_assignment_corrects_unchanged_turns(self) -> None:
        _ = self.agents_path.write_bytes(b"")
        _ = self.collect()
        before = self.rows("SELECT family, agent_id, turn_key FROM turns ORDER BY family, agent_id, turn_key")
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE attribution = 'unassigned'")), 10)
        cursors = "SELECT * FROM cursors WHERE path NOT LIKE '%agents.jsonl' ORDER BY machine, path"
        cursor_before = self.values(self.rows(cursors), "machine", "path", "offset")
        _ = shutil.copyfile(FIXTURES / "ledgers/late-prefix.jsonl", self.agents_path)
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"), [("worker", "recorded")] * 4)
        self.assertEqual(self.values(self.turns("codex", THREAD), "role", "attribution"), [(None, "unassigned")] * 4)
        self.assertEqual(self.values(self.turns("claude", CHILD), "role", "attribution"), [(None, "unassigned")] * 2)
        after = self.rows("SELECT family, agent_id, turn_key FROM turns ORDER BY family, agent_id, turn_key")
        self.assertEqual(self.values(after, "family", "agent_id", "turn_key"), self.values(before, "family", "agent_id", "turn_key"))
        cursor_after = self.values(self.rows(cursors), "machine", "path", "offset")
        self.assertEqual(cursor_after, cursor_before)

    def test_new_prefix_collision_removes_an_old_binding(self) -> None:
        self.use_fixture("identity")
        other = self.claude_root / "project/deadbeef-2222-4222-8222-222222222222.jsonl"
        content = other.read_bytes()
        other.unlink()
        _ = self.collect()
        first_id = "deadbeef-1111-4111-8111-111111111111"
        self.assertEqual(self.values(self.turns("claude", first_id), "role"), [("worker",)])
        _ = other.write_bytes(content)
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", first_id), "role", "attribution"), [(None, "unassigned")])

    def test_late_exact_assignment_is_temporal_and_replaces_missing_lens(self) -> None:
        _ = self.collect()
        self.append_bytes(self.agents_path,
                          b'{"id":"a1b2c3d4-2222-4222-8222-222222222222","role":"review","lens":"craft","at":"2026-10-10T21:35:05Z"}\n')
        _ = self.collect()
        rows = self.turns("codex", THREAD)
        self.assertEqual(self.values(rows, "role", "attribution", "lens", "lens_state"), [
            ("impl", "recorded", None, "not_applicable"),
            ("impl", "recorded", None, "not_applicable"),
            ("review", "recorded", "contract", "known"),
            ("review", "recorded", "craft", "known"),
        ])

    def test_future_assignment_does_not_attribute_an_earlier_turn(self) -> None:
        _ = self.agents_path.write_bytes(
            b'{"id":"a1b2c3d4-1111-4111-8111-111111111111","role":"director","at":"2026-10-10T21:35:03Z"}\n')
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"), [
            (None, "unassigned"), (None, "unassigned"), ("director", "recorded"), ("director", "recorded"),
        ])

    def test_historical_director_uses_session_identity_and_assignment_time(self) -> None:
        _ = self.agents_path.write_bytes(b"")
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/director.jsonl", history / "invented-run.jsonl")
        _ = self.collect()
        rows = self.turns("claude", SESSION)
        self.assertEqual(self.values(rows, "role"), [(None,), ("director",), ("director",), ("director",)])
        self.assertEqual(cast(str, rows[0]["attribution"]), "unassigned")
        self.assertEqual(self.values(rows[1:], "attribution"), [("estimated",)] * 3)
        self.assertEqual(self.values(self.turns("codex", THREAD), "role", "attribution"), [(None, "unassigned")] * 4)

    def test_director_handoff_starts_at_first_event_for_each_identity(self) -> None:
        ledger = (FIXTURES / "ledgers/handoff.jsonl").read_bytes().splitlines(keepends=True)
        _ = self.agents_path.write_bytes(ledger[0])
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/handoff.jsonl", history / "invented-handoff.jsonl")
        _ = self.collect()
        rows = self.turns("claude", SESSION)
        self.assertEqual(self.values(rows, "role", "attribution"), [
            ("interactive", "recorded"), ("interactive", "recorded"),
            ("director", "estimated"), ("director", "estimated"),
        ])
        _ = self.collect()
        self.assertEqual(self.all_values(self.turns("claude", SESSION)), self.all_values(rows))

    def test_repeated_history_events_do_not_reassign_the_same_director(self) -> None:
        _ = shutil.copyfile(FIXTURES / "ledgers/handoff.jsonl", self.agents_path)
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/handoff.jsonl", history / "invented-handoff.jsonl")
        self.append_bytes(self.source_path("claude"), (FIXTURES / "tails/claude-append.jsonl").read_bytes())
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"), [
            ("interactive", "recorded"), ("interactive", "recorded"), ("director", "estimated"),
            ("interactive", "recorded"), ("interactive", "recorded"),
        ])

    def test_empty_history_override_finds_director_evidence_under_home(self) -> None:
        _ = self.agents_path.write_bytes(b"")
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/director.jsonl", history / "invented-run.jsonl")
        _ = self.collect(history_override="empty")
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"), [
            (None, "unassigned"), ("director", "estimated"),
            ("director", "estimated"), ("director", "estimated"),
        ])

    def test_historical_director_evidence_does_not_override_a_recorded_assignment(self) -> None:
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/director.jsonl", history / "invented-run.jsonl")
        _ = self.collect()
        self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution"), [
            ("interactive", "recorded"), ("worker", "recorded"), ("worker", "recorded"), ("worker", "recorded"),
        ])

    def test_later_exact_interactive_evidence_replaces_historical_director_attribution(self) -> None:
        _ = self.agents_path.write_bytes(b"")
        history = self.root / ".local/state/plan-delegate/runs"
        history.mkdir(parents=True)
        _ = shutil.copyfile(FIXTURES / "history/director.jsonl", history / "invented-run.jsonl")
        _ = self.collect()
        before = self.turns("claude", SESSION)
        self.assertEqual(self.values(before, "role"), [(None,), ("director",), ("director",), ("director",)])
        self.append_bytes(self.agents_path,
                          b'{"id":"a1b2c3d4-1111-4111-8111-111111111111","role":"interactive","at":"2026-10-10T21:35:03Z"}\n')
        _ = self.collect()
        after = self.turns("claude", SESSION)
        self.assertEqual(self.values(after, "role"), [(None,), ("director",), ("interactive",), ("interactive",)])
        self.assertEqual(self.values(after[-2:], "attribution", "lens", "lens_state"),
                         [("recorded", None, "not_applicable")] * 2)
        self.assertEqual(self.values(after, "turn_key"), self.values(before, "turn_key"))

    def test_replaced_role_log_removes_stale_attribution_without_duplicating_turns(self) -> None:
        _ = self.collect()
        _ = self.agents_path.write_bytes(b"")
        _ = self.collect()
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 10)
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE role IS NULL AND lens IS NULL "
                                       + "AND attribution = 'unassigned' AND lens_state = 'not_applicable'")), 10)

    def test_malformed_role_log_snapshot_is_stable_and_valid_lines_attribute(self) -> None:
        _ = shutil.copyfile(FIXTURES / "ledgers/malformed.jsonl", self.agents_path)
        for _ in range(2):
            _ = self.collect()
            sources = self.rows("SELECT * FROM role_log_sources WHERE machine = 'natedev'")
            self.assertEqual(len(sources), 1)
            self.assertEqual(cast(int, sources[0]["problems"]), 3)
            self.assertTrue(cast(str, sources[0]["state"]))
            self.assertEqual(self.values(self.turns("claude", SESSION), "role", "attribution", "lens", "lens_state"),
                             [("review", "recorded", "craft", "known")] * 4)
            stamp = datetime.fromisoformat(cast(str, sources[0]["read_at"]))
            self.assertEqual(stamp.utcoffset(), timedelta(0))
        valid_line = self.agents_path.read_bytes().splitlines(keepends=True)[0]
        _ = self.agents_path.write_bytes(valid_line)
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT problems FROM role_log_sources"), "problems"), [(0,)])

    def test_missing_sources_succeed_and_remain_unassigned(self) -> None:
        self.agents_path.unlink()
        _ = self.collect()
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 10)
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE attribution = 'unassigned' AND role IS NULL")), 10)
        self.assertEqual(len(self.rows("SELECT * FROM role_log_sources")), 1)
        shutil.rmtree(self.claude_root)
        shutil.rmtree(self.codex_root)
        _ = self.collect()
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 10)

    def test_rerun_adds_nothing_and_cursors_cover_complete_lines(self) -> None:
        first_output = self.collect()
        snapshot = {
            table: self.all_values(self.rows(f"SELECT * FROM {table} ORDER BY 1, 2, 3"))
            for table in ("turns", "compactions", "cursors")
        }
        second_output = self.collect()
        for table, before in snapshot.items():
            after = self.all_values(self.rows(f"SELECT * FROM {table} ORDER BY 1, 2, 3"))
            self.assertEqual(after, before, table)
        for output in (first_output, second_output):
            lines = output.casefold().splitlines()
            self.assertTrue(any("claude" in line for line in lines), output)
            self.assertTrue(any("codex" in line for line in lines), output)
        for family in ("claude", "codex"):
            self.assertTrue(any(family in line and "0" in line for line in second_output.casefold().splitlines()), second_output)
            path = self.source_path(family)
            self.assertEqual(self.cursor(path), path.stat().st_size)

    def test_incomplete_claude_tail_is_resumed_without_losing_or_repeating_turns(self) -> None:
        _ = self.collect()
        path = self.source_path("claude")
        complete_size = path.stat().st_size
        tail = (FIXTURES / "tails/claude-append.jsonl").read_bytes()
        split = len(tail) - 12
        self.append_bytes(path, tail[:split])
        _ = self.collect()
        self.assertEqual(len(self.turns("claude", SESSION)), 4)
        self.assertEqual(self.cursor(path), complete_size)
        self.append_bytes(path, tail[split:])
        _ = self.collect()
        rows = self.turns("claude", SESSION)
        self.assertEqual(len(rows), 5)
        self.assertEqual(self.values(rows[-1:], "input", "output", "before_first_edit", "role"), [(5, 6, 0, "worker")])
        self.assertEqual(self.cursor(path), path.stat().st_size)
        _ = self.collect()
        self.assertEqual(len(self.turns("claude", SESSION)), 5)

    def test_incomplete_codex_tail_retains_the_context_and_first_edit(self) -> None:
        _ = self.collect()
        path = self.source_path("codex")
        complete_size = path.stat().st_size
        tail = (FIXTURES / "tails/codex-append.jsonl").read_bytes()
        split = len(tail) - 9
        self.append_bytes(path, tail[:split])
        _ = self.collect()
        self.assertEqual(len(self.turns("codex", THREAD)), 4)
        self.assertEqual(self.cursor(path), complete_size)
        self.append_bytes(path, tail[split:])
        _ = self.collect()
        rows = self.turns("codex", THREAD)
        self.assertEqual(self.values(rows[-1:], "model", "effort", "input", "cache_read", "output", "before_first_edit"),
                         [("unknown-future-model", None, 30, 30, 5, 0)])
        self.assertEqual(self.cursor(path), path.stat().st_size)
        _ = self.collect()
        self.assertEqual(len(self.turns("codex", THREAD)), 5)

    def test_machine_is_part_of_turn_compaction_cursor_and_diagnostic_identity(self) -> None:
        _ = self.collect()
        _ = self.collect(machine="fixture-machine")
        counts = self.rows("SELECT machine, COUNT(*) AS turns FROM turns GROUP BY machine ORDER BY machine")
        self.assertEqual(self.values(counts, "machine", "turns"), [("fixture-machine", 10), ("natedev", 10)])
        compactions = self.rows("SELECT machine, COUNT(*) AS events FROM compactions GROUP BY machine ORDER BY machine")
        self.assertEqual(self.values(compactions, "machine", "events"), [("fixture-machine", 3), ("natedev", 3)])
        for table in ("cursors", "role_log_sources"):
            machines = {cast(str, row["machine"]) for row in self.rows(f"SELECT machine FROM {table}")}
            self.assertEqual(machines, {"natedev", "fixture-machine"})
        _ = self.collect(machine="fixture-machine")
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 20)

    def test_timestamps_are_normalized_to_utc(self) -> None:
        _ = self.collect()
        rows = self.rows("SELECT at FROM turns UNION ALL SELECT at FROM compactions")
        for row in rows:
            stamp = datetime.fromisoformat(cast(str, row["at"]))
            self.assertEqual(stamp.utcoffset(), timedelta(0))
        first = datetime.fromisoformat(cast(str, self.turns("claude", SESSION)[0]["at"]))
        self.assertEqual(first, BOUNDARY - timedelta(seconds=1))

    def test_database_write_failure_has_nonzero_exit(self) -> None:
        _ = self.collect()
        result = self.cli(db=self.state)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stderr)
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 10)

    def test_no_mac_never_starts_ssh(self) -> None:
        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        ssh = binary_directory / "ssh"
        _ = ssh.write_text('#!/bin/sh\n: > "$HOME/ssh-called"\nexit 99\n', encoding="utf-8")
        ssh.chmod(0o700)
        output = self.collect()
        self.assertFalse((self.root / "ssh-called").exists())
        self.assertEqual({cast(str, row["machine"]) for row in self.rows("SELECT machine FROM turns")}, {"natedev"})
        sources = self.rows("SELECT machine, family, state, last_complete_at FROM turn_sources "
                            + "WHERE family IN ('claude', 'codex') ORDER BY machine, family")
        self.assertEqual(self.values(sources, "machine", "family", "state"), [
            ("mac", "claude", "skipped"), ("mac", "codex", "skipped"),
            ("natedev", "claude", "ready"), ("natedev", "codex", "ready"),
        ])
        self.assertEqual(self.values(sources[:2], "last_complete_at"), [(None,), (None,)])
        self.assertTrue(all(line.endswith("skipped") for line in output.splitlines() if line.startswith("mac ")))

    def test_weights_apply_on_read_per_model_without_changing_raw_rows(self) -> None:
        _ = self.collect()
        before = self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key")
        claude = self.turns("claude", SESSION)
        codex = self.turns("codex", THREAD)
        expected = [(claude[0], 39.25), (claude[1], 30.6), (claude[2], 48.45),
                    (claude[3], 40.3), (codex[0], 37.0), (codex[1], 40.75), (codex[2], 51.0)]
        for row, amount in expected:
            with self.subTest(model=cast(str, row["model"]), at=cast(str, row["at"])):
                at = datetime.fromisoformat(cast(str, row["at"]))
                self.assertAlmostEqual(self.weights().weighted(row, at), amount)
        after = self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key")
        self.assertEqual(self.all_values(after), self.all_values(before))

    def test_weight_date_boundary_and_unknown_model_have_no_fallback(self) -> None:
        _ = self.collect()
        codex = self.turns("codex", THREAD)
        row = codex[1]
        self.assert_unweighted(row, BOUNDARY - timedelta(microseconds=1))
        self.assertAlmostEqual(self.weights().weighted(row, BOUNDARY), 40.75)
        self.assertAlmostEqual(self.weights().weighted(row, BOUNDARY + timedelta(microseconds=1)), 40.75)
        self.assert_unweighted(codex[-1], BOUNDARY + timedelta(seconds=6))


@final
class MacCollectTests(CollectorFixture):
    mac_home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    execution_directory: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        super().setUp()
        self.mac_home = self.root / "fixture-mac-home"
        _ = shutil.copytree(FIXTURES / "mac/home", self.mac_home)
        self.execution_directory = self.root / "outside-checkout"
        self.execution_directory.mkdir()
        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        ssh = binary_directory / "ssh"
        _ = shutil.copyfile(FIXTURES / "mac/fake_ssh.py", ssh)
        ssh.chmod(0o700)
        self.claude_root = self.root / "local/claude"
        self.codex_root = self.root / "local/codex"
        self.claude_root.mkdir(parents=True)
        self.codex_root.mkdir()
        self.agents_path = self.root / "local/agents.jsonl"
        _ = self.agents_path.write_bytes(b"")

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

    def test_remote_session_files_keep_matching_request_and_edit_offsets_as_text_keys(self) -> None:
        sessions = self.mac_home / ".codex/sessions"
        shutil.rmtree(sessions)
        first = sessions / "2026/10/10/rollout-first.jsonl"
        second = sessions / "2026/10/11/rollout-second.jsonl"
        turn_offset, edit_offset = self.write_codex_session(first, MAC_THREAD)
        self.assertEqual(self.write_codex_session(second, MAC_THREAD), (turn_offset, edit_offset))
        run = self.mac_collect()
        turns = self.rows("SELECT * FROM turns WHERE machine = 'mac' AND family = 'codex'")
        edits = self.rows("SELECT * FROM edits WHERE machine = 'mac' AND family = 'codex'")
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(edits), 2)
        self.assertCountEqual(self.values(turns, "turn_key"),
                              [(f"{first.name}:{turn_offset}",), (f"{second.name}:{turn_offset}",)])
        self.assertCountEqual(self.values(edits, "edit_key"),
                              [(f"{first.name}:{edit_offset}",), (f"{second.name}:{edit_offset}",)])
        self.assertEqual(self.values(turns, "input", "cache_read", "output"), [(20, 10, 2), (20, 10, 2)])
        self.assertEqual(run.stream["edit_records"], 4, "Two Codex edits and the fixture's two Claude edits")
        before_turns, before_edits = self.all_values(turns), self.all_values(edits)
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["measurement_records"], 0)
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("DELETE FROM cursors WHERE machine = 'mac' AND path = ?", (str(first),))
        moved = sessions / "2026/10/12" / second.name
        moved.parent.mkdir(parents=True)
        _ = second.rename(moved)
        replayed = self.mac_collect()
        self.assert_source_additions(replayed.output, "codex", 0, "mac")
        self.assertEqual(self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' AND family = 'codex'")),
                         before_turns)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM edits WHERE machine = 'mac' AND family = 'codex'")),
                         before_edits)

    def test_mac_wire_carries_request_activity(self) -> None:
        self.write_activity_rollout(self.mac_home / ".codex/sessions/2026/10/13/rollout-activity.jsonl")
        self.write_activity_transcript(self.mac_home / ".claude/projects/project" / f"{ACTIVITY_SESSION}.jsonl")
        _ = self.mac_collect()
        self.assertEqual(self.activities("mac", "codex", ACTIVITY_THREAD), CODEX_ACTIVITIES)
        self.assertEqual(self.activities("mac", "claude", ACTIVITY_SESSION), CLAUDE_ACTIVITIES)

    def test_running_pass_keeps_its_lock_through_the_mac_read_until_exit(self) -> None:
        report = self.root / "ssh-report.json"
        waiting = report.with_suffix(".waiting")
        release = report.with_suffix(".release")
        configuration = {"home": str(self.mac_home), "execution_directory": str(self.execution_directory),
                         "report": str(report), "behavior": "wait_for_release", "database": str(self.db_path),
                         "previous_role_read_at": ""}
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        command, environment = self.collector_invocation(db=self.db_path, mac_pass="fixture")
        with subprocess.Popen(command, env=environment, cwd=REPOSITORY, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + TEST_HANG_GUARD_SECS
                while not waiting.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(waiting.exists(), "The fixture pass must reach its Mac read")
                before = {cast(str, row["name"]): self.all_values(self.rows('SELECT * FROM "' + cast(str, row["name"]) + '"'))
                          for row in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
                for attempt in range(2):
                    with self.subTest(attempt=attempt):
                        result = self.cli(db=self.db_path)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, "usage collect: another pass is running\n")
                        self.assertEqual(result.stderr, "")
                        self.assertIsNone(process.poll())
                        for table, rows in before.items():
                            self.assertEqual(self.all_values(self.rows('SELECT * FROM "' + table + '"')), rows, table)
                _ = release.write_bytes(b"")
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
            finally:
                _ = release.touch()
                if process.poll() is None:
                    process.terminate()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue(report.is_file())
        _ = cast(object, json.loads(report.read_text(encoding="utf-8")))
        report.unlink()
        waiting.unlink()
        release.unlink()
        _ = self.mac_collect()

    def test_ssh_first_nonempty_stderr_line_is_the_reason_even_when_the_pipe_breaks(self) -> None:
        for behavior in ("unreachable", "early_disconnect"):
            with self.subTest(behavior=behavior):
                run = self.mac_collect(behavior)
                for line in run.output.splitlines():
                    if line.startswith("mac "):
                        self.assertIn("fixture SSH is unreachable", line)
                        self.assertNotIn("Broken pipe", line)
                        self.assertNotIn("second fixture diagnostic", line)
                self.assert_source_states(run.output, "mac", "unreachable")

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

    def test_mac_without_a_checkout_matches_local_rows_and_writes_no_remote_files(self) -> None:
        before_entries = sorted(str(path.relative_to(self.mac_home)) for path in self.mac_home.rglob("*"))
        before = {str(path.relative_to(self.mac_home)): path.read_bytes()
                  for path in self.mac_home.rglob("*") if path.is_file()}
        self.claude_root = self.mac_home / ".claude/projects"
        self.codex_root = self.mac_home / ".codex/sessions"
        self.agents_path = self.mac_ledger()
        _ = shutil.copytree(self.mac_history(), self.root / ".local/state/plan-delegate/runs")
        run = self.mac_collect()
        turn_columns = ("family, kind, parent_id, agent_id, turn_key, at, model, effort, input, "
                        + "cache_write_5m, cache_write_1h, cache_read, output, before_first_edit, "
                        + "role, lens, attribution, lens_state, activity, wait_action")
        for table, columns, order in (
            ("turns", turn_columns, "family, parent_id, agent_id, turn_key"),
            ("compactions", "family, parent_id, agent_id, at", "family, parent_id, agent_id, at"),
            ("cursors", "path, offset", "path"),
        ):
            with self.subTest(table=table):
                scope = " AND path NOT LIKE '%/plan-delegate/runs/%'" if table == "cursors" else ""
                remote = self.rows(f"SELECT {columns} FROM {table} WHERE machine = 'mac'{scope} ORDER BY {order}")
                local = self.rows(f"SELECT {columns} FROM {table} WHERE machine = 'natedev'{scope} ORDER BY {order}")
                self.assertEqual(self.all_values(remote), self.all_values(local))
        self.assert_mac_fixture_totals()
        director = self.rows("SELECT role, attribution FROM turns WHERE machine = 'mac' AND agent_id = ?", (MAC_SECOND,))
        self.assertEqual(self.values(director, "role", "attribution"), [("director", "estimated")])
        self.assertEqual(run.stream["turn_records"], 8)
        self.assertEqual(run.stream["compaction_records"], 4)
        self.assertEqual(run.stream["opening_records"], 4)
        self.assertEqual(run.stream["edit_records"], 3)
        self.assertEqual(run.stream["cursor_records"], 4)
        self.assertGreater(run.stream["stdin_bytes"], 10_000)
        self.assertIn("BatchMode=yes", run.stream["options"])
        self.assertIn("ConnectTimeout=5", run.stream["options"])
        self.assertEqual(list(self.execution_directory.iterdir()), [])
        after = {str(path.relative_to(self.mac_home)): path.read_bytes()
                 for path in self.mac_home.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(sorted(str(path.relative_to(self.mac_home)) for path in self.mac_home.rglob("*")), before_entries)
        self.assert_source_states(run.output, "natedev", "ready")
        self.assert_source_states(run.output, "mac", "ready")
        diagnostics = self.mac_rows("role_log_sources")
        self.assertEqual(self.values(diagnostics, "problems", "state"), [(1, "ready")])

    def test_cut_after_first_cursor_keeps_file_and_resume_adds_only_remaining_turns(self) -> None:
        cut = self.mac_collect("cut_after_first_cursor")
        self.assertEqual(cut.stream["cursor_records"], 1)
        self.assert_first_file_is_committed()
        self.assert_source_additions(cut.output, "claude", 2, "mac")
        self.assert_source_additions(cut.output, "codex", 0, "mac")
        self.assert_source_states(cut.output, "mac", "partial")
        self.assert_source_states(cut.output, "natedev", "ready")
        self.assertEqual(self.complete_times("mac"), [("claude", None), ("codex", None)])
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])
        resumed = self.mac_collect()
        self.assert_source_additions(resumed.output, "claude", 2, "mac")
        self.assert_source_additions(resumed.output, "codex", 2, "mac")
        self.assertEqual(resumed.stream["cursor_records"], 3)
        self.assert_mac_fixture_totals()
        self.assert_source_states(resumed.output, "mac", "ready")
        snapshot = self.all_values(self.mac_rows("turns"))
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["measurement_records"], 0)
        self.assertEqual(repeated.stream["cursor_records"], 0)
        self.assertEqual(self.all_values(self.mac_rows("turns")), snapshot)

    def test_cut_inside_second_file_rolls_back_new_rows_merges_compactions_and_cursor(self) -> None:
        cut = self.mac_collect("cut_inside_second_file")
        self.assertEqual(cut.stream["cursor_records"], 1)
        self.assertEqual(cut.stream["turn_records"], 4)
        self.assert_first_file_is_committed()
        self.assert_source_additions(cut.output, "claude", 2, "mac")
        self.assert_source_states(cut.output, "mac", "partial")
        resumed = self.mac_collect()
        self.assert_source_additions(resumed.output, "claude", 2, "mac")
        self.assert_mac_fixture_totals()
        self.assertEqual(len(self.mac_rows("compactions")), 4)
        self.assertEqual(len(self.mac_rows("cursors")), 4)

    def test_role_snapshot_and_history_corrections_survive_a_later_cut(self) -> None:
        history = (self.mac_history() / "invented-mac.jsonl").read_bytes()
        (self.mac_history() / "invented-mac.jsonl").unlink()
        _ = self.mac_collect()
        unassigned = self.rows("SELECT role, attribution FROM turns WHERE machine = 'mac' AND agent_id = ?", (MAC_SECOND,))
        self.assertEqual(self.values(unassigned, "role", "attribution"), [(None, "unassigned")])
        self.append_bytes(self.mac_ledger(),
                          b'{"id":"00000001-1111-4111-8111-111111111111","role":"review","lens":"craft","at":"2026-10-10T21:35:09Z"}\n\xff\n')
        _ = (self.mac_history() / "invented-mac.jsonl").write_bytes(history)
        self.append_bytes(self.mac_transcript(MAC_PARENT), b'{"type":"user","timestamp":"2026-10-10T21:36:00Z"}\n')
        complete_times = self.complete_times("mac")
        cut = self.mac_collect("cut_after_first_cursor")
        self.assertEqual(cut.stream["measurement_records"], 0)
        self.assert_source_states(cut.output, "mac", "partial")
        self.assertEqual(self.complete_times("mac"), complete_times)
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(2, "ready")])
        shared = self.rows("SELECT role, lens, attribution FROM turns WHERE machine = 'mac' AND output = 73")
        self.assertEqual(self.values(shared, "role", "lens", "attribution"), [("review", "craft", "recorded")])
        director = self.rows("SELECT role, attribution FROM turns WHERE machine = 'mac' AND agent_id = ?", (MAC_SECOND,))
        self.assertEqual(self.values(director, "role", "attribution"), [("director", "estimated")])
        self.assertTrue(any(line.startswith("mac agents:") and "ready" in line and "2 problems" in line
                            for line in cut.output.splitlines()), cut.output)

    def test_cut_inside_first_file_preserves_committed_role_snapshot_and_reattribution(self) -> None:
        _ = self.mac_collect()
        cursors = self.all_values(self.mac_rows("cursors"))
        keys = self.values(self.mac_rows("turns"), "agent_id", "turn_key")
        complete_times = self.complete_times("mac")
        self.append_bytes(self.mac_ledger(),
                          b'{"id":"00000001-1111-4111-8111-111111111111","role":"review","lens":"craft","at":"2026-10-10T21:35:09Z"}\n\xff\n')
        self.append_bytes(self.mac_transcript(MAC_PARENT), (FIXTURES / "mac/updates/claude-append.jsonl").read_bytes())
        run = self.mac_collect("cut_inside_first_file")
        self.assertEqual(run.stream["turn_records"], 1)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assert_committed_role_snapshot_is_preserved(run)
        self.assert_source_states(run.output, "mac", "unreachable")
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assertEqual(self.all_values(self.mac_rows("cursors")), cursors)
        self.assertEqual(self.values(self.mac_rows("turns"), "agent_id", "turn_key"), keys)
        self.assertEqual(self.complete_times("mac"), complete_times)

    def test_missing_status_on_unchanged_pass_preserves_committed_role_snapshot(self) -> None:
        _ = self.mac_collect()
        cursors = self.all_values(self.mac_rows("cursors"))
        complete_times = self.complete_times("mac")
        self.append_bytes(self.mac_ledger(),
                          b'{"id":"00000001-1111-4111-8111-111111111111","role":"review","lens":"craft","at":"2026-10-10T21:35:09Z"}\n\xff\n')
        run = self.mac_collect("cut_before_status")
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assert_committed_role_snapshot_is_preserved(run)
        self.assert_source_states(run.output, "mac", "unreachable")
        self.assertEqual(self.all_values(self.mac_rows("cursors")), cursors)
        self.assertEqual(self.complete_times("mac"), complete_times)

    def test_unlistable_mac_project_marks_claude_partial_and_keeps_complete_time(self) -> None:
        _ = self.mac_collect()
        previous = self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'mac' AND family = 'claude'"),
            "last_complete_at")
        blocked = self.add_listing_projects(self.mac_home / ".claude/projects")
        self.deny_listing(blocked)
        run = self.mac_collect()
        self.assert_family_state(run.output, "mac", "claude", "partial")
        self.assert_family_state(run.output, "mac", "codex", "ready")
        self.assert_source_additions(run.output, "claude", 1, "mac")
        rows = self.rows("SELECT agent_id, input, output FROM turns WHERE machine = 'mac' AND agent_id IN (?, ?)",
                         (READABLE_SESSION, BLOCKED_SESSION))
        self.assertEqual(self.values(rows, "agent_id", "input", "output"), [(READABLE_SESSION, 7, 9)])
        self.assertEqual(self.values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'mac' AND family = 'claude'"),
            "last_complete_at"), previous)

    def test_unchanged_mac_transcripts_take_new_ledger_attribution_without_measurements(self) -> None:
        _ = self.mac_ledger().write_bytes(b"")
        _ = self.mac_collect()
        cursors = self.all_values(self.mac_rows("cursors"))
        keys = self.values(self.mac_rows("turns"), "agent_id", "turn_key")
        self.append_bytes(self.mac_ledger(),
                          b'{"id":"00000003-3333-4333-8333-333333333333","role":"review","lens":"craft","at":"2026-10-10T21:35:16Z"}\n')
        run = self.mac_collect()
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assertEqual(self.all_values(self.mac_rows("cursors")), cursors)
        self.assertEqual(self.values(self.mac_rows("turns"), "agent_id", "turn_key"), keys)
        rows = self.rows("SELECT role, lens, attribution FROM turns WHERE machine = 'mac' AND family = 'codex' ORDER BY at")
        self.assertEqual(self.values(rows, "role", "lens", "attribution"), [("review", "craft", "recorded")] * 2)
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")
        self.assert_source_states(run.output, "mac", "ready")

    def test_shorter_mac_files_are_read_again_from_zero(self) -> None:
        _ = self.mac_collect()
        for path, replacement in ((self.mac_transcript(MAC_SECOND), "claude-shorter.jsonl"),
                                  (self.mac_rollout(), "codex-shorter.jsonl")):
            before = path.stat().st_size
            _ = shutil.copyfile(FIXTURES / "mac/updates" / replacement, path)
            self.assertLess(path.stat().st_size, before)
        run = self.mac_collect()
        self.assert_source_additions(run.output, "claude", 1, "mac")
        self.assert_source_additions(run.output, "codex", 1, "mac")
        self.assertEqual(run.stream["cursor_records"], 2)
        replacement_rows = self.rows("SELECT family, model, effort, input, cache_read, output, before_first_edit FROM turns "
                                     + "WHERE machine = 'mac' AND at = '2026-10-10T21:35:25+00:00' ORDER BY family")
        self.assertEqual(self.values(replacement_rows, "family", "model", "effort", "input", "cache_read", "output",
                                     "before_first_edit"), [
            ("claude", "claude-haiku-4-5", "medium", 6, 0, 7, 1),
            ("codex", "gpt-6.1-sol", "high", 7, 5, 7, 1),
        ])
        for path in (self.mac_transcript(MAC_SECOND), self.mac_rollout()):
            cursor = self.rows("SELECT offset FROM cursors WHERE machine = 'mac' AND path = ?", (str(path),))
            self.assertEqual(self.values(cursor, "offset"), [(path.stat().st_size,)])

    def test_mac_incomplete_tail_recovers_prior_codex_model_effort_and_edit_context(self) -> None:
        _ = self.mac_collect()
        path = self.mac_rollout()
        complete_size = path.stat().st_size
        tail = (FIXTURES / "mac/updates/codex-append.jsonl").read_bytes()
        self.append_bytes(path, tail[:-9])
        incomplete = self.mac_collect()
        self.assertEqual(incomplete.stream["measurement_records"], 0)
        cursor = self.rows("SELECT offset FROM cursors WHERE machine = 'mac' AND path = ?", (str(path),))
        self.assertEqual(self.values(cursor, "offset"), [(complete_size,)])
        self.append_bytes(path, tail[-9:])
        completed = self.mac_collect()
        self.assert_source_additions(completed.output, "codex", 1, "mac")
        rows = self.rows("SELECT * FROM turns WHERE machine = 'mac' AND family = 'codex' ORDER BY at")
        self.assertEqual(self.values(rows[-1:], "model", "effort", "input", "cache_read", "output", "before_first_edit"),
                         [("gpt-6.1-sol", "low", 30, 30, 5, 0)])
        self.assertEqual(completed.stream["turn_records"], 1)

    def test_mac_failures_before_any_file_record_unreachable_and_leave_no_turns(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("unreachable", "python_error")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assertEqual(self.mac_rows("turns"), [])
                self.assertEqual(self.mac_rows("compactions"), [])
                self.assertEqual(self.mac_rows("cursors"), [])
                self.assert_source_states(run.output, "mac", "unreachable")
                self.assert_source_states(run.output, "natedev", "ready")
                self.assert_source_additions(run.output, "claude", 0, "mac")
                self.assert_source_additions(run.output, "codex", 0, "mac")
                self.assertEqual(self.complete_times("mac"), [("claude", None), ("codex", None)])
                self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(0, "unreachable")])
                self.assertTrue(any(line.startswith("mac agents:") and "unreachable" in line
                                    for line in run.output.splitlines()), run.output)

    def test_unreachable_mac_keeps_prior_rows_diagnostics_and_complete_times(self) -> None:
        _ = self.mac_collect()
        snapshot = {table: self.all_values(self.mac_rows(table)) for table in ("turns", "compactions", "cursors")}
        complete_times = self.complete_times("mac")
        run = self.mac_collect("unreachable")
        for table, before in snapshot.items():
            self.assertEqual(self.all_values(self.mac_rows(table)), before, table)
        self.assert_source_states(run.output, "mac", "unreachable")
        self.assertEqual(self.complete_times("mac"), complete_times)
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "unreachable")])
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")

    def test_no_mac_after_full_read_records_skipped_and_preserves_complete_times(self) -> None:
        _ = self.mac_collect()
        complete_times = self.complete_times("mac")
        previous_sources = self.source_rows("mac")
        snapshot = self.all_values(self.mac_rows("turns"))
        output = self.collect()
        self.assert_source_states(output, "mac", "skipped")
        self.assert_source_states(output, "natedev", "ready")
        self.assertEqual(self.complete_times("mac"), complete_times)
        self.assertEqual(self.all_values(self.mac_rows("turns")), snapshot)
        self.assertFalse((self.root / "ssh-report.json").exists(), "A skipped pass cannot invoke fake SSH")
        for before, after in zip(previous_sources, self.source_rows("mac"), strict=True):
            self.assertGreaterEqual(datetime.fromisoformat(cast(str, after["read_at"])),
                                    datetime.fromisoformat(cast(str, before["read_at"])))

    def test_missing_mac_roots_record_missing_and_preserve_complete_times(self) -> None:
        _ = self.mac_collect()
        complete_times = self.complete_times("mac")
        shutil.rmtree(self.mac_home / ".claude/projects")
        shutil.rmtree(self.mac_home / ".codex/sessions")
        run = self.mac_collect()
        self.assert_source_states(run.output, "mac", "missing")
        self.assertEqual(self.complete_times("mac"), complete_times)
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(len(self.mac_rows("turns")), 6)

    def test_unchanged_mac_rerun_transfers_no_measurements_or_cursors(self) -> None:
        _ = self.mac_collect()
        snapshot = {table: self.all_values(self.mac_rows(table)) for table in ("turns", "compactions", "cursors")}
        previous_times = self.complete_times("mac")
        run = self.mac_collect()
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")
        self.assert_source_states(run.output, "mac", "ready")
        for table, before in snapshot.items():
            self.assertEqual(self.all_values(self.mac_rows(table)), before, table)
        for before, after in zip(previous_times, self.complete_times("mac"), strict=True):
            self.assertGreater(datetime.fromisoformat(cast(str, after[1])), datetime.fromisoformat(cast(str, before[1])))

    def test_large_non_measurement_line_does_not_cross_the_mac_link(self) -> None:
        first = self.mac_collect()
        snapshot = self.all_values(self.mac_rows("turns"))
        ignored = json.dumps({"type": "user", "timestamp": "2026-10-10T21:36:00Z",
                              "message": {"content": "x" * (2 * 1024 * 1024)}}).encode("utf-8") + b"\n"
        source = self.mac_transcript(MAC_PARENT)
        self.append_bytes(source, ignored)
        run = self.mac_collect()
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 1)
        self.assertLess(run.stream["forwarded_bytes"], len(ignored) // 8)
        self.assertLessEqual(run.stream["forwarded_bytes"], first.stream["forwarded_bytes"] + 8192)
        self.assertEqual(self.all_values(self.mac_rows("turns")), snapshot)
        cursor = self.rows("SELECT offset FROM cursors WHERE machine = 'mac' AND path = ?", (str(source),))
        self.assertEqual(self.values(cursor, "offset"), [(source.stat().st_size,)])

    def test_large_history_without_director_evidence_sends_no_history_bytes(self) -> None:
        first = self.mac_collect()
        snapshot = {table: self.all_values(self.mac_rows(table)) for table in ("turns", "compactions", "cursors")}
        record = cast(dict[str, object], json.loads(
            (FIXTURES / "mac/updates/history-no-director.jsonl").read_bytes()))
        record["details"] = "x" * (2 * 1024 * 1024)
        ignored = json.dumps(record).encode("utf-8") + b"\n"
        _ = (self.mac_history() / "large-invented.jsonl").write_bytes(ignored)
        run = self.mac_collect()
        self.assertEqual(first.stream["director_records"], 1)
        self.assertEqual(run.stream["director_records"], 1)
        self.assertEqual(run.stream["history_file_records"], 1)
        self.assertEqual(run.stream["history_chunks"], 0)
        self.assertEqual(run.stream["metadata_chunks"], 1, "Only the small role log travels as bytes")
        self.assertEqual(run.stream["outcome_records"], 0)
        self.assertEqual(run.stream["history_cursor_records"], 1)
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assertLess(run.stream["forwarded_bytes"], len(ignored) // 128)
        self.assertLessEqual(run.stream["forwarded_bytes"], first.stream["forwarded_bytes"] + 2048)
        for table, before in snapshot.items():
            self.assertEqual(self.all_values(self.mac_rows(table)), before, table)
        self.assert_source_states(run.output, "mac", "ready")

    def test_mac_director_evidence_matches_local_attribution_at_each_identity_start(self) -> None:
        _ = shutil.copyfile(FIXTURES / "mac/updates/director-handoff.jsonl",
                            self.mac_history() / "invented-mac.jsonl")
        _ = shutil.copyfile(FIXTURES / "mac/updates/handoff-agents.jsonl", self.mac_ledger())
        self.claude_root = self.mac_home / ".claude/projects"
        self.codex_root = self.mac_home / ".codex/sessions"
        self.agents_path = self.mac_ledger()
        _ = shutil.copytree(self.mac_history(), self.root / ".local/state/plan-delegate/runs")
        run = self.mac_collect()
        columns = ("family, kind, parent_id, agent_id, turn_key, at, model, effort, input, "
                   + "cache_write_5m, cache_write_1h, cache_read, output, before_first_edit, "
                   + "role, lens, attribution, lens_state, activity, wait_action")
        order = "family, parent_id, agent_id, turn_key"
        remote = self.rows(f"SELECT {columns} FROM turns WHERE machine = 'mac' ORDER BY {order}")
        local = self.rows(f"SELECT {columns} FROM turns WHERE machine = 'natedev' ORDER BY {order}")
        self.assertEqual(self.all_values(remote), self.all_values(local))
        self.assertEqual(run.stream["director_records"], 2, "Repeated history events do not repeat an assignment")
        self.assertEqual(run.stream["history_file_records"], 1)
        self.assertEqual(run.stream["history_chunks"], 0)
        for machine in ("natedev", "mac"):
            rows = self.rows("SELECT role, attribution FROM turns WHERE machine = ? AND agent_id = ? ORDER BY at",
                             (machine, MAC_THREAD))
            self.assertEqual(self.values(rows, "role", "attribution"),
                             [("interactive", "recorded"), ("director", "estimated")])
            director = self.rows("SELECT role, attribution FROM turns WHERE machine = ? AND agent_id = ?",
                                 (machine, MAC_SECOND))
            self.assertEqual(self.values(director, "role", "attribution"), [("director", "estimated")])
            self.assert_source_states(run.output, machine, "ready")

    def test_new_mac_history_evidence_corrects_unchanged_turns_without_measurements(self) -> None:
        history = self.mac_history() / "invented-mac.jsonl"
        history.unlink()
        first = self.mac_collect()
        self.assertEqual(first.stream["director_records"], 0)
        rows = self.rows("SELECT role, attribution FROM turns WHERE machine = 'mac' AND agent_id = ?", (MAC_SECOND,))
        self.assertEqual(self.values(rows, "role", "attribution"), [(None, "unassigned")])
        raw_columns = ("family", "parent_id", "agent_id", "turn_key", "at", "model", "effort", "input",
                       "cache_write_5m", "cache_write_1h", "cache_read", "output", "before_first_edit")
        before = self.values(self.mac_rows("turns"), *raw_columns)
        cursors = self.all_values(self.mac_rows("cursors"))
        _ = shutil.copyfile(FIXTURES / "mac/home/.local/state/plan-delegate/runs/invented-mac.jsonl", history)
        run = self.mac_collect()
        self.assertEqual(run.stream["director_records"], 1)
        self.assertEqual(run.stream["history_file_records"], 1)
        self.assertEqual(run.stream["history_chunks"], 0)
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["cursor_records"], 0)
        self.assertEqual(self.values(self.mac_rows("turns"), *raw_columns), before)
        self.assertEqual(self.all_values(self.mac_rows("cursors")), cursors)
        rows = self.rows("SELECT role, attribution FROM turns WHERE machine = 'mac' AND agent_id = ?", (MAC_SECOND,))
        self.assertEqual(self.values(rows, "role", "attribution"), [("director", "estimated")])
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])
        self.assert_source_additions(run.output, "claude", 0, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")
        self.assert_source_states(run.output, "mac", "ready")

    def test_invalid_mac_director_evidence_ends_pass_before_snapshot_commit(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("missing_evidence_field", "malformed_evidence_field")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                _ = self.mac_collect()
                snapshot = {table: self.all_values(self.mac_rows(table))
                            for table in ("turns", "compactions", "cursors")}
                complete_times = self.complete_times("mac")
                run = self.mac_collect(behavior)
                self.assertEqual(run.stream["altered_evidence_records"], 1,
                                 "Change one payload field while preserving the evidence record tag")
                self.assertEqual(run.stream["history_file_records"], 0)
                self.assertEqual(run.stream["history_chunks"], 0)
                self.assert_source_states(run.output, "mac", "unreachable")
                self.assert_source_states(run.output, "natedev", "ready")
                self.assert_source_additions(run.output, "claude", 0, "mac")
                self.assert_source_additions(run.output, "codex", 0, "mac")
                self.assertEqual(self.complete_times("mac"), complete_times)
                self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"),
                                 [(1, "unreachable")])
                self.assertTrue(any(line.startswith("mac agents:") and "unreachable" in line
                                    for line in run.output.splitlines()), run.output)
                for table, before in snapshot.items():
                    self.assertEqual(self.all_values(self.mac_rows(table)), before, table)

    def test_invalid_remote_measurements_end_pass_without_defaults_or_second_file_commit(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("malformed_record", "unknown_measurement", "missing_output",
                                              "invalid_activity", "wait_without_action", "missing_activity")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assert_first_file_is_committed()
                self.assert_source_states(run.output, "mac", "partial")
                self.assert_source_additions(run.output, "claude", 2, "mac")
                self.assert_source_additions(run.output, "codex", 0, "mac")
                self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])
                if behavior == "malformed_record":
                    self.assertIn("partial (invalid remote stream)", run.output)
                if behavior in ("invalid_activity", "wait_without_action", "missing_activity"):
                    self.assertIn("activity", run.output)

    def test_oversized_wire_count_ends_pass_and_records_sources_without_second_file_commit(self) -> None:
        run = self.mac_collect("oversized_output")
        self.assert_first_file_is_committed()
        self.assert_source_states(run.output, "mac", "partial")
        self.assert_source_states(run.output, "natedev", "ready")
        self.assert_source_additions(run.output, "claude", 2, "mac")
        self.assert_source_additions(run.output, "codex", 0, "mac")
        self.assertEqual(self.values(self.mac_rows("role_log_sources"), "problems", "state"), [(1, "ready")])

    def test_cut_inside_record_reports_connection_cut_and_keeps_only_finished_file(self) -> None:
        run = self.mac_collect("cut_inside_record")
        self.assert_first_file_is_committed()
        self.assert_source_states(run.output, "mac", "partial")
        for family in ("claude", "codex"):
            lines = [line for line in run.output.splitlines() if line.startswith(f"mac {family}:")]
            self.assertRegex(lines[0], r"\bcut\b")
            self.assertNotIn("invalid remote stream", lines[0])

    def test_cut_stream_drains_remote_writer_before_reaping_shell(self) -> None:
        report = self.root / "ssh-report.json"
        finished = self.root / "remote-writer-finished"
        configuration = {"home": str(self.mac_home), "execution_directory": str(self.execution_directory),
                         "report": str(report), "behavior": "cut_inside_record", "database": str(self.db_path),
                         "previous_role_read_at": ""}
        config = self.root / "ssh-configuration.json"
        _ = config.write_text(json.dumps(configuration), encoding="utf-8")
        records = ('{"type":"file","kind":"claude"}\n{"type":"cursor"}\n'
                   '{"type":"file","kind":"claude"}\n{"type":"turn"}\n')
        writer = (
            "import sys; from pathlib import Path; "
            f"sys.stdout.write({records!r}); sys.stdout.flush(); "
            "sys.stdout.write('{}\\n' * 100000); sys.stdout.flush(); "
            "Path(sys.argv[1]).write_text('finished')"
        )
        remote_command = shlex.join((sys.executable, "-c", writer, str(finished))) + '; printf "\\nrc=%s\\n" "$?"'
        result = subprocess.run(
            [str(self.root / "bin/ssh"), remote_command],
            input=b"fixture request", capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False,
            env={**os.environ, "USAGE_TEST_SSH_CONFIG": str(config)},
        )
        self.assertEqual(result.stderr, b"", result.stderr.decode(errors="replace"))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(finished.is_file(), "A cut fixture must reap the writer after it finishes")
        lines = records.splitlines(keepends=True)
        expected = "".join(lines[:-1]) + lines[-1][:len(lines[-1]) // 2]
        self.assertEqual(result.stdout, expected.encode())


if __name__ == "__main__":
    _ = unittest.main()
