"""Exercise the usage collector against invented source files."""

from __future__ import annotations

import json
import shutil
import unittest
from typing import cast, final

from scripts.usage.collect_test_support import (
    FIXTURES,
    MAC_PARENT,
    MAC_SECOND,
    MAC_THREAD,
    MacCollectorFixture,
    SshBehavior,
)


@final
class MacCollectTests(MacCollectorFixture):
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



if __name__ == "__main__":
    _ = unittest.main()
