"""Exercise the usage collector against invented source files."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from typing import cast, final

from scripts.usage.collect_test_support import (
    CODEX_ACTIVITIES,
    CLAUDE_ACTIVITIES,
    ACTIVITY_SESSION,
    ACTIVITY_THREAD,
    BLOCKED_SESSION,
    BOUNDARY,
    CHILD,
    COLLISION,
    CollectorFixture as CollectorFixture,
    FAILING_WATCH as FAILING_WATCH,
    FIXTURES as FIXTURES,
    MacRun as MacRun,
    MacStreamReport as MacStreamReport,
    READABLE_SESSION,
    REPOSITORY as REPOSITORY,
    SESSION,
    SNAPSHOT_COMMIT_SCRIPT as SNAPSHOT_COMMIT_SCRIPT,
    SshBehavior as SshBehavior,
    THREAD,
)
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class CollectTests(CollectorFixture):
    def test_collector_cli_keeps_durable_commits_by_default(self) -> None:
        self.assert_cli_durability([], "PRAGMA synchronous=FULL")

    def test_collector_cli_can_disable_sync_for_disposable_ledgers(self) -> None:
        self.assert_cli_durability(["--durability", "off"], "PRAGMA synchronous=OFF")

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



if __name__ == "__main__":
    _ = unittest.main()
