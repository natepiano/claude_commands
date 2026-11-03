"""Exercise the usage collector against invented source files."""

from __future__ import annotations

import json
import shutil
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from typing import cast, final

from scripts.usage.collect_test_support import (
    CODEX_ACTIVITIES,
    CLAUDE_ACTIVITIES,
    ACTIVITY_SESSION,
    ACTIVITY_THREAD,
    BLOCKED_SESSION,
    FIXTURES,
    MAC_PARENT,
    MAC_SECOND,
    MAC_THREAD,
    MacCollectorFixture,
    READABLE_SESSION,
)


@final
class MacCollectTests(MacCollectorFixture):
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



if __name__ == "__main__":
    _ = unittest.main()
