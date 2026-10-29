"""Exercise outcome and lifecycle storage through isolated collector runs."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import unittest
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast, final, override
from unittest.mock import patch

from scripts.usage import collect, outcomes, usage_report
from scripts.whoami import worker_usage
from scripts.usage.test_collect import (
    CollectorFixture, FIXTURES as COLLECT_FIXTURES, MacRun, MacStreamReport, SshBehavior,
)


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

class DefaultSourcesTests(unittest.TestCase):
    def test_first_read_accepts_a_concurrent_baseline_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / ".local/state/usage-model"
            previous = state / "seats-baseline"
            previous.mkdir(parents=True)
            _ = (previous / "readings-2026-10-10.jsonl").write_text("old reading\n")
            rename = Path.rename

            def convert_before_rename(path: Path, target: str | Path) -> Path:
                _ = rename(path, target)
                return rename(path, target)

            with (patch.object(Path, "home", return_value=home),
                  patch.object(Path, "rename", convert_before_rename)):
                sources = outcomes.default_sources()
                self.assertEqual(sources.baseline_readings.read_text(), "old reading\n")
                self.assertEqual(sources.baseline_readings.parent, state / "workers-baseline")
                self.assertFalse(previous.exists())
                self.assertEqual(outcomes.default_sources(), sources)

    def test_first_read_moves_the_old_baseline_and_later_reads_use_only_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / ".local/state/usage-model"
            previous = state / "seats-baseline"
            previous.mkdir(parents=True)
            _ = (previous / "readings-2026-10-10.jsonl").write_text("old reading\n")
            with patch.object(Path, "home", return_value=home):
                sources = outcomes.default_sources()
                self.assertEqual(sources.baseline_readings.read_text(), "old reading\n")
                self.assertEqual(sources.baseline_readings.parent, state / "workers-baseline")
                self.assertFalse(previous.exists())
                previous.mkdir()
                _ = (previous / "readings-2026-10-10.jsonl").write_text("later old reading\n")
                self.assertEqual(outcomes.default_sources(), sources)
                self.assertEqual(sources.baseline_readings.read_text(), "old reading\n")


FIXTURES = Path(__file__).with_name("fixtures") / "outcomes"
SESSION = "12345678-1111-4111-8111-111111111111"
CHILD = "outcome-child"
THREAD = "outcome-thread"
CORRECTED_THREAD = "opening-correction-thread"
OUTCOME_TABLES = ("runs", "phases", "passes", "findings", "repair_rounds")
DATA_TABLES = (*OUTCOME_TABLES, "checkpoints", "account_readings", "account_role_changes",
               "turns", "compactions", "openings", "edits", "folders", "cursors")

WEIGHTED_CHECKPOINTS = """
WITH used AS (
    SELECT day, role, model, sum(tokens) AS weighted, sum(tokens IS NULL) AS unweighted_turns
    FROM (SELECT substr(at, 1, 10) AS day, coalesce(role, 'unassigned') AS role, model,
                 weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output) AS tokens
          FROM turns)
    GROUP BY day, role, model
), landed AS (
    SELECT substr(at, 1, 10) AS day, count(*) AS checkpoints FROM checkpoints GROUP BY 1
)
SELECT used.day, used.role, used.model, used.weighted, used.unweighted_turns,
       coalesce(landed.checkpoints, 0) AS checkpoints,
       used.weighted / landed.checkpoints AS weighted_per_checkpoint
