"""Ledger records behavior, using independent copied repositories."""

from __future__ import annotations

import unittest
import fcntl
import io
import json
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from typing import IO, cast, final
from unittest import mock
import rules
import style_ledger
from approved_renames import AppliedInCode


from ledger_test_support import (LedgerTestCase, LedgerStatus, PARENT_FILE)


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300


@final
class StyleLedgerRecordsTests(LedgerTestCase):
    def test_metadata_only_rule_edit_accepts_pin_and_keeps_cell_current_but_body_edit_does_not(self) -> None:
        rule_id = self.rule("metadata")
        path = self.rule_dir / "metadata.md"
        _ = subprocess.run(["git", "init", "-q", str(self.rule_dir.parent)], check=True)
        _ = subprocess.run(["git", "-C", str(self.rule_dir.parent), "hash-object", "-w", str(path)], check=True,
                           capture_output=True)
        with self.ledger() as ledger:
            ledger.set_tip(self.initial_tip)
            old = self.blob(rule_id)
            ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", [self.sources[0]], [rule_id])
            ledger.record(rule_id, self.sources[0], self.initial_tip, old, "clean", ticket="read-1", worker="old")
            _ = path.write_text(path.read_text().replace("input_scope: file", "input_scope: file\napproval: always"))
            current = self.blob(rule_id)
            with self.ledger() as refreshed:
                self.assertTrue(any(cell.path == self.sources[0] and cell.state == "current"
                                    for cell in refreshed.cells(rule_id)))
            try:
                ledger.record(rule_id, self.sources[0], self.initial_tip, current, "clean", ticket="read-1", worker="new")
            except style_ledger.LedgerError as error:
                self.fail(f"metadata-only pin must remain valid: {error}")
            self.assertEqual(ledger._judgments(rule_id, self.sources[0])[0].rule_blob, current)  # pyright: ignore[reportPrivateUsage]
            with self.ledger() as refreshed:
                state = tuple(refreshed.cells())
                self.assertTrue(any(cell.rule == rule_id and cell.path == self.sources[0] and cell.state == "current"
                                    for cell in state))
            _ = path.write_text(path.read_text() + "Changed body\n")
            with self.assertRaisesRegex(style_ledger.LedgerError, "differs from ticket pin"):
                ledger.record(rule_id, self.sources[0], self.initial_tip, self.blob(rule_id), "clean", ticket="read-1", worker="body")

    def test_refill_lock_blocks_another_worktree_until_the_body_exits(self) -> None:
        worktree = self.root / "other-checkout"
        _ = self.git("worktree", "add", "-q", "-b", "other", str(worktree))
        lock_path = self.root / "state" / self.repo.name / "refill.lock"
        self.assertEqual(lock_path.parent, style_ledger.ledger_location(self.repo).database.parent)
        self.assertFalse(lock_path.parent.exists())
        blocked, acquired = threading.Event(), threading.Event()
        flock = fcntl.flock

        def observe_lock(file: IO[str], operation: int) -> None:
            self.assertEqual(operation, fcntl.LOCK_EX)
            try:
                flock(file, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                blocked.set()
                flock(file, operation)

        def take_lock() -> None:
            with style_ledger.refill_lock(worktree):
                acquired.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with style_ledger.refill_lock(self.repo):
                self.assertTrue(lock_path.is_file())
                with mock.patch.object(fcntl, "flock", side_effect=observe_lock):
                    contender = pool.submit(take_lock)
                    self.assertTrue(blocked.wait(TEST_HANG_GUARD_SECS), "second worktree did not contend for refill")
                    self.assertFalse(acquired.is_set(), "second worktree entered a held refill lock")
            contender.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue(acquired.is_set(), "refill lock was not released on context exit")

    def test_migration_27_upgrades_copies_at_25_and_26_and_is_repeatable(self) -> None:
        rule = self.rule("pipeline")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-9", "names", "read", "names", "crates/alpha", [self.sources[0]], [rule])
        ledger.ticket_assign("names-9", "main")
        _ = ledger.ticket_base("names-9")
        self.record(rule, self.sources[0], self.initial_tip, ticket="names-9")
        ledger.ticket_checkpoint("names-9", self.initial_tip)
        schema = {"bulk_workspace_tickets", "bulk_workspace_batch", "ticket_predecessors", "tickets_one_assigned"}
        with closing(sqlite3.connect(ledger.database)) as fresh:
            self.assertEqual(fresh.execute("SELECT version FROM schema_version").fetchone(), (27,))
            names = cast(list[tuple[str]], fresh.execute("SELECT name FROM sqlite_master").fetchall())
            self.assertTrue(schema.issubset({row[0] for row in names}))
        for version in (25, 26):
            with self.subTest(version=version):
                database = self.root / f"existing-copy-{version}.sqlite"
                with closing(sqlite3.connect(ledger.database)) as source, closing(sqlite3.connect(database)) as copy:
                    source.backup(copy)
                    _ = copy.executescript("""
                        DROP INDEX tickets_one_assigned;
                        DROP TABLE ticket_predecessors;
                        CREATE UNIQUE INDEX tickets_one_open ON tickets(unit) WHERE state IN ('assigned','checkpointed');
                    """)
                    if version == 25:
                        _ = copy.execute("DROP TABLE bulk_workspace_tickets")
                    else:
                        _ = copy.execute("INSERT INTO bulk_workspace_tickets VALUES ('names-9','existing-batch')")
                    _ = copy.execute("UPDATE schema_version SET version=?", (version,))
                    copy.commit()
                    rows = copy.execute("SELECT * FROM tickets").fetchall()
                    judgments = copy.execute("SELECT * FROM judgments").fetchall()
                for _ in range(2):
                    with style_ledger.Ledger(self.repo, database):
                        pass
                with closing(sqlite3.connect(database)) as copy:
                    self.assertEqual(copy.execute("SELECT version FROM schema_version").fetchone(), (27,))
                    names = cast(list[tuple[str]], copy.execute("SELECT name FROM sqlite_master").fetchall())
                    self.assertTrue(schema.issubset({row[0] for row in names}))
                    self.assertEqual(copy.execute("SELECT * FROM tickets").fetchall(), rows)
                    self.assertEqual(copy.execute("SELECT * FROM judgments").fetchall(), judgments)
                    self.assertEqual(copy.execute("SELECT * FROM bulk_workspace_tickets").fetchall(),
                                     [("names-9", "existing-batch")] if version == 26 else [])
                    for _ in range(2):
                        _ = copy.executescript(style_ledger.MIGRATIONS[27])
                    self.assertEqual(copy.execute("SELECT name FROM sqlite_master WHERE name='tickets_one_open'").fetchall(), [])
                with style_ledger.Ledger(self.repo, database) as upgraded:
                    upgraded.ticket_add("names-10", "names", "read", "names", "crates/beta", [self.sources[2]], [rule])
                    upgraded.ticket_assign("names-10", "main")
                    self.assertEqual(upgraded.ticket_base("names-10"), self.initial_tip)
                    self.assertEqual(len(upgraded.ticket_open("names")), 2)
        self.assertEqual(len(ledger.ticket_open("names")), 1)

    def pipeline_tickets(self) -> tuple[style_ledger.Ledger, str, str]:
        rule = self.rule("pipeline")
        ledger = self.ledger()
        _ = self.git("branch", "style/main")
        ledger.set_tip(self.initial_tip, "style/main")
        # A queued lower-number id proves reconcile follows dependencies rather than textual/numeric ids.
        for ticket, path in (("names-10", self.sources[0]), ("names-2", self.sources[2]), ("names-11", self.sources[3])):
            ledger.ticket_add(ticket, "names", "read", "names", path.split("/src/")[0], [path], [rule])
        ledger.ticket_assign("names-10", "main")
        _ = ledger.ticket_base("names-10")
        self.write(self.sources[0], "pub fn first() {}\n")
        first = self.commit("First ticket")
        self.record(rule, self.sources[0], first, ticket="names-10")
        ledger.ticket_checkpoint("names-10", first)
        ledger.ticket_assign("names-2", "main")
        self.assertEqual(ledger.ticket_base("names-2"), first)
        self.write(self.sources[2], "pub fn second() {}\n")
        second = self.commit("Second ticket")
        self.record(rule, self.sources[2], second, ticket="names-2")
        ledger.ticket_checkpoint("names-2", second)
        return ledger, first, second

    def test_pipeline_bounds_assignment_and_merge_waits_for_the_predecessor(self) -> None:
        ledger, _, second = self.pipeline_tickets()
        with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
            ledger.ticket_assign("names-11", "main")
        _ = self.git("update-ref", "refs/heads/style/main", second)
        with self.assertRaisesRegex(style_ledger.LedgerError, "waiting for predecessor names-10"):
            _ = ledger.ticket_merge("names-2")
        self.assertEqual(len(ledger.ticket_open("names")), 2)
        _ = ledger.ticket_merge("names-10")
        _ = ledger.ticket_merge("names-2")
        self.assertEqual(ledger.ticket_open("names"), ())

    def test_pipeline_reconcile_merges_predecessors_before_successors(self) -> None:
        ledger, _, second = self.pipeline_tickets()
        _ = self.git("update-ref", "refs/heads/style/main", second)
        output = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertLess(output.stdout.index("ticket names-10:"), output.stdout.index("ticket names-2:"))
        self.assertEqual(ledger.ticket_open("names"), ())

    def test_pipeline_growth_merge_refuses_legacy_overlap_even_after_predecessor_merged(self) -> None:
        ledger, _, second = self.pipeline_tickets()
        _ = self.git("update-ref", "refs/heads/style/main", second)
        # A ledger written by the earlier implementation can already contain overlapping footprints.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET files=? WHERE id='names-2'",
                                   (json.dumps([self.sources[0], self.sources[2]]),))
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            _ = ledger.ticket_merge("names-10")
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET state='merged' WHERE id='names-10'")
        with self.assertRaisesRegex(style_ledger.LedgerError, "overlap: " + self.sources[0]):
            _ = ledger.ticket_merge("names-2")
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "checkpointed")

    def test_pipeline_assignment_refuses_an_assigned_predecessor_and_wrong_branch_tip(self) -> None:
        rule = self.rule("pipeline")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        for number in (1, 2):
            ledger.ticket_add(f"names-{number}", "names", "read", "names", "crates/alpha", [self.sources[number - 1]], [rule])
        ledger.ticket_assign("names-1", "main")
        with self.assertRaisesRegex(style_ledger.LedgerError, "already has"):
            ledger.ticket_assign("names-2", "main")
        self.record(rule, self.sources[0], self.initial_tip, ticket="names-1")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        self.write(self.sources[0], "pub fn unrelated() {}\n")
        _ = self.commit("Wrong branch tip")
        with self.assertRaisesRegex(style_ledger.LedgerError, "branch must start at waiting checkpoint"):
            ledger.ticket_assign("names-2", "main")
        self.assertEqual(len(ledger.ticket_open("names")), 1)

    def test_migration_25_backfills_from_merged_build_ticket_else_the_finding(self) -> None:
        database = self.root / "version-24.sqlite"
        rule = self.rule("backfill")
        with closing(sqlite3.connect(database)) as connection, connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 24:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (24)")
            for ticket, state in (("merged", "merged"), ("pending", "checkpointed")):
                _ = connection.execute(
                    "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,branch,"
                    + "checkpoint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticket, ticket, "proposal", "names", rule, "crates/alpha", "[]", "[]", "[]", "{}", state,
                     "main", "", "created", "2026-10-20T08:00:00+00:00"),
                )
            for finding_id, state, ticket in (("merged-build", "built", "merged"), ("pending-build", "built", "pending"),
                                               ("no-ticket", "built", ""), ("missing-ticket", "built", "missing"),
                                               ("not-built", "filed", "merged")):
                _ = connection.execute(
                    "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,"
                    + "proposal,created_at,updated_at,build_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (finding_id, rule, self.sources[0], 1, "change", "type", "u32", "u64", self.blob(rule), "judge",
                     state, "", "created", "2026-10-21T09:00:00+00:00", ticket),
                )
            legacy_times = dict(cast(list[tuple[str, str]], connection.execute(
                f"SELECT h.finding_id,{style_ledger.legacy_built_at_sql('h')} FROM held_findings h WHERE h.state='built'",
            ).fetchall()))
        with style_ledger.Ledger(self.repo, database):
            pass
        with closing(sqlite3.connect(database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT finding_id,built_at FROM held_findings ORDER BY finding_id",
            ).fetchall())
            self.assertEqual(dict(rows), {
                "merged-build": "2026-10-20T08:00:00+00:00",
                "pending-build": "2026-10-21T09:00:00+00:00",
                "no-ticket": "2026-10-21T09:00:00+00:00",
                "missing-ticket": "2026-10-21T09:00:00+00:00", "not-built": "",
            })
            self.assertEqual({finding: at for finding, at in rows if finding != "not-built"}, legacy_times)
            self.assertEqual(connection.execute("SELECT * FROM refill_results").fetchall(), [])

    def test_record_refill_upserts_only_the_outcome_and_leaves_reserved_columns_alone(self) -> None:
        ledger = self.ledger()
        first = "2026-10-27T08:15:00+00:00"
        later = "2026-10-28T09:30:00+00:00"
        with (mock.patch.object(rules, "load_rules", side_effect=AssertionError("record_refill loads no catalog")),
              mock.patch.object(style_ledger, "open_ledger", side_effect=AssertionError("record_refill runs no migration"))):
            with mock.patch.object(style_ledger, "_now", return_value=first):
                style_ledger.record_refill(self.repo, "names-unit", "phase written")
            self.assertEqual(self.refill_result(ledger.database), (first, "phase written", "", 0, 0, 0, 0))
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE refill_results SET split_at=?,ready=1,changed=2,structure=3,other=4", (first,))
            with mock.patch.object(style_ledger, "_now", return_value=later):
                style_ledger.record_refill(self.repo, "names-unit", "repair first")
                style_ledger.record_refill(self.repo, "types-unit", "board unavailable")
            self.assertEqual(self.refill_result(ledger.database), (later, "repair first", first, 1, 2, 3, 4))
            self.assertEqual(self.refill_result(ledger.database, "types-unit"), (later, "board unavailable", "", 0, 0, 0, 0))
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM refill_results").fetchone(), (2,))

    def test_record_refill_does_nothing_without_a_database_or_table(self) -> None:
        database = style_ledger.ledger_location(self.repo).database
        style_ledger.record_refill(self.repo, "names-unit", "repair first")
        self.assertFalse(database.exists())
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.execute("CREATE TABLE existing(value TEXT)")
            _ = connection.execute("INSERT INTO existing VALUES ('kept')")
        before = database.read_bytes()
        style_ledger.record_refill(self.repo, "names-unit", "no work")
        self.assertEqual(database.read_bytes(), before)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("existing",)])

    def test_a_first_split_inserts_counts_without_inventing_a_refill(self) -> None:
        ledger = self.ledger()
        measured_at = "2026-10-28T10:01:02+00:00"
        split = style_ledger.RecordedSplit(7, 6, 5, 4, measured_at)
        with (mock.patch.object(rules, "load_rules", side_effect=AssertionError("no catalog write")),
              mock.patch.object(style_ledger, "open_ledger", side_effect=AssertionError("no migration"))):
            style_ledger.record_split(self.repo, "names-unit", split)
        self.assertEqual(self.refill_result(ledger.database), ("", "", measured_at, 7, 6, 5, 4))

    def test_the_split_blob_cache_reads_each_tree_once_and_keeps_versions_distinct(self) -> None:
        path = self.sources[0]
        old = (self.repo / path).read_text()
        self.write(path, "pub fn changed() -> u64 { 2 }\n")
        current = self.commit("Change one source")
        read = style_ledger._git  # pyright: ignore[reportPrivateUsage]
        trees: list[tuple[str, ...]] = []

        def git(repo: Path, *arguments: str) -> str:
            if arguments[0] == "ls-tree":
                trees.append(arguments)
            return read(repo, *arguments)

        with mock.patch.object(style_ledger, "_git", side_effect=git):
            cache = style_ledger.GitReadCache(self.repo)
            for _ in range(2):
                self.assertEqual(cache.text(self.initial_tip, path), old)
                self.assertEqual(cache.text(current, path), "pub fn changed() -> u64 { 2 }\n")
                self.assertEqual(cache.blob(current, self.sources[1]), cache.blob(self.initial_tip, self.sources[1]))
        self.assertEqual(len(trees), 2)
        self.assertEqual({arguments[-1] for arguments in trees}, {self.initial_tip, current})
        self.assertTrue(all(any(option.startswith("-") and "r" in option for option in arguments[1:-1])
                            for arguments in trees))

    def test_git_read_cache_and_ledgers_resolve_a_branch_again_after_it_moves(self) -> None:
        cache = style_ledger.GitReadCache(self.repo)
        ledger = self.ledger()
        with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
            self.assertEqual(cache.tip("main").commit, self.initial_tip)
            self.assertEqual(cached_ledger._tip("main").commit, self.initial_tip)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(ledger._tip("main").commit, self.initial_tip)  # pyright: ignore[reportPrivateUsage]
            self.write(self.sources[0], "pub fn advanced() {}\n")
            current = self.commit("Advance main after caching its tip")
            with self.subTest(reader="cache"):
                self.assertEqual(cache.tip("main").commit, current)
            with self.subTest(reader="cached ledger"):
                self.assertEqual(cached_ledger._tip("main").commit, current)  # pyright: ignore[reportPrivateUsage]
            with self.subTest(reader="ledger"):
                self.assertEqual(ledger._tip("main").commit, current)  # pyright: ignore[reportPrivateUsage]
            self.assertNotIn("main", cache.tips)
            self.assertEqual(cache.tip(self.initial_tip).commit, self.initial_tip)

    def test_site_proof_shares_blob_reads_with_the_split_cache(self) -> None:
        source = "fn work() {\n    let old = 791;\n}\n"
        ledger, _, finding = self.site_fixture(source=source)
        base = ledger.synced_commit()
        tip = self.commit_tip(ledger, {PARENT_FILE: "// header\n" + source})
        cache = style_ledger.GitReadCache(self.repo)
        with mock.patch.object(style_ledger, "blob_contents", wraps=style_ledger.blob_contents) as reads:
            self.assertEqual(cache.file(base, PARENT_FILE), source.encode())
            self.assertEqual(cache.file(tip, PARENT_FILE), ("// header\n" + source).encode())
            # The direct site read and the declaration step's batched preparation share those bytes.
            with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
                self.assertIsInstance(self.strict_site(cached_ledger, finding, tip), style_ledger.UnchangedSite)
            with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
                self.assertIsInstance(cached_ledger.finding_site(finding, tip), style_ledger.UnchangedSite)
        requested = [blob for call in reads.call_args_list for blob in cast(dict[str, str], call.args[1]).values()]
        for commit in (base, tip):
            with self.subTest(commit=commit):
                self.assertEqual(requested.count(cache.blob(commit, PARENT_FILE)), 1)

    def test_split_write_uses_a_small_busy_timeout_on_a_locked_ledger(self) -> None:
        ledger = self.ledger()
        previous = style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00")
        style_ledger.record_split(self.repo, "names-unit", previous)
        before = self.refill_result(ledger.database)
        with closing(sqlite3.connect(ledger.database)) as writer:
            _ = writer.execute("BEGIN IMMEDIATE")
            connect = sqlite3.connect
            busy_budgets: list[tuple[int]] = []

            def observe_connect(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
                connection = connect(database, uri=uri, timeout=timeout)
                busy_budgets.append(cast(tuple[int], connection.execute("PRAGMA busy_timeout").fetchone()))
                return connection

            with (mock.patch.object(sqlite3, "connect", side_effect=observe_connect),
                  self.assertRaisesRegex(sqlite3.OperationalError, "database is locked")):
                style_ledger.record_split(self.repo, "names-unit",
                                          style_ledger.RecordedSplit(1, 2, 3, 4, "2026-10-28T12:00:00+00:00"),
                                          busy_timeout_seconds=0.05)
            writer.rollback()
        self.assertEqual(busy_budgets, [(50,)])
        self.assertEqual(self.refill_result(ledger.database), before)

    def test_a_later_split_preserves_the_refill_and_a_refill_preserves_the_split(self) -> None:
        ledger = self.ledger()
        first_at = "2026-10-27T12:00:00+00:00"
        later_at = "2026-10-28T10:00:00+00:00"
        with mock.patch.object(style_ledger, "_now", return_value=first_at):
            style_ledger.record_refill(self.repo, "names-unit", "phase written")
        style_ledger.record_split(self.repo, "names-unit", style_ledger.RecordedSplit(1, 2, 3, 4, first_at))
        style_ledger.record_split(self.repo, "names-unit", style_ledger.RecordedSplit(0, 0, 0, 0, later_at))
        self.assertEqual(self.refill_result(ledger.database), (first_at, "phase written", later_at, 0, 0, 0, 0))
        with mock.patch.object(style_ledger, "_now", return_value=later_at):
            style_ledger.record_refill(self.repo, "names-unit", "waiting for sync")
        self.assertEqual(self.refill_result(ledger.database), (later_at, "waiting for sync", later_at, 0, 0, 0, 0))

    def test_withdraw_duplicates_is_idempotent_refuses_an_open_ticket_and_the_held_list_names_the_duplicate(self) -> None:
        rule, issue, ids = self.decision_findings()
        ledger = self.ledger()
        bare = [ledger.hold(rule, path, 5, "required change", "rename", "Old", "New", self.blob(rule), "outside").finding_id
                for path in self.sources[:2]]
        for finding_id in bare:
            ledger.file_held(finding_id, issue)
        duplicates = [style_ledger.BareDuplicate(finding_id, qualified, issue) for finding_id, qualified in zip(bare, ids)]
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[1]], [rule],
                          proposal=str(issue), finding_ids=(bare[1],))
        before = self.held_rows(ledger.database)
        with self.assertRaisesRegex(style_ledger.LedgerError, re.escape(f"{bare[1]}: build ticket names-1 still holds the finding")):
            ledger.withdraw_duplicates(duplicates)
        self.assertEqual(self.held_rows(ledger.database), before)
        ledger.withdraw_duplicates(duplicates[:1])
        withdrawn = self.held_rows(ledger.database)
        ledger.withdraw_duplicates(duplicates[:1])
        self.assertEqual(self.held_rows(ledger.database), withdrawn)
        held = {finding.finding_id: finding.state for finding in ledger.proposal_findings(issue)}
        self.assertEqual((held[bare[0]], held[bare[1]]),
                         (style_ledger.Withdrawn(issue, style_ledger.DuplicateOf(ids[0])), style_ledger.Filed(issue)))
        listed = {cast(str, finding["finding_id"]): finding
                  for finding in cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))}
        self.assertEqual((listed[bare[0]]["withdrawn_by"], listed[bare[0]]["duplicate_of"]), (0, ids[0]))
        self.assertNotIn("duplicate_of", listed[bare[1]])
        lines = {line.split("\t")[0]: line.split("\t")[3:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual((lines[bare[0]], lines[bare[1]]), (["withdrawn", f"duplicate of {ids[0]}"], ["filed"]))

    def test_a_person_reading_stays_on_the_row_and_the_held_list_shows_it_by_state(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        not_applied, checked, applied = (self.hold_finding(rule_id, line) for line in (1, 2, 3))
        proposal = self.root / "issues/rename.md"
        ledger = self.ledger()
        for finding in (not_applied, checked, applied):
            ledger.file_held(finding, proposal)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE held_findings SET state='built'")
        ledger.return_to_filed({not_applied: style_ledger.ReadByPerson("nate"), checked: style_ledger.NotReadByPerson()})
        ledger.record_person_applied({applied: style_ledger.ReadByPerson("ana")})
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{checked}: finding is not built"):
            ledger.record_person_applied({checked: style_ledger.ReadByPerson("ana")})
        held = {finding.finding_id: finding for finding in ledger.proposal_findings(proposal)}
        self.assertEqual([(held[finding].state, held[finding].reading) for finding in (not_applied, checked, applied)],
                         [(style_ledger.Filed(proposal), style_ledger.ReadByPerson("nate")),
                          (style_ledger.Filed(proposal), style_ledger.NotReadByPerson()),
                          (style_ledger.Built(proposal), style_ledger.ReadByPerson("ana"))])
        shown = {not_applied: "filed again by nate: a person read the code", checked: "",
                 applied: "applied, read by ana: a person read the code"}
        listed = cast(list[dict[str, object]], json.loads(self.cli("held", "list", "--json").stdout))
        self.assertEqual({finding["finding_id"]: finding["reading"] for finding in listed}, shown)
        lines = {line.split("\t")[0]: line.split("\t")[4:] for line in self.cli("held", "list").stdout.splitlines()}
        self.assertEqual(lines, {finding: [text] if text else [] for finding, text in shown.items()})
        # A later state change keeps the reading.
        ledger.waive_held(not_applied, style_ledger.WaiveByPerson("nate"))
        self.assertEqual(ledger.proposal_findings(proposal)[0].reading, style_ledger.ReadByPerson("nate"))

    def test_decision_migration_eleven_preserves_version_ten_findings_and_tickets(self) -> None:
        rule = self.rule("read")
        database = self.root / "version-ten.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 10:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (10)")
            _ = connection.execute("INSERT INTO held_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("legacy", rule, self.sources[0], 2, "change", "rename", "old", "new", self.blob(rule),
                                    "read-1", "filed", str(self.root / "issue.md"), "created", "updated"))
            _ = connection.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("read-1", "read", "read", "names", "", "crates/alpha", json.dumps(list(self.sources[:2])), "[]",
                                    json.dumps([rule]), json.dumps({rule: self.blob(rule)}), "assigned", "refs/heads/main", "", "created", "updated", "", "", "[]"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            finding, = ledger.proposal_findings(self.root / "issue.md")
            self.assertEqual((finding.finding_id, finding.before, finding.after, finding.ticket), ("legacy", "old", "new", "read-1"))
            self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
            self.assertEqual(ledger.ticket_open("read")[0]["files"], list(self.sources[:2]))
            ledger.record_candidate("read-1", self.sources[0], 2, "Check this idea")
            self.assertEqual(ledger.rule_candidates()[0].idea, "Check this idea")

    def test_file_scope_verdict_crate_clean_tracks_only_its_file_and_rule(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record(rule, self.sources[1], self.initial_tip)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.assertEqual(self.states(rule)[self.sources[1]], "stale")
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository())
                              if row.rule_id == rule).current, 1)
        self.write(self.sources[0], "pub fn own_file_changed() {}\n")
        tip = self.commit("Change judged file")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")
        ledger.record(rule, self.sources[0], tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        _ = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n# Revised rule\n")
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_file_scope_verdict_module_exception_tracks_only_its_file_and_rule(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        ledger.record(rule, path, base, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record(rule, sibling, base, "exception")
        self.write(sibling, "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change module sibling"))
        self.assertEqual(self.states(rule)[path], "current")
        self.assertEqual(self.states(rule)[sibling], "stale")
        self.write(path, "pub fn own_file_changed() {}\n")
        tip = self.commit("Change module file")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[path], "stale")
        ledger.record(rule, path, tip, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        _ = self.rule("structure", "tier: D\nfamily: modules\n# Revised rule\n")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_file_scope_verdict_rejects_other_verdicts(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        for verdict in ("fixed", "waived"):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(style_ledger.LedgerError, verdict):
                ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), verdict,
                              input_scope=style_ledger.DeclaredInputScope.FILE)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_scoped_verdict_without_input_scope_is_refused_on_crate_and_module_cells(self) -> None:
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        ledger = self.ledger()
        refusal = "say --input-scope file \\(the verdict rests on this file alone; .*--input-scope rule \\("
        for rule, path in ((read, self.sources[0]), (structure, module_path)):
            for verdict in style_ledger.SCOPED_VERDICTS:
                with self.subTest(rule=rule, verdict=verdict), self.assertRaisesRegex(style_ledger.LedgerError, refusal):
                    ledger.record(rule, path, base, self.blob(rule), verdict)
        result = self.cli("record", "--rule", read, "--path", self.sources[0], "--commit", base,
                          "--rule-blob", self.blob(read), "--verdict", "clean", "--ticket", "outside", "--worker", "test", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{read}: {self.sources[0]}: say --input-scope file", result.stderr)
        self.assertIn("or --input-scope rule", result.stderr)
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_scoped_verdicts_record_with_either_declared_scope(self) -> None:
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        ledger = self.ledger()
        recorded: list[tuple[str, str, str, str]] = []
        for rule, path in ((read, self.sources[0]), (structure, module_path)):
            _ = ledger.hold(rule, path, 1, "Required change", "restructure", "before", "after", self.blob(rule), "outside")
            for verdict in style_ledger.SCOPED_VERDICTS:
                for scope in style_ledger.DeclaredInputScope:
                    ledger.record(rule, path, base, self.blob(rule), verdict, ticket=f"{verdict}-{scope}",
                                  input_scope=scope)
                    recorded.append((rule, verdict, scope.value, self.git("rev-parse", f"{base}:{path}")
                                     if scope == style_ledger.DeclaredInputScope.FILE else ""))
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str, str, str]], connection.execute(
                "SELECT rule,verdict,declared_scope,input_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual([(rule, verdict, declared, fingerprint if declared == "file" else "")
                          for rule, verdict, declared, _, fingerprint in rows], recorded)
        self.assertEqual({(declared, scope) for _, _, declared, scope, _ in rows},
                         {("file", "file"), ("rule", "crate"), ("rule", "module")})

    def test_file_scoped_held_verdict_stays_current_after_a_crate_change(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        findings: dict[str, style_ledger.HeldFinding] = {}
        for path, scope in ((self.sources[0], style_ledger.DeclaredInputScope.FILE),
                            (self.sources[1], style_ledger.DeclaredInputScope.RULE)):
            findings[path] = ledger.hold(rule, path, 1, "Rename value", "rename", "value", "amount", self.blob(rule), "outside")
            ledger.record(rule, path, self.initial_tip, self.blob(rule), "held", ticket="outside", input_scope=scope)
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Add a file to the crate"))
        self.assertEqual({path: state for path, state in self.states(rule).items() if path in findings},
                         {self.sources[0]: "current", self.sources[1]: "stale"})
        self.assertEqual(next(row for row in ledger.rule_counts(style_ledger.WholeRepository())
                              if row.rule_id == rule).held, 1)
        outcomes = ledger.decline_findings((finding.finding_id for finding in findings.values()), "declined", style_ledger.WaiveByPerson("user"))
        self.assertIsInstance(outcomes[findings[self.sources[0]].finding_id], style_ledger.CellWaived)
        self.assertIsInstance(outcomes[findings[self.sources[1]].finding_id], style_ledger.InputChangedSinceJudged)

    def test_local_rule_cell_requires_an_input_scope_like_a_catalog_rule(self) -> None:
        local_dir = self.repo / "docs/style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text("---\ntier: C\nfamily: names\ninput_scope: crate\n---\n# Local\n")
        rule = "local:docs/style/local.md"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{rule}: {self.sources[0]}: say --input-scope file"):
            ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        ledger.record(rule, self.sources[1], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Add a file to the crate"))
        self.assertEqual({path: state for path, state in self.states(rule).items() if path in self.sources[:2]},
                         {self.sources[0]: "current", self.sources[1]: "stale"})

    def test_cells_without_file_scope_record_without_an_input_scope(self) -> None:
        read = self.rule("read")
        crate_read = self.rule("crate-read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        ledger = self.ledger()
        cells: tuple[tuple[str, str, style_ledger.Verdict], ...] = (
            (read, self.sources[0], "clean"), (manifest, "crates/alpha", "held"),
            (structure, "crates/alpha", "exception"),
            (crate_read, self.sources[0], "fixed"), (crate_read, self.sources[1], "waived"),
        )
        _ = ledger.hold(manifest, "crates/alpha", 1, "Change manifest", "restructure", "before", "after", self.blob(manifest), "outside")
        for rule, path, verdict in cells:
            ledger.record(rule, path, self.initial_tip, self.blob(rule), verdict)
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,path,declared_scope FROM judgments ORDER BY id").fetchall())
        self.assertEqual(rows, [(rule, path, "rule") for rule, path, _ in cells])

    def test_file_scope_verdict_rejects_manifest_crate_and_excluded_paths(self) -> None:
        manifest = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        read = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        for rule, path in ((manifest, "crates/alpha"), (manifest, self.sources[0]),
                           (structure, "crates/alpha"), (read, "crates/alpha/vendor/embedded.rs")):
            with self.subTest(rule=rule, path=path), self.assertRaises(style_ledger.LedgerError):
                ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean",
                              input_scope=style_ledger.DeclaredInputScope.FILE)

    def test_file_scope_verdict_already_file_scope_preserves_behavior(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn own_file_changed() {}\n")
        self.set_tip(self.commit("Change judged file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

        listed = self.rule("shell", "tier: B\nfamily: values\ninput_scope: crate\ncandidates:\n"
                           + "  kind: regex\n  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n")
        script = "crates/alpha/check.sh"
        self.write(script, "cargo test\n")
        tip = self.commit("Add already file-scoped shell site")
        self.set_tip(tip)
        ledger.record(listed, script, tip, self.blob(listed), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[0], "pub fn unrelated_change() {}\n")
        self.set_tip(self.commit("Change source outside shell cell"))
        self.assertEqual(self.states(listed), {script: "current"})
        self.write(script, "cargo test --workspace\n")
        self.set_tip(self.commit("Change shell cell"))
        self.assertEqual(self.states(listed), {script: "stale"})

    def test_file_scope_verdict_no_op_keeps_older_ancestor_current(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean")
        _ = self.git("checkout", "-q", "-b", "outside")
        self.write("README.md", "Unmerged work\n")
        unmerged = self.commit("Work outside the synced branch")
        ledger.record(rule, self.sources[0], unmerged, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        cell = next(cell for cell in ledger.cells(rule=rule) if cell.path == self.sources[0])
        self.assertEqual(cell.state, "current")

    def test_file_scope_verdict_cli_passes_declaration_and_persists_file_fingerprint(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        _ = self.subprocess_cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                     "--rule-blob", self.blob(rule), "--verdict", "exception", "--ticket", "outside",
                     "--worker", "test", "--input-scope", "file")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            row = cast(tuple[str, str], connection.execute("SELECT declared_scope,input_fingerprint FROM judgments").fetchone())
        self.assertEqual(row, ("file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}")))
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change sibling"))
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))
        cell = next(cell for cell in status["cells"] if cell["rule"] == rule and cell["path"] == self.sources[0])
        self.assertEqual(cell["state"], "current")

    def test_rule_reads_stays_current_after_unrelated_crate_and_module_changes(self) -> None:
        crate_rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        module_rule = self.rule("structure", "tier: D\nfamily: modules\n")
        module_path, _, _, base = self.structure_module()
        self.set_tip(base)
        for rule, path in ((crate_rule, self.sources[0]), (module_rule, module_path)):
            _ = self.ledger().hold(rule, path, 1, "Required change", "restructure", "before", "after",
                                   self.blob(rule), "outside")
        self.write("crates/alpha/src/added.rs", "pub fn added() {}\n")
        self.write("crates/alpha/src/theme/added.rs", "pub fn added() {}\n")
        self.set_tip(self.commit("Change unrelated crate and module files"))
        for rule, path in ((crate_rule, self.sources[0]), (module_rule, module_path)):
            for verdict in style_ledger.SCOPED_VERDICTS:
                with self.subTest(rule=rule, verdict=verdict):
                    self.record_reads(rule, path, base, (self.sources[1],), verdict=verdict, ticket=verdict)
                    self.assertEqual(self.states(rule)[path], "current")

    def test_rule_reads_becomes_stale_when_own_file_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change judged file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_becomes_stale_when_read_file_changes(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change read file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_becomes_stale_when_read_file_is_deleted(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        (self.repo / self.sources[1]).unlink()
        self.set_tip(self.commit("Delete read file"))
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_awaiting_merge_uses_only_named_files(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn checked() {}\n")
        checked = self.commit("Change judged file on style branch")
        self.record_reads(rule, self.sources[0], checked, (self.sources[1],))
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        style_tip = self.commit("Advance style branch unrelated file")
        self.assertEqual(self.states(rule)[self.sources[0]], "awaiting merge")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        _ = self.commit("Advance style branch read file")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_rule_reads_judged_at_base_uses_only_named_files(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1],))
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        base = self.commit("Advance unit base unrelated file")
        self.write(self.sources[1], "pub fn changed() {}\n")
        changed = self.commit("Change read file at synced tip")
        self.set_tip(changed)
        cell = next(cell for cell in self.cells(rule) if cell.path == self.sources[0])
        self.assertEqual(cell.state, "stale")
        ledger = self.ledger()
        self.assertEqual(ledger.judged_at_base((cell,), base), (cell,))
        self.assertEqual(ledger.judged_at_base((cell,), changed), ())

    def test_rule_reads_checkpoint_keeps_named_files_for_later_comparisons(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record_reads(rule, path, self.initial_tip, (self.sources[1],), ticket="names-1")
        self.write(path, "pub fn ticket_changed() {}\n")
        checkpoint = self.commit("Ticket edit")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.write("crates/alpha/src/added.rs", "pub fn unrelated() {}\n")
        ledger.set_tip(self.commit("Unrelated change after checkpoint"), "main")
        self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[path], "current")
        self.write(self.sources[1], "pub fn changed() {}\n")
        ledger.set_tip(self.commit("Read file change after checkpoint"), "main")
        self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[path], "stale")

    def test_rule_reads_latest_declared_scope_supersedes_old_file_verdict(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        path = self.sources[0]
        self.set_tip(self.initial_tip)
        self.record(rule, path, self.initial_tip, input_scope=style_ledger.DeclaredInputScope.FILE)
        self.record_reads(rule, path, self.initial_tip, (self.sources[1],))
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change named read after scope declaration"))
        self.assertEqual(self.states(rule)[path], "stale")
        self.record(rule, path, self.initial_tip, input_scope=style_ledger.DeclaredInputScope.FILE)
        self.assertEqual(self.states(rule)[path], "current")

    def test_rule_reads_requires_rule_scope_and_scoped_verdict(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        for scope, verdict, message in (
            ((), "clean", "--reads requires --input-scope rule"),
            (("--input-scope", "file"), "clean", "--reads requires --input-scope rule"),
            (("--input-scope", "rule"), "fixed", "fixed: --reads requires clean, exception or held"),
            (("--input-scope", "rule"), "waived", "waived: --reads requires clean, exception or held"),
        ):
            with self.subTest(scope=scope, verdict=verdict):
                result = self.cli(
                    "record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule), "--verdict", verdict, "--ticket", "outside", "--worker", "test",
                    *scope, "--reads", self.sources[1], success=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), f"style_ledger: {message}")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM judgments").fetchone(), (0,))

    def test_rule_reads_rejects_file_manifest_and_crate_cells(self) -> None:
        file_rule = self.rule("file")
        manifest_rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        crate_rule = self.rule("crate", "tier: D\nfamily: modules\ninput_scope: file\n")
        for rule, path in ((file_rule, self.sources[0]), (manifest_rule, "crates/alpha"), (crate_rule, "crates/alpha")):
            with self.subTest(rule=rule, path=path):
                result = self.cli(
                    "record", "--rule", rule, "--path", path, "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "outside", "--worker", "test",
                    "--input-scope", "rule", "--reads", self.sources[1], success=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    result.stderr.strip(),
                    f"style_ledger: {rule}: {path}: --reads requires a crate or module cell of an included Rust file",
                )

    def test_rule_reads_rejects_path_missing_at_recorded_commit(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        missing = "crates/alpha/src/not_committed.rs"
        self.write(missing, "pub fn uncommitted() {}\n")
        result = self.cli(
            "record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "outside", "--worker", "test",
            "--input-scope", "rule", "--reads", missing, success=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), f"style_ledger: {missing}: no file at {self.initial_tip}")

    def test_rule_reads_normalizes_paths_and_ignores_order_duplicates_and_own_path(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        self.record_reads(rule, self.sources[0], self.initial_tip, (self.sources[1], self.sources[2]), ticket="first")
        self.record_reads(rule, self.sources[0], self.initial_tip,
                          (self.sources[2], "./" + self.sources[1], self.sources[1], self.sources[0]),
                          ticket="second")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT declared_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0][0], "rule")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

    def test_rule_reads_migration_eighteen_preserves_schema_seventeen_crate_judgments(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        database = self.root / "version-seventeen.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 17:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (17)")
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,'
                + 'verdict_detail,ticket,worker,recorded_at,declared_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rule, self.blob(rule), "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha"),
                 self.sources[0], self.initial_tip, "clean", "old detail", "old-ticket", "test", "2026-01-01", "rule"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchone(), (max(style_ledger.MIGRATIONS),))
            ledger.set_tip(self.initial_tip)
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "current")
            self.write(self.sources[1], "pub fn changed() {}\n")
            ledger.set_tip(self.commit("Change old judgment crate inputs"))
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "stale")

    def test_file_scope_verdict_migration_ten_keeps_version_nine_rows_unmarked(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        database = self.root / "version-nine.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number <= 9:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (9)")
            for index, path in enumerate(self.sources[:2]):
                _ = connection.execute(
                    'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,'
                    + 'verdict_detail,ticket,worker,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (rule, self.blob(rule), "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha"),
                     path, self.initial_tip, "clean", "original detail", f"ticket-{index}", "test", "2026-01-01"))
            connection.commit()
        with style_ledger.Ledger(self.repo, database) as ledger:
            with closing(sqlite3.connect(database)) as connection:
                rows = cast(list[tuple[str, str, str]], connection.execute(
                    "SELECT path,declared_scope,verdict_detail FROM judgments ORDER BY id").fetchall())
                version = cast(tuple[int], connection.execute("SELECT version FROM schema_version").fetchone())
            self.assertEqual(version, (max(style_ledger.MIGRATIONS),))
            self.assertEqual(rows, [(path, "rule", "original detail") for path in self.sources[:2]])
            self.write(self.sources[1], "pub fn sibling_changed() {}\n")
            ledger.set_tip(self.commit("Change sibling after migration"))
            self.assertEqual({cell.path: cell.state for cell in ledger.cells(rule=rule)}[self.sources[0]], "stale")

    def test_file_scope_verdict_awaiting_merge_tracks_file_on_style_branch(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn checked() {}\n")
        checked = self.commit("Change judged file on style branch")
        self.ledger().record(rule, self.sources[0], checked, self.blob(rule), "clean",
                             input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        style_tip = self.commit("Advance style branch sibling")
        self.assertEqual(self.states(rule)[self.sources[0]], "awaiting merge")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.write(self.sources[0], "pub fn changed_again() {}\n")
        _ = self.commit("Advance style branch judged file")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")

    def test_file_scope_verdict_latest_declaration_supersedes_both_directions(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        ledger = self.ledger()
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "exception",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.write(self.sources[1], "pub fn sibling_changed() {}\n")
        tip = self.commit("Change sibling")
        self.set_tip(tip)
        self.assertEqual(self.states(rule)[self.sources[0]], "stale")
        ledger.record(rule, self.sources[0], self.initial_tip, self.blob(rule), "clean",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

    def test_file_scope_verdict_checkpoint_preserves_declaration(self) -> None:
        rule = self.rule("read", "tier: C\nfamily: names\ninput_scope: crate\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        ledger.record(rule, path, self.initial_tip, self.blob(rule), "clean", ticket="names-1",
                      input_scope=style_ledger.DeclaredInputScope.FILE)
        self.write(path, "pub fn ticket_changed() {}\n")
        checkpoint = self.commit("Ticket edit")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        self.write(self.sources[1], "pub fn later_sibling_changed() {}\n")
        ledger.set_tip(self.commit("Change sibling after checkpoint"), "main")
        self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str, str]], connection.execute(
                'SELECT "commit",declared_scope,input_fingerprint FROM judgments ORDER BY id').fetchall())
        self.assertEqual(rows, [
            (self.initial_tip, "file", self.git("rev-parse", f"{self.initial_tip}:{path}")),
            (checkpoint, "file", self.git("rev-parse", f"{checkpoint}:{path}")),
        ])

    def test_latest_held_judgments_selects_each_cells_latest_commit_and_excludes_later_non_held_verdicts(self) -> None:
        rule = self.rule("read")
        other_rule = self.rule("other-read")
        ledger = self.ledger()
        for held_rule, path in ((rule, self.sources[0]), (rule, self.sources[1]), (other_rule, self.sources[0])):
            _ = ledger.hold(held_rule, path, 1, "Required rename", "rename", "value", "amount", self.blob(held_rule), "outside")
        self.record(rule, self.sources[0], self.initial_tip, "held")
        self.record(rule, self.sources[1], self.initial_tip, "held")
        self.record(other_rule, self.sources[0], self.initial_tip, "held")
        self.write(self.sources[0], "pub fn changed() {}\n")
        tip = self.commit("Change the held file")
        self.record(rule, self.sources[0], tip, "held")
        ledger = self.ledger()
        self.assertEqual(ledger.latest_held_judgments(), {
            (rule, self.sources[0]): tip,
            (rule, self.sources[1]): self.initial_tip,
            (other_rule, self.sources[0]): self.initial_tip,
        })

        self.record(rule, self.sources[0], tip, "clean", ticket="later-clean")
        self.assertEqual(ledger.latest_held_judgments(), {
            (rule, self.sources[1]): self.initial_tip,
            (other_rule, self.sources[0]): self.initial_tip,
        })

    def test_structure_module_cell_ignores_other_directories_but_tracks_siblings_and_parent(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, parent, base = self.structure_module()
        self.set_tip(base)
        self.record(rule, path, base)
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change another module"))
        self.assertEqual(self.states(rule)[path], "current")
        for changed in (sibling, parent, path):
            with self.subTest(changed=changed):
                self.write(changed, "pub fn changed() {}\n")
                tip = self.commit("Change a module input")
                self.set_tip(tip)
                self.assertEqual(self.states(rule)[path], "stale")
                self.record(rule, path, tip)
                self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            scopes = cast(list[tuple[str]], connection.execute("SELECT DISTINCT input_scope FROM judgments").fetchall())
        self.assertEqual(scopes, [("module",)])

    def test_a_modules_cell_stays_current_after_a_comment_only_change_at_each_scope(self) -> None:
        file_rule, module_rule, crate_rule, _, path = self.comment_scope_rules()
        for relative in (self.sources[0], self.sources[1], path, "crates/alpha/src/theme/mod.rs", "crates/alpha/src/theme.rs"):
            self.comment_only_change(relative)
        self.set_tip(self.commit("Change comments only"))
        for rule, judged in ((file_rule, self.sources[0]), (module_rule, path), (crate_rule, self.sources[0])):
            with self.subTest(rule=rule):
                self.assertEqual(self.states(rule)[judged], "current")

    def test_a_names_cell_goes_stale_after_a_comment_only_change(self) -> None:
        _, _, _, names_rule, _ = self.comment_scope_rules()
        self.comment_only_change(self.sources[0])
        self.set_tip(self.commit("Change comments only"))
        self.assertEqual(self.states(names_rule)[self.sources[0]], "stale")

    def test_a_modules_cell_goes_stale_after_a_code_inner_doc_file_set_or_manifest_change(self) -> None:
        file_rule, module_rule, crate_rule, _, path = self.comment_scope_rules()
        sibling = "crates/alpha/src/theme/mod.rs"
        library = (self.repo / self.sources[0]).read_text()
        changes: tuple[tuple[str, str, str, str, str | None], ...] = (
            ("code", module_rule, path, sibling, "pub fn changed() {}\n"),
            ("inner line doc", module_rule, path, sibling, "//! Theme values.\npub fn changed() {}\n"),
            ("inner block doc", file_rule, self.sources[0], self.sources[0], "/*! The alpha crate. */\n" + library),
            ("added file", module_rule, path, "crates/alpha/src/theme/added.rs", "pub fn added() {}\n"),
            ("manifest", crate_rule, self.sources[0], "crates/alpha/Cargo.toml",
             (self.repo / "crates/alpha/Cargo.toml").read_text() + "# A note.\n"),
            ("removed file", crate_rule, self.sources[0], self.sources[1], None),
            ("removed module file", module_rule, path, "crates/alpha/src/theme.rs", None),
        )
        tip = self.git("rev-parse", "HEAD")
        for label, rule, judged, relative, content in changes:
            with self.subTest(change=label):
                self.record(rule, judged, tip)
                if content is None:
                    (self.repo / relative).unlink()
                else:
                    self.write(relative, content)
                tip = self.commit(f"Change {label}")
                self.set_tip(tip)
                self.assertEqual(self.states(rule)[judged], "stale")

    def test_a_modules_cell_goes_stale_when_an_inner_doc_moves_to_another_module_block(self) -> None:
        file_rule, _, _, _, _ = self.comment_scope_rules()
        blocks = "mod alpha {{\n{}    pub fn a() {{}}\n}}\nmod beta {{\n{}    pub fn b() {{}}\n}}\n"
        doc = "    //! Alpha facade.\n"
        self.write(self.sources[0], blocks.format(doc, ""))
        tip = self.commit("Document the alpha block")
        self.set_tip(tip)
        self.record(file_rule, self.sources[0], tip)
        self.write(self.sources[0], blocks.format("", doc))
        self.set_tip(self.commit("Move the inner doc to the beta block"))
        self.assertEqual(self.states(file_rule)[self.sources[0]], "stale")

    def test_a_modules_cell_goes_stale_after_a_mode_or_submodule_change_beside_a_comment_edit(self) -> None:
        _, _, crate_rule, _, _ = self.comment_scope_rules()
        submodule = self.repo / "crates/alpha/vendored"
        submodule.mkdir()

        def submodule_commit(message: str) -> None:
            _ = subprocess.run(["git", "-C", str(submodule), "-c", "user.name=Ledger tests", "-c",
                                "user.email=ledger-tests@example.invalid", "commit", "-q", "--allow-empty", "-m", message],
                               check=True, capture_output=True)

        _ = subprocess.run(["git", "-C", str(submodule), "init", "-q"], check=True, capture_output=True)
        submodule_commit("First")
        tip = self.commit("Add a submodule")
        self.assertIn("160000 commit", self.git("ls-tree", tip, "crates/alpha/vendored"))
        changes: tuple[tuple[str, Callable[[], None]], ...] = (
            ("submodule pointer", lambda: submodule_commit("Second")),
            ("manifest mode", lambda: (self.repo / "crates/alpha/Cargo.toml").chmod(0o755)),
            ("Rust file mode", lambda: (self.repo / self.sources[1]).chmod(0o755)),
        )
        for label, change in changes:
            with self.subTest(change=label):
                self.set_tip(tip)
                self.record(crate_rule, self.sources[0], tip)
                change()
                self.comment_only_change(self.sources[0])
                tip = self.commit(f"Change {label} and a comment")
                self.set_tip(tip)
                self.assertEqual(self.states(crate_rule)[self.sources[0]], "stale")

    def test_a_modules_judgment_with_reads_stays_current_after_a_comment_only_change_in_a_read(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        self.record_reads(rule, path, base, (self.sources[1],))
        self.comment_only_change(self.sources[1])
        self.set_tip(self.commit("Change comments in a read file"))
        self.assertEqual(self.states(rule)[path], "current")

    def test_legacy_structure_crate_key_carries_over_without_rewriting_the_row(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        self.set_tip(base)
        self.insert_legacy_structure_judgment(rule, path, base)
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change outside the judged module"))
        self.assertEqual(self.states(rule)[path], "current")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            stored = cast(list[tuple[str, str, str]], connection.execute(
                'SELECT input_scope,input_fingerprint,"commit" FROM judgments').fetchall())
        self.assertEqual(stored, [("crate", self.git("rev-parse", f"{base}:crates/alpha"), base)])
        self.write(sibling, "pub fn sibling_changed() {}\n")
        self.set_tip(self.commit("Change a sibling after legacy judgment"))
        self.assertEqual(self.states(rule)[path], "stale")

    def test_legacy_structure_module_key_carries_over_on_the_style_branch(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, sibling, _, base = self.structure_module()
        _ = self.cli("set-tip", "--tip", base, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(path, "pub fn fixed() {}\n")
        checked = self.commit("Fix module on style branch")
        self.insert_legacy_structure_judgment(rule, path, checked, "fixed")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        _ = self.commit("Change another module on style branch")
        self.assertEqual(self.states(rule)[path], "awaiting merge")
        self.write(sibling, "pub fn changed() {}\n")
        _ = self.commit("Change judged module on style branch")
        self.assertEqual(self.states(rule)[path], "stale")
        self.record(rule, path, self.git("rev-parse", "HEAD"), "fixed")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated_again() {}\n")
        _ = self.commit("Change another module after new judgment")
        self.assertEqual(self.states(rule)[path], "awaiting merge")

    def test_legacy_structure_held_judgment_carries_over_for_decline(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
        self.insert_legacy_structure_judgment(rule, path, base, "held")
        self.write("crates/alpha/src/unrelated/item.rs", "pub fn unrelated() {}\n")
        self.set_tip(self.commit("Change outside held module"))
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.CellWaived)
        self.assertEqual(self.states(rule)[path], "current")

    def test_structure_row_with_an_unresolvable_commit_counts_as_changed(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        path, _, _, base = self.structure_module()
        self.set_tip(base)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
        self.insert_legacy_structure_judgment(rule, path, base, "held")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute('UPDATE judgments SET "commit"=?', ("0" * 40,))
            connection.commit()
        self.assertEqual(self.states(rule)[path], "stale")
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.InputChangedSinceJudged)

    def test_structure_crate_and_manifest_cells_retain_their_original_keys(self) -> None:
        crate_rule = self.rule("structure", "tier: D\nfamily: modules\n")
        manifest_rule = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        finding = ledger.hold(crate_rule, "crates/alpha", 1, "Split crate", "restructure",
                              "before", "after", self.blob(crate_rule), "outside")
        self.record(crate_rule, "crates/alpha", self.initial_tip, "held")
        self.record(manifest_rule, "crates/alpha", self.initial_tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change crate source"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,input_scope,input_fingerprint FROM judgments ORDER BY id").fetchall())
        self.assertEqual(stored[0], (crate_rule, "crate", self.git("rev-parse", f"{self.initial_tip}:crates/alpha")))
        self.assertEqual(stored[1][1], "manifest")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "current")
        self.assertIsInstance(ledger.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user"))[finding.finding_id], style_ledger.InputChangedSinceJudged)
        self.write("crates/alpha/Cargo.toml", (self.repo / "crates/alpha/Cargo.toml").read_text() + "\n[features]\nextra = []\n")
        self.set_tip(self.commit("Change workspace manifest"))
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "stale")

    def test_celled_structure_record_refuses_rule_path_pairs_outside_ticket_ownership(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        _ = self.cli("ticket", "add", "--id", "modules-1", "--unit", "modules", "--kind", "structure",
                     "--family", "modules", "--crate", "crates/alpha", "--files", json.dumps(list(self.sources[:2])),
                     "--rules", json.dumps([rule]), "--cells", json.dumps([[rule, self.sources[0]]]))
        ledger.ticket_assign("modules-1", "main")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"{rule} {self.sources[1]} is outside ticket cells"):
            ledger.record(rule, self.sources[1], self.initial_tip, "", "clean", ticket="modules-1")
        ledger.record(rule, self.sources[0], self.initial_tip, "", "clean", ticket="modules-1",
                      input_scope=style_ledger.DeclaredInputScope.RULE)
        self.assertEqual(ledger.unjudged_ticket_cells("modules-1", "HEAD", self.in_worktree), ())
        with self.assertRaisesRegex(style_ledger.LedgerError, "different fields"):
            ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha",
                              list(self.sources[:2]), [rule], cells=((rule, self.sources[1]),))

    def test_migration_nine_preserves_legacy_crate_ticket_and_does_not_cover_new_file_cells(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha", list(self.sources[:2]), [rule])
        ledger.ticket_assign("modules-1", "main")
        database = ledger.database
        ledger.close()
        with closing(sqlite3.connect(database)) as connection:
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN cells")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN declared_scope")
            _ = connection.execute("ALTER TABLE judgments DROP COLUMN read_files")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waive_provenance")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN waived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN build_ticket")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN moved_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN withdrawn_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN person_read_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN duplicate_of")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN unwaived_by")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_from_line")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN followed_at")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN declaration_gone_at")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN finding_ids")
            _ = connection.execute("DROP TABLE rule_candidates")
            _ = connection.execute("DROP TABLE ticket_widened_paths")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=8")
            connection.commit()
        migrated = self.ledger()
        self.assertEqual(migrated.ticket_open("modules")[0]["cells"], [])
        self.assertEqual(migrated.unjudged_ticket_cells("modules-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, "crates/alpha"),))
        migrated.record(rule, "crates/alpha", self.initial_tip, "", "clean", ticket="modules-1")
        migrated.ticket_checkpoint("modules-1", self.initial_tip)
        self.assertEqual(migrated.ticket_open("modules")[0]["state"], "checkpointed")
        self.assertEqual(self.states(rule), dict.fromkeys(self.sources, "open"))

    def test_structure_file_cells_track_module_inputs_and_decline_only_the_matching_file(self) -> None:
        rule = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        holds = [ledger.hold(rule, path, 1, "Split module", "restructure", "before", "after", self.blob(rule), "outside")
                 for path in self.sources[:2]]
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, "held")
        self.assertIsInstance(ledger.decline_findings((holds[0].finding_id,), "declined", style_ledger.WaiveByPerson("user"))[holds[0].finding_id], style_ledger.CellWaived)
        progress = next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule)
        self.assertEqual((progress.current, progress.held), (2, 1))
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change a crate input"))
        self.assertEqual(self.states(rule), {**dict.fromkeys(self.sources[:2], "stale"), **dict.fromkeys(self.sources[2:], "open")})

    def test_structure_file_scope_keys_rust_modules_and_preserves_non_rust_site_fingerprints(self) -> None:
        rule = self.rule("structure-sites", "tier: D\nfamily: modules\ninput_scope: file\ncandidates:\n"
                         + "  kind: regex\n  pattern: PLACEHOLDER\n  globs: [README.md]\n  surface: partial\n"
                         + "  uncovered: Read module ownership.\n")
        self.write("README.md", "PLACEHOLDER module documentation\n")
        tip = self.commit("Add a non-Rust structure site")
        self.set_tip(tip)
        ledger = self.ledger()
        self.assertEqual({(cell.path, cell.crate) for cell in ledger.cells(rule)},
                         {(path, "crates/alpha" if "/alpha/" in path else "crates/beta") for path in self.sources}
                         | {("README.md", ".")})
        self.record(rule, "README.md", tip)
        self.record(rule, self.sources[0], tip)
        self.write(self.sources[1], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change another file"))
        self.assertEqual(self.states(rule), {**dict.fromkeys(self.sources, "open"),
                                           self.sources[0]: "stale", "README.md": "current"})

    def test_initial_universe_has_file_cells_but_no_vendor_or_process_cells(self) -> None:
        file_rule = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        listed = self.rule("listed", "tier: B\nfamily: names\npre_filter: word\n")
        process = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(file_rule), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(structure), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.cells(listed), [])
        self.assertEqual(self.cells(process), [])
        ledger = self.ledger()
        progress = {rule.rule_id: rule for rule in ledger.rule_counts(style_ledger.WholeRepository())}
        self.assertTrue(progress[listed].complete)
        self.assertEqual(progress[listed].total, 0)
        self.assertEqual(progress[file_rule].total, 4)
        self.assertFalse(progress[file_rule].complete)
        self.assertEqual({cell.path for cell in ledger.cells(crate="crates/alpha")}, {
            "crates/alpha/src/lib.rs", "crates/alpha/src/other.rs",
        })
        status = self.cli("status").stdout
        self.assertIn(file_rule, status)
        self.assertIn(listed, status)
        self.assertNotIn("unclassified:", status)

    def test_rule_counts_fold_components_and_filter_every_state_and_current_held_verdicts(self) -> None:
        read = self.rule("read")
        partial = self.rule(
            "partial", "tier: B\nfamily: types\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        process = self.rule("process", "tier: E\n")
        _ = self.rule("legacy", "mechanism: llm\n")
        self.set_tip(self.initial_tip)
        _ = self.hold_finding(read, 1)
        self.record(read, self.sources[0], self.initial_tip, "held")
        self.record(read, self.sources[1], self.initial_tip)
        for path in self.sources:
            self.record(partial, path, self.initial_tip)
        _ = self.hold_finding(partial + "#uncovered", 1)
        self.record(partial + "#uncovered", self.sources[0], self.initial_tip, "held")
        self.write(self.sources[1], "pub fn value() -> u32 { 2 }\n")
        main_tip = self.commit("Make judged inputs stale")
        _ = self.cli("set-tip", "--tip", main_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[2], "pub fn corrected() {}\n")
        pending_tip = self.commit("Judge a pending style correction")
        _ = self.ledger().hold(read, self.sources[2], 1, "Required rename", "rename", "corrected", "value", self.blob(read), "outside")
        self.record(read, self.sources[2], pending_tip, "held")
        ledger = self.ledger()

        with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
            repository_counts = ledger.rule_counts(style_ledger.WholeRepository())
            coverage.assert_called_once_with(None, None)
        by_rule = {row.rule_id: row for row in repository_counts}
        self.assertEqual(by_rule, {
            read: style_ledger.RuleCounts(read, "C", 1, 1, 1, 1, 1, "file"),
            partial: style_ledger.RuleCounts(partial, "B", 4, 1, 3, 0, 1, "file"),
            process: style_ledger.RuleCounts(process, "E", 0, 0, 0, 0, 0, "crate"),
        })
        self.assertEqual({row.rule_id: (row.total, row.complete) for row in repository_counts}, {
            read: (4, False), partial: (8, False), process: (0, True),
        })
        text_rows = [line.split("\t") for line in self.cli("status").stdout.splitlines()
                     if line.startswith("global:")]
        self.assertEqual({fields[0]: tuple(int(value) for value in fields[1:6]) for fields in text_rows}, {
            row.rule_id: (row.current, row.stale, row.open, row.awaiting_merge, row.held)
            for row in repository_counts
        })
        for crate, expected in (
            ("crates/alpha", {
                read: style_ledger.RuleCounts(read, "C", 1, 1, 0, 0, 1, "file"),
                partial: style_ledger.RuleCounts(partial, "B", 2, 1, 1, 0, 1, "file"),
                process: by_rule[process],
            }),
            ("crates/beta", {
                read: style_ledger.RuleCounts(read, "C", 0, 0, 1, 1, 0, "file"),
                partial: style_ledger.RuleCounts(partial, "B", 2, 0, 2, 0, 0, "file"),
                process: by_rule[process],
            }),
        ):
            with self.subTest(crate=crate):
                with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
                    scoped = ledger.rule_counts(style_ledger.CrateCoverage(crate))
                    coverage.assert_called_once_with(None, crate)
                self.assertEqual({row.rule_id: row for row in scoped}, expected)

        finding = self.hold_finding(read, 1)
        ledger.file_held(finding, self.root / "proposal.md")
        ledger.handoff_add(pending_tip)
        with mock.patch.object(ledger, "_coverage", side_effect=AssertionError("summary_of rescanned cells")):
            summary = ledger.summary_of(repository_counts)
        self.assertEqual(summary, ledger.summary())
        self.assertEqual((summary.rules_done, summary.rules_total, summary.stale, summary.proposals), (0, 2, 2, 1))
        self.assertAlmostEqual(summary.cells_current_pct, 100 * 5 / 12)
        self.assertEqual(summary.unclassified_rules, 1)
        self.assertIsInstance(summary.waiting, style_ledger.PendingHandoffs)

    def test_status_crate_counts_answer_for_each_crate(self) -> None:
        read = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources[:2]:
            self.record(read, path, self.initial_tip)

        output = self.cli("status", "--crate", "crates/alpha").stdout

        self.assertEqual(output,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/read.md\t2\t0\t0\t0\t0\ttrue\n")
        beta = self.cli("status", "--crate", "crates/beta").stdout
        self.assertIn(read + "\t0\t0\t2\t0\t0\tfalse\n", beta)
        whole = self.cli("status", "--rule", read).stdout
        self.assertIn(read + "\t2\t0\t2\t0\t0\tfalse\n", whole)

    def test_status_reuses_repository_counts_and_scans_again_only_for_a_selector(self) -> None:
        read = self.rule("read")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for rule, scope, passes in ((None, style_ledger.WholeRepository(), 1),
                                    (read, style_ledger.WholeRepository(), 2),
                                    (None, style_ledger.CrateCoverage("crates/alpha"), 2)):
            with self.subTest(rule=rule, scope=scope):
                output = io.StringIO()
                with (
                    redirect_stdout(output),
                    mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage,
                ):
                    ledger.status(rule=rule, scope=scope)
                    self.assertEqual(coverage.call_count, passes)
                self.assertIn(read + "\t", output.getvalue())

    def test_status_cli_converts_omitted_empty_and_named_crates_to_coverage_scopes(self) -> None:
        read = self.rule("read")
        _ = self.git("mv", "crates/alpha", "alpha")
        self.write("Cargo.toml", '[workspace]\nmembers = ["alpha", "crates/beta"]\nresolver = "2"\n')
        tip = self.commit("Move alpha crate to the workspace root")
        self.set_tip(tip)
        self.record(read, "alpha/src/lib.rs", tip)
        header = "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
        whole = header + "global:rust/read.md\t1\t0\t3\t0\t0\tfalse\n"
        selected = header + "global:rust/read.md\t1\t0\t1\t0\t0\tfalse\n"
        for arguments, scope, expected in (
            ([], style_ledger.WholeRepository(), whole),
            (["--crate", ""], style_ledger.WholeRepository(), whole),
            (["--crate", "alpha"], style_ledger.CrateCoverage("alpha"), selected),
        ):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["style_ledger.py", "status", "--repo", str(self.repo), *arguments]),
                    mock.patch.object(style_ledger.Ledger, "status", autospec=True,
                                      side_effect=style_ledger.Ledger.status) as status,
                    redirect_stdout(output),
                ):
                    style_ledger.main()
                status.assert_called_once_with(mock.ANY, None, scope, False)
                self.assertEqual(output.getvalue(), expected)

    def test_status_uncovered_selector_prints_only_component_counts_and_parent_done(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        self.set_tip(self.initial_tip)
        _ = self.hold_finding(partial + "#uncovered", 1)
        for path in self.sources:
            self.record(partial + "#uncovered", path, self.initial_tip,
                        "held" if path == self.sources[0] else "clean")

        output = self.cli("status", "--rule", partial + "#uncovered").stdout

        self.assertEqual(output,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/partial.md\t4\t0\t0\t0\t1\tfalse\n")
        self.assertEqual(self.cli("status", "--rule", partial).stdout,
                         "rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone\n"
                         + "global:rust/partial.md\t4\t0\t4\t0\t1\tfalse\n")

    def test_zero_site_partial_rule_requires_uncovered_cells_and_counts_only_its_parent(self) -> None:
        rule_id = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: absent_marker\n  surface: partial\n  uncovered: other sites\n",
        )
        uncovered = rule_id + "#uncovered"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(self.states(uncovered), dict.fromkeys(self.sources, "open"))
        self.assertEqual({cell.rule for cell in ledger.cells()}, {uncovered})
        self.assertEqual({cell.tier for cell in ledger.cells()}, {"C"})
        self.assertEqual({cell.family for cell in ledger.cells()}, {"names"})
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 0, 4, False))

        for path in self.sources:
            self.record(uncovered, path, self.initial_tip)

        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 4, 4, True))
        row = next(line.split("\t") for line in self.cli("status").stdout.splitlines()
                   if line.startswith(rule_id + "\t"))
        self.assertEqual((row[1], row[-1]), ("4", "true"))
        self.assertNotIn(uncovered + "\t", self.cli("status").stdout)

    def test_partial_listed_rule_is_done_only_after_both_components_are_current(self) -> None:
        rule_id = self.rule(
            "partial", "tier: B\nfamily: types\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n",
        )
        uncovered = rule_id + "#uncovered"
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(uncovered, path, self.initial_tip)
        ledger = self.ledger()
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.current, progress.total, progress.complete), (4, 8, False))

        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)

        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 8, 8, True))
        rule_path = self.rule_dir / "partial.md"
        _ = rule_path.write_text(rule_path.read_text() + "Another requirement\n")
        self.assertEqual({cell.state for cell in ledger.cells()}, {"stale"})

    def test_partial_structure_rule_keeps_file_cells_without_uncovered_pseudo_rule(self) -> None:
        rule_id = self.rule(
            "structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n"
            + "  pattern: value\n  surface: partial\n  uncovered: crate structure\n",
        )
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        self.assertEqual({(cell.rule, cell.path, cell.tier) for cell in ledger.cells()}, {
            (rule_id, path, "D") for path in self.sources
        })
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual((progress.rule_id, progress.current, progress.total, progress.complete),
                         (rule_id, 4, 4, True))

    def test_pre_filter_lists_matching_rust_lines_and_accepts_a_file_judgment(self) -> None:
        rule_id = self.rule("words", "tier: B\nfamily: names\ninput_scope: file\npre_filter: '\\bbanned\\b'\n")
        self.write(self.sources[0], "// banned name\npub fn banned() {}\n// unbanned\n")
        tip = self.commit("Add forbidden word hits")
        self.set_tip(tip)
        ledger = self.ledger()

        self.assertEqual([(site.rule, site.path, site.crate, site.line, site.text)
                          for site in ledger.listed_sites(rule_id)], [
            (rule_id, self.sources[0], "crates/alpha", 1, "// banned name"),
            (rule_id, self.sources[0], "crates/alpha", 2, "pub fn banned() {}"),
        ])
        self.assertEqual(self.states(rule_id), {self.sources[0]: "open"})
        self.record(rule_id, self.sources[0], tip)
        self.assertEqual(self.states(rule_id), {self.sources[0]: "current"})
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertTrue(progress.complete)

    def test_shell_sites_are_rule_specific_file_cells_even_for_crate_scoped_rules(self) -> None:
        rule_id = self.rule(
            "nextest", "tier: B\nfamily: values\ninput_scope: crate\ncandidates:\n"
            + "  kind: regex\n  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        read_rule = self.rule("read")
        script = "crates/alpha/check.sh"
        self.write(script, "#!/bin/sh\ncargo test\ncargo test --workspace\n")
        tip = self.commit("Add test script")
        self.set_tip(tip)
        ledger = self.ledger()

        self.assertEqual([(site.path, site.crate, site.line) for site in ledger.listed_sites(rule_id)], [
            (script, "crates/alpha", 2), (script, "crates/alpha", 3),
        ])
        self.assertEqual(self.states(rule_id), {script: "open"})
        self.assertEqual(set(self.states(read_rule)), set(self.sources))
        self.record(rule_id, script, tip)
        self.assertEqual(self.states(rule_id), {script: "current"})

        self.write(self.sources[0], "pub fn unrelated_change() {}\n")
        self.set_tip(self.commit("Change source in script crate"))
        self.assertEqual(self.states(rule_id), {script: "current"})
        self.write(script, "#!/bin/sh\ncargo test --all-features\n")
        self.set_tip(self.commit("Change listed script"))
        self.assertEqual(self.states(rule_id), {script: "stale"})

    def test_site_export_uses_synced_objects_and_excludes_untracked_vendor_and_ignored_paths(self) -> None:
        rule_id = self.rule(
            "scripts", "tier: B\nfamily: values\ncandidates:\n  kind: regex\n"
            + "  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        self.write("check.sh", "cargo test\n")
        self.write("vendor/check.sh", "cargo test\n")
        self.write("crates/alpha/vendor/check.sh", "cargo test\n")
        self.write("ignored/check.sh", "cargo test\n")
        self.write("rustfmt.toml", 'ignore = ["ignored"]\n')
        tip = self.commit("Add tracked scripts and exclusions")
        self.write("check.sh", "cargo nextest run\n")
        self.write("untracked.sh", "cargo test\n")
        self.set_tip(tip)

        self.assertEqual([(site.path, site.line, site.text) for site in self.ledger().listed_sites(rule_id)],
                         [("check.sh", 1, "cargo test")])
        self.assertEqual(self.states(rule_id), {"check.sh": "open"})

    def test_listed_sites_ignore_archive_attributes_and_preserve_committed_text(self) -> None:
        listed = self.rule(
            "attributes", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: attribute_marker\n  surface: whole\n",
        )
        words = self.rule("words", "tier: B\nfamily: names\npre_filter: attribute_marker\n")
        ignored, substituted = self.sources[:2]
        ignored_text = "// attribute_marker in an export-ignored file"
        substituted_text = "// attribute_marker $Format:%H$"
        self.write(ignored, ignored_text + "\n")
        self.write(substituted, substituted_text + "\n")
        self.write(".gitattributes", f"{ignored} export-ignore\n{substituted} export-subst\n")
        self.set_tip(self.commit("Add archive attributes to tracked sources"))

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual({(cell["rule"], cell["path"], cell["state"]) for cell in status["cells"]}, {
            (rule_id, path, "open") for rule_id in (listed, words) for path in (ignored, substituted)
        })
        ledger = self.ledger()
        for rule_id in (listed, words):
            with self.subTest(rule=rule_id):
                self.assertEqual([(site.path, site.line, site.text) for site in ledger.listed_sites(rule_id)], [
                    (ignored, 1, ignored_text), (substituted, 1, substituted_text),
                ])

    def test_member_listed_and_uncovered_components_stay_inside_member_reach(self) -> None:
        local_dir = self.repo / "docs/alpha/style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text(
            "---\ntier: B\nfamily: names\ninput_scope: file\ncandidates:\n  kind: regex\n"
            + "  pattern: value\n  surface: partial\n  uncovered: other names\n---\n# Local\n"
        )
        rule_id = "local:docs/alpha/style/local.md"
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources[:2]))
        self.assertEqual({(cell.rule, cell.path) for cell in ledger.cells()}, {
            (component, path) for component in (rule_id, rule_id + "#uncovered") for path in self.sources[:2]
        })
        self.assertEqual(ledger.listed_sites(rule_id, crate="crates/beta"), ())
        for component in (rule_id, rule_id + "#uncovered"):
            with self.subTest(component=component):
                refusal = self.cli(
                    "record", "--rule", component, "--path", self.sources[2], "--commit", self.initial_tip,
                    "--rule-blob", self.blob(rule_id), "--verdict", "clean", "--ticket", "unit-1",
                    "--worker", "test-worker", success=False,
                )
                self.assertEqual(refusal.returncode, 2)
                self.assertIn(component, refusal.stderr)

    def test_summary_excludes_legacy_process_and_pseudo_rules_and_counts_both_components(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n",
        )
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(partial + "#uncovered", path, self.initial_tip)
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["rules_total"], report["unclassified_rules"]), (0, 1, 1))
        self.assertEqual(report["cells_current_pct"], 50.0)
        for path in self.sources:
            self.record(partial, path, self.initial_tip)
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["rules_total"], report["cells_current_pct"]), (1, 1, 100.0))
        rule_path = self.rule_dir / "partial.md"
        _ = rule_path.write_text(rule_path.read_text() + "More requirements\n")
        report = self.summary_report()
        self.assertEqual((report["rules_done"], report["stale"], report["cells_current_pct"]), (0, 8, 0.0))

    def test_summary_matches_rule_progress_with_only_one_partial_component_done(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other names\n",
        )
        read_rule = self.rule("read")
        lint_rule = self.rule("lint", "tier: A\nmechanism: clippy\n")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        legacy = self.rule("legacy", "mechanism: llm\n")
        process = self.rule("process", "tier: E\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "clippy", True, {})
        for path in self.sources:
            self.record(read_rule, path, self.initial_tip)
            self.record(partial, path, self.initial_tip)
        ledger = self.ledger()
        progress = {row.rule_id: row for row in ledger.rule_counts(style_ledger.WholeRepository())}
        self.assertTrue(progress[lint_rule].complete)
        self.assertTrue(progress[read_rule].complete)
        self.assertFalse(progress[partial].complete)
        self.assertFalse(progress[structure].complete)
        self.assertIn(process, progress)
        self.assertNotIn(legacy, progress)
        counted = [row for row in progress.values() if row.tier in ("A", "B", "C", "D")]

        with mock.patch.object(ledger, "_coverage", wraps=cast(object, getattr(ledger, "_coverage"))) as coverage:
            summary = ledger.summary()
            coverage.assert_called_once_with(None, None)

        self.assertEqual(summary.rules_done, sum(row.complete for row in counted))
        self.assertEqual(summary.rules_total, len(counted))
        self.assertEqual(summary.unclassified_rules, 1)

    def test_summary_all_legacy_is_zero_of_zero_with_no_pending_handoffs(self) -> None:
        _ = self.rule("legacy", "mechanism: llm\n")
        _ = self.rule("old-tool", "mechanism: clippy\n")
        self.set_tip(self.initial_tip)
        report = self.summary_report()
        self.assertEqual(set(report), {
            "rules_done", "rules_total", "unclassified_rules", "cells_current_pct", "stale", "proposals",
            "waiting_to_merge_back", "oldest_waiting", "synced_at", "synced_tip",
        })
        self.assertEqual((report["rules_done"], report["rules_total"], report["unclassified_rules"]), (0, 0, 2))
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (0, None))
        self.assertIsInstance(self.ledger().summary().waiting, style_ledger.NoPendingHandoffs)
        self.assertEqual(self.cli("handoff", "pending").stdout.strip(), "no pending hand-offs")

    def test_summary_counts_unsent_handoffs_from_creation_and_keeps_oldest_after_send(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("branch", "style/main")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", self.initial_tip)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            created = cast(list[tuple[str]], connection.execute("SELECT created_at FROM handoffs").fetchall())[0][0]
        report = self.summary_report()
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (1, created))
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Another checkpoint")
        second = self.git("rev-parse", "HEAD")
        _ = self.cli("handoff", "add", "--checkpoint", second)
        _ = self.cli("handoff", "send", "--checkpoint", self.initial_tip, "--to", "parent")
        report = self.summary_report()
        self.assertEqual((report["waiting_to_merge_back"], report["oldest_waiting"]), (2, created))
        waiting = self.ledger().summary().waiting
        self.assertIsInstance(waiting, style_ledger.PendingHandoffs)
        if isinstance(waiting, style_ledger.PendingHandoffs):
            self.assertEqual((waiting.count, waiting.oldest_pending_since), (2, datetime.fromisoformat(created)))

    def test_synced_time_survives_reopen_and_repeat_set_tip_advances_it(self) -> None:
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        first = ledger.synced_at()
        self.assertEqual(first.utcoffset(), timezone.utc.utcoffset(first))
        self.assertEqual(self.ledger().synced_at(), first)
        self.assertEqual(datetime.fromisoformat(self.summary_report()["synced_at"]), first)

        self.set_tip(self.initial_tip)

        self.assertGreater(ledger.synced_at(), first)
        self.assertEqual(self.summary_report()["synced_tip"], self.initial_tip)

    def test_version_three_migration_keeps_judgments_sites_and_style_branch_and_dates_sync(self) -> None:
        read_rule = self.rule("read")
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            for version in (1, 2, 3):
                _ = connection.executescript(style_ledger.MIGRATIONS[version])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (3)")
            _ = connection.execute("INSERT INTO sync VALUES (1,?,?)", (self.initial_tip, "refs/heads/style/main"))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (read_rule, self.blob(read_rule), "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"),
                 self.sources[0], self.initial_tip, "held", "reviewed", "old-ticket", "worker", "2026-10-16T00:00:00+00:00"),
            )
            _ = connection.execute("INSERT INTO listed_rules VALUES (?,?,?)", (listed, self.blob(listed), self.initial_tip))
            _ = connection.execute("INSERT INTO listed_sites VALUES (?,?,?,?,?,?)", (
                listed, self.blob(listed), self.initial_tip, self.sources[0], 1, "pub fn value() -> u32 { 1 }",
            ))
            connection.commit()
        before = datetime.now(timezone.utc)

        ledger = self.ledger()

        self.assertEqual(self.states(read_rule)[self.sources[0]], "current")
        self.assertEqual([(site.path, site.line, site.text) for site in ledger.listed_sites(listed)], [
            (self.sources[0], 1, "pub fn value() -> u32 { 1 }"),
        ])
        self.assertLessEqual(before.replace(microsecond=0), ledger.synced_at())
        self.assertLessEqual(ledger.synced_at(), datetime.now(timezone.utc))
        self.assertEqual(self.ledger().synced_at(), ledger.synced_at())
        with closing(sqlite3.connect(state_file)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])
        self.assertEqual(sync, [(self.initial_tip, "refs/heads/style/main")])

    def test_rust_files_exposes_the_synced_universe_even_with_only_crate_rules(self) -> None:
        _ = self.rule("structure", "tier: D\nfamily: modules\n")
        self.write("crates/alpha/src/ignored/hidden.rs", "pub fn ignored() {}\n")
        self.write("rustfmt.toml", 'ignore = ["crates/alpha/src/ignored"]\n')
        self.write("tools/root.rs", "pub fn root() {}\n")
        tip = self.commit("Add ignored and root Rust files")
        self.set_tip(tip)
        self.write("crates/alpha/src/untracked.rs", "pub fn untracked() {}\n")
        self.write("rustfmt.toml", 'ignore = ["crates/beta"]\n')
        ledger = self.ledger()

        self.assertEqual(ledger.rust_files("crates/alpha"), self.sources[:2])
        self.assertEqual(ledger.rust_files("crates/beta"), self.sources[2:])
        self.assertEqual(ledger.rust_files("."), ("tools/root.rs",))
        self.assertEqual(ledger.rust_files("missing"), ())

    def test_version_four_migration_preserves_work_and_reports_missing_historical_pin_text(self) -> None:
        rule = self.rule("read")
        blob = self.blob(rule)
        database = self.root / "state/sample/ledger.sqlite"
        database.parent.mkdir(parents=True)
        now = "2026-10-16T10:00:00+00:00"
        with closing(sqlite3.connect(database)) as connection:
            for version in (1, 2, 3, 4):
                _ = connection.executescript(style_ledger.MIGRATIONS[version])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (4)")
            _ = connection.execute("INSERT INTO sync VALUES (1,?,?,?)", (self.initial_tip, "refs/heads/main", now))
            _ = connection.execute(
                "INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,'queued','','',?,?)",
                ("read-1", "read", "read", "names", "", "crates/alpha", json.dumps(list(self.sources[:2])), "[]",
                 json.dumps([rule]), json.dumps({rule: blob}), now, now),
            )
            _ = connection.execute("INSERT INTO handoffs VALUES (?,'sent','','parent',?,?)", (self.initial_tip, now, now))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (rule, blob, "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"), self.sources[0],
                 self.initial_tip, "clean", "", "read-1", "worker", now),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("read")], ["read-1"])
        self.assertEqual(ledger.queued_tickets("read")[0]["base"], "")
        self.assertEqual(ledger.next_ticket_id("read"), "read-2")
        self.assertEqual([(handoff["checkpoint"], handoff["state"]) for handoff in ledger.pending_handoffs()], [(self.initial_tip, "sent")])
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"read-1: no pinned text for {blob}"):
            _ = ledger.pinned_rule_texts("read-1")
        ledger.ticket_add("read-2", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(ledger.pinned_rule_texts("read-1"), ledger.pinned_rule_texts("read-2"))
        with closing(sqlite3.connect(database)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])


    def test_workspace_checkpoint_shares_only_recorded_matching_ticket_proofs_across_reopen(self) -> None:
        source = "fn f() {\n    let old = input;\n    consume(old);\n    let second = input;\n    consume(second);\n}\n"
        ledger, issue, base = self.binding_ticket(source, ((2, "old", "fresh"),))
        path = self.sources[0]
        rule = self.rule("prefer-type-named-fields-and-bindings")
        second = ledger.hold(rule, path, 4, source.splitlines()[3], "rename", "second", "next", self.blob(rule), "outside")
        other_issue = self.root / "second.md"
        ledger.file_held(second.finding_id, other_issue)
        # Workspace tickets retain separate decisions and finding ownership.
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("UPDATE tickets SET crate='.' WHERE id='names-1'")
        ledger.ticket_add("names-second-1", "names-second", "proposal", "names", ".", [path], [rule],
                          proposal=str(other_issue), finding_ids=(second.finding_id,))
        ledger.ticket_assign("names-second-1", "main")
        self.assertEqual(ledger.ticket_base("names-second-1"), base)
        self.write(path, source.replace("old", "fresh").replace("second", "next"))
        checkpoint = self.commit("Two separately approved binding renames")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("names-1", checkpoint)
        with self.assertRaises(style_ledger.LedgerError):
            ledger.bulk_share_checkpoint(("names-1", "names-second-1"))
        ledger.ticket_checkpoint("names-second-1", checkpoint)
        first, = ledger.proposal_findings(issue)
        self.assertNotIsInstance(ledger.applied_check(first, checkpoint), AppliedInCode)
        for column, changed in (("base", self.initial_tip), ("branch", "refs/heads/unrelated"),
                                ("checkpoint", base), ("rules", "[]"), ("crate", "crates/alpha"),
                                ("kind", "read"), ("family", "values")):
            with self.subTest(column=column), closing(sqlite3.connect(ledger.database)) as connection:
                previous = cast(tuple[str], connection.execute(f"SELECT {column} FROM tickets WHERE id='names-second-1'").fetchone())[0]
                with connection:
                    _ = connection.execute(f"UPDATE tickets SET {column}=? WHERE id='names-second-1'", (changed,))
                with self.assertRaises(style_ledger.LedgerError):
                    ledger.bulk_share_checkpoint(("names-1", "names-second-1"))
                with connection:
                    _ = connection.execute(f"UPDATE tickets SET {column}=? WHERE id='names-second-1'", (previous,))
        ledger.bulk_share_checkpoint(("names-1", "names-second-1"))
        ledger.bulk_share_checkpoint(("names-second-1", "names-1"))  # Idempotent; also clears an earlier incomplete proof.
        self.assertIsInstance(ledger.applied_check(first, checkpoint), AppliedInCode)
        self.assertNotIsInstance(ledger.applied_check(first, base), AppliedInCode)
        # A fresh process reads the group; merging the first ticket must not hide its proof from the second.
        reopened = self.ledger()
        self.assertEqual(reopened.ticket_merge("names-1"), ())
        self.assertEqual(reopened.ticket_merge("names-second-1"), ())
        self.assertTrue(all(isinstance(f.state, style_ledger.Built)
                            for f in reopened.named_held((first.finding_id, second.finding_id))))

if __name__ == "__main__":
    _ = unittest.main()
