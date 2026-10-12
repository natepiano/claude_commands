"""Exercise the usage collector's CLI against small invented source files."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, cast, override


REPOSITORY = Path(__file__).resolve().parents[2]
COLLECTOR = Path(__file__).with_name("collect.py")
FIXTURES = Path(__file__).with_name("fixtures") / "collect"
SESSION = "a1b2c3d4-1111-4111-8111-111111111111"
THREAD = "a1b2c3d4-2222-4222-8222-222222222222"
CHILD = "a0123456789abcdef"
COLLISION = "facefeed-4444-4444-8444-444444444444"
BOUNDARY = datetime(2026, 10, 10, 21, 35, tzinfo=timezone.utc)


class WeightReader(Protocol):
    def weighted(self, turn_row: sqlite3.Row, at: datetime) -> float: ...


class CollectTests(unittest.TestCase):
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
        self.use_fixture("basic")

    def use_fixture(self, name: str) -> None:
        destination = self.root / "sources"
        if destination.exists():
            shutil.rmtree(destination)
        _ = shutil.copytree(FIXTURES / name, destination)
        self.claude_root = destination / "claude"
        self.codex_root = destination / "codex"
        self.agents_path = destination / "agents.jsonl"

    def cli(self, *, db: Path, machine: str = "natedev",
            history_override: Literal["configured", "empty"] = "configured") -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["USAGE_STATE_DIR"] = str(self.state)
        environment["HOME"] = str(self.root)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PATH"] = f"{self.root / 'bin'}:{environment.get('PATH', '')}"
        environment["PLAN_DELEGATE_HISTORY_DIR"] = (
            "" if history_override == "empty" else str(self.root / ".local/state/plan-delegate"))
        return subprocess.run(
            [sys.executable, str(COLLECTOR), "--db", str(db),
             "--machine", machine, "--claude-root", str(self.claude_root),
             "--codex-root", str(self.codex_root), "--agents-log", str(self.agents_path),
             "--no-mac"],
            cwd=REPOSITORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def collect(self, *, machine: str = "natedev",
                history_override: Literal["configured", "empty"] = "configured") -> str:
        result = self.cli(db=self.db_path, machine=machine, history_override=history_override)
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

    def weights(self) -> WeightReader:
        return cast(WeightReader, cast(object, importlib.import_module("scripts.usage.weights")))

    def assert_unweighted(self, row: sqlite3.Row, at: datetime) -> None:
        try:
            result = self.weights().weighted(row, at)
        except (ValueError, LookupError) as exc:
            self.assertTrue(str(exc), "An unavailable weight needs an explanation")
        else:
            self.assertNotIsInstance(result, (int, float), "An unavailable weight cannot use a fallback")

    def test_schema_preserves_all_raw_columns_and_identity_keys(self) -> None:
        _ = self.collect()
        expected = {
            "turns": (
                ("machine", "family", "kind", "parent_id", "agent_id", "turn_key", "at", "model",
                 "effort", "input", "cache_write_5m", "cache_write_1h", "cache_read", "output",
                 "before_first_edit", "role", "lens", "attribution", "lens_state"),
                ("machine", "family", "parent_id", "agent_id", "turn_key"),
            ),
            "compactions": (
                ("machine", "family", "parent_id", "agent_id", "at"),
                ("machine", "family", "parent_id", "agent_id", "at"),
            ),
            "cursors": (("machine", "path", "offset"), ("machine", "path")),
            "role_log_sources": (("machine", "problems", "state", "read_at"), ("machine",)),
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

    def test_codex_turn_keys_are_byte_offsets(self) -> None:
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
        actual_offsets = [int(cast(str, row["turn_key"])) for row in self.turns("codex", THREAD)]
        self.assertEqual(actual_offsets, expected_offsets)

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
        _ = self.collect()
        self.assertFalse((self.root / "ssh-called").exists())
        self.assertEqual({cast(str, row["machine"]) for row in self.rows("SELECT machine FROM turns")}, {"natedev"})

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