FROM used LEFT JOIN landed ON landed.day = used.day
ORDER BY used.day, used.role, used.model
"""


class SqlWeights(Protocol):
    def register(self, db: sqlite3.Connection) -> None: ...


class OutcomeFixture(CollectorFixture):
    history: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repository: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    production_docs: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = shutil.copytree(FIXTURES / "home", self.root, dirs_exist_ok=True)
        self.claude_root: Path = self.root / ".claude/projects"
        self.codex_root: Path = self.root / ".codex/sessions"
        self.agents_path: Path = self.root / ".local/state/usage-model/agents.jsonl"
        self.history = self.root / ".local/state/plan-delegate/runs"
        self.repository = self.root / "fixture-repository"
        self.production_docs = self.root / "production-docs"
        _ = self.git(self.repository, "init", "--quiet")

    def git(self, repository: Path, *arguments: str, at: str = "2026-10-10T21:40:00Z") -> str:
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                           GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
        result = subprocess.run(["git", "-C", str(repository), *arguments], env=environment,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def commit(self, subject: str, at: str = "2026-10-10T21:40:00Z") -> str:
        _ = self.git(self.repository, "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty",
                     "-m", subject, at=at)
        return self.git(self.repository, "rev-parse", "HEAD")

    def history_path(self) -> Path:
        return self.history / "fixture-run.jsonl"

    def snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        return {table: sorted(self.all_values(self.rows(f"SELECT * FROM {table}")), key=repr)
                for table in DATA_TABLES}

    def weighted_rows(self, sql: str = WEIGHTED_CHECKPOINTS) -> list[sqlite3.Row]:
        weights = cast(SqlWeights, cast(object, importlib.import_module("scripts.usage.weights")))
        with closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)) as database:
            database.row_factory = sqlite3.Row
            weights.register(database)
            return cast(list[sqlite3.Row], database.execute(sql).fetchall())

    def cursor_for(self, path: Path, machine: str = "natedev") -> int:
        rows = self.rows("SELECT offset FROM cursors WHERE machine = ? AND path = ?", (machine, str(path)))
        self.assertEqual(len(rows), 1, str(path))
        return cast(int, rows[0]["offset"])

    def assert_outcomes_added(self, output: str, family: str, count: int, machine: str = "natedev") -> None:
        lines = [line for line in output.splitlines() if line.startswith(f"{machine} {family}:")]
        self.assertEqual(len(lines), 1, output)
        self.assertRegex(lines[0], rf"\b{count} outcomes added\b")

    def assert_corrected_opening(self, at: str, machine: str = "natedev") -> None:
        rows = self.rows("SELECT * FROM openings WHERE machine = ? AND family = 'codex' AND agent_id = ?",
                         (machine, CORRECTED_THREAD))
        self.assertEqual(self.values(rows, "parent_id", "agent_id", "at"), [("", CORRECTED_THREAD, at)])


class StoredWorkerRoleTests(unittest.TestCase):
    def test_store_upgrade_preserves_switches_before_and_across_the_worker_rename(self) -> None:
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            _ = db.executescript(collect.SCHEMA)
            _ = db.execute(f"PRAGMA user_version = {collect.FILE_NAMED_CODEX_KEYS_VERSION}")
            _ = db.executemany("INSERT INTO account_role_changes VALUES (?, ?, ?, ?)", (
                ("natedev", "2026-10-01T00:00:00+00:00", "seats", "codex1"),
                ("natedev", "2026-10-02T00:00:00+00:00", "seats", "codex2"),
                ("natedev", "2026-10-03T00:00:00+00:00", "workers", "codex3"),
            ))
            collect.migrate_store(db)
            history = worker_usage.store_switch_history(db)
            self.assertEqual(history.switches, (
                datetime.fromisoformat("2026-10-02T00:00:00+00:00"),
                datetime.fromisoformat("2026-10-03T00:00:00+00:00"),
            ))
            outcome = usage_report.OUTCOMES["account_switches"]
            assert isinstance(outcome, usage_report.QueryOutcome)
            self.assertEqual(db.execute("SELECT count(*) FROM (" + outcome.query + ")").fetchone()[0], 2)
            rows = cast(list[sqlite3.Row], db.execute(
                "SELECT machine, at, role, label FROM account_role_changes ORDER BY at").fetchall())
            self.assertEqual([tuple(cast(object, row[column]) for column in ("machine", "at", "role", "label"))
                              for row in rows], [
                ("natedev", "2026-10-01T00:00:00+00:00", "workers", "codex1"),
                ("natedev", "2026-10-02T00:00:00+00:00", "workers", "codex2"),
                ("natedev", "2026-10-03T00:00:00+00:00", "workers", "codex3"),
            ])
            changes = db.total_changes
            collect.migrate_store(db)
            self.assertEqual(db.total_changes, changes)

    def test_store_upgrade_merges_old_roles_with_their_recollected_worker_records(self) -> None:
        with closing(sqlite3.connect(":memory:")) as db:
            _ = db.executescript(collect.SCHEMA)
            _ = db.execute(f"PRAGMA user_version = {collect.FILE_NAMED_CODEX_KEYS_VERSION}")
            _ = db.executemany("INSERT INTO account_role_changes VALUES (?, ?, ?, ?)", (
                ("natedev", "2026-10-01T00:00:00+00:00", "seats", "codex1"),
                ("natedev", "2026-10-01T00:00:00+00:00", "workers", "codex1"),
            ))
            collect.migrate_store(db)
            self.assertEqual(outcomes.save_account_role(db, {
                "at": "2026-10-01T00:00:00Z", "role": "workers", "label": "codex1",
            }), 0)
            self.assertEqual(db.execute("SELECT * FROM account_role_changes").fetchall(), [
                ("natedev", "2026-10-01T00:00:00+00:00", "workers", "codex1"),
            ])


class ProductionPathsTests(unittest.TestCase):
    def test_production_documents_contribute_repositories_and_worktrees(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        document = Path(directory.name) / "sample-production.md"
        _ = document.write_text(
            "".join((
                "# Production\n\n- **Repository:** `/repo/main`\n\n",
                "## Units\n\n| Unit | Worktree |\n| --- | --- |\n",
                "| a | `/repo/worktree` |\n",
            )),
            encoding="utf-8",
        )
        self.assertEqual(
            outcomes.production_paths(document),
            (Path("/repo/main"), Path("/repo/worktree")),
        )


class OutcomeTests(OutcomeFixture):
    def test_late_old_role_append_is_canonicalized_after_its_complete_line_arrives(self) -> None:
        _ = self.collect()
        path = self.root / "account-roles.jsonl"
        previous = self.cursor_for(path)
        self.append_bytes(path, b'not json\n{"at":"2026-10-10T22:00:00Z","role":"seats","label":"codex2"}')
        _ = self.collect()
        self.assertEqual(self.cursor_for(path), previous + len(b"not json\n"))
        self.assertEqual(self.rows("SELECT * FROM account_role_changes WHERE label = 'codex2'"), [])
        self.append_bytes(path, b"\n")
        output = self.collect()
        self.assert_outcomes_added(output, "account_roles", 1)
        self.assertEqual(self.values(self.rows("SELECT * FROM account_role_changes WHERE label = 'codex2'"),
                                     "machine", "at", "role", "label"), [
            ("natedev", "2026-10-10T22:00:00+00:00", "workers", "codex2"),
        ])
        self.assertEqual(self.cursor_for(path), path.stat().st_size)
        self.assert_outcomes_added(self.collect(), "account_roles", 0)

    def test_baseline_only_reading_has_its_own_source_and_cursor(self) -> None:
        baseline = self.root / "baseline-readings.jsonl"
        _ = baseline.write_text('{"account":"codex fixture","at":"2026-10-03T18:17:00Z",' +
                                '"remaining":70,"credits":100}\n', encoding="utf-8")
        output = self.collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM account_readings WHERE account = 'codex fixture'"),
                                     "machine", "account", "at", "remaining", "credits"),
                         [("natedev", "codex fixture", "2026-10-03T18:17:00+00:00", 70.0, 100.0)])
        self.assert_outcomes_added(output, "baseline_readings", 1)
        self.assert_family_state(output, "natedev", "baseline_readings", "ready")
        self.assertEqual(self.cursor_for(baseline), baseline.stat().st_size)

    def test_live_reading_wins_over_baseline_in_both_file_read_orders(self) -> None:
        baseline = self.root / "baseline-readings.jsonl"
        live = self.root / "readings.jsonl"
        baseline_line = ('{"account":"codex fixture","at":"2026-10-03T18:17:00Z",'
                         '"remaining":70,"credits":100}\n')
        live_line = ('{"account":"codex fixture","at":"2026-10-03T18:17:00Z",'
                     '"remaining":60,"credits":80}\n')
        for first in ("baseline", "live"):
            with self.subTest(first=first):
                self.db_path: Path = self.state / f"{first}-first.db"
                _ = baseline.write_text(baseline_line if first == "baseline" else "", encoding="utf-8")
                _ = live.write_text(live_line if first == "live" else "", encoding="utf-8")
                _ = self.collect()
                if first == "baseline":
                    self.append_bytes(live, live_line.encode())
                else:
                    self.append_bytes(baseline, baseline_line.encode())
                _ = self.collect()
                self.assertEqual(self.values(self.rows("SELECT * FROM account_readings"), "remaining", "credits"),
                                 [(60.0, 80.0)])
                before = self.all_values(self.rows("SELECT * FROM account_readings"))
                _ = self.collect()
                self.assertEqual(self.all_values(self.rows("SELECT * FROM account_readings")), before)

    def test_folder_schema_version_and_turn_time_index(self) -> None:
        _ = self.collect()
        columns = self.rows("PRAGMA table_info(folders)")
        self.assertEqual(self.values(columns, "name"),
                         [("machine",), ("family",), ("parent_id",), ("agent_id",), ("folder",)])
        self.assertEqual(self.values(columns, "pk"), [(1,), (2,), (3,), (4,), (0,)])
        self.assertEqual(cast(int, self.rows("PRAGMA user_version")[0][0]), 10)
        self.assertEqual(self.values(self.rows("PRAGMA index_info(turns_at)"), "name"), [("at",)])

    def test_claude_folder_uses_first_cwd_even_after_saved_cursor(self) -> None:
        path = self.claude_root / "project/folder-session.jsonl"
        lines = (FIXTURES / "updates/claude-folders.jsonl").read_bytes().splitlines(keepends=True)
        _ = path.write_bytes(b"".join(lines[:2]))
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM folders WHERE agent_id = 'folder-session'"), "folder"),
                         [("/invented/first",)])
        self.append_bytes(path, lines[2])
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM folders WHERE agent_id = 'folder-session'"), "folder"),
                         [("/invented/first",)])

    def test_codex_folder_follows_latest_metadata_in_one_read_and_after_cursor(self) -> None:
        path = self.codex_root / "2026/10/10/rollout-folder.jsonl"
        lines = (FIXTURES / "updates/codex-folders.jsonl").read_bytes().splitlines(keepends=True)
        _ = path.write_bytes(b"".join(lines))
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM folders WHERE agent_id = 'folder-thread'"), "folder"),
                         [("/invented/later",)])
        self.append_bytes(path, lines[0])
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM folders WHERE agent_id = 'folder-thread'"), "folder"),
                         [("/invented/first",)])
        before = self.all_values(self.rows("SELECT * FROM folders"))
        _ = self.collect()
        self.assertEqual(self.all_values(self.rows("SELECT * FROM folders")), before)

    def test_version_six_store_is_refused_without_replaying_folders_or_turns(self) -> None:
        path = self.claude_root / "project/folder-session.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/claude-folders.jsonl", path)
        _ = self.collect()
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("PRAGMA user_version = 6")
        before = self.snapshot()
        result = self.cli(db=self.db_path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "collect: store version 6 predates file-named Codex keys; "
                         + f"delete {self.db_path} and run the collector again to rebuild it\n")
        self.assertEqual(cast(int, self.rows("PRAGMA user_version")[0][0]), 6)
        self.assertEqual(self.snapshot(), before)

    def test_outcome_schema_keeps_identity_and_source_event_without_finding_details(self) -> None:
        _ = self.collect()
        expected = {
            "runs": (("machine", "run_id", "started_at", "plan_doc", "worktree", "branch", "state", "finished_at",
                      "source_event"), ("machine", "run_id")),
            "phases": (("machine", "run_id", "phase_instance_id", "phase_id", "title", "started_at", "state",
                        "finished_at", "source_event"), ("machine", "phase_instance_id")),
            "passes": (("machine", "run_id", "phase_instance_id", "pass_instance_id", "kind", "task", "slot",
                        "fix_pass", "started_at", "state", "finished_at", "source_event"), ("machine", "pass_instance_id")),
            "findings": (("machine", "run_id", "phase_instance_id", "finding_id", "severity", "caught_by", "lens",
                          "opened_round", "opened_at", "state", "state_at", "source_event"),
                         ("machine", "phase_instance_id", "finding_id")),
            "repair_rounds": (("machine", "run_id", "phase_instance_id", "round", "dispatched_at", "covered", "state",
                               "state_at", "source_event"), ("machine", "phase_instance_id", "round")),
            "checkpoints": (("commit_id", "at", "plan_slug", "phase_id", "mapping", "machine", "run_id",
                             "phase_instance_id"), ("commit_id",)),
            "account_readings": (("machine", "account", "at", "remaining_state", "remaining", "credits_state", "credits"),
                                 ("machine", "account", "at")),
            "account_role_changes": (("machine", "at", "role", "label"), ("machine", "at", "role", "label")),
            "openings": (("machine", "family", "parent_id", "agent_id", "at"),
                         ("machine", "family", "parent_id", "agent_id")),
            "edits": (("machine", "family", "parent_id", "agent_id", "edit_key", "at"),
                      ("machine", "family", "parent_id", "agent_id", "edit_key")),
        }
        for table, (columns, primary_key) in expected.items():
            with self.subTest(table=table):
                information = self.rows(f"PRAGMA table_info({table})")
                self.assertEqual(tuple(cast(str, row["name"]) for row in information), columns)
                keys = sorted((cast(int, row["pk"]), cast(str, row["name"]))
                              for row in information if cast(int, row["pk"]) > 0)
                self.assertEqual(tuple(name for _, name in keys), primary_key)

    def test_history_rows_use_file_run_id_creation_event_and_later_status(self) -> None:
        output = self.collect()
        self.assert_outcomes_added(output, "history", 9)
        self.assertEqual(self.values(self.rows("SELECT * FROM runs"), "run_id", "started_at", "plan_doc", "worktree",
                                     "branch", "state", "finished_at", "source_event"), [
            ("fixture-run", "2026-10-10T21:00:00+00:00", "docs/plans/fixture-plan.md", "/invented/worktree",
             "fixture-branch", "open", None, "run-create"),
        ])
        self.assertEqual(self.values(self.rows("SELECT * FROM phases"), "phase_instance_id", "phase_id", "title",
                                     "state", "finished_at", "source_event"),
                         [("fixture-phase", "6", "Store outcomes", "open", None, "phase-create")])
        passes = self.rows("SELECT * FROM passes ORDER BY started_at")
        self.assertEqual(self.values(passes, "pass_instance_id", "kind", "task", "slot", "fix_pass", "state",
                                     "finished_at", "source_event"), [
            ("fixture-pass", "review", "direct.review", "test", 0, "passed", "2026-10-10T21:12:00+00:00", "pass-create"),
            ("unfinished-pass", None, None, None, None, "open", None, "pass-open"),
        ])
        self.assertEqual(self.values(self.rows("SELECT * FROM findings ORDER BY opened_at"), "finding_id", "severity",
                                     "caught_by", "lens", "opened_round", "state", "state_at", "source_event"), [
            ("one", "blocker", "test", "adversary", 1, "closed", "2026-10-10T21:08:00+00:00", "finding-one-create"),
            ("two", "minor", "impl", "contract", 1, "closed", "2026-10-10T21:09:00+00:00", "finding-two-create"),
            ("three", "nit", "test", None, 2, "open", "2026-10-10T21:05:00+00:00", "finding-three-create"),
        ])
        self.assertEqual(self.values(self.rows("SELECT * FROM repair_rounds ORDER BY round"), "round", "covered", "state",
                                     "state_at", "source_event"), [
            (1, 2, "landed", "2026-10-10T21:07:00+00:00", "round-one-create"),
            (2, 1, "abandoned", "2026-10-10T21:11:00+00:00", "round-two-create"),
        ])
        self.assertEqual(self.cursor_for(self.history_path()), self.history_path().stat().st_size)

    def test_history_finish_tail_waits_for_newline_then_updates_existing_rows(self) -> None:
        _ = self.collect()
        path = self.history_path()
        previous = self.cursor_for(path)
        tail = (FIXTURES / "updates/finish.jsonl").read_bytes()
        self.append_bytes(path, tail[:-1])
        output = self.collect()
        self.assert_outcomes_added(output, "history", 0)
        self.assertEqual(self.values(self.rows("SELECT state, source_event FROM runs"), "state", "source_event"),
                         [("open", "run-create")])
        self.assertEqual(self.cursor_for(path), previous + len(tail) - len(tail.splitlines(keepends=True)[-1]))
        self.append_bytes(path, b"\n")
        output = self.collect()
        self.assert_outcomes_added(output, "history", 0)
        self.assertEqual(self.values(self.rows("SELECT * FROM runs"), "state", "finished_at", "source_event"),
                         [("complete", "2026-10-10T22:03:00+00:00", "run-create")])
        self.assertEqual(self.values(self.rows("SELECT * FROM phases"), "state", "finished_at", "source_event"),
                         [("complete", "2026-10-10T22:02:00+00:00", "phase-create")])
        self.assertEqual(self.values(self.rows("SELECT * FROM passes WHERE pass_instance_id = 'unfinished-pass'"),
                                     "state", "finished_at", "source_event"),
                         [("cancelled", "2026-10-10T22:00:00+00:00", "pass-open")])
        self.assertEqual(self.values(self.rows("SELECT * FROM findings WHERE finding_id = 'three'"), "state", "state_at"),
                         [("closed", "2026-10-10T22:01:00+00:00")])
        self.assertEqual(self.cursor_for(path), path.stat().st_size)

    def test_orphan_updates_and_unknown_events_create_no_outcomes(self) -> None:
        _ = shutil.copyfile(FIXTURES / "updates/orphans.jsonl", self.history / "orphan-run.jsonl")
        _ = self.collect()
        self.assertEqual([len(self.rows(f"SELECT * FROM {table}")) for table in OUTCOME_TABLES], [1, 1, 2, 3, 2])
        self.assertFalse(self.rows("SELECT * FROM runs WHERE run_id = 'orphan-run'"))

    def test_unchanged_sources_add_nothing_and_preserve_every_data_row(self) -> None:
        _ = self.commit("checkpoint(fixture-plan): phase 6 — Outcomes")
        _ = self.collect()
        before = self.snapshot()
        output = self.collect()
        self.assertEqual(self.snapshot(), before)
        for family in ("history", "checkpoints", "readings", "account_roles"):
            self.assert_outcomes_added(output, family, 0)
        self.assert_source_additions(output, "claude", 0)
        self.assert_source_additions(output, "codex", 0)

    def test_account_numbers_preserve_availability_bounds_and_utc_times(self) -> None:
        _ = self.collect(machine="fixture-local")
        readings = self.rows("SELECT * FROM account_readings ORDER BY at")
        self.assertEqual(self.values(readings, "machine", "account"), [("natedev", "fixture-account")] * 10)
        self.assertEqual(self.values(readings, "remaining_state", "remaining", "credits_state", "credits"), [
            ("read", 25, "read", 4.5), ("not_reported", None, "not_reported", None),
            ("not_reported", None, "not_reported", None), ("read", 0, "read", 0),
            ("read", 100, "read", 1000000), *[("unreadable", None, "unreadable", None)] * 5,
        ])
        self.assertEqual(cast(str, readings[0]["at"]), "2026-10-10T21:00:00+00:00")
        changes = self.rows("SELECT * FROM account_role_changes ORDER BY at, role")
        self.assertEqual(self.values(changes, "machine", "at", "role", "label"), [
            ("natedev", "2026-10-10T21:00:00+00:00", "primary", "fixture-a"),
            ("natedev", "2026-10-10T21:01:00+00:00", "primary", "fixture-b"),
            ("natedev", "2026-10-10T21:01:00+00:00", "secondary", "fixture-b"),
        ])

    def test_account_reading_and_role_tails_resume_from_complete_lines(self) -> None:
        _ = self.collect()
        reading_path = self.root / "readings.jsonl"
        role_path = self.root / "account-roles.jsonl"
        reading_size = reading_path.stat().st_size
        role_size = role_path.stat().st_size
        self.append_bytes(reading_path, b'{"account":"fixture-account","at":"2026-10-10T22:00:00Z","remaining":50}')
        self.append_bytes(role_path, b'{"at":"2026-10-10T22:00:00Z","role":"primary","label":"fixture-c"}')
        _ = self.collect()
        self.assertEqual(self.cursor_for(reading_path), reading_size)
        self.assertEqual(self.cursor_for(role_path), role_size)
        self.assertEqual(len(self.rows("SELECT * FROM account_readings")), 10)
        self.assertEqual(len(self.rows("SELECT * FROM account_role_changes")), 3)
        for path in (reading_path, role_path):
            self.append_bytes(path, b"\n")
        output = self.collect()
        self.assert_outcomes_added(output, "readings", 1)
        self.assert_outcomes_added(output, "account_roles", 1)
        self.assertEqual(len(self.rows("SELECT * FROM account_readings")), 11)
        self.assertEqual(len(self.rows("SELECT * FROM account_role_changes")), 4)

    def test_checkpoint_subjects_committer_times_and_latest_active_phase_mapping(self) -> None:
        self.append_bytes(self.history_path(), json.dumps({
            "event_id": "phase-restart", "event_type": "phase_started", "timestamp": "2026-10-10T21:30:00Z",
            "phase_instance_id": "latest-phase", "phase_id": "6", "phase_title": "Repeated phase",
        }).encode("utf-8") + b"\n")
        mapped = self.commit("checkpoint(fixture-plan): phase 6 — Outcomes", "2026-10-10T21:40:00Z")
        before = self.commit("checkpoint(fixture-plan): phase 6 — Before start", "2026-10-10T20:59:00Z")
        unknown = self.commit("checkpoint(other-plan): phase 6 — Unknown plan")
        malformed = self.commit("checkpoint(fixture-plan): saved work")
        _ = self.commit("Ordinary commit")
        _ = self.collect()
        rows = self.rows("SELECT * FROM checkpoints")
        self.assertEqual(len(rows), 4)
        by_id = {cast(str, row["commit_id"]): row for row in rows}
        self.assertEqual(self.values([by_id[mapped]], "at", "plan_slug", "phase_id", "mapping", "machine", "run_id",
                                     "phase_instance_id"), [
            ("2026-10-10T21:40:00+00:00", "fixture-plan", "6", "mapped", "natedev", "fixture-run", "latest-phase"),
        ])
        for commit_id in (before, unknown):
            self.assertEqual(self.values([by_id[commit_id]], "mapping", "machine", "run_id", "phase_instance_id"),
                             [("unmapped", None, None, None)])
        self.assertEqual(self.values([by_id[malformed]], "plan_slug", "phase_id", "mapping"), [(None, None, "unmapped")])

    def test_checkpoint_mapping_recomputes_for_late_phase_and_finish_boundaries(self) -> None:
        history = self.history_path().read_bytes()
        _ = self.history_path().write_bytes(b"")
        boundary = self.commit("checkpoint(fixture-plan): phase 6 — Boundary", "2026-10-10T22:02:00Z")
        after = self.commit("checkpoint(fixture-plan): phase 6 — Too late", "2026-10-10T22:02:01Z")
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT mapping FROM checkpoints"), "mapping"), [("unmapped",)] * 2)
        _ = self.history_path().write_bytes(history + (FIXTURES / "updates/finish.jsonl").read_bytes())
        output = self.collect()
        self.assert_outcomes_added(output, "checkpoints", 0)
        mapped = self.rows("SELECT * FROM checkpoints WHERE commit_id = ?", (boundary,))
        self.assertEqual(self.values(mapped, "mapping", "phase_instance_id"), [("mapped", "fixture-phase")])
        self.assertEqual(self.values(self.rows("SELECT * FROM checkpoints WHERE commit_id = ?", (after,)),
                                     "mapping", "machine"), [("unmapped", None)])

    def test_checkpoint_phase_id_with_spaces_stops_at_first_title_delimiter(self) -> None:
        self.append_bytes(self.history_path(), (FIXTURES / "updates/spaced-phase.jsonl").read_bytes())
        plain = self.commit("checkpoint(fixture-plan): phase ad hoc — Invented work")
        divided = self.commit("checkpoint(fixture-plan): phase ad hoc — Invented work — More detail")
        _ = self.collect()
        for commit_id in (plain, divided):
            with self.subTest(commit_id=commit_id):
                row = self.rows("SELECT * FROM checkpoints WHERE commit_id = ?", (commit_id,))
                self.assertEqual(self.values(row, "plan_slug", "phase_id", "mapping", "machine", "run_id",
                                             "phase_instance_id"), [
                    ("fixture-plan", "ad hoc", "mapped", "natedev", "fixture-run", "spaced-phase"),
                ])

    def test_checkpoints_in_production_repositories_and_other_refs_count_once(self) -> None:
        first = self.commit("checkpoint(fixture-plan): phase 6 — Main")
        _ = self.git(self.repository, "checkout", "--quiet", "-b", "fixture-side")
        side = self.commit("checkpoint(fixture-plan): phase 6 — Side")
        _ = self.git(self.repository, "checkout", "--quiet", "-")
        second_repo = self.root / "second-repository"
        second_repo.mkdir()
        _ = self.git(second_repo, "init", "--quiet")
        _ = self.git(second_repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m",
                     "checkpoint(fixture-plan): phase 6 — Other repository")
        second = self.git(second_repo, "rev-parse", "HEAD")
        _ = (self.production_docs / "fixture-production.md").write_text(
            f"- **Repository:** `{second_repo}`\n\n## Units\n\n| Worktree |\n| --- |\n| `{self.repository}` |\n",
            encoding="utf-8")
        _ = self.collect()
        self.assertEqual({cast(str, row["commit_id"]) for row in self.rows("SELECT * FROM checkpoints")}, {first, side, second})
        _ = self.collect()
        self.assertEqual(len(self.rows("SELECT * FROM checkpoints")), 3)

    def test_printed_and_stored_source_states_cover_history_and_local_outcome_sources(self) -> None:
        _ = self.commit("checkpoint(fixture-plan): phase 6 — Outcomes")
        output = self.collect(machine="fixture-local")
        for family in ("checkpoints", "readings", "account_roles"):
            self.assert_family_state(output, "natedev", family, "ready")
            self.assertFalse(self.rows("SELECT * FROM turn_sources WHERE machine = 'mac' AND family = ?", (family,)))
        self.assert_family_state(output, "fixture-local", "history", "ready")
        self.assert_family_state(output, "mac", "history", "skipped")
        complete = self.values(self.rows("SELECT family, last_complete_at FROM turn_sources WHERE machine = 'natedev' "
                                          + "AND family IN ('readings', 'account_roles') ORDER BY family"),
                               "family", "last_complete_at")
        (self.root / "readings.jsonl").unlink()
        (self.root / "account-roles.jsonl").unlink()
        output = self.collect(machine="fixture-local")
        for family in ("readings", "account_roles"):
            self.assert_family_state(output, "natedev", family, "missing")
        self.assertEqual(self.values(self.rows("SELECT family, last_complete_at FROM turn_sources WHERE machine = 'natedev' "
                                                + "AND family IN ('readings', 'account_roles') ORDER BY family"),
                                     "family", "last_complete_at"), complete)

    def test_unreadable_checkpoint_repository_records_partial_without_failing_run(self) -> None:
        _ = self.commit("checkpoint(fixture-plan): phase 6 — Outcomes")
        _ = self.collect()
        before = self.rows("SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'checkpoints'")
        shutil.rmtree(self.repository / ".git")
        output = self.collect()
        self.assert_family_state(output, "natedev", "checkpoints", "partial")
        self.assertEqual(self.all_values(self.rows(
            "SELECT last_complete_at FROM turn_sources WHERE machine = 'natedev' AND family = 'checkpoints'")),
                         self.all_values(before))
        self.assertEqual(len(self.rows("SELECT * FROM checkpoints")), 1)

    def test_removed_worktree_named_by_production_doc_leaves_checkpoints_ready(self) -> None:
        commit_id = self.commit("checkpoint(fixture-plan): phase 6 — Outcomes")
        _ = (self.production_docs / "fixture-production.md").write_text(
            f"## Units\n\n| Worktree |\n| --- |\n| `{self.root / 'removed-worktree'}` |\n", encoding="utf-8")
        output = self.collect()
        self.assert_family_state(output, "natedev", "checkpoints", "ready")
        self.assertEqual({cast(str, row["commit_id"]) for row in self.rows("SELECT * FROM checkpoints")}, {commit_id})

    def test_sql_weights_keep_date_boundaries_and_no_checkpoint_days(self) -> None:
        _ = self.commit("checkpoint(fixture-plan): phase 6 — First")
        _ = self.commit("checkpoint(fixture-plan): phase 6 — Second", "2026-10-10T21:41:00Z")
        _ = self.collect()
        before = self.snapshot()
        rows = self.weighted_rows()
        self.assertEqual(self.all_values(rows), [
            ("2026-10-10", "review", "gpt-6.1-sol", 35.5, 1, 2, 17.75),
            ("2026-10-10", "worker", "claude-opus-4-6", 27.25, 0, 2, 13.625),
            ("2026-10-11", "review", "unknown-model", None, 1, 0, None),
        ])
        self.assertEqual(self.snapshot(), before, "SQL weights are read-time values")

    def test_daily_aggregate_joins_preserve_weighted_totals_beside_many_outcomes(self) -> None:
        _ = self.commit("checkpoint(fixture-plan): phase 6 — First")
        _ = self.commit("checkpoint(fixture-plan): phase 6 — Second")
        _ = self.collect()
        rows = self.weighted_rows("""
            WITH used AS (
                SELECT substr(at, 1, 10) AS day,
                       sum(weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output)) AS weighted
                FROM turns GROUP BY 1
            ), closed AS (
                SELECT substr(state_at, 1, 10) AS day, count(*) AS findings_closed
                FROM findings WHERE state = 'closed' GROUP BY 1
            ), reviewed AS (
                SELECT substr(started_at, 1, 10) AS day, count(*) AS passes FROM passes GROUP BY 1
            ), repaired AS (
                SELECT substr(dispatched_at, 1, 10) AS day, count(*) AS rounds FROM repair_rounds GROUP BY 1
            ), landed AS (
                SELECT substr(at, 1, 10) AS day, count(*) AS checkpoints FROM checkpoints GROUP BY 1
            )
            SELECT used.day, used.weighted, coalesce(closed.findings_closed, 0), coalesce(reviewed.passes, 0),
                   coalesce(repaired.rounds, 0), coalesce(landed.checkpoints, 0)
            FROM used LEFT JOIN closed USING(day) LEFT JOIN reviewed USING(day)
                      LEFT JOIN repaired USING(day) LEFT JOIN landed USING(day) ORDER BY used.day
        """)
        self.assertEqual(self.all_values(rows), [("2026-10-10", 62.75, 2, 2, 2, 2), ("2026-10-11", None, 0, 0, 0, 0)])
        alone = self.weighted_rows("SELECT sum(weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, "
                                   + "cache_read, output)) FROM turns WHERE substr(at, 1, 10) = '2026-10-10'")
        self.assertEqual(self.all_values(alone), [(62.75,)])

    def test_openings_exist_without_tokens_and_claude_uses_first_timestamp(self) -> None:
        _ = self.collect()
        openings = self.rows("SELECT * FROM openings ORDER BY family, agent_id")
        self.assertEqual(self.values(openings, "family", "parent_id", "agent_id", "at"), [
            ("claude", "", SESSION, "2026-10-10T21:00:00+00:00"),
            ("claude", SESSION, CHILD, "2026-10-10T21:35:03+00:00"),
            ("codex", "", THREAD, "2026-10-10T21:00:00+00:00"),
            ("codex", "", "thread-without-tokens", "2026-10-10T21:10:00+00:00"),
        ])
        self.assertFalse(self.rows("SELECT * FROM turns WHERE agent_id = 'thread-without-tokens'"))
        self.assertFalse(self.rows("SELECT * FROM openings WHERE agent_id LIKE '87654321%'"))

    def test_edits_after_tokens_and_without_usage_keep_keys_and_first_presenter(self) -> None:
        _ = self.collect()
        edits = self.rows("SELECT * FROM edits ORDER BY family, at")
        claude = [row for row in edits if row["family"] == "claude"]
        self.assertEqual(self.values(claude, "parent_id", "agent_id", "at"), [
            ("", SESSION, "2026-10-10T21:35:02+00:00"), (SESSION, CHILD, "2026-10-10T21:35:04+00:00"),
        ])
        self.assertEqual([cast(object, json.loads(cast(str, row["edit_key"]))) for row in claude],
                         [["edit-message", "edit-without-usage"], ["child-edit-message", "child-edit"]])
        path = self.codex_root / "2026/10/10/rollout-used.jsonl"
        expected: list[int] = []
        offset = 0
        for line in path.read_bytes().splitlines(keepends=True):
            record = cast(dict[str, object], json.loads(line))
            payload = cast(dict[str, object], record["payload"])
            if payload.get("name") == "apply_patch":
                expected.append(offset)
            offset += len(line)
        codex = [row for row in edits if row["family"] == "codex"]
        self.assertEqual(self.values(codex, "edit_key"), [(f"{path.name}:{offset}",) for offset in expected])
        self.assertEqual(self.values(codex, "at"), [("2026-10-11T00:01:05+00:00",), ("2026-10-11T00:01:06+00:00",)])
        last_turn = self.rows("SELECT max(at) AS at FROM turns WHERE agent_id = ?", (THREAD,))
        self.assertTrue(all(cast(str, row["at"]) > cast(str, last_turn[0]["at"]) for row in codex))
        before = self.all_values(edits)
        _ = self.collect()
        self.assertEqual(self.all_values(self.rows("SELECT * FROM edits ORDER BY family, at")), before)

    def test_codex_latest_metadata_corrects_opening_in_a_single_read(self) -> None:
        path = self.codex_root / "2026/10/10/rollout-opening-correction.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/codex-opening-earlier.jsonl", path)
        _ = self.collect()
        self.assert_corrected_opening("2026-10-10T21:00:00+00:00")
        self.assertEqual(self.cursor_for(path), path.stat().st_size)
        before = self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id"))
        _ = self.collect()
        self.assertEqual(self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id")),
                         before)

    def test_codex_metadata_after_saved_cursor_moves_opening_later(self) -> None:
        path = self.codex_root / "2026/10/10/rollout-opening-correction.jsonl"
        first, correction = (FIXTURES / "updates/codex-opening-later.jsonl").read_bytes().splitlines(keepends=True)
        _ = path.write_bytes(first)
        _ = self.collect()
        self.assert_corrected_opening("2026-10-10T21:00:00+00:00")
        self.assertEqual(self.cursor_for(path), len(first))
        self.append_bytes(path, correction)
        _ = self.collect()
        self.assert_corrected_opening("2026-10-10T21:25:00+00:00")
        self.assertEqual(self.cursor_for(path), path.stat().st_size)
        before = self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id"))
        _ = self.collect()
        self.assertEqual(self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id")),
                         before)

    def test_claude_edit_without_message_id_keeps_first_timestamp_opening(self) -> None:
        path = self.claude_root / "project/edit-without-id.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/claude-edit-without-id.jsonl", path)
        _ = self.collect()
        opening = self.rows("SELECT * FROM openings WHERE machine = 'natedev' AND agent_id = 'edit-without-id'")
        self.assertEqual(self.values(opening, "family", "parent_id", "at"),
                         [("claude", "", "2026-10-10T21:15:00+00:00")])
        self.assertFalse(self.rows("SELECT * FROM edits WHERE machine = 'natedev' AND agent_id = 'edit-without-id'"))
        self.assertEqual(self.cursor_for(path), path.stat().st_size)

    def test_old_store_replays_cursors_once_without_changing_existing_turns(self) -> None:
        _ = self.collect()
        before = self.all_values(self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key"))
        version = cast(int, self.rows("PRAGMA user_version")[0][0])
        self.assertGreater(version, 0)
        with closing(sqlite3.connect(self.db_path)) as database, database:
            for table in ("openings", "edits", *OUTCOME_TABLES, "checkpoints", "account_readings", "account_role_changes"):
                _ = database.execute(f"DROP TABLE {table}")
            _ = database.execute("PRAGMA user_version = 0")
            _ = database.execute("INSERT INTO cursors VALUES ('natedev', 'old-cursor-sentinel', 123)")
        output = self.collect()
        self.assertEqual(cast(int, self.rows("PRAGMA user_version")[0][0]), version)
        self.assertFalse(self.rows("SELECT * FROM cursors WHERE path = 'old-cursor-sentinel'"))
        self.assertEqual(self.all_values(self.rows("SELECT * FROM turns ORDER BY family, agent_id, turn_key")), before)
        self.assertEqual(len(self.rows("SELECT * FROM openings")), 4)
        self.assertEqual(len(self.rows("SELECT * FROM edits")), 4)
        self.assert_source_additions(output, "claude", 0)
        self.assert_source_additions(output, "codex", 0)
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("INSERT INTO cursors VALUES ('natedev', 'current-cursor-sentinel', 456)")
        before_rows = self.snapshot()
        _ = self.collect()
        self.assertEqual(self.snapshot(), before_rows, "The new version does not clear cursors again")


@final
class MacOutcomeTests(OutcomeFixture):
    mac_home: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    execution_directory: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        super().setUp()
        self.mac_home = self.root / "fixture-mac-home"
        _ = shutil.copytree(FIXTURES / "home", self.mac_home)
        self.execution_directory = self.root / "outside-checkout"
        self.execution_directory.mkdir()
        binary_directory = self.root / "bin"
        binary_directory.mkdir()
        ssh = binary_directory / "ssh"
        _ = shutil.copyfile(COLLECT_FIXTURES / "mac/fake_ssh.py", ssh)
        ssh.chmod(0o700)

    def mac_collect(self, behavior: SshBehavior = "complete") -> MacRun:
        report_path = self.root / "ssh-report.json"
        self.assertFalse(report_path.exists())
        configuration = {
            "home": str(self.mac_home), "execution_directory": str(self.execution_directory),
            "report": str(report_path), "behavior": behavior, "database": str(self.db_path),
            "previous_role_read_at": "",
        }
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        output = self.collect(mac_pass="fixture")
        self.assertTrue(report_path.is_file())
        report = cast(MacStreamReport, json.loads(report_path.read_text(encoding="utf-8")))
        report_path.unlink()
        return MacRun(output, report)

    def remote_history(self) -> Path:
        return self.mac_home / ".local/state/plan-delegate/runs/fixture-run.jsonl"

    def add_folder_files(self, home: Path) -> tuple[Path, Path]:
        claude = home / ".claude/projects/project/folder-session.jsonl"
        codex = home / ".codex/sessions/2026/10/10/rollout-folder.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/claude-folders.jsonl", claude)
        _ = shutil.copyfile(FIXTURES / "updates/codex-folders.jsonl", codex)
        return claude, codex

    def test_mac_folders_match_local_first_claude_and_latest_codex_metadata(self) -> None:
        _ = self.add_folder_files(self.root)
        _ = self.add_folder_files(self.mac_home)
        run = self.mac_collect()
        self.assertEqual(run.stream["folder_records"], 3)
        for machine in ("natedev", "mac"):
            rows = self.rows("SELECT * FROM folders WHERE machine = ? ORDER BY family", (machine,))
            self.assertEqual(self.values(rows, "family", "parent_id", "agent_id", "folder"), [
                ("claude", "", "folder-session", "/invented/first"),
                ("codex", "", "folder-thread", "/invented/later"),
            ])
        before = self.all_values(self.rows("SELECT * FROM folders ORDER BY machine, family"))
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["folder_records"], 0)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM folders ORDER BY machine, family")), before)

    def test_mac_folder_update_after_cursor_keeps_claude_and_changes_codex(self) -> None:
        claude, codex = self.add_folder_files(self.mac_home)
        _ = self.mac_collect()
        claude_lines = (FIXTURES / "updates/claude-folders.jsonl").read_bytes().splitlines(keepends=True)
        codex_lines = (FIXTURES / "updates/codex-folders.jsonl").read_bytes().splitlines(keepends=True)
        self.append_bytes(claude, claude_lines[2])
        self.append_bytes(codex, codex_lines[0])
        _ = self.mac_collect()
        self.assertEqual(self.values(self.rows("SELECT * FROM folders WHERE machine = 'mac' ORDER BY family"), "folder"),
                         [("/invented/first",), ("/invented/first",)])

    def test_mac_folder_record_requires_a_string_folder_and_rolls_back_file(self) -> None:
        _ = self.add_folder_files(self.mac_home)
        for behavior in ("missing_folder", "invalid_folder"):
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assertEqual(run.stream["altered_lifecycle_records"], 1)
                self.assertFalse(self.rows("SELECT * FROM folders WHERE machine = 'mac'"))
                self.assertFalse(self.rows("SELECT * FROM cursors WHERE machine = 'mac' AND path = ?",
                                           (str(self.mac_home / '.claude/projects/project/folder-session.jsonl'),)))
                self.assert_family_state(run.output, "mac", "claude", "partial")

    def test_version_six_mac_store_is_refused_without_reading_remote_sources(self) -> None:
        _ = self.add_folder_files(self.mac_home)
        _ = self.mac_collect()
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("PRAGMA user_version = 6")
        before = self.snapshot()
        result = self.cli(db=self.db_path, mac_pass="fixture")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "collect: store version 6 predates file-named Codex keys; "
                         + f"delete {self.db_path} and run the collector again to rebuild it\n")
        self.assertEqual(cast(int, self.rows("PRAGMA user_version")[0][0]), 6)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "ssh-report.json").exists())

    def test_mac_outcomes_and_lifecycle_equal_local_rows_without_remote_checkout(self) -> None:
        before = {str(path.relative_to(self.mac_home)): path.read_bytes()
                  for path in self.mac_home.rglob("*") if path.is_file()}
        run = self.mac_collect()
        for table in (*OUTCOME_TABLES, "openings", "edits"):
            with self.subTest(table=table):
                remote = self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'")
                local = self.rows(f"SELECT * FROM {table} WHERE machine = 'natedev'")
                remote_values = [tuple(cast(object, row[index]) for index in range(1, len(row))) for row in remote]
                local_values = [tuple(cast(object, row[index]) for index in range(1, len(row))) for row in local]
                self.assertEqual(sorted(remote_values, key=repr), sorted(local_values, key=repr))
                self.assertTrue(remote)
        self.assertEqual(run.stream["opening_records"], 4)
        self.assertGreaterEqual(run.stream["edit_records"], 4)
        self.assertEqual(run.stream["outcome_records"], 14)
        self.assertEqual(run.stream["history_chunks"], 0)
        self.assertEqual(run.stream["history_cursor_records"], 1)
        self.assertEqual(self.cursor_for(self.remote_history(), "mac"), self.remote_history().stat().st_size)
        self.assertEqual(list(self.execution_directory.iterdir()), [])
        after = {str(path.relative_to(self.mac_home)): path.read_bytes()
                 for path in self.mac_home.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assert_outcomes_added(run.output, "history", 9, "mac")
        self.assert_family_state(run.output, "mac", "history", "ready")
        for family in ("checkpoints", "readings", "account_roles"):
            self.assertFalse(self.rows("SELECT * FROM turn_sources WHERE machine = 'mac' AND family = ?", (family,)))
        for table in ("account_readings", "account_role_changes"):
            self.assertFalse(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'"))

    def test_mac_history_updates_incrementally_and_unchanged_pass_sends_no_outcomes(self) -> None:
        _ = self.mac_collect()
        self.append_bytes(self.remote_history(), (FIXTURES / "updates/finish.jsonl").read_bytes())
        run = self.mac_collect()
        self.assertEqual(run.stream["outcome_records"], 4)
        self.assertEqual(run.stream["measurement_records"], 0)
        self.assertEqual(run.stream["history_cursor_records"], 1)
        self.assert_outcomes_added(run.output, "history", 0, "mac")
        self.assertEqual(self.values(self.rows("SELECT * FROM runs WHERE machine = 'mac'"), "state", "finished_at", "source_event"),
                         [("complete", "2026-10-10T22:03:00+00:00", "run-create")])
        before = self.snapshot()
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["outcome_records"], 0)
        self.assertEqual(repeated.stream["measurement_records"], 0)
        self.assertEqual(repeated.stream["history_cursor_records"], 0)
        self.assertEqual(repeated.stream["cursor_records"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_mac_codex_latest_metadata_corrects_opening_in_a_single_read(self) -> None:
        path = self.mac_home / ".codex/sessions/2026/10/10/rollout-opening-correction.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/codex-opening-earlier.jsonl", path)
        run = self.mac_collect()
        self.assertEqual(run.stream["opening_records"], 6)
        self.assert_corrected_opening("2026-10-10T21:00:00+00:00", "mac")
        self.assertEqual(self.cursor_for(path, "mac"), path.stat().st_size)
        before = self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id"))
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["opening_records"], 0)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id")),
                         before)

    def test_mac_codex_metadata_after_saved_cursor_moves_opening_later(self) -> None:
        path = self.mac_home / ".codex/sessions/2026/10/10/rollout-opening-correction.jsonl"
        first, correction = (FIXTURES / "updates/codex-opening-later.jsonl").read_bytes().splitlines(keepends=True)
        _ = path.write_bytes(first)
        _ = self.mac_collect()
        self.assert_corrected_opening("2026-10-10T21:00:00+00:00", "mac")
        self.assertEqual(self.cursor_for(path, "mac"), len(first))
        self.append_bytes(path, correction)
        corrected = self.mac_collect()
        self.assertEqual(corrected.stream["opening_records"], 1)
        self.assert_corrected_opening("2026-10-10T21:25:00+00:00", "mac")
        self.assertEqual(self.cursor_for(path, "mac"), path.stat().st_size)
        before = self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id"))
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["opening_records"], 0)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM openings ORDER BY machine, family, parent_id, agent_id")),
                         before)

    def test_mac_claude_edit_without_message_id_keeps_first_timestamp_opening(self) -> None:
        path = self.mac_home / ".claude/projects/project/edit-without-id.jsonl"
        _ = shutil.copyfile(FIXTURES / "updates/claude-edit-without-id.jsonl", path)
        _ = self.mac_collect()
        opening = self.rows("SELECT * FROM openings WHERE machine = 'mac' AND agent_id = 'edit-without-id'")
        self.assertEqual(self.values(opening, "family", "parent_id", "at"),
                         [("claude", "", "2026-10-10T21:15:00+00:00")])
        self.assertFalse(self.rows("SELECT * FROM edits WHERE machine = 'mac' AND agent_id = 'edit-without-id'"))
        self.assertEqual(self.cursor_for(path, "mac"), path.stat().st_size)

    def test_mac_history_without_newline_waits_then_finishes_run(self) -> None:
        _ = self.mac_collect()
        path = self.remote_history()
        previous = path.stat().st_size
        tail = b'{"event_id":"mac-finish","event_type":"run_finished","timestamp":"2026-10-10T22:00:00Z","status":"complete"}'
        self.append_bytes(path, tail)
        incomplete = self.mac_collect()
        self.assertEqual(incomplete.stream["outcome_records"], 0)
        self.assertEqual(self.cursor_for(path, "mac"), previous)
        self.assertEqual(self.values(self.rows("SELECT state FROM runs WHERE machine = 'mac'"), "state"), [("open",)])
        self.append_bytes(path, b"\n")
        complete = self.mac_collect()
        self.assertEqual(complete.stream["outcome_records"], 1)
        self.assertEqual(self.cursor_for(path, "mac"), path.stat().st_size)
        self.assertEqual(self.values(self.rows("SELECT state FROM runs WHERE machine = 'mac'"), "state"), [("complete",)])

    def test_cut_inside_history_rolls_back_its_rows_and_cursor_then_resume_adds_once(self) -> None:
        cut = self.mac_collect("cut_inside_history")
        self.assertEqual(cut.stream["outcome_records"], 1)
        self.assertEqual(cut.stream["history_cursor_records"], 0)
        for table in OUTCOME_TABLES:
            self.assertFalse(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'"))
        self.assertFalse(self.rows("SELECT * FROM cursors WHERE machine = 'mac' AND path = ?", (str(self.remote_history()),)))
        resumed = self.mac_collect()
        self.assert_outcomes_added(resumed.output, "history", 9, "mac")
        self.assertEqual([len(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'")) for table in OUTCOME_TABLES],
                         [1, 1, 2, 3, 2])
        self.assertEqual(self.cursor_for(self.remote_history(), "mac"), self.remote_history().stat().st_size)
        before = self.snapshot()
        _ = self.mac_collect()
        self.assertEqual(self.snapshot(), before)

    def test_history_file_commit_survives_cut_after_its_cursor(self) -> None:
        cut = self.mac_collect("cut_after_history_cursor")
        self.assertEqual(cut.stream["history_cursor_records"], 1)
        self.assertEqual([len(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'")) for table in OUTCOME_TABLES],
                         [1, 1, 2, 3, 2])
        self.assertEqual(self.cursor_for(self.remote_history(), "mac"), self.remote_history().stat().st_size)
        self.assert_outcomes_added(cut.output, "history", 9, "mac")
        for family in ("history", "claude", "codex"):
            self.assert_family_state(cut.output, "mac", family, "partial")
        run = self.mac_collect()
        self.assertEqual(run.stream["outcome_records"], 0)
        self.assert_outcomes_added(run.output, "history", 0, "mac")

    def test_malformed_mac_outcome_ends_read_without_partial_file_rows(self) -> None:
        run = self.mac_collect("missing_outcome_field")
        self.assertEqual(run.stream["altered_outcome_records"], 1)
        for table in OUTCOME_TABLES:
            self.assertFalse(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac'"))
        self.assertFalse(self.rows("SELECT * FROM cursors WHERE machine = 'mac' AND path = ?", (str(self.remote_history()),)))
        for family in ("history", "claude", "codex"):
            rows = self.rows("SELECT state FROM turn_sources WHERE machine = 'mac' AND family = ?", (family,))
            self.assertEqual(len(rows), 1)
            self.assertIn(cast(str, rows[0]["state"]), ("partial", "unreachable"))

    def test_malformed_mac_lifecycle_measurements_fail_read_and_leave_no_defaults(self) -> None:
        behaviors: tuple[SshBehavior, ...] = ("missing_opening_time", "invalid_edit_time")
        for behavior in behaviors:
            with self.subTest(behavior=behavior):
                self.db_path = self.state / f"{behavior}.db"
                run = self.mac_collect(behavior)
                self.assertEqual(run.stream["altered_lifecycle_records"], 1)
                table = "openings" if behavior == "missing_opening_time" else "edits"
                self.assertFalse(self.rows(f"SELECT * FROM {table} WHERE machine = 'mac' AND agent_id = ?", (SESSION,)))
                self.assertFalse(self.rows("SELECT * FROM cursors WHERE machine = 'mac' AND path = ?",
                                           (str(self.mac_home / '.claude/projects/project' / f'{SESSION}.jsonl'),)))
                self.assertTrue(any("unreachable" in line or "partial" in line
                                    for line in run.output.splitlines() if line.startswith("mac claude:")))

    def test_checkpoint_mapping_selects_the_latest_phase_on_either_machine(self) -> None:
        record = cast(dict[str, object], json.loads(self.remote_history().read_bytes().splitlines()[1]))
        record.update(timestamp="2026-10-10T21:30:00Z", phase_instance_id="remote-latest", event_id="remote-restart")
        self.append_bytes(self.remote_history(), json.dumps(record).encode("utf-8") + b"\n")
        commit_id = self.commit("checkpoint(fixture-plan): phase 6 — Mac phase")
        _ = self.mac_collect()
        mapped = self.rows("SELECT * FROM checkpoints WHERE commit_id = ?", (commit_id,))
        self.assertEqual(self.values(mapped, "mapping", "machine", "run_id", "phase_instance_id"),
                         [("mapped", "mac", "fixture-run", "remote-latest")])

    def test_old_mac_cursors_replay_lifecycle_once_and_keep_turn_counts(self) -> None:
        _ = self.mac_collect()
        before = self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY family, agent_id, turn_key"))
        with closing(sqlite3.connect(self.db_path)) as database, database:
            _ = database.execute("DELETE FROM openings WHERE machine = 'mac'")
            _ = database.execute("DELETE FROM edits WHERE machine = 'mac'")
            _ = database.execute("PRAGMA user_version = 0")
        replayed = self.mac_collect()
        self.assertGreater(replayed.stream["measurement_records"], 0)
        self.assertEqual(len(self.rows("SELECT * FROM openings WHERE machine = 'mac'")), 4)
        self.assertEqual(len(self.rows("SELECT * FROM edits WHERE machine = 'mac'")), 4)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM turns WHERE machine = 'mac' ORDER BY family, agent_id, turn_key")), before)
        self.assert_source_additions(replayed.output, "claude", 0, "mac")
        self.assert_source_additions(replayed.output, "codex", 0, "mac")
        repeated = self.mac_collect()
        self.assertEqual(repeated.stream["measurement_records"], 0)
        self.assertEqual(repeated.stream["outcome_records"], 0)

    def test_history_source_keeps_last_complete_time_when_unreachable_skipped_or_missing(self) -> None:
        _ = self.mac_collect()
        query = "SELECT last_complete_at FROM turn_sources WHERE machine = 'mac' AND family = 'history'"
        complete = self.all_values(self.rows(query))
        self.assertIsInstance(complete[0][0], str)
        failed = self.mac_collect("unreachable")
        self.assert_family_state(failed.output, "mac", "history", "unreachable")
        self.assertEqual(self.all_values(self.rows(query)), complete)
        skipped = self.collect()
        self.assert_family_state(skipped, "mac", "history", "skipped")
        self.assertEqual(self.all_values(self.rows(query)), complete)
        shutil.rmtree(self.remote_history().parent)
        missing = self.mac_collect()
        self.assert_family_state(missing.output, "mac", "history", "missing")
        self.assertEqual(self.all_values(self.rows(query)), complete)
        self.assertEqual(len(self.rows("SELECT * FROM runs WHERE machine = 'mac'")), 1)


if __name__ == "__main__":
    _ = unittest.main()
